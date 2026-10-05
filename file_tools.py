"""
Step 5 — File transfer tools (MCP): phone <-> Claude, SHA-256 verified, chunked, resumable.

Pure Python (network se independent) — relay.call ke upar chalta hai. Direction naam phone ke
endpoints jaise hain:
  send_file    = phone SE file aati hai (phone -> Claude)     [/file/send/*]
  receive_file = phone KO file jaati hai (Claude -> phone)    [/file/receive/*]

Honest limits: backend par koi file storage nahi (Railway ephemeral) — send_file sirf chhoti files
(<= 512 KB) seedha jawab mein lauta sakta hai; receive_file ka content Claude khud deta hai (<= 1 MB).
Badi files ke liye phone ki /file/* API directly use karo.
"""
import base64
import hashlib
import json

from relay import DeviceError, DeviceTimeout

MAX_PULL_BYTES = 512 * 1024          # send_file: isse badi file inline nahi lautate
MAX_INLINE_BINARY_BYTES = 64 * 1024  # binary (non-text/image) ko base64 text mein sirf itna
MAX_PUSH_BYTES = 1_000_000           # receive_file: content size limit
CHUNK_RETRIES = 2

_TEXT_MIMES = ("text/", "application/json", "application/xml", "text/csv")


def _text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


class _PhoneError(Exception):
    pass


