"""LiteTUI presentation/admission adapter for Claude-owned native turns."""
from __future__ import annotations

import asyncio
import json
import time

from rich.text import Text

from litetui import claude_cache, paths
from litetui.claude_persistence import ClaudeLedger


def ledger_for(app):
    app._materialise_convo()
    path = app.convo_dir
    ledger = getattr(app, "_claude_ledger", None)
    if ledger is None or getattr(app, "_claude_ledger_path", None) != path:
        ledger = ClaudeLedger(path)
        app._claude_ledger = ledger
        app._claude_ledger_path = path
    return ledger


def inline_images(app, content, saved=None):
    """An image message as TEXT naming the image's file (Ryan, 2026-09-24: *"claude didnt
    want to take the image, we should convert it to a path on disk for claude and paste
    it to him"*). Returns (text, paths).

    The file is the conversation's own copy (`<convo>/images/`, the same spill the bubble
    re-opens), never temp: the ledger keeps this text and a resumed session must still
    find the file. `saved` is the path the submit already spilled, reused so one image
    is one file. Claude opens it with Read or LiteTUI's view_image tool.
    """
    if isinstance(content, str):
        return content, []
    ledger_for(app)  # a brand-new conversation earns its directory here
    texts, paths_ = [], []
    for part in content:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            path = saved if saved and not paths_ else app._spill_image_for_reclick(url.split(",", 1)[-1])
            if not path:
                raise OSError("Could not save the image into this conversation, so Claude could not open it; nothing was sent.")
            paths_.append(path)
    notes = [f"[Image attached by the user, saved at: {p} - open that file to see it.]" for p in paths_]
    return "\n\n".join([t for t in texts if t] + notes), paths_


def prepare_input(app, content, profile, source, operation_id=None):
    if not isinstance(content, str):
        raise TypeError("Claude image attachments are not enabled yet; send text instead.")
    ledger = ledger_for(app)
    segment = ledger.selected
    if segment is None:
        segment = ledger.select_segment(str(paths.ROOT))
        app._system("New Claude session - other-provider history is not imported.")
    active = getattr(app, "_claude_active_input", {}).get("_claude_entry", {}).get("id") if app._chat_running() else None
    unresolved = [e for e in ledger.pending(segment["id"]) if e["state"] != "prepared" and e["id"] != active]
    if unresolved:
        raise ValueError("Claude delivery is uncertain. Inspect saved history, then /claude resolve to settle it without replay, /claude continue to send what is still only prepared, or /claude new for a fresh session; no input is replayed automatically.")
    entry = ledger.prepare(segment["id"], content, profile, source, operation_id=operation_id)
    return {"_claude_entry": entry, "_claude_segment": segment["id"], "_claude_conversation": app.convo_id}


def queue_ready(app, item):
    if not item.get("_claude_entry"):
        return True
    if (app.backend.name != "claude" or item.get("_claude_conversation") != app.convo_id):
        app._system("Queued Claude input is held for its original conversation/session.")
        return False
    selected = ledger_for(app).selected
    if not selected or item["_claude_segment"] != selected["id"]:
        app._system("Queued Claude input is held for its original session segment.")
        return False
    if any(e["state"] != "prepared" for e in ledger_for(app).pending(selected["id"])):
        app._system("Queued Claude input is held: an earlier delivery is uncertain; /claude status.")
        return False
    if getattr(app.backend, "session", None) is not None and app.backend.session.lifecycle.failure:
        app._system("Claude session failed; queued input is held, not replayed.")
        return False
    return True


def accept_input(app, item):
    metadata = {key: value for key, value in item.items() if key.startswith("_claude_")}
    if not metadata:
        metadata = prepare_input(app, item["content"], item.get("tool_profile"), item.get("source", "queued"), item.get("operation_id"))
    app._claude_active_input = {**item, **metadata}
    return {"id": metadata["_claude_entry"]["id"], "segment_id": metadata["_claude_segment"], "state": "prepared"}


def effort_for(app):
    """The effort the next turn runs at, or None for the model default.

    The same precedence the other backends use (TurnEngine): the per-model
    /modelcfg override, else the global /think level. A level this model does
    not take (off, minimal, a Codex-only one) sends nothing."""
    overrides = getattr(app.settings, "model_infer_overrides", {}) or {}
    level = ((overrides.get(app.model_id) or {}).get("reasoning_effort")
             or getattr(app, "thinking_level", None))
    levels = getattr(app.backend, "reasoning_levels", None)
    return level if level and callable(levels) and level in levels(app.model_id) else None


