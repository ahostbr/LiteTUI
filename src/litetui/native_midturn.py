"""Queue a reviewed text draft behind the exact ongoing host-managed turn."""
from __future__ import annotations

from litetui import background_input as work
from litetui import native_runtime_controls as controls
from litetui import seat_authority
from litetui.native_context import read_binding
from litetui.textfmt import midturn_action

REVIEW = "gui.prompt.native_queue_review"
APPLY = "gui.prompt.native_queue_apply"
SEND_REVIEW = "gui.prompt.native_send_review"
SEND_APPLY = "gui.prompt.native_send_apply"
MAX_TEXT = 16384


def interrupt_queue(app, bridge):
    if len(app._pending_input) > work.LIMITS["inputs"]:
        raise ValueError("Interrupt queue exceeds the Work input limit")
    prior = []
    for item in app._pending_input:
        entry = work.entry_for(app, item)
        if (entry is None or entry.recovered or entry.state != "queued" or entry.claim_task is not None
                or entry.epoch["instance"] != bridge.instance):
            raise ValueError("Interrupt cannot change the order of held or unowned input")
        if entry.ref["input_id"] in prior:
            raise ValueError("Interrupt queue contains the same input twice")
        prior.append(entry.ref["input_id"])
    return prior


def dispatch(app, cmd):
    operation = cmd["type"]
    if operation not in (REVIEW, APPLY, SEND_REVIEW, SEND_APPLY):
        raise ValueError("Unknown mid-turn operation")
    extended = operation in (SEND_REVIEW, SEND_APPLY)
    apply = operation in (APPLY, SEND_APPLY)
    fields = ("origin", "chord", "text", "profile", "policy_epoch") + (("review",) if apply else ())
    bridge, binding = read_binding(app, cmd, operation, *fields)
    pending = bridge.user_queue_review
    bridge.user_queue_review = None
    if not controls.enabled(app) or bridge.provider != "host":
        raise ValueError("Mid-turn composer queueing requires a captured host-managed stream")
    state = controls._state(app)
    if reason := work.prompt_refusal(app):
        raise ValueError(reason)
    controls._guard(app, state)
    owner = controls._owner(state)
    if (owner["state"] != "active" or owner["origin"] != cmd["origin"]
            or owner["origin"]["source"] != "rpc"
            or not owner["origin"]["hook_id"] or owner["origin"]["hook_id"] != getattr(app, "_hook_turn_id", None)
            or getattr(app, "_hook_source", None) != "rpc"
            or getattr(app, "_gui_operation_id", None) != owner["origin"]["operation_id"]):
        raise ValueError("The reviewed foreground turn finished or changed")
    worker, origin = bridge.executions[owner["origin"]["execution_id"]]
    if (worker.is_finished or not app._chat_running() or getattr(app, "_stop_requested", False)
            or getattr(app, "_mcp_maintenance", False) or getattr(app, "pending_image", None)
            or getattr(app, "_conversation_transition_pending", False)
            or any(not e.recovered and e.admission and e.admission["origin"]["execution_id"] == origin.execution_id
                   for e in bridge.inputs.values())):
        raise ValueError("The foreground turn cannot accept this queued draft")
    chord, message = cmd["chord"], cmd["text"]
    if (chord not in ("enter", "alternate") or type(message) is not str or not message.strip()
            or len(message.encode()) > MAX_TEXT or "\0" in message or message.lstrip().startswith("/")):
        raise ValueError("Queue a plain text draft of 1 to 16384 UTF-8 bytes")
    swapped = app.settings.enter_interrupts
    if type(swapped) is not bool:
        raise ValueError("The effective Enter behavior is unavailable")
    behavior = midturn_action(swapped, chord == "alternate")
    if behavior == "interrupt" and not extended:
        raise ValueError("This key would interrupt the turn. Use the other send key to queue, or stop the turn explicitly.")
    profile = seat_authority.seat_profile(app)
    epoch = state.generation + 1
    if type(cmd["policy_epoch"]) is not int or not 1 <= epoch == cmd["policy_epoch"] or cmd["profile"] != profile:
        raise ValueError("The queued text belongs to a different runtime policy")
    payload = {"content": message, "text": message, "source": "rpc", "tool_profile": profile}
    work.validate_payload(payload)
    value = {"version": 1, **binding, "origin": origin.envelope_fields(), "chord": chord, "text": message,
             "behavior": behavior, "enter_interrupts": swapped, "profile": profile,
             "policy_epoch": epoch, "payload_sha256": work.digest(work.encoded(payload))}
    if behavior == "interrupt":
        value["prior_inputs"] = interrupt_queue(app, bridge)
    if not apply:
        value.update(kind="midturn_review", review=work.ident())
        bridge.user_queue_review = (worker, work.encoded([extended, value]))
        return value
    value.update(kind="midturn_review", review=cmd["review"])
    if pending != (worker, work.encoded([extended, value])):
        raise ValueError("The queue review is stale or consumed")
    producer, entry = work.queue_user_input(app, origin, value["review"], payload, interrupt=behavior == "interrupt")
    return {**value, "kind": "midturn_queued", "operation_id": cmd["id"], "producer": producer, "input": entry}
