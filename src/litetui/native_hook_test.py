"""One explicitly reviewed sample invocation, using canonical non-interactive hook authority."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from litetui import (
    gui_rpc,
    hook_host,
    lifecycle_hooks,
    native_context,
    native_hook_toggle,
    native_runtime_controls,
    tool_policy,
)

REVIEW = "gui.hooks.native_test_review"
RUN = "gui.hooks.native_test_run"


@dataclass
class Attempt:
    owner: object
    binding: dict
    revision: str
    hook: lifecycle_hooks.Hook
    event: str
    profile: str
    token: str
    run_id: str | None = None
    receipt: dict | None = None


def sample(event):
    if event in ("tool_before", "tool_after"):
        return {"tool": "read", "args": {}}
    if event == "prompt_before":
        return {"text": "Hook test"}
    return {}


def review(app, cmd):
    bridge, binding = native_context.read_binding(app, cmd, REVIEW, "scope", "revision", "hook_id", "event")
    gui_rpc._idle(app)
    bridge.hook_test = None
    if not native_runtime_controls.enabled(app):
        raise ValueError("Hook tests require active, owned runtime authorization and shutdown tracking.")
    if cmd["scope"] not in native_hook_toggle.SCOPES:
        raise ValueError("Unknown scope for hook test.")
    bridge.hook_toggle = None
    captured = native_hook_toggle.capture(app)
    if getattr(app, "_conversation_transition_cleanup_error", None):
        raise ValueError("Previous hook cleanup is unverified.")
    if captured.revision != cmd["revision"] or captured.effective.disabled:
        raise ValueError("Hook configuration changed or lifecycle hooks are switched off.")
    _, hook = native_hook_toggle.row(captured, cmd["scope"], cmd["hook_id"])
    event = cmd["event"]
    if event not in hook.events:
        raise ValueError("Select one of this hook's configured events.")
    profile = app.chosen_tool_profile
    if profile not in tool_policy.PROFILES:
        raise ValueError("Unknown tool profile for hook test.")
    attempt = Attempt(app.hook_config, binding.copy(), captured.revision, hook, event, profile, secrets.token_hex(32))
    bridge.hook_test = attempt
    return {"version": 1, **binding, "kind": "hook_test_review", "scope": hook.scope,
            "token": attempt.token, "revision": captured.revision, "profile": profile,
            "row": native_hook_toggle.details(app, hook, captured.effective),
            "event": event, "source": "typed", "sample": sample(event)}


async def run(app, cmd):
    bridge, binding = native_context.read_binding(app, cmd, RUN, "scope", "token")
    gui_rpc._idle(app)
    a = getattr(bridge, "hook_test", None)
    if not a or a.binding != binding or a.owner is not app.hook_config or cmd["token"] != a.token or cmd["scope"] != a.hook.scope:
        raise ValueError("Review the current hook and sample before testing.")
    if a.run_id is not None:
        if a.run_id == cmd["id"] and a.receipt is not None:
            return a.receipt
        raise ValueError("This reviewed test has already been attempted.")
    a.run_id = cmd["id"]
    context = {"source": "typed", "conversation_id": None, "turn_id": None}
    async with native_runtime_controls.authorization(app, profile=a.profile, captured=context) as ticket:
        # Recheck after any runtime policy reservation wait, immediately before the
        # canonical authorizer sees the original argv, cwd and environment.
        gui_rpc._idle(app)
        bridge.check()
        app._validate_owned_identity()
        if (bridge.closing or getattr(app, "_gui_quitting", False) or bridge.hook_test is not a
                or a.owner is not app.hook_config or native_hook_toggle.capture(app).revision != a.revision
                or a.profile != app.chosen_tool_profile or ticket is None or ticket.profile != a.profile):
            raise ValueError("Hook configuration, policy or owner changed before the test.")
        document = lifecycle_hooks.event_document(a.event, app._hook_workspace, sample(a.event), source="typed")
        try:
            result = await hook_host.invoke(app, a.hook, document, a.profile, allow_prompt=False, testing=True)
        except Exception as exc:
            raise ValueError("Hook test outcome unavailable.") from exc
    exit_code = result.exit_code
    if bridge.closing and result.reason == "hook cancelled":
        status = "cancelled"
    elif result.timed_out:
        status = "timeout"
    elif exit_code is None:
        status = "unavailable"
    elif result.allowed:
        status = "allowed"
    else:
        status = "denied"
    # stdout, stderr, a user-defined gate denial reason and OS errors can all
    # contain private environment values. The native receipt is metadata only.
    cleanup_verified = not bool(getattr(app, "_conversation_transition_cleanup_error", None))
    a.receipt = {"version": 1, **binding, "kind": "hook_test_result", "scope": a.hook.scope,
                 "token": a.token, "run_id": a.run_id, "event": a.event, "status": status,
                 "exit_code": exit_code, "truncated": result.truncated, "cleanup_verified": cleanup_verified,
                 "stdout_bytes": len(result.stdout.encode()), "stderr_bytes": len(result.stderr.encode())}
    return a.receipt