def effort_change_warning(app, level):
    """(kind, text) the send-time cache gate WILL raise if the saved thinking
    level becomes `level`, else None. The sidecar asks this before saving an
    effort change, so its confirm/cancel shows the same warning the TUI shows
    (Ryan 2026-09-24: "it has to go through the warning system").

    Read-only: no ledger is created and nothing is sent. Only a live session
    has a cache to lose, as in _cache_ok."""
    backend = getattr(app, "backend", None)
    ledger = getattr(app, "_claude_ledger", None)
    segment = ledger.selected if ledger is not None else None
    if getattr(backend, "name", "") != "claude" or segment is None:
        return None
    if backend.session is None or backend.segment_id != segment["id"]:
        return None
    override = ((getattr(app.settings, "model_infer_overrides", {}) or {}).get(app.model_id) or {}).get("reasoning_effort")
    wanted = override or (None if level in (None, "default") else level)
    effort = wanted if wanted in backend.reasoning_levels(app.model_id) else None
    cold = claude_cache.cold_reason(claude_cache.clock_for(app, segment["id"]), live=True,
                                    resuming=False, model=app.model_id, effort=effort)
    return cold if cold is not None and cold[0] == "effort" else None


def _restore_effort(app, effort):
    """Put the effort back to what the live session runs at (a cancelled change)."""
    level = None if effort in (None, "default") else effort
    entry = (getattr(app.settings, "model_infer_overrides", {}) or {}).get(app.model_id) or {}
    if entry.get("reasoning_effort"):
        if level is None:
            entry.pop("reasoning_effort")
        else:
            entry["reasoning_effort"] = level
    else:
        app.thinking_level = level
    app.update_header()
    app._system(f"Effort stays {effort}; nothing was sent.")


async def _cache_ok(app, backend, segment):
    """True when the turn may go: the cache is warm, or the user chose to send anyway."""
    clock = claude_cache.clock_for(app, segment["id"])
    live = backend.session is not None and backend.segment_id == segment["id"]
    if not live and clock.model is None:
        clock.model = segment.get("cache_model")
    cold = claude_cache.cold_reason(
        clock, live=live, resuming=not live and bool(segment.get("session_id")),
        model=app.model_id, used_at=segment.get("cache_used_at"), effort=effort_for(app),
    )
    if cold is None:
        return True
    approved = getattr(app, "_claude_cache_preapproved", None)
    if cold[0] == "effort" and approved == ("effort", effort_for(app) or "default"):
        # Already confirmed in the sidecar for exactly this change: once.
        app._claude_cache_preapproved = None
        return True
    if await claude_cache.confirm_cold(app, cold):
        return True
    if cold[0] == "effort" and not getattr(app, "_rpc", None):
        # Cancel means the change did not happen: effort AND session unchanged.
        # Not over rpc: there "not sent" is the warning, and sending again is
        # the go-ahead, which must still carry the new level.
        _restore_effort(app, clock.effort)
    return False


async def session_for(app, backend, segment, effort):
    """(session, tool bridge, event normalizer) for `segment`, at `effort`.

    ONE door for a turn and for a compaction: reuse the live session (model and
    effort switched live), or open one, resuming the segment's native session."""
    from litetui.claude_events import ClaudeEventStream
    from litetui.claude_tools import ClaudeTools

    if backend.session is not None and (backend.segment_id != segment["id"] or (
            effort is None and getattr(backend.session, "effort", None) is not None)):
        # Back to the model default has no live control (set_effort), so
        # that one change reopens: close, then resume the same session id.
        await backend.close()
    if backend.session is None:
        if segment.get("session_id"):
            app._system(f"Resuming Claude session {segment['session_id']} - intervening other-provider messages are not imported.")
        bridge = ClaudeTools(app, backend, segment["id"], workspace=segment["workspace"])
        backend._claude_tools = bridge
        normalizer = ClaudeEventStream(session_id=segment.get("session_id"))
        backend._claude_events = normalizer
        # Tool permission remains enforced on every callback. Inventory is
        # fixed for this session; changed inventory requires /claude new.
        session = await backend.open_session(segment, app.model_id, effort=effort, **bridge.sdk_options())
        return session, bridge, normalizer
    session = backend.session
    bridge, normalizer = backend._claude_tools, backend._claude_events
    bridge.cancelled.clear()
    await session.set_model(app.model_id)
    if effort != getattr(session, "effort", None):
        await session.set_effort(effort)
    normalizer.reset_turn()
    return session, bridge, normalizer


