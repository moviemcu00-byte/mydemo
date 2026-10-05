"""
Feature #59 — Cross-device: kaunse devices allowed hain aur unka token kya hai.

Environment variable  DEVICES  (comma-separated, har entry  id:platform:token ):
    phone:android:AbC123...,laptop:linux:XyZ789...,office:windows:Qwe456...

Purana single-phone setup bhi chalta hai:  DEVICE_TOKEN=...  => device "phone" (android).

Rules (jaanbujh ke sakht):
  id        a-z se shuru, a-z 0-9 _ -  (max 32)
  platform  android | windows | linux | macos
  token     sirf letters+digits, kam se kam 24 chars, har device ka alag
"""
import hmac
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")
PLATFORMS = ("android", "windows", "linux", "macos")
MIN_TOKEN_LEN = 24


class DeviceConfig(NamedTuple):
    id: str
    platform: str
    token: str


def parse_devices(devices_env: str, legacy_token: str = "") -> Tuple[Dict[str, DeviceConfig], List[str]]:
    """Return (devices by id, errors). Errors wali entries skip hoti hain, baaki chalti hain."""
    devices: Dict[str, DeviceConfig] = {}
    errors: List[str] = []

    entries = [e.strip() for e in (devices_env or "").split(",") if e.strip()]
    for entry in entries:
        parts = entry.split(":")
        if len(parts) != 3:
            errors.append(f"bad DEVICES entry (need id:platform:token): '{entry[:12]}...'")
            continue
        dev_id, platform, token = (p.strip() for p in parts)
        problem = _check(dev_id, platform, token)
        if problem:
            errors.append(f"device '{dev_id}': {problem}")
            continue
        if dev_id in devices:
            errors.append(f"device '{dev_id}': duplicate id")
            continue
        devices[dev_id] = DeviceConfig(dev_id, platform, token)

    if legacy_token and "phone" not in devices:
        problem = _check("phone", "android", legacy_token)
        if problem:
            errors.append(f"DEVICE_TOKEN: {problem}")
        else:
            devices["phone"] = DeviceConfig("phone", "android", legacy_token)

    # Do devices ka same token = pehchaan mein confusion => dono ko hata do
    by_token: Dict[str, List[str]] = {}
    for cfg in devices.values():
        by_token.setdefault(cfg.token, []).append(cfg.id)
    for ids in by_token.values():
        if len(ids) > 1:
            errors.append(f"devices {ids} share the same token — all of them disabled")
            for i in ids:
                devices.pop(i, None)

    return devices, errors


def _check(dev_id: str, platform: str, token: str) -> Optional[str]:
    if not ID_RE.match(dev_id):
        return "invalid id (use a-z, 0-9, _ or -, start with a letter, max 32)"
    if platform not in PLATFORMS:
        return f"platform must be one of {', '.join(PLATFORMS)}"
    if len(token) < MIN_TOKEN_LEN or not TOKEN_RE.match(token):
        return f"token must be letters+digits only and at least {MIN_TOKEN_LEN} characters"
    return None


def authenticate(devices: Dict[str, DeviceConfig], token: str) -> Optional[DeviceConfig]:
    """Constant-time compare — har device ke saath, early exit nahi (timing se token guess na ho)."""
    if not token:
        return None
    found = None
    for cfg in devices.values():
        if hmac.compare_digest(cfg.token.encode(), token.encode()):
            found = cfg
    return found
