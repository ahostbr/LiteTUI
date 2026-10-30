"""Hooks at host boundaries. The app retains tool policy and turn ownership."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path

from litetui import lifecycle_hooks as hooks
from litetui import seat_authority, tool_policy
from litetui.event_origin import EventOrigin
from litetui.widgets import _mark_delivered


def initialize(app):
    app._hook_workspace = Path.cwd().resolve()
    app.hook_config = hooks.HookConfig(*hooks.config_paths(app._hook_workspace))
    app.hook_results = {}
    app.rejected_prompts = []
    app._hook_turn_id = None
    app._hook_source = "typed"
    app._hooks_suppressed = False
    app._hook_corrections = 0
    app._hook_conversation_id = None
    app._hook_lifecycle = []
    app._hook_lock = asyncio.Lock()


def snapshot(app):
    captured = getattr(app, "_conversation_transition_hook_snapshot", None)
    if captured is not None:
        return captured
    config = getattr(app, "hook_config", None)
    return config.snapshot() if config else hooks.Snapshot()


def context(app):
    return {"source": getattr(app, "_hook_source", "typed"),
            "conversation_id": getattr(app, "convo_id", None),
            "turn_id": getattr(app, "_hook_turn_id", None)}


def report_failure(app, event, message):
    if getattr(app, "_hook_shutting_down", False):
        # Textual has removed the chat DOM before on_unmount. Keep shutdown
        # diagnostics metadata-only; no hook output or free-form error text.
        app.log.warning(f"Lifecycle hook failure during shutdown: {event}")
    else:
        app._system(message)


async def invoke(app, hook, document, profile, *, allow_prompt=True, testing=False):
    workspace = app._hook_workspace
    # The existing classifier receives the command plus its arguments; the
    # runner still executes the original argument array directly.
    args = {"command": json.dumps([hook.executable, *hook.argv]),
            "cwd": hook.cwd or str(workspace), "env": dict(hook.env)}
    refusal = await app._authorize_action(
        hook.approval_name(workspace), args, tool_policy.SHELL_POLICY,
        profile=profile, workspace=workspace, allow_prompt=allow_prompt, stop_on_denial=not testing,
        hook_test=testing,  # T1049-B K1: the one caller a relay refusal must not stop
    )
    if refusal:
        result = hooks.HookResult(False, refusal[0])
    else:
        options = {}
        scoped = getattr(app, "_conversation_transition_hook_outcomes", None) is not None
        terminal_seen = False
        if scoped:
            def settled(_result, proof):
                nonlocal terminal_seen
                terminal_seen = True
                if proof["process_started"] and (not proof["process_exited"] or not proof["pipes_settled"]
                        or proof["kill_requested"] and proof["kill_confirmed"] is not True):
                    app._conversation_transition_cleanup_error = "Lifecycle hook process or pipe cleanup is unverified"
            options["on_settled"] = settled
        try:
            result = await hooks.run_hook(hook, document, workspace,
                cancelled=lambda: not testing and document.get("event") not in ("tool_after", "app_shutdown")
                and getattr(app, "_stop_requested", False), **options)
        finally:
            if scoped and not terminal_seen:
                app._conversation_transition_cleanup_error = "Lifecycle hook did not report terminal cleanup proof"
    app.hook_results[(hook.scope, hook.id)] = result
    recorder = getattr(app, "_conversation_transition_hook_outcomes", None)
    if recorder is not None and document.get("event") in ("conversation_leave", "conversation_start"):
        recorder.append({"event": document["event"], "scope": hook.scope, "id": hook.id,
                         "conversation_id": document.get("conversation_id"), "allowed": result.allowed,
                         "executed": not bool(refusal), "exit_code": result.exit_code})
        if not refusal and result.exit_code is None:
            app._conversation_transition_cleanup_error = "Lifecycle hook process termination is unverified"
    return result


async def dispatch(app, event, data, *, profile=None, captured=None,
                   allow_prompt=True, selected=None):
    state = selected if selected is not None else snapshot(app)
    if state.disabled:
        return hooks.HookResult(True)
    if state.error:
        report_failure(app, event, f"[hooks configuration error] {state.error}")
        return hooks.HookResult(event not in hooks.GATES, state.error)
    ctx = captured or context(app)
    matches = state.matching(event, ctx["source"], data.get("tool", ""))
    if not matches:
        return hooks.HookResult(True)
    document = hooks.event_document(event, app._hook_workspace, data, **ctx)
    profile = profile or getattr(app, "_active_tool_profile", None) or tool_policy.STRICT
    refusals = []
    for hook in matches:
        if getattr(app, "_stop_requested", False) and event not in ("tool_after", "app_shutdown"):
            return hooks.HookResult(False, "stopped by user")
        result = await invoke(app, hook, document, profile, allow_prompt=allow_prompt)
        if not result.allowed:
            report_failure(app, event, f"[hook {hook.id}] {result.reason}")
            if hook.mode == "gate":
                refusals.append(f"{hook.id}: {result.reason}")
    return hooks.HookResult(not refusals, "\n".join(refusals))


def queue_lifecycle(app, event, conversation_id=None):
    if not hasattr(app, "_hook_lifecycle"):
        return
    ctx = {**context(app), "conversation_id": conversation_id}
    app._hook_lifecycle.append((event, ctx, snapshot(app)))
    if (app.is_running and not getattr(app, "_hook_shutting_down", False)
            and getattr(app, "_conversation_transition_hook_outcomes", None) is None):
        app.run_worker(drain_lifecycle(app), group="hooks", exit_on_error=False)


async def drain_lifecycle(app):
    async with app._hook_lock:
        while app._hook_lifecycle:
            event, ctx, state = app._hook_lifecycle.pop(0)
            await dispatch(app, event, {}, captured=ctx, selected=state,
                           allow_prompt=event != "app_shutdown" and not getattr(app, "_hook_shutting_down", False)
                           and getattr(app, "_conversation_transition_hook_outcomes", None) is None)


def leave_conversation(app):
    id = getattr(app, "_hook_conversation_id", None)
    if id:
        queue_lifecycle(app, "conversation_leave", id)
        app._hook_conversation_id = None


def enter_conversation(app, event):
    if not hasattr(app, "hook_config"):
        return
    id = app.convo_id
    if id != app._hook_conversation_id:
        leave_conversation(app)
        app._hook_conversation_id = id
        queue_lifecycle(app, event, id)


def _turn_source(item) -> str:
    # T1043: an item with NO source is "unlabelled", never "queued". "queued" is
    # an ATTENDED label (seat_authority.ATTENDED_SOURCES, exempt in Owner's own
    # instance) and must be earned by a typed submit, not by a producer that
    # forgot to say what it is. The steering ledger's copy, for one, keeps
    # "source" but drops "goal_continuation". Its tool profile is unchanged:
    # turn_profile treats any unlisted source like an attended submit.
    # T1043: a goal item's source carries its loop's origin ("goal-owner" /
    # "goal", goal_loop), so the source wins; goal_continuation alone (a goal item
    # saved before that) is plain "goal".
    if item.get("source"):
        return item["source"]
    return "goal" if item.get("goal_continuation") else "unlabelled"


def refuse_below_floor(app, item, *, on_refused=None) -> bool:
    """T1043: True (and the item retained, said and reported) when the seat is
    below the fleet floor. Nothing is appended and nothing reaches the model."""
    why = seat_authority.floor_refusal(app, _turn_source(item))
    if why is None:
        return False
    if not hasattr(app, "rejected_prompts"):
        app.rejected_prompts = []
    app.rejected_prompts.append({**item, "reason": why})
    app._system(why)
    # Through the one turn_end door, WITH the reason: a supervising parent maps a
    # not-started fleet_floor turn_end to a failed child carrying this text
    # (Dijkstra Q3) instead of "Child completed without a started turn".
    if on_refused is not None:
        on_refused(why)
        return True
    end = getattr(app, "_emit_turn_end", None)
    if end is not None:
        end("fleet_floor", None, None, error=why)
    elif getattr(app, "_rpc_emit", None) is not None:
        app._rpc_emit({"type": "turn_end", "stopReason": "fleet_floor", "error": why})
    return True


def refuse_engine_admission(app, item, *, on_refused=None) -> bool:
    from litetui.backend_transition import submission_refusal
    why = submission_refusal(app)
    if why is None:
        return False
    if not hasattr(app, "rejected_prompts"):
        app.rejected_prompts = []
    app.rejected_prompts.append({**item, "reason": why})
    app._system(why)
    if on_refused is not None:
        on_refused(why)
    return True


def accept_prompt(app, item, *, native_accepted=False) -> bool:
    """Boolean admission contract retained for queued/native callers."""
    return accept_prompt_origin(app, item, native_accepted=native_accepted) is not None


def accept_prompt_origin(app, item, *, native_accepted=False, origin=None, on_refused=None,
                         persist_user=None) -> EventOrigin | None:
    """Accept input and return its captured origin, without scheduling execution.

    `native_accepted` is passed ONLY by codex_steering.accept_steered, for input
    the Codex app-server already holds. It is not trusted item metadata.
    """
    if refuse_engine_admission(app, item, on_refused=on_refused):
        return None
    if origin is None:
        origin = EventOrigin(operation_id=item.get("operation_id"), source=item.get("source", "queued"))
    if not native_accepted and refuse_below_floor(app, item, on_refused=on_refused):
        return None
    if not item.get("_gui_in_turn"):
        app._gui_operation_id = item.get("operation_id")
    # T1027: the ONE place a turn's authority is decided, for every source.
    # The producer's profile is only a request; the seat's flag and choice rule.
    # T1043: the fleet floor was checked above, before anything is appended.
    app._active_tool_profile = seat_authority.turn_profile(
        app, _turn_source(item), item.get("tool_profile"))
    app._hooks_suppressed = False
    app._hook_corrections = 0
    hook_id = str(uuid.uuid4())
    app._hook_turn_id = hook_id
    app._hook_source = item.get("source", "queued")
    materialise = getattr(app, "_materialise_convo", None)
    if materialise is not None:
        materialise()
    session_id = getattr(app, "convo_id", None)
    message = {"role": "user", "content": item["content"]}
    if getattr(getattr(app, "backend", None), "owns_native_turns", False):
        from litetui.claude_turn import UncertainDelivery, accept_input, hold_input
        try:
            message["claude_delivery"] = accept_input(app, item)
        except UncertainDelivery as exc:
            hold_input(app, item, exc)
            return None
    entry = item.get("_codex_entry")
    if entry:
        message["codex_delivery"] = {"id": entry["id"], "threadId": entry["threadId"],
                                     "state": "accepted" if entry["state"] == "accepted" else "pending"}
        if item.get("_codex_metadata"):
            message["provider_metadata"] = item["_codex_metadata"]
        if item.get("bubble") is not None and entry["state"] != "accepted":
            if not hasattr(app, "_codex_delivery_bubbles"):
                app._codex_delivery_bubbles = {}
            app._codex_delivery_bubbles[entry["id"]] = item["bubble"]
    # Only an internal caller can supply a persistence strategy. Never accept
    # a wire/item flag that could skip durable user-message materialization.
    (persist_user or app._append)(message)
    ident = item.get("approval_request_id")
    if ident:
        from litetui import approval_delivery
        # Admission is model-input injection, not proof of provider processing.
        approval_delivery.stage(ident, "injected")
    if not entry or entry["state"] == "accepted":
        _mark_delivered(item)
    return replace(origin, session_id=session_id, hook_id=hook_id, execution_id=None)


async def admit_prompt(app, item) -> bool:
    return await admit_prompt_origin(app, item) is not None


async def admit_prompt_origin(app, item, *, on_refused=None, before_accept=None,
                              persist_user=None) -> EventOrigin | None:
    if refuse_engine_admission(app, item, on_refused=on_refused):
        return None
    # Capture input metadata before the first suspension, not from mutable app
    # fields after hooks or lifecycle work have run.
    origin = EventOrigin(operation_id=item.get("operation_id"), source=item.get("source", "queued"))
    # T1043: before the prompt_before hooks, so no hook runs for a refused turn.
    if refuse_below_floor(app, item, on_refused=on_refused):
        return None
    if not item.get("_gui_in_turn"):
        app._gui_operation_id = item.get("operation_id")
    app._stop_requested = False
    await drain_lifecycle(app)
    if refuse_engine_admission(app, item, on_refused=on_refused):
        return None
    if before_accept is not None:
        before_accept()
    ctx = {**context(app), "source": item.get("source", "queued"), "turn_id": str(uuid.uuid4())}
    result = await dispatch(app, "prompt_before", {"prompt": item["content"]},
                            profile=seat_authority.turn_profile(app, _turn_source(item), item.get("tool_profile")),
                            captured=ctx)
    if refuse_engine_admission(app, item, on_refused=on_refused):
        return None
    if before_accept is not None:
        before_accept()
    if result.allowed and not app._stop_requested:
        accepted = accept_prompt_origin(app, item, origin=origin, on_refused=on_refused,
                                        persist_user=persist_user)
        if accepted is None:
            return None
        app._hook_turn_id = ctx["turn_id"]
        accepted = replace(accepted, hook_id=ctx["turn_id"])
        await drain_lifecycle(app)
        return accepted
    app.rejected_prompts.append({**item, "reason": result.reason})
    app._system(f"[prompt rejected; retained in /hooks] {result.reason}")
    if on_refused is not None:
        on_refused(result.reason or ("Stopped by user" if app._stop_requested else "Prompt refused"))
    else:
        app._rpc_emit({"type": "turn_end", "stopReason": "hook_denied"})
    return None


def start_prompt(app, item):
    entry = item.get("_codex_entry")
    if entry and entry.get("state") in ("admitted", "next_turn"):
        accepted = accept_prompt_origin(app, item)
        if accepted is None:
            return
        app._hook_turn_id = entry.get("admission", {}).get("turn_id", app._hook_turn_id)
        app._stream(origin=replace(accepted, hook_id=app._hook_turn_id))
        return
    state = snapshot(app)
    if not state.hooks and not state.error:
        accepted = accept_prompt_origin(app, item)
        if accepted is not None:
            app._stream(origin=accepted)
        return

    async def deliver():
        accepted = await admit_prompt_origin(app, item)
        if accepted is not None:
            app._stream(origin=accepted)
    app.run_worker(deliver(), group="chat", exclusive=True, exit_on_error=False)


async def queued_prompt(app):
    from litetui.backend_transition import submission_refusal
    if submission_refusal(app):
        return False
    if app._pending_input and app._pending_input[0].get("_codex_entry"):
        return False
    state = snapshot(app)
    if not state.hooks and not state.error:
        return app._deliver_queued_input()
    if not app._pending_input or app._stop_requested:
        return False
    from litetui.claude_turn import queue_ready
    if not queue_ready(app, app._pending_input[0]):
        return False
    item = app._pending_input.pop(0)
    try:
        return await admit_prompt(app, {**item, "_gui_in_turn": True})
    finally:
        if submission_refusal(app) and not any(held is item for held in app._pending_input):
            app._pending_input.insert(0, item)


async def completion(app, answer):
    if getattr(app, "_hooks_suppressed", False):
        return "allow"
    result = await dispatch(app, "completion_before", {"answer": answer})
    if getattr(app, "_stop_requested", False):
        return "pause"
    if result.allowed:
        return "allow"
    app._system(f"[completion rejected; draft retained] {result.reason}")
    if getattr(app, "_hook_corrections", 0) >= 3:
        app._system("[paused — completion hooks rejected three corrective continuations]")
        return "pause"
    app._hook_corrections = getattr(app, "_hook_corrections", 0) + 1
    app._append({"role": "user", "content": "[internal hook correction]\n" + result.reason})
    return "retry"
