"""
Feature #54 — MCP wrapper layer

Control Hub ke Local API endpoints ko MCP "tools" ke format mein badalta hai.
Ye file pure Python hai (FastAPI/network se independent), isliye alag se test
ho sakti hai. main.py sirf HTTP/WebSocket ka glue hai.

Transport: MCP "Streamable HTTP" (stateless) — client POST karta hai JSON-RPC,
hum JSON mein jawab dete hain. Koi session/SSE nahi.
"""
import json
import re
from urllib.parse import quote

from relay import DeviceOffline, DeviceTimeout, DeviceError
import file_tools

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "android-control-hub", "version": "0.2.0"}

INSTRUCTIONS = (
    "This server controls a real Android phone through an Accessibility Service. "
    "Work like a careful person: call get_scene (or read_screen / screenshot) to LOOK "
    "before you act, act once, then look again to VERIFY the screen changed as expected. "
    "element_id values come from the most recent read_screen/get_scene call and become "
    "stale when the screen changes - re-read before tapping. "
    "stop_app and type_text are high-risk: only pass confirm=true after the user has "
    "explicitly approved that specific action in this conversation. "
    "If the phone is offline, tell the user (check device_status) instead of retrying in a loop. "
    "The phone must be awake and unlocked for taps, typing and screenshots to work. "
    "Several devices can be registered: call device_status to see them; if more than one device "
    "of the needed kind is connected, pass the 'device' argument. Android devices use the phone tools "
    "(and call_module for extra SDK modules listed by get_capabilities); non-Android agents "
    "(Windows/Linux/macOS) are used only through device_call with the capabilities they announce. "
    "For multi-step work prefer create_task (the phone runs it in the background, checkpointed, with verification) "
    "and poll task_status instead of driving every tap yourself. UI tasks need allow_foreground=true (they open apps "
    "and tap on screen) - ask the user first. Replying through a notification (mode=background) is the only approach "
    "that does not touch the screen, and its delivery cannot be confirmed. "
    "stop_all_automation is a fail-safe: use it immediately if anything looks wrong; only the user can resume on the phone."
)

# Per-request timeout (seconds) — tests isse chhota kar sakte hain
CALL_TIMEOUT = 25.0


def _obj(props, required=None):
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_PKG = {"type": "string", "description": "Android package name, e.g. com.whatsapp"}
_IDEM = {"type": "string", "description": "Optional unique key (8-100 chars of A-Za-z0-9_.:-). Re-sending the SAME call with the same key never executes twice - use it when retrying."}
_CONFIRM = {
    "type": "boolean",
    "description": "Must be true. Only set after the user explicitly approved this action.",
}

