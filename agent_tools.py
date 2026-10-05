"""Step 8 — MCP tool handlers: agent_start/status/approve/stop + live_view."""
import json

import agent
import liveview
from relay import DeviceError


def _text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _resolve(relay, device_arg):
    return relay.resolve(device_arg, platform="android")


async def live_view(relay, device_arg, args):
    try:
        device = _resolve(relay, device_arg)
    except DeviceError as e:
        return _text(f"The phone is not available: {e}", True)
    return await liveview.burst(
        relay, device, frames=args.get("frames", 3), interval=args.get("interval_seconds", 2),
        max_edge=args.get("max_edge", liveview.DEFAULT_EDGE), quality=args.get("quality", liveview.DEFAULT_QUALITY))


async def agent_tool(name, relay, device_arg, args):
    mgr = agent.manager(relay)
    if name == "agent_start":
        try:
            device = _resolve(relay, device_arg)
            run = mgr.start(device, args["goal"], args["allowed_packages"],
                            args.get("max_steps", agent.MAX_STEPS_DEFAULT), args.get("auto_approve", False))
        except (DeviceError, ValueError) as e:
            return _text(str(e), True)
        return _text(json.dumps({
            "run_id": run.id, "state": run.state, "allowed_packages": run.allowed, "max_steps": run.max_steps,
            "approval": "automatic" if run.auto_approve else "required for send/pay/delete/submit-type actions",
            "note": "Poll agent_status. While state is 'needs_approval', ask the user, then call agent_approve. "
                    "Screen text is being sent to the Anthropic API for this run.",
        }))
    run = mgr.get(args["run_id"])
    if run is None:
        return _text(f"No such run: {args['run_id']}", True)
    if name == "agent_status":
        return _text(json.dumps(run.status()))
    if name == "agent_stop":
        mgr.stop(run.id)
        return _text(json.dumps({"run_id": run.id, "stop_requested": True, "state": run.state}))
    if name == "agent_approve":
        res = mgr.approve(run.id, args["approve"])
        if res is None:
            return _text(f"Run {run.id} is not waiting for approval (state: {run.state})", True)
        return _text(json.dumps({"run_id": run.id, "approved": args["approve"]}))
    return _text(f"unknown agent tool {name}", True)