def _parse(resp):
    """Phone response -> (status, dict). Non-JSON body par {}."""
    status = resp.get("status", 0)
    try:
        data = json.loads(resp.get("body") or "{}")
    except (ValueError, TypeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _err_text(status, data):
    msg = data.get("error") or f"HTTP {status}"
    hints = {
        413: " (file too large for the phone's limit)",
        415: " (this file type is blocked on the phone)",
        422: " (checksum mismatch — the file was discarded; try again)",
        429: " (too many active transfers on the phone)",
        507: " (phone storage is full)",
    }
    return msg + hints.get(status, "")


async def _call(relay, device, method, path, body, timeout):
    try:
        return await relay.call(device, method, path, body, timeout=timeout)
    except DeviceTimeout:
        raise _PhoneError("The phone did not answer in time. Do not retry in a loop - tell the user.")
    except DeviceError as e:
        raise _PhoneError(f"Relay error: {e}")


async def send_file(relay, device, args, timeout):
    """Phone -> Claude. args: file_name (phone ke outbox/ ke andar)."""
    file_name = args["file_name"]
    try:
        resp = await _call(relay, device, "POST", "/file/send/start", {"fileName": file_name}, timeout)
        status, meta = _parse(resp)
        if status != 200:
            return _text_result("Could not start transfer: " + _err_text(status, meta), True)

        tid, total, n_chunks, want_sha = meta["transferId"], meta["totalBytes"], meta["totalChunks"], meta["sha256"]
        mime = meta.get("mimeType", "application/octet-stream")

        if total > MAX_PULL_BYTES:
            await _call(relay, device, "POST", "/file/send/cancel", {"transferId": tid}, timeout)
            return _text_result(
                f"'{file_name}' is {total} bytes (sha256 {want_sha}) — too large to return inline "
                f"(limit {MAX_PULL_BYTES}). Use the phone's /file/send API directly.", True)

        data = bytearray()
        for i in range(n_chunks):
            chunk = None
            last = "unknown error"
            for _ in range(CHUNK_RETRIES + 1):
                resp = await _call(relay, device, "GET", f"/file/send/chunk?transferId={tid}&index={i}", None, timeout)
                status, cd = _parse(resp)
                if status != 200:
                    last = _err_text(status, cd)
                    if status in (400, 404):   # retry se theek nahi hoga (paused/cancelled/out of range)
                        break
                    continue
                raw = base64.b64decode(cd.get("dataBase64", ""))
                if cd.get("chunkSha256") and _sha256(raw) != cd["chunkSha256"]:
                    last = f"chunk {i} checksum mismatch"
                    continue
                chunk = raw
                break
            if chunk is None:
                return _text_result(f"Transfer failed at chunk {i}/{n_chunks}: {last}", True)
            data += chunk

        got_sha = _sha256(bytes(data))
        if got_sha != want_sha:
            return _text_result(f"SHA-256 mismatch for '{file_name}' (expected {want_sha}, got {got_sha}) — discarded.", True)
    except _PhoneError as e:
        return _text_result(str(e), True)

    header = f"{file_name} — {len(data)} bytes, {mime}, sha256 {got_sha} (verified)"
    if mime.startswith("image/"):
        return {"content": [
            {"type": "text", "text": header},
            {"type": "image", "data": base64.b64encode(bytes(data)).decode(), "mimeType": mime},
        ], "isError": False}
    if mime.startswith(_TEXT_MIMES):
        try:
            return _text_result(header + "\n\n" + bytes(data).decode("utf-8"))
        except UnicodeDecodeError:
            pass
    if len(data) <= MAX_INLINE_BINARY_BYTES:
        return _text_result(header + "\n\nbase64:\n" + base64.b64encode(bytes(data)).decode())
    return _text_result(header + f"\n\nBinary content not inlined (over {MAX_INLINE_BINARY_BYTES} bytes).")


async def receive_file(relay, device, args, timeout):
    """Claude -> phone. args: file_name, content_text | content_base64, [resume_transfer_id]."""
    file_name = args["file_name"]
    has_text, has_b64 = "content_text" in args, "content_base64" in args
    if has_text == has_b64:
        return _text_result("Invalid arguments: give exactly one of content_text or content_base64", True)
    try:
        content = args["content_text"].encode("utf-8") if has_text else base64.b64decode(args["content_base64"], validate=True)
    except Exception:
        return _text_result("Invalid arguments: content_base64 is not valid base64", True)
    if len(content) > MAX_PUSH_BYTES:
        return _text_result(f"Content is {len(content)} bytes — limit here is {MAX_PUSH_BYTES}. Use the phone's /file/receive API directly.", True)

    want_sha = _sha256(content)
    try:
        resume_id = args.get("resume_transfer_id")
        if resume_id:
            resp = await _call(relay, device, "GET", f"/file/receive/status?transferId={resume_id}", None, timeout)
            status, meta = _parse(resp)
            if status != 200:
                return _text_result("Cannot resume: " + _err_text(status, meta), True)
            if meta.get("totalBytes") != len(content):
                return _text_result("Cannot resume: this content is not the same size as the original transfer.", True)
            if meta.get("state") == "PAUSED":
                await _call(relay, device, "POST", "/file/receive/resume", {"transferId": resume_id}, timeout)
            tid = resume_id
            start = max(0, meta.get("nextMissingIndex", 0))
        else:
            resp = await _call(relay, device, "POST", "/file/receive/start",
                               {"fileName": file_name, "totalBytes": len(content), "sha256": want_sha}, timeout)
            status, meta = _parse(resp)
            if status != 200:
                return _text_result("Phone rejected the file: " + _err_text(status, meta), True)
            tid, start = meta["transferId"], 0

        chunk_size, n_chunks = meta["chunkSize"], meta["totalChunks"]
        final = meta
        for i in range(start, n_chunks):
            piece = content[i * chunk_size:(i + 1) * chunk_size]
            body = {"transferId": tid, "index": i,
                    "dataBase64": base64.b64encode(piece).decode(), "chunkSha256": _sha256(piece)}
            ok = False
            last = "unknown error"
            for _ in range(CHUNK_RETRIES + 1):
                resp = await _call(relay, device, "POST", "/file/receive/chunk", body, timeout)
                status, final = _parse(resp)
                if status == 200:
                    ok = True
                    break
                last = _err_text(status, final)
                if status == 422:        # final checksum mismatch — file discard ho chuki
                    return _text_result("Transfer failed: " + last, True)
            if not ok:
                return _text_result(
                    f"Transfer stopped at chunk {i}/{n_chunks}: {last}. "
                    f"You can resume with resume_transfer_id='{tid}'.", True)
    except _PhoneError as e:
        return _text_result(str(e), True)

    if final.get("state") == "COMPLETED" and (n_chunks == 0 or final.get("verified") is True):
        return _text_result(json.dumps({
            "saved": True, "savedAs": final.get("savedAs"), "bytes": len(content),
            "sha256": final.get("sha256", want_sha), "verified": True, "transferId": tid,
        }))
    return _text_result(f"Transfer did not complete cleanly: state={final.get('state')} verified={final.get('verified')} "
                        f"error={final.get('error')}. transferId='{tid}'", True)