async def stream_turn(app):
    from litetui.claude_backend import settle_close

    backend = app.backend
    ledger = ledger_for(app)
    # Admission is what creates the durable entry (prepare_input at the submit
    # door). Reaching here without it means a caller bypassed that door, and an
    # unhandled AttributeError inside a Textual worker tells nobody anything.
    item = getattr(app, "_claude_active_input", None)
    if not isinstance(item, dict) or not (item.get("_claude_entry") or {}).get("id"):
        app._system("Claude turn started without admission; nothing was sent. Submit again, or /claude status.")
        return
    entry_id = item["_claude_entry"]["id"]
    segment = ledger.segment(item["_claude_segment"])
    # T911: WARN FIRST. Before a bubble, a turn_start or any byte to Claude, so a
    # cancel leaves nothing half-started and the entry stays `prepared`.
    if segment is not None and not await _cache_ok(app, backend, segment):
        if getattr(app, "_rpc", None):
            # A headless host confirms by sending again, which prepares a new
            # entry; closing this one keeps /claude continue from sending both.
            ledger.update_delivery(entry_id, "terminal", stop_reason="not_sent_cache_warning")
        else:
            app._system("Not sent: cancelled at the cache warning. Your message is kept; "
                        "/claude continue sends it when you are ready.")
        return
    started = time.monotonic()
    app._active_turn_started_at = started
    app._stop_requested = False
    app._stop_reason = None
    app._turn_stop_line_settled = False
    app._turn_abandoned = False
    app.tps = None
    app._rpc_emit({"type": "turn_start", "model": app.model_id, "provider": "claude"})
    widget = app._assistant_bubble()
    app._active_turn_widget = widget
    app._elapsed.start(widget.body, started_at=started)
    text = ""
    message_texts = {}
    tool_cards = {}
    activity_records = []
    thinking = None
    thinking_text = ""
    terminal = False
    submitted = False
    monitor = None
    bridge = getattr(backend, "_claude_tools", None)
    normalizer = getattr(backend, "_claude_events", None)
    reason = "error"
    failure = None
    try:
        for cleanup_failure in await settle_close(app):
            app._system(f"Claude cleanup: {cleanup_failure}")
        if getattr(app, "_claude_closing", None):
            raise RuntimeError("Previous Claude runtime is still closing; this input remains unsent.")
        if segment is None:
            raise RuntimeError("Claude session segment is missing; inspect the saved ledger before continuing")
        def require_current_segment():
            selected = ledger.selected
            if (app.backend is not backend or app.convo_id != item["_claude_conversation"]
                    or not selected or selected["id"] != segment["id"]):
                raise RuntimeError("Claude input is held for its original conversation/session; nothing was sent.")

        require_current_segment()
        effort = effort_for(app)
        session, bridge, normalizer = await session_for(app, backend, segment, effort)
        require_current_segment()  # Startup/model control awaited; ownership may have changed.
        ledger.update_delivery(entry_id, "submitted")
        submitted = True
        await session.query(entry_id, item["content"])
        clock = claude_cache.clock_for(app, segment["id"])
        clock.model, clock.effort = app.model_id, effort or "default"

        async def watch_stop():
            while not terminal:
                if app._stop_requested:
                    bridge.stop()
                    await session.interrupt()
                    return
                await asyncio.sleep(0.05)

        monitor = asyncio.create_task(watch_stop(), name="claude-turn-stop")
        async for message in session.events():
            if app.backend is not backend or app.convo_id != item["_claude_conversation"]:
                raise RuntimeError("Claude callback belongs to an inactive conversation")
            if session.session_id and segment.get("session_id") != session.session_id:
                # A different ID is a recovery boundary, not permission to
                # silently migrate pending deliveries to another session.
                segment = ledger.bind_session(segment["id"], session.session_id)
            for event in normalizer.push(message):
                if event.kind == "text_delta":
                    key = event.message_id or "current"
                    message_texts[key] = message_texts.get(key, "") + event.text
                    text = "\n\n".join(message_texts.values())
                    widget.body.content = Text(text + " |")
                    app._rpc_emit({"type": "text_delta", "text": event.text})
                    app._scroll_down()
                elif event.kind == "message":
                    key = event.message_id or "current"
                    if event.reconcile == "append":
                        message_texts[key] = message_texts.get(key, "") + event.text
                        if event.text:
                            app._rpc_emit({"type": "text_delta", "text": event.text})
                    else:
                        message_texts[key] = event.text
                        app._rpc_emit({"type": "native_text_snapshot", "provider": "claude", "message_id": key, "text": event.text})
                    text = "\n\n".join(message_texts.values())
                    widget.set_answer(text)
                elif event.kind in {"thinking_delta", "thinking"}:
                    from litetui.widgets import ThinkingBlock
                    # A "replace" snapshot means the deltas and the snapshot
                    # disagree — a delta was lost mid-trace — so the rendered
                    # text is wrong, not merely short. Appending the snapshot
                    # onto it showed the trace twice.
                    snapshot = event.kind == "thinking" and event.reconcile == "replace"
                    stale = snapshot and thinking_text and event.text != thinking_text
                    thinking_text = event.text if snapshot else thinking_text + event.text
                    app._rpc_emit({"type": "reasoning_delta", "text": event.text})
                    if stale and thinking is not None:
                        # ThinkingBlock only appends (widgets.py:454) and owns
                        # no setter, so the authoritative trace is rebuilt into
                        # a fresh block rather than concatenated onto a wrong
                        # prefix. Mounted before the old one is removed so the
                        # trace never blinks out of the transcript.
                        stale_block, thinking = thinking, ThinkingBlock()
                        widget.thinking = thinking
                        app._thinking_live = thinking
                        widget.mount(thinking, before=widget.body)
                        stale_block.remove()
                        thinking.append(thinking_text)
                    elif thinking is None and app.settings.show_thinking:
                        thinking = ThinkingBlock()
                        widget.thinking = thinking
                        app._thinking_live = thinking
                        widget.mount(thinking, before=widget.body)
                        # The whole trace, not this one event: a block created
                        # late (show_thinking turned on mid-turn, or a snapshot
                        # arriving with no deltas before it) still owns
                        # everything the turn has reasoned so far.
                        thinking.append(thinking_text)
                    elif thinking is not None and not snapshot:
                        thinking.append(event.text)
                    elif thinking is not None and event.text:
                        # An "append" snapshot carries only the tail the deltas
                        # never delivered; "" when the stream was complete.
                        thinking.append(event.text)
                elif event.kind in {"tool_use", "tool_result"}:
                    app._rpc_emit({"type": "native_activity", "provider": "claude", "kind": event.kind,
                                   "id": event.tool_id, "name": event.tool_name, "complete": event.complete,
                                   "input": event.tool_input, "result": event.tool_result})
                    # Display records are not executable tool_calls/tool messages.
                    if event.complete or event.kind == "tool_result":
                        activity_records.append({"kind": event.kind, "id": event.tool_id,
                            "name": event.tool_name, "input": event.tool_input,
                            "result": event.tool_result, "is_error": event.is_error,
                            "parent_tool_use_id": event.parent_tool_use_id})
                    from litetui.widgets import ToolMessage
                    card = tool_cards.get(event.tool_id)
                    if card is None:
                        card = ToolMessage("Claude · " + (event.tool_name or event.tool_id or "tool"))
                        tool_cards[event.tool_id] = card
                        app.query_one("#chat-log").mount(card)
                    if event.kind == "tool_use" and event.tool_input is not None:
                        card.set_args(json.dumps(event.tool_input))
                    elif event.kind == "tool_result":
                        card.set_result(str(event.tool_result), not event.is_error)

                elif event.kind == "compaction":
                    from litetui.claude_compact import native_compaction
                    native_compaction(app, event)
                elif event.kind == "usage":
                    from dataclasses import asdict
                    app._claude_usage = event.usage
                    clock = claude_cache.clock_for(app, segment["id"])
                    if clock.observe(event.usage) and clock.used_at is not None:
                        ledger.note_cache(segment["id"], clock.used_at, app.model_id)
                        claude_cache.ensure_ticker(app)
                        app._refresh_ctx_label()
                    if event.usage and event.usage.max_context_tokens:
                        # WINDOW FIRST. `ctx_used` is a reactive and
                        # `watch_ctx_used` reads `ctx_max` to render
                        # "used/total" — assigning it first fired the watcher
                        # against the previous window (None on the first
                        # observation, so the footer said "unknown"), and a
                        # reactive does not re-fire for an unchanged value.
                        # 🔴 The window rides on the RESULT frame
                        # (modelUsage.contextWindow); message frames carry
                        # none. Reading it only from message frames set None
                        # on every turn: the footer said "ctx 11,264 / ?"
                        # (live, 2026-09-24) and LiteTUI's autocompact, which
                        # needs a window, could never fire. Kept once known.
                        app.ctx_max = event.usage.max_context_tokens
                        app.ctx_loaded = True
                    if event.usage and event.usage.source == "message":
                        app.ctx_used = event.usage.context_tokens
                    if event.usage and (event.usage.source == "message" or event.usage.max_context_tokens):
                        app._refresh_ctx_label()
                    app._rpc_emit({"type": "native_usage", "provider": "claude", "data": asdict(event.usage) if event.usage else {}})
                elif event.kind in {"diagnostic", "notice", "rate_limit", "task", "session", "user_text"}:
                    app._rpc_emit({"type": "native_event", "provider": "claude", "kind": event.kind,
                                   "detail": event.detail, "data": event.data})
                    if event.kind == "diagnostic":
                        app._system("Claude diagnostic: " + event.detail)
                elif event.kind == "reset":
                    app._claude_usage = None
                    raise RuntimeError("Claude reset its native conversation; select /claude new before continuing. No queued input was migrated.")
                elif event.kind == "error":
                    failure = event.detail or "Claude reported an error"
                elif event.kind == "result":
                    terminal = True
                    reason = "error" if event.is_error or failure else ("cancelled" if app._stop_requested else "stop")
                    ledger.update_delivery(entry_id, "terminal", stop_reason=reason)
                    if event.is_error:
                        failure = event.detail or event.text or failure or "Claude turn failed"
        if not terminal:
            raise RuntimeError("Claude ended without a terminal result; delivery is uncertain")
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
        reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        if bridge:
            bridge.stop()
        if submitted and not terminal:
            try:
                ledger.update_delivery(entry_id, "uncertain", error=failure)
            except (OSError, RuntimeError) as persist_error:
                app._system(f"Claude delivery state could not be saved: {persist_error}; do not replay this input.")
        try:
            await backend.close()
        except Exception as cleanup_error:  # noqa: BLE001 - preserve primary failure
            app._system(f"Claude cleanup also failed: {cleanup_error}")
        # 🔴 CLEANING UP IS NOT THE SAME AS CONSUMING THE CANCELLATION. Closing
        # on the way out is why this clause catches BaseException at all, but
        # returning normally afterwards made the turn un-cancellable: the
        # Textual worker (app.py `_stream`, @work(exclusive=True, group="chat"))
        # reported SUCCESS, and the `finally` below still appended this turn's
        # assistant row and emitted its turn_end — while a replacement turn was
        # already running. An ordinary failure still becomes `failure` text;
        # anything that is not an Exception is a teardown and has to continue.
        # The `finally` runs first either way, so the partial answer and the
        # ledger update are still saved.
        if not isinstance(exc, Exception):
            raise
    finally:
        if monitor:
            if not monitor.done() and not terminal:
                monitor.cancel()
            outcomes = await asyncio.gather(monitor, return_exceptions=True)
            if isinstance(outcomes[0], Exception) and not failure:
                failure = str(outcomes[0])
                reason = "error"
        app._elapsed.stop_body()
        app._thinking_done()
        if text or activity_records or thinking_text:
            widget.set_answer(text)
            app._append({"role": "assistant", "content": text, "claude_native": {
                "segment_id": item["_claude_segment"], "session_id": (segment or {}).get("session_id"), "delivery_id": entry_id,
                "activities": activity_records, "message_ids": list(message_texts),
            }})
        if failure:
            app._system(f"Claude: {failure}")
            if not text:
                widget.body.content = Text(failure, style="bold red")
        widget.settled = True
        app._settle_turn_stop_line(widget, started_at=started, final_tps=None, stopped=reason != "stop")
        app._emit_turn_end(reason, None, error=failure)
        if reason == "stop" and hasattr(app, "call_after_refresh"):
            # LiteTUI's threshold decides for Claude too (its own autocompact
            # is off). Scheduled, like the host path: never from inside the
            # chat worker, which _compact would cancel.
            app.call_after_refresh(app._maybe_autocompact)


