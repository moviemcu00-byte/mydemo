"""
Step 8 — "Live view" = screenshot burst (video stream nahi). Chhote JPEG frames: phone par downscale + compress
(maxEdge/quality), 1-5 sec ke gap par. Android ka takeScreenshot ~0.3 sec se tez nahi hota, aur har frame
phone ki battery/data kharch karta hai — isliye frames aur speed limited hain.

PRIVACY: screenshot mein kuch bhi dikh sakta hai (messages, passwords). MCP tool sirf authenticated Claude
connector ko milta hai; HTTP viewer (/live/<secret>) default band hai — LIVE_VIEW=1 se ON hota hai.
"""
import asyncio
import time

from relay import DeviceError

DEFAULT_EDGE = 640
DEFAULT_QUALITY = 50
MAX_FRAMES = 6
MIN_INTERVAL = 1.0
MAX_INTERVAL = 5.0


class FrameError(Exception):
    pass


async def grab_frame(relay, device, max_edge=DEFAULT_EDGE, quality=DEFAULT_QUALITY, timeout=25.0):
    """-> (base64_jpeg, mime). FrameError agar nahi mila."""
    path = f"/screenshot?maxEdge={int(max_edge)}&quality={int(quality)}"
    try:
        resp = await relay.call(device, "GET", path, None, timeout=timeout)
    except DeviceError as e:
        raise FrameError(f"phone unavailable: {e}")
    if resp.get("status") == 200 and resp.get("bodyBase64"):
        return resp["bodyBase64"], resp.get("contentType", "image/jpeg")
    msg = resp.get("body") or f"HTTP {resp.get('status')}"
    raise FrameError(f"screenshot failed: {str(msg)[:120]}")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


async def burst(relay, device, frames=3, interval=2.0, max_edge=DEFAULT_EDGE, quality=DEFAULT_QUALITY, sleep=asyncio.sleep):
    """MCP result: text + `frames` images. Beech mein fail ho to jo mile wo + error note."""
    frames = clamp(int(frames), 1, MAX_FRAMES)
    interval = clamp(float(interval), MIN_INTERVAL, MAX_INTERVAL)
    max_edge, quality = clamp(int(max_edge), 240, 1000), clamp(int(quality), 30, 80)
    content, errors = [], None
    t0 = time.monotonic()
    for i in range(frames):
        try:
            b64, mime = await grab_frame(relay, device, max_edge, quality)
        except FrameError as e:
            errors = f"frame {i + 1}/{frames}: {e}"
            break
        content.append({"type": "text", "text": f"frame {i + 1}/{frames} (+{time.monotonic() - t0:.1f}s)"})
        content.append({"type": "image", "data": b64, "mimeType": mime})
        if i < frames - 1:
            await sleep(interval)
    if errors:
        content.append({"type": "text", "text": f"stopped early — {errors}"})
    if not any(c["type"] == "image" for c in content):
        return {"content": content or [{"type": "text", "text": errors or "no frames"}], "isError": True}
    return {"content": content, "isError": False}


class FrameCache:
    """HTTP viewer ke liye: bahut saare viewers hon to bhi phone ko har TTL sec mein ek hi screenshot."""

    def __init__(self, ttl=1.0, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._frames = {}
        self._locks = {}

    async def get(self, relay, device, max_edge=DEFAULT_EDGE, quality=DEFAULT_QUALITY):
        lock = self._locks.setdefault(device.id, asyncio.Lock())
        async with lock:
            cached = self._frames.get(device.id)
            if cached and self.clock() - cached[0] < self.ttl:
                return cached[1], cached[2]
            b64, mime = await grab_frame(relay, device, max_edge, quality)
            self._frames[device.id] = (self.clock(), b64, mime)
            return b64, mime


VIEWER_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Control Hub — live view</title>
<style>body{margin:0;background:#111;color:#ddd;font:14px system-ui;text-align:center}
img{max-width:100%;max-height:90vh;border:1px solid #333;margin-top:8px}#s{padding:6px}</style></head>
<body><div id="s">connecting…</div><img id="f" alt="phone screen">
<script>
const base = location.pathname.replace(/\\/$/, '');
const dev = new URLSearchParams(location.search).get('device') || '';
let busy = false;
async function tick(){
  if (busy) return; busy = true;
  try {
    const r = await fetch(base + '/frame.jpg?device=' + encodeURIComponent(dev) + '&t=' + Date.now());
    if (r.ok) { document.getElementById('f').src = URL.createObjectURL(await r.blob());
                document.getElementById('s').textContent = 'live (~1 frame / 1.5 s) ' + new Date().toLocaleTimeString(); }
    else { document.getElementById('s').textContent = 'no frame: ' + (await r.text()).slice(0,120); }
  } catch(e){ document.getElementById('s').textContent = 'error: ' + e; }
  busy = false;
}
setInterval(tick, 1500); tick();
</script></body></html>"""
