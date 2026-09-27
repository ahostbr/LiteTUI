"""Hooks at host boundaries. The app retains tool policy and turn ownership."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from litetui import lifecycle_hooks as hooks
from litetui import seat_authority, tool_policy
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
    )
    if refusal:
        result = hooks.HookResult(False, refusal[0])
    else:
        result = await hooks.run_hook(hook, document, workspace,
            cancelled=lambda: not testing and document.get("event") not in ("tool_after", "app_shutdown")
            and getattr(app, "_stop_requested", False))
    app.hook_results[(hook.scope, hook.id)] = result
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
    if app.is_running and not getattr(app, "_hook_shutting_down", False):
        app.run_worker(drain_lifecycle(app), group="hooks", exit_on_error=False)


async def drain_lifecycle(app):
    async with app._hook_lock:
        while app._hook_lifecycle:
            event, ctx, state = app._hook_lifecycle.pop(0)
            await dispatch(app, event, {}, captured=ctx, selected=state,
                           allow_prompt=event != "app_shutdown" and not getattr(app, "_hook_shutting_down", False))


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
    # an ATTENDED label (seat_authority.ATTENDED_SOURCES, exempt in Ryan's own
    # instance) and must be earned by a typed submit, not by a producer that
    # forgot to say what it is. The steering ledger's copy, for one, keeps
    # "source" but drops "goal_continuation". Its tool profile is unchanged:
    # turn_profile treats any unlisted source like an attended submit.
    # T1043: a goal item's source carries its loop's origin ("goal-ryan" /
    # "goal", goal_loop), so the source wins; goal_continuation alone (a goal item
    # saved before that) is plain "goal".
    if item.get("source"):
        return item["source"]
    return "goal" if item.get("goal_continuation") else "unlabelled"


def refuse_below_floor(app, item) -> bool:
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
    end = getattr(app, "_emit_turn_end", None)
    if end is not None:
        end("fleet_floor", None, None, error=why)
    elif getattr(app, "_rpc_emit", None) is not None:
        app._rpc_emit({"type": "turn_end", "stopReason": "fleet_floor", "error": why})
    return True


def accept_prompt(app, item, *, native_accepted=False) -> bool:
    """Start this item's turn. False when the fleet floor refused it (T1043).

    `native_accepted` is passed ONLY by codex_steering.accept_steered, for input
    the Codex app-server already holds: it was checked at HostSteering.admit, and
    refusing it here would leave the transcript and the thread disagreeing. A
    keyword, not item data, so no stored or forged state can reach the skip; and
    accept_steered never starts a stream."""
    if not native_accepted and refuse_below_floor(app, item):
        return False
    if not item.get("_gui_in_turn"):
        app._gui_operation_id = item.get("operation_id")
    # T1027: the ONE place a turn's authority is decided, for every source.
    # The producer's profile is only a request; the seat's flag and choice rule.
    # T1043: the fleet floor was checked above, before anything is appended.
    app._active_tool_profile = seat_authority.turn_profile(
        app, _turn_source(item), item.get("tool_profile"))
    app._hooks_suppressed = False
    app._hook_corrections = 0
    app._hook_turn_id = str(uuid.uuid4())
    app._hook_source = item.get("source", "queued")
    materialise = getattr(app, "_materialise_convo", None)
    if materialise is not None:
        materialise()
    message = {"role": "user", "content": item["content"]}
    if getattr(getattr(app, "backend", None), "owns_native_turns", False):
        from litetui.claude_turn import accept_input
        message["claude_delivery"] = accept_input(app, item)
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
    app._append(message)
    if not entry or entry["state"] == "accepted":
        _mark_delivered(item)
    return True


async def admit_prompt(app, item):
    # T1043: before the prompt_before hooks, so no hook runs for a refused turn.
    if refuse_below_floor(app, item):
        return False
    if not item.get("_gui_in_turn"):
        app._gui_operation_id = item.get("operation_id")
    app._stop_requested = False
    await drain_lifecycle(app)
    ctx = {**context(app), "source": item.get("source", "queued"), "turn_id": str(uuid.uuid4())}
    result = await dispatch(app, "prompt_before", {"prompt": item["content"]},
                            profile=seat_authority.turn_profile(app, _turn_source(item), item.get("tool_profile")),
                            captured=ctx)
    if result.allowed and not app._stop_requested:
        if not accept_prompt(app, item):
            return False
        app._hook_turn_id = ctx["turn_id"]
        await drain_lifecycle(app)
        return True
    app.rejected_prompts.append({**item, "reason": result.reason})
    app._system(f"[prompt rejected; retained in /hooks] {result.reason}")
    app._rpc_emit({"type": "turn_end", "stopReason": "hook_denied"})
    return False


def start_prompt(app, item):
    entry = item.get("_codex_entry")
    if entry and entry.get("state") in ("admitted", "next_turn"):
        if not accept_prompt(app, item):
            return
        app._hook_turn_id = entry.get("admission", {}).get("turn_id", app._hook_turn_id)
        app._stream()
        return
    state = snapshot(app)
    if not state.hooks and not state.error:
        if accept_prompt(app, item):
            app._stream()
        return

    async def deliver():
        if await admit_prompt(app, item):
            app._stream()
    app.run_worker(deliver(), group="chat", exclusive=True, exit_on_error=False)


async def queued_prompt(app):
    if app._pending_input and app._pending_input[0].get("_codex_entry"):
        return False
    state = snapshot(app)
    if not state.hooks and not state.error:
        return app._deliver_queued_input()
    if not app._pending_input or app._stop_requested:
        return False
    return await admit_prompt(app, {**app._pending_input.pop(0), "_gui_in_turn": True})


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