def command(app, argument):
    if app._chat_running():
        app._system("Finish or stop the current turn before changing Claude sessions.")
        return
    if app.backend.name != "claude":
        app._system("/claude requires /backend claude.")
        return
    verb = argument.strip().lower() or "status"
    if verb == "status":
        ledger = ledger_for(app)
        segment = ledger.selected
        pending = [{key: entry.get(key) for key in ("id", "state", "uncertain_from")} for entry in ledger.pending(segment["id"])] if segment else []
        app._system(f"Claude session: {segment and segment.get('session_id')}; segment: {segment and segment['id']}; pending: {pending}")
    elif verb == "resolve":
        ledger = ledger_for(app)
        segment = ledger.selected
        for entry in ledger.pending(segment["id"]) if segment else []:
            if entry["state"] == "uncertain":
                ledger.update_delivery(entry["id"], "terminal", stop_reason="user_resolved_without_replay")
        app._system("Uncertain deliveries explicitly resolved WITHOUT replay. Inspect native history before relying on their effects.")
    elif verb == "continue":
        ledger = ledger_for(app)
        segment = ledger.selected
        held = ledger.pending(segment["id"]) if segment else []
        if any(entry["state"] != "prepared" for entry in held):
            app._system("Resolve uncertain deliveries first; no automatic replay.")
            return
        queued = {item.get("_claude_entry", {}).get("id") for item in app._pending_input}
        for entry in held:
            if entry["id"] not in queued:
                app._pending_input.append({"content": entry["content"], "text": entry["content"],
                    "tool_profile": entry["profile"], "source": entry["source"],
                    "_claude_entry": entry, "_claude_segment": segment["id"], "_claude_conversation": app.convo_id})
        app._flush_pending_input()
    elif verb == "new":
        async def create():
            # The close must SETTLE before a new segment exists. Selecting one
            # anyway would hide a runtime that is still alive behind a UI that
            # says the session was replaced — and this is the command the
            # uncertain-delivery refusal sends people to, so it has to be
            # honest about failing. exit_on_error=False means an escaping
            # exception would be silent, so it is reported here instead.
            try:
                from litetui.claude_backend import settle_close
                for cleanup_failure in await settle_close(app):
                    app._system(f"Claude cleanup: {cleanup_failure}")
                if getattr(app, "_claude_closing", None):
                    raise RuntimeError("Previous Claude runtime is still closing")
                await app.backend.close()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                app._system(f"Claude session did not close cleanly: {exc}. "
                            "The previous runtime may still be alive; no new session was selected.")
                return
            ledger_for(app).select_segment(str(paths.ROOT), new=True)
            app._system("New Claude session selected. Old session and held inputs remain saved; no history imported.")
        app.run_worker(create(), group="claude-session", exclusive=True, exit_on_error=False)
    else:
        app._system("/claude status | resolve | continue | new - native slash commands and rollback are unsupported.")


def replay_activity(app, metadata):
    """Restore inert native activity cards; never dispatch a saved tool use."""
    from litetui.widgets import ToolMessage
    cards = {}
    for activity in metadata.get("activities", []):
        ident = activity.get("id")
        card = cards.get(ident)
        if card is None:
            card = ToolMessage("Claude - " + (activity.get("name") or ident or "tool"))
            cards[ident] = card
            app.query_one("#chat-log").mount(card)
        if activity.get("kind") == "tool_use":
            card.set_args(json.dumps(activity.get("input") or {}))
        else:
            card.set_result(str(activity.get("result")), not activity.get("is_error"))


async def approval_dialog(app, name, args, decision):
    """An owned view: cancellation closes this approval, never another dialog."""
    from functools import partial

    from litetui.side_panel import DialogController
    from litetui.tool_approval import ToolApprovalBody

    controller = DialogController(app, partial(ToolApprovalBody, name, args, decision),
                                  app.settings.dialog_style, app.settings.dialog_side)
    try:
        return await controller.open()
    finally:
        controller.resolve(None)