# name -> definition. "route" (args) -> (method, path, body) ; None matlab backend-local tool
_DEFS = [
    {
        "name": "device_status",
        "description": "Check whether the phone is currently connected to the relay. "
                       "Call this first if any other tool reports the phone is offline.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": None,
    },
    {
        "name": "get_capabilities",
        "description": "List everything the phone-side Control Hub API supports (endpoints, scopes, events).",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/capabilities", None),
    },
    {
        "name": "list_apps",
        "description": "List launchable apps installed on the phone (package name, label).",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/apps", None),
    },
    {
        "name": "app_status",
        "description": "Check whether one app is running. Only accurate if Usage Access is enabled on the phone; otherwise returns false.",
        "inputSchema": _obj({"package": _PKG}, ["package"]),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/app/status?package=" + quote(a["package"], safe=""), None),
    },
    {
        "name": "launch_app",
        "description": "Open an app by package name (brings it to the foreground).",
        "inputSchema": _obj({"package": _PKG}, ["package"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/app/launch", {"package": a["package"]}),
    },
    {
        "name": "stop_app",
        "description": "HIGH RISK. Ask Android to kill an app's background process (best-effort, cannot force-stop a foreground app). Needs confirm=true after explicit user approval.",
        "inputSchema": _obj({"package": _PKG, "confirm": _CONFIRM}, ["package", "confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": lambda a: ("POST", "/app/stop", {"package": a["package"], "confirm": True}),
        "needs_confirm": True,
    },
    {
        "name": "read_screen",
        "description": "Read the current screen as a flat list of UI elements (elementId, text, className, clickable, bounds...).",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/ui/read", None),
    },
    {
        "name": "get_scene",
        "description": "Like read_screen but each element also has a semantic role (button, text_field, text, image, toggle...). Best tool for deciding what to tap.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/scene", None),
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot of the phone (downscaled JPEG). Needs Android 11+ and the phone unlocked.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/screenshot", None),
    },
    {
        "name": "tap",
        "description": "Tap a UI element by element_id (from the latest read_screen/get_scene) OR at screen coordinates x,y.",
        "inputSchema": _obj({
            "element_id": {"type": "integer", "description": "elementId from read_screen/get_scene"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        }),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,  # custom validation neeche (_build_tap)
    },
    {
        "name": "type_text",
        "description": "HIGH RISK. Type text into the currently focused input field (tap the field first). Needs confirm=true after explicit user approval.",
        "inputSchema": _obj({"text": {"type": "string"}, "confirm": _CONFIRM}, ["text", "confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": lambda a: ("POST", "/ui/type", {"text": a["text"], "confirm": True}),
        "needs_confirm": True,
    },
    {
        "name": "scroll",
        "description": "Scroll the screen by swiping in a direction.",
        "inputSchema": _obj(
            {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}},
            ["direction"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/ui/scroll", {"direction": a["direction"]}),
    },
    {
        "name": "press_back",
        "description": "Press the Android Back button.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/ui/back", {}),
    },
    {
        "name": "press_home",
        "description": "Press the Android Home button.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/ui/home", {}),
    },
    {
        "name": "long_press",
        "description": "Long-press an element (by element_id from read_screen) or at coordinates - opens context menus, selects messages, etc.",
        "inputSchema": _obj({"element_id": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,   # custom (_build_long_press)
    },
    {
        "name": "press_key",
        "description": "Press a system key/action: back, home, recents, notifications, quick_settings, enter (IME enter, Android 11+), clear (empties the focused input).",
        "inputSchema": _obj({"key": {"type": "string", "enum": ["back", "home", "recents", "notifications", "quick_settings", "enter", "clear"]}}, ["key"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/ui/key", {"key": a["key"]}),
    },
    {
        "name": "device_state",
        "description": "Live phone state: battery (level/charging/power-save), accessibility + notification-access status, screen lock, foreground app, active task, emergency stop, battery-optimization exemption.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/device/state", None),
    },
    {
        "name": "delete_task",
        "description": "Permanently delete a FINISHED task (done/failed/cancelled) from the phone, including its instruction text.",
        "inputSchema": _obj({"task_id": {"type": "integer"}, "task_uuid": {"type": "string"}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": None,
    },
    {
        "name": "purge_tasks",
        "description": "HIGH RISK. Bulk-delete finished tasks (optionally only those older than N days, or one status). Needs confirm=true.",
        "inputSchema": _obj({
            "older_than_days": {"type": "integer"},
            "status": {"type": "string", "enum": ["done", "failed", "cancelled"]},
            "idempotency_key": _IDEM,
            "confirm": _CONFIRM,
        }, ["confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": lambda a: ("POST", "/task/purge", _with_idem(
            {k: v for k, v in (("olderThanDays", a.get("older_than_days")), ("status", a.get("status"))) if v is not None}
            | {"confirm": True}, a)),
        "needs_confirm": True,
    },
    {
        "name": "live_view",
        "description": "'Live view' as a burst of small screenshots (1-6 frames, 1-5 s apart). Not a video stream - each frame costs phone battery. "
                       "Screenshots can contain private content.",
        "inputSchema": _obj({
            "frames": {"type": "integer", "description": "1-6, default 3"},
            "interval_seconds": {"type": "number", "description": "1-5, default 2"},
            "max_edge": {"type": "integer", "description": "longest side in px, 240-1000, default 640"},
            "quality": {"type": "integer", "description": "JPEG quality 30-80, default 50"},
        }),
        "annotations": {"readOnlyHint": True},
        "route": None,   # custom (agent_tools.live_view)
    },
    {
        "name": "agent_start",
        "description": "HIGH RISK. Start an autonomous agent run: the backend repeatedly reads the phone screen, asks Claude for the next action and performs it, "
                       "until the goal is done. SCREEN TEXT IS SENT TO THE ANTHROPIC API on every step (backend's ANTHROPIC_API_KEY, billed to the owner). "
                       "allowed_packages is required - the agent can only work inside those apps. Send/pay/delete/submit-type actions pause for approval "
                       "(agent_approve) unless auto_approve=true. Needs confirm=true after explicit user approval. Returns a run_id; poll agent_status.",
        "inputSchema": _obj({
            "goal": {"type": "string", "description": "What to achieve, 1-1000 chars"},
            "allowed_packages": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"com.whatsapp\"]"},
            "max_steps": {"type": "integer", "description": "1-40, default 15"},
            "auto_approve": {"type": "boolean", "description": "true = never pause for approval (dangerous)"},
            "confirm": _CONFIRM,
        }, ["goal", "allowed_packages", "confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": None,   # custom (agent_tools)
        "needs_confirm": True,
    },
    {
        "name": "agent_status",
        "description": "State of an agent run (running / needs_approval / done / failed / stopped), recent steps, pending approval, token usage. 'done' is the agent's own claim - verify important results.",
        "inputSchema": _obj({"run_id": {"type": "string"}}, ["run_id"]),
        "annotations": {"readOnlyHint": True},
        "route": None,
    },
    {
        "name": "agent_approve",
        "description": "Approve or deny the action an agent run is waiting on (state 'needs_approval'). Ask the user first; approve=true performs the action (e.g. sends the message).",
        "inputSchema": _obj({"run_id": {"type": "string"}, "approve": {"type": "boolean"}}, ["run_id", "approve"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": None,
    },
    {
        "name": "agent_stop",
        "description": "Stop an agent run immediately (fail-safe, no confirmation needed). stop_all_automation also halts the phone itself.",
        "inputSchema": _obj({"run_id": {"type": "string"}}, ["run_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,
    },
    # ---------------- Step 5: tasks ----------------
    {
        "name": "create_task",
        "description": "HIGH RISK. Create a background automation task on the phone. 'instructions' = commands separated by newline or ';;': "
                       "open:<pkg>, tap:<selectors>, longtap:<selectors>, type:<text>, scroll:<dir>, back, home, key:<back|home|recents|notifications|quick_settings|enter|clear>, wait:<sec>, reply:<pkg|*>|<contact|key>|<text>, "
                       "do:<adapter>.<action>|args (e.g. do:whatsapp.send_message|Ibrahim|Hello). Selectors: id= desc= text= has= cls= within= nth= xy= "
                       "(join with ' & ', alternatives with ' || '). Add ' => appears:<sel> | gone:<sel> | pkg:<package> | text:<str>' to verify a step. "
                       "mode: auto (default), background (notification reply only) or foreground. UI commands need allow_foreground=true. "
                       "Needs confirm=true after explicit user approval.",
        "inputSchema": _obj({
            "instructions": {"type": "string"},
            "mode": {"type": "string", "enum": ["auto", "background", "foreground"]},
            "allow_foreground": {"type": "boolean", "description": "true = the phone may open apps and tap on screen (screen must be on and unlocked)"},
            "priority": {"type": "integer", "description": "-10..10, higher runs first (default 0)"},
            "timeout_seconds": {"type": "integer", "description": "1..3600, default 300"},
            "restore_app": {"type": "boolean", "description": "default true: after a foreground task, return the phone to the app the user was in"},
            "redact_when_done": {"type": "boolean", "description": "true = erase the task's message text from the phone as soon as the task finishes"},
            "idempotency_key": _IDEM,
            "confirm": _CONFIRM,
        }, ["instructions", "confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": lambda a: ("POST", "/task/create", _task_create_body(a)),
        "needs_confirm": True,
    },
    {
        "name": "task_status",
        "description": "Full state of one task: status (pending/running/paused/done/failed/cancelled), currentStep/totalSteps, retryCount, lastError, deadline.",
        "inputSchema": _obj({"task_id": {"type": "integer"}, "task_uuid": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
        "route": None,   # custom (_build_task_status)
    },
    {
        "name": "list_tasks",
        "description": "Recent tasks on the phone (newest first), optionally filtered by status.",
        "inputSchema": _obj({
            "status": {"type": "string", "enum": ["pending", "running", "paused", "done", "failed", "cancelled"]},
            "limit": {"type": "integer", "description": "1..100, default 20"},
        }),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/task/list" + _qs({"status": a.get("status"), "limit": a.get("limit")}), None),
    },
    {
        "name": "pause_task",
        "description": "Pause a task at its next checkpoint. Resume continues from the same step.",
        "inputSchema": _obj({"task_id": {"type": "integer"}, "task_uuid": {"type": "string"}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,   # custom (_build_task_ref)
    },
    {
        "name": "resume_task",
        "description": "Resume a paused task from its last checkpoint.",
        "inputSchema": _obj({"task_id": {"type": "integer"}, "task_uuid": {"type": "string"}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,
    },
    {
        "name": "cancel_task",
        "description": "Cancel a pending/running/paused task.",
        "inputSchema": _obj({"task_id": {"type": "integer"}, "task_uuid": {"type": "string"}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,
    },
    {
        "name": "retry_task",
        "description": "Retry a FAILED task, from its last checkpoint (default) or from the start. Check the chat first if the task sent a message - a retry could duplicate it.",
        "inputSchema": _obj({
            "task_id": {"type": "integer"}, "task_uuid": {"type": "string"},
            "from_start": {"type": "boolean"}, "timeout_seconds": {"type": "integer"},
        }),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,
    },
    {
        "name": "stop_all_automation",
        "description": "EMERGENCY FAIL-SAFE. Immediately halts all automation on the phone and cancels every active task. No confirmation needed - use it at once "
                       "if anything looks wrong. Only the user can resume, on the phone itself.",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": lambda a: ("POST", "/emergency/stop", {}),
    },
    # ---------------- Step 5: notifications + adapters ----------------
    {
        "name": "get_notifications",
        "description": "Active notifications on the phone. Title/text are included only if the user turned on 'Expose notification text' in the app; otherwise only metadata (package, key, hasReply).",
        "inputSchema": _obj({"package": _PKG, "limit": {"type": "integer"}}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/notifications" + _qs({"package": a.get("package"), "limit": a.get("limit")}), None),
    },
    {
        "name": "reply_notification",
        "description": "HIGH RISK. Reply to a chat notification WITHOUT opening the app (inline reply, key from get_notifications). "
                       "Delivery cannot be confirmed - do not send it twice. Needs confirm=true after explicit user approval.",
        "inputSchema": _obj({"key": {"type": "string"}, "text": {"type": "string"},
                             "idempotency_key": _IDEM, "confirm": _CONFIRM}, ["key", "text", "confirm"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": lambda a: ("POST", "/notification/reply", _with_idem({"key": a["key"], "text": a["text"], "confirm": True}, a)),
        "needs_confirm": True,
    },
    {
        "name": "list_adapters",
        "description": "App adapters available for create_task (do:<adapter>.<action>|...), their actions and status ('starter' = selectors not yet verified on a real device).",
        "inputSchema": _obj({}),
        "annotations": {"readOnlyHint": True},
        "route": lambda a: ("GET", "/adapters", None),
    },
    # ---------------- Step 5: files ----------------
    {
        "name": "send_file",
        "description": "Pull a file FROM the phone (it must be in the app's outbox folder). SHA-256 verified. Returns text/image content inline; "
                       "files over 512 KB are not returned inline.",
        "inputSchema": _obj({"file_name": {"type": "string", "description": "File name inside the phone's outbox/ folder"}}, ["file_name"]),
        "annotations": {"readOnlyHint": True},
        "route": None,   # custom (file_tools)
    },
    {
        "name": "receive_file",
        "description": "Push a file TO the phone (saved in the app's received_files folder). Chunked, SHA-256 verified; blocked types (apk, exe, sh...) and files over 100 MB are rejected. "
                       "Give content_text OR content_base64 (max 1 MB). If a transfer was interrupted, pass resume_transfer_id with the same content.",
        "inputSchema": _obj({
            "file_name": {"type": "string"},
            "content_text": {"type": "string"},
            "content_base64": {"type": "string"},
            "resume_transfer_id": {"type": "string"},
        }, ["file_name"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "route": None,   # custom (file_tools)
    },
    {
        "name": "call_module",
        "description": "Call an extra SDK module capability on an Android phone (see 'modules' in get_capabilities for what exists, "
                       "its scope and arguments). For capabilities marked HIGH_RISK pass confirm=true - only after explicit user approval.",
        "inputSchema": _obj({
            "module": {"type": "string", "description": "Module name, e.g. sample"},
            "capability": {"type": "string", "description": "Capability name, e.g. echo"},
            "arguments": {"type": "object", "description": "Arguments for the capability (see get_capabilities)"},
            "confirm": {"type": "boolean", "description": "true only for HIGH_RISK capabilities, after explicit user approval"},
        }, ["module", "capability"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": None,   # custom (_build_module_call)
    },
    {
        "name": "device_call",
        "description": "Call a capability on a NON-Android agent (Windows/Linux/macOS). Only capabilities the agent announced "
                       "(see device_status) are accepted. Android phones use the other tools instead.",
        "inputSchema": _obj({
            "device": {"type": "string", "description": "Device id from device_status"},
            "capability": {"type": "string", "description": "Capability the agent announced"},
            "arguments": {"type": "object", "description": "Arguments for the capability"},
        }, ["device", "capability"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "route": None,   # custom (_device_call)
        "device_arg": "required",
    },
]

# Har device-routed tool ko optional 'device' argument milta hai (device_status ko nahi,
# device_call mein wo already required hai)
for _d in _DEFS:
    if _d["name"] == "device_status":
        _d["device_arg"] = None
    else:
        _d.setdefault("device_arg", "optional")

_BY_NAME = {d["name"]: d for d in _DEFS}


_DEVICE_PROP = {
    "type": "string",
    "description": "Target device id (from device_status). Only needed when several devices are connected.",
}


def _effective_schema(d):
    """Optional 'device' property jodta hai (original definition ko chhue bina)."""
    schema = d["inputSchema"]
    if d.get("device_arg") == "optional":
        schema = {**schema, "properties": {**schema["properties"], "device": _DEVICE_PROP}}
    return schema


# Requirements doc (section 14) ke tool naam — purane naam bhi chalte rehte hain (kuch toot na jaye)
_ALIASES = {
    "device_capabilities": "get_capabilities",
    "observe_ui": "read_screen",
    "take_screenshot": "screenshot",
    "click_element": "tap",
}


def list_tools():
    tools = [
        {
            "name": d["name"],
            "description": d["description"],
            "inputSchema": _effective_schema(d),
            "annotations": d["annotations"],
        }
        for d in _DEFS
    ]
    for alias, canonical in _ALIASES.items():
        d = next(x for x in _DEFS if x["name"] == canonical)
        tools.append({
            "name": alias,
            "description": d["description"] + f" (Same as {canonical}.)",
            "inputSchema": _effective_schema(d),
            "annotations": d["annotations"],
        })
    return tools


_TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def validate_args(schema, args):
    """Chhota manual validator (jsonschema dependency nahi). Error string ya None."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = schema.get("properties", {})
    for key in args:
        if key not in props:
            return f"unknown argument '{key}'"
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required argument '{key}'"
    for key, value in args.items():
        spec = props[key]
        check = _TYPE_CHECK.get(spec.get("type"))
        if check and not check(value):
            return f"argument '{key}' must be of type {spec.get('type')}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument '{key}' must be one of {spec['enum']}"
        if spec.get("type") == "array" and "items" in spec:
            item_check = _TYPE_CHECK.get(spec["items"].get("type"))
            if item_check and not all(item_check(v) for v in value):
                return f"every item of '{key}' must be of type {spec['items'].get('type')}"
    return None


def _build_tap(args):
    if "element_id" in args:
        if "x" in args or "y" in args:
            return None, "give either element_id OR x+y, not both"
        return ("POST", "/ui/click", {"element_id": args["element_id"]}), None
    if "x" in args and "y" in args:
        return ("POST", "/ui/click", {"x": args["x"], "y": args["y"]}), None
    return None, "give element_id, or both x and y"


def _build_long_press(args):
    if "element_id" in args:
        if "x" in args or "y" in args:
            return None, "give either element_id OR x+y, not both"
        return ("POST", "/ui/long_click", {"element_id": args["element_id"]}), None
    if "x" in args and "y" in args:
        return ("POST", "/ui/long_click", {"x": args["x"], "y": args["y"]}), None
    return None, "give element_id, or both x and y"


def _qs(params):
    """None values hata ke query string banata hai ('' agar kuch nahi)"""
    parts = [f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None]
    return ("?" + "&".join(parts)) if parts else ""


def _with_idem(body, a):
    """Retry/replay par dobara execute na ho: caller ki idempotency_key phone tak jaati hai.
    Na di ho to phone khud har relay request ke unique id se key banata hai."""
    if a.get("idempotency_key"):
        body["idempotencyKey"] = a["idempotency_key"]
    return body


def _task_create_body(a):
    body = _with_idem({"instructions": a["instructions"], "confirm": True}, a)
    for src, dst in (("mode", "mode"), ("allow_foreground", "allowForeground"),
                     ("priority", "priority"), ("timeout_seconds", "timeoutSeconds"),
                     ("restore_app", "restoreApp"), ("redact_when_done", "redactWhenDone")):
        if src in a:
            body[dst] = a[src]
    return body


def _task_ref(a):
    """task_id YA task_uuid (kam se kam ek zaroori)"""
    if "task_id" in a and "task_uuid" in a:
        return None, "give either task_id OR task_uuid, not both"
    if "task_id" in a:
        return {"taskId": a["task_id"]}, None
    if "task_uuid" in a:
        return {"taskUuid": a["task_uuid"]}, None
    return None, "give task_id or task_uuid"


def _build_task_status(args):
    if "task_id" in args and "task_uuid" in args:
        return None, "give either task_id OR task_uuid, not both"
    if "task_id" in args:
        return ("GET", "/task/status?id=" + str(args["task_id"]), None), None
    if "task_uuid" in args:
        return ("GET", "/task/status?uuid=" + quote(args["task_uuid"], safe=""), None), None
    return None, "give task_id or task_uuid"


_TASK_ACTIONS = {"pause_task": "/task/pause", "resume_task": "/task/resume",
                 "cancel_task": "/task/cancel", "retry_task": "/task/retry", "delete_task": "/task/delete"}


def _build_task_action(name, args):
    ref, err = _task_ref(args)
    if err:
        return None, err
    body = dict(ref)
    if name == "retry_task":
        if "from_start" in args:
            body["fromStart"] = args["from_start"]
        if "timeout_seconds" in args:
            body["timeoutSeconds"] = args["timeout_seconds"]
    return ("POST", _TASK_ACTIONS[name], body), None


def _text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def render_response(resp):
    """Phone ke relay response ko MCP tool result mein badalta hai."""
    status = resp.get("status", 0)
    is_error = status >= 400 or status == 0
    ctype = (resp.get("contentType") or "").lower()

    if resp.get("bodyBase64") and ctype.startswith("image/") and not is_error:
        return {
            "content": [{
                "type": "image",
                "data": resp["bodyBase64"],
                "mimeType": ctype.split(";")[0],
            }],
            "isError": False,
        }

    body = resp.get("body")
    if body is None:
        body = f"HTTP {status}"
    return _text_result(body, is_error)


def _build_module_call(args):
    module, capability = args["module"], args["capability"]
    if not _NAME_RE.match(module) or not _NAME_RE.match(capability):
        return None, "module and capability must be lowercase names like 'sample' / 'echo'"
    body = {"arguments": args.get("arguments", {})}
    if args.get("confirm") is True:
        body["confirm"] = True
    return ("POST", f"/x/{module}/{capability}", body), None


async def _device_call(args, device_arg, relay):
    capability = args["capability"]
    if not _NAME_RE.match(capability):
        return _text_result("Invalid capability name", True)
    try:
        device = relay.resolve(device_arg, platform=None)
    except DeviceError as e:
        return _text_result(f"Cannot use that device: {e}", True)

    if device.platform == "android":
        return _text_result(
            f"'{device.id}' is an Android phone - use the phone tools (tap, read_screen, call_module...) instead.", True)
    if capability not in device.capabilities:
        return _text_result(
            f"'{device.id}' ({device.platform}) does not announce capability '{capability}'. "
            f"Available: {device.capabilities or 'none'}", True)

    try:
        resp = await relay.call(device, "CALL", "/cap/" + capability, args.get("arguments", {}), timeout=CALL_TIMEOUT)
    except DeviceTimeout:
        return _text_result("The device did not answer in time. Do not retry in a loop - tell the user.", True)
    except DeviceError as e:
        return _text_result(f"Relay error: {e}", True)
    return render_response(resp)


async def call_tool(name, args, relay):
    name = _ALIASES.get(name, name)
    d = _BY_NAME.get(name)
    if d is None:
        raise KeyError(name)

    err = validate_args(_effective_schema(d), args)
    if err:
        return _text_result(f"Invalid arguments: {err}", True)
    args = dict(args or {})

    if d.get("needs_confirm") and args.get("confirm") is not True:
        return _text_result(
            f"'{name}' is a high-risk action. Ask the user for explicit approval, "
            "then call again with confirm=true.", True)

    if name == "device_status":
        return _text_result(json.dumps(relay.status()))

    device_arg = args.pop("device", None)

    if name == "device_call":
        return await _device_call(args, device_arg, relay)

    if name == "live_view":
        import agent_tools
        return await agent_tools.live_view(relay, device_arg, args)
    if name in ("agent_start", "agent_status", "agent_approve", "agent_stop"):
        import agent_tools
        return await agent_tools.agent_tool(name, relay, device_arg, args)

    if name in ("send_file", "receive_file"):
        try:
            device = relay.resolve(device_arg, platform="android")
        except DeviceError as e:
            return _text_result(f"The phone is not available: {e}", True)
        fn = file_tools.send_file if name == "send_file" else file_tools.receive_file
        return await fn(relay, device, args, CALL_TIMEOUT)

    if name == "tap":
        route, err = _build_tap(args)
    elif name == "long_press":
        route, err = _build_long_press(args)
    elif name == "task_status":
        route, err = _build_task_status(args)
    elif name in _TASK_ACTIONS:
        route, err = _build_task_action(name, args)
    elif name == "call_module":
        route, err = _build_module_call(args)
    else:
        route, err = d["route"](args), None
    if err:
        return _text_result(f"Invalid arguments: {err}", True)

    method, path, body = route
    try:
        device = relay.resolve(device_arg, platform="android")
        resp = await relay.call(device, method, path, body, timeout=CALL_TIMEOUT)
    except DeviceOffline as e:
        return _text_result(
            f"The phone is not available: {e}. Ask the user to open Control Hub, tap "
            "'Start API Server', and make sure the phone has internet.", True)
    except DeviceTimeout:
        return _text_result(
            "The phone did not answer in time. It may be locked, asleep, or the action is "
            "taking too long. Do not retry in a loop - tell the user.", True)
    except DeviceError as e:
        return _text_result(f"Relay error: {e}", True)

    return render_response(resp)


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


async def handle_rpc(msg, relay):
    """
    Ek JSON-RPC message handle karta hai.
    Return: response dict, ya None (notification / response-type message ke liye).
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request")

    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    # Client ke bheje hue responses (hum server-initiated requests nahi bhejte) — ignore
    if method is None:
        return None

    if is_notification:
        return None  # e.g. notifications/initialized, notifications/cancelled

    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return _rpc_result(msg_id, {})

    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": list_tools()})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or (name not in _BY_NAME and name not in _ALIASES):
            return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
        result = await call_tool(name, params.get("arguments"), relay)
        return _rpc_result(msg_id, result)

    return _rpc_error(msg_id, -32601, f"Method not found: {method}")
