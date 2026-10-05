"""
Control Hub relay + MCP server (FastAPI) — Railway pe deploy hoga.

Endpoints:
  GET  /                  -> status (secrets/tokens nahi dikhata)
  POST /mcp/{MCP_SECRET}  -> MCP Streamable-HTTP endpoint (Claude.ai connector yahi URL use karega)
  WS   /device            -> koi bhi device (phone/laptop/agent) yahan outbound connect karta hai
                             (Authorization: Bearer <uska apna token>)

Environment variables (Railway > Variables):
  MCP_SECRET    lamba random string — connector URL ka hissa
  DEVICE_TOKEN  ek phone ke liye seedha shortcut (device id "phone", platform "android")
  ANTHROPIC_API_KEY (optional) agent_start tools ke liye. Bina iske agent band rehta hai.
                Dhyan: har agent step par screen ka text Anthropic API ko jaata hai aur bill aapko aata hai.
  AGENT_MODEL   (optional) default claude-sonnet-5-5 (sasta/tez: claude-haiku-4-5-20251001)
  LIVE_VIEW     (optional) "1" = browser live-view page (/live/<MCP_SECRET>) ON. Default OFF (screenshots private hote hain).
  DEVICES       (optional, Feature #59) kayi devices ek saath:
                  id:platform:token,id:platform:token,...
                  platform = android | windows | linux | macos
                Dono ek saath bhi chal sakte hain (DEVICE_TOKEN + DEVICES).
"""
import base64
import hmac
import logging
import os

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import liveview
from devices import authenticate, parse_devices
from mcp_core import handle_rpc
from relay import DeviceError, DeviceRelay

log = logging.getLogger("control-hub-relay")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Control Hub Relay", docs_url=None, redoc_url=None, openapi_url=None)

MCP_SECRET = os.environ.get("MCP_SECRET", "")
_DEVICES, _DEVICE_ERRORS = parse_devices(os.environ.get("DEVICES", ""), os.environ.get("DEVICE_TOKEN", ""))
for _err in _DEVICE_ERRORS:
    log.warning("DEVICES config problem: %s", _err)

relay = DeviceRelay(configured={d.id: d.platform for d in _DEVICES.values()})

MIN_SECRET_LEN = 24


def _safe_eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _configured() -> bool:
    return len(MCP_SECRET) >= MIN_SECRET_LEN and len(_DEVICES) > 0


@app.get("/")
async def status():
    return {
        "service": "control-hub-relay",
        "configured": _configured(),
        "device_config_errors": len(_DEVICE_ERRORS),
        **relay.status(),
    }


@app.post("/mcp/{secret}")
async def mcp_post(secret: str, request: Request):
    if not _configured():
        return JSONResponse(
            {"error": "server not configured (set MCP_SECRET, min 24 chars, and at least one valid device — DEVICE_TOKEN or DEVICES)"},
            status_code=503)
    if not _safe_eq(secret, MCP_SECRET):
        return Response(status_code=404)  # 401 nahi — endpoint ka wujood chhupa rahe

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400)

    if isinstance(payload, list):  # JSON-RPC batch
        responses = []
        for item in payload:
            r = await handle_rpc(item, relay)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    result = await handle_rpc(payload, relay)
    if result is None:
        return Response(status_code=202)  # notification — body nahi
    return JSONResponse(result)


# ---- Step 8: browser live view (screenshot burst, ~1 frame/1.5 s). Default OFF: LIVE_VIEW=1 se ON ----
LIVE_VIEW = os.environ.get("LIVE_VIEW", "") == "1"
_frames = liveview.FrameCache(ttl=1.0)


def _live_allowed(secret: str) -> bool:
    return LIVE_VIEW and _configured() and _safe_eq(secret, MCP_SECRET)


@app.get("/live/{secret}")
async def live_page(secret: str):
    if not _live_allowed(secret):
        return Response(status_code=404)  # band ho ya secret galat — wujood chhupao
    return HTMLResponse(liveview.VIEWER_HTML, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/live/{secret}/frame.jpg")
async def live_frame(secret: str, device: str = ""):
    if not _live_allowed(secret):
        return Response(status_code=404)
    try:
        dev = relay.resolve(device or None, platform="android")
    except DeviceError as e:
        return Response(str(e), status_code=503)
    try:
        b64, mime = await _frames.get(relay, dev)
    except liveview.FrameError as e:
        return Response(str(e), status_code=502)
    return Response(content=base64.b64decode(b64), media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/mcp/{secret}")
@app.delete("/mcp/{secret}")
async def mcp_other(secret: str):
    # Hum SSE stream ya sessions nahi dete — spec ke hisaab se 405 valid hai
    return Response(status_code=405, headers={"Allow": "POST"})


@app.websocket("/device")
async def device_ws(ws: WebSocket):
    auth = ws.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    cfg = authenticate(_DEVICES, token) if _configured() else None
    if cfg is None:
        await ws.close(code=1008)  # handshake hi reject — kaunsa hissa galat hai wo nahi batate
        return

    await ws.accept()
    old_ws = relay.attach(cfg.id, cfg.platform, ws)
    log.info("device connected: %s (%s)", cfg.id, cfg.platform)
    if old_ws is not None:
        try:
            await old_ws.close(code=4000)
        except Exception:
            pass

    try:
        while True:
            text = await ws.receive_text()
            relay.on_message(cfg.id, ws, text)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("device socket error (%s): %s", cfg.id, type(e).__name__)
    finally:
        relay.detach(cfg.id, ws)
        log.info("device disconnected: %s", cfg.id)
