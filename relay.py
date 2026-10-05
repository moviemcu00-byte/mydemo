"""
DeviceRelay — devices ke saath persistent WebSocket connections ka bridge.

Devices (phone, laptop...) khud outbound connect karte hain (NAT ke peeche hote hain).
Backend MCP tool call aane par sahi device ko request bhejta hai, jawab wapas laata hai.

Shared protocol (JSON text frames) — poori detail DEVICE_PROTOCOL.md mein:
  backend -> device : {"type":"request","id":"..","method":"GET","path":"/apps","body":null|{..}}
  device  -> backend: {"type":"response","id":"..","status":200,"contentType":"application/json","body":".."}
                      (image ke liye "bodyBase64" hota hai "body" ke bajaye)
  device  -> backend: {"type":"hello","capabilities":["system_info", ...]}   (sirf non-android agents)
"""
import asyncio
import json
import re
import time
import uuid

CAP_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MAX_CAPABILITIES = 32


class DeviceError(Exception):
    pass


class DeviceOffline(DeviceError):
    pass


class DeviceTimeout(DeviceError):
    pass


class DeviceAmbiguous(DeviceError):
    pass


class DevicePlatformMismatch(DeviceError):
    pass


class Device:
    def __init__(self, device_id, platform, ws):
        self.id = device_id
        self.platform = platform
        self.ws = ws
        self.capabilities = []      # sirf non-android agents 'hello' se bharte hain
        self.connected_at = time.time()
        self.last_message_at = time.time()


class DeviceRelay:
    def __init__(self, configured=None):
        # configured: {device_id: platform} — offline devices bhi status mein dikhane ke liye
        self._configured = dict(configured or {})
        self._devices = {}          # id -> Device (sirf connected)
        self._pending = {}          # request id -> (device_id, Future)

    # ---------------- connection lifecycle ----------------
    def attach(self, device_id, platform, ws):
        """Device register karo. Us id ka purana connection (agar hai) return hota hai — caller close kare."""
        old = self._devices.get(device_id)
        self._devices[device_id] = Device(device_id, platform, ws)
        if old is not None:
            self._fail_pending(device_id, DeviceOffline("device reconnected, request dropped"))
            return old.ws
        return None

    def detach(self, device_id, ws):
        """Sirf tab jab yehi current connection ho (purane socket ka close naye ko na tode)."""
        dev = self._devices.get(device_id)
        if dev is not None and dev.ws is ws:
            del self._devices[device_id]
            self._fail_pending(device_id, DeviceOffline("device disconnected"))

    def _fail_pending(self, device_id, exc):
        for req_id, (dev_id, fut) in list(self._pending.items()):
            if dev_id == device_id:
                if not fut.done():
                    fut.set_exception(exc)
                self._pending.pop(req_id, None)

    # ---------------- incoming messages ----------------
    def on_message(self, device_id, ws, text):
        dev = self._devices.get(device_id)
        if dev is None or dev.ws is not ws:
            return          # purane / anjaan socket ki baat ignore
        dev.last_message_at = time.time()
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return

        kind = msg.get("type")
        if kind == "hello":
            caps = msg.get("capabilities")
            if isinstance(caps, list):
                clean = [c for c in caps if isinstance(c, str) and CAP_RE.match(c)]
                dev.capabilities = sorted(set(clean))[:MAX_CAPABILITIES]
        elif kind == "response":
            entry = self._pending.get(msg.get("id"))
            # Jawab sirf wahi device de sakta hai jisko request gayi thi (device A, device B ki request ka jawab na de)
            if entry is not None and entry[0] == device_id and not entry[1].done():
                entry[1].set_result(msg)

    # ---------------- lookup ----------------
    @property
    def connected(self):
        return bool(self._devices)

    def status(self):
        now = time.time()
        rows = []
        ids = sorted(set(self._configured) | set(self._devices))
        for dev_id in ids:
            dev = self._devices.get(dev_id)
            rows.append({
                "id": dev_id,
                "platform": dev.platform if dev else self._configured.get(dev_id),
                "connected": dev is not None,
                "capabilities": list(dev.capabilities) if dev else [],
                "connected_for_seconds": int(now - dev.connected_at) if dev else None,
                "seconds_since_last_message": int(now - dev.last_message_at) if dev else None,
            })
        return {"devices": rows, "pending_requests": len(self._pending)}

    def resolve(self, device_arg=None, platform=None):
        """
        device_arg diya ho to wahi device (connected hona chahiye, platform match hona chahiye).
        Nahi diya to: platform ke hisaab se connected candidates mein se agar sirf ek ho to wahi.
        """
        connected_ids = sorted(self._devices)

        if device_arg:
            dev = self._devices.get(device_arg)
            if dev is None:
                raise DeviceOffline(
                    f"device '{device_arg}' is not connected (connected: {connected_ids or 'none'})")
            if platform and dev.platform != platform:
                raise DevicePlatformMismatch(
                    f"this tool works on {platform} devices, but '{device_arg}' is {dev.platform}")
            return dev

        candidates = [d for d in self._devices.values() if not platform or d.platform == platform]
        if not candidates:
            wanted = f"{platform} " if platform else ""
            raise DeviceOffline(f"no {wanted}device is connected (connected: {connected_ids or 'none'})")
        if len(candidates) > 1:
            raise DeviceAmbiguous(
                "several devices are connected: " + ", ".join(sorted(d.id for d in candidates))
                + " — pass the 'device' argument")
        return candidates[0]

    # ---------------- request/response ----------------
    async def call(self, device, method, path, body=None, timeout=25.0):
        req_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = (device.id, fut)
        try:
            await device.ws.send_text(json.dumps({
                "type": "request", "id": req_id,
                "method": method, "path": path, "body": body,
            }))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise DeviceTimeout("device did not respond in time")
        except DeviceError:
            raise
        except Exception as e:  # send fail (socket toota) etc.
            raise DeviceOffline(f"send failed: {e}")
        finally:
            self._pending.pop(req_id, None)
