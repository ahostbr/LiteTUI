"""LiteTUI's own compaction, run on the Claude backend.

RYAN, 2026-09-24: "I just don't think it's possible to keep [LiteTUI]-wise
compaction, right? Like I would rather keep it if it's possible" — and — "I
don't want to change any of the other backend compaction display to fit
Claude. I want to only change Claude."

So Claude adapts to the host's compaction, not the other way round. The shape
is the host's (`LiteTUI._compact`): LiteTUI's COMPACT_PROMPT, the same
CompactionCard, the same `_truncate` record, the same `compaction` rpc event,
the same meter reset. What differs is only what Claude needs:

  * the summary is written by Claude INSIDE its live session, because that is
    where the conversation is (LiteTUI's display rows are not the context);
    the cache is still warm there, so the summary turn is the cheap one;
  * then the session is CLOSED and a fresh segment is selected, seeded with
    the summary (claude_turn.seeded): its first message carries it, so the
    system prompt stays byte-identical and model, effort and identity carry
    over through the ordinary open path.

Claude's own autocompact is off for LiteTUI sessions (claude_backend
AUTOCOMPACT_ENV, measured). If Claude compacts anyway, `native_compaction`
renders its compact_boundary through the same card.
"""
from __future__ import annotations

import json
import time
import uuid

from rich.text import Text

from litetui import claude_cache, paths
from litetui.claude_backend import COMPACT_MARKER
from litetui.claude_turn import effort_for, ledger_for, session_for

#: Said in the cache warning for a user-triggered /compact.
COMPACT_COLD = ("Compacting ends this Claude session: Claude writes a summary, and your next message "
                "starts a fresh session carrying it. A fresh session has an empty cache, so that "
                "message is read at full input price.")


def request(extra: str = "") -> str:
    """LiteTUI's summary instructions, as the marked request Claude's system
    prompt declares genuine (claude_backend.APPEND).

    STEP 2 of COMPACT_PROMPT, verbatim: what a summary must cover is LiteTUI's
    rule for every backend. STEP 1 (write memories/, memory.md, soul.md) is
    left out: those are LiteTUI's store files, which Claude cannot see, and
    asking for them is what made the whole message read as an injection.
    """
    from litetui.app import COMPACT_PROMPT

    _, sep, step2 = COMPACT_PROMPT.partition("STEP 2")
    body = (sep + step2) if sep else COMPACT_PROMPT
    return (f"{COMPACT_MARKER} LiteTUI is compacting this conversation now. Use no tools. Your reply "
            f"becomes the notes the next session starts from.\n\n{body.strip()}"
            + (f"\n\n{extra}" if extra else ""))


async def compact(app, extra: str = "", *, auto: bool = False, handoff: str | None = None) -> None:
    """Summarise with Claude, then continue in a fresh session seeded with it."""
    prompt = request(extra)
    from litetui.claude_backend import settle_close
    from litetui.widgets import CompactionCard, ToolMessage

    backend = app.backend
    ledger = ledger_for(app)
    segment = ledger.selected
    if segment is None or (not segment.get("session_id") and backend.session is None):
        app._system("Nothing to compact yet — have a conversation first.")
        app._emit_compaction("too_short")
        return
    if any(e["state"] != "prepared" for e in ledger.pending(segment["id"])):
        app._system("Claude delivery is uncertain; /claude status first. Nothing was compacted.")
        app._emit_compaction("failed", tokens_before=app.ctx_used,
                             tokens_before_exact=app.ctx_used is not None)
        return
    if not auto:
        # The same gate as a model or effort switch (claude_cache.confirm_cold).
        clock = claude_cache.clock_for(app, segment["id"])
        live = backend.session is not None and backend.segment_id == segment["id"]
        cold = claude_cache.cold_reason(clock, live=live, resuming=not live, model=app.model_id,
                                        used_at=segment.get("cache_used_at"), effort=effort_for(app))
        reason = COMPACT_COLD + (f"\n\nAlso: {cold[1]}" if cold else "")
        if not await claude_cache.confirm_cold(app, ("compact", reason)):
            if not getattr(app, "_rpc", None):
                app._system("Compaction cancelled; the Claude session is unchanged.")
            return

    app._stop_requested = False
    app._stop_reason = None
    started = time.perf_counter()
    tokens_before = app.ctx_used
    before_count = len(app.conversation)
    before_chars = app._msg_chars(app.conversation)
    old_session = segment.get("session_id") or getattr(backend.session, "session_id", None) or "live"
    card = CompactionCard(
        plan=(f"Claude session {str(old_session)[:8]} → summary · the next message "
              "starts a fresh session carrying it"),
        prompt_text=prompt, auto=auto,
    )
    await app.query_one("#chat-log").mount(card)
    app._compact_card = card
    app._elapsed.ensure_running()
    app._scroll_down()

    summary = ""
    failure = None
    tools: dict[str | None, ToolMessage] = {}
    writes = []
    try:
        for cleanup_failure in await settle_close(app):
            app._system(f"Claude cleanup: {cleanup_failure}")
        card.set_status(f"asking {app.model_id} for the summary")
        session, bridge, normalizer = await session_for(app, backend, segment, effort_for(app))
        if session.session_id and not segment.get("session_id"):
            segment = ledger.bind_session(segment["id"], session.session_id)
        await session.query(f"compact-{uuid.uuid4().hex}", prompt)
        texts = {}
        async for message in session.events():
            if app._stop_requested:
                bridge.stop()
                await session.interrupt()
            for event in normalizer.push(message):
                if event.kind in {"text_delta", "message"}:
                    card.thinking_done()
                    key = event.message_id or "current"
                    if event.kind == "message" and event.reconcile != "append":
                        texts[key] = event.text
                    else:
                        texts[key] = texts.get(key, "") + event.text
                    # The summary is the LAST message; earlier ones narrate tool use.
                    summary = list(texts.values())[-1]
                    card.body.content = Text(summary + " ▌")
                    card.set_status(f"summary {len(summary):,} chars")
                    app._scroll_down()
                elif event.kind == "thinking_delta":
                    card.think(event.text)
                elif event.kind in {"tool_use", "tool_result"}:
                    card.thinking_done()
                    msg = tools.get(event.tool_id)
                    if msg is None:
                        msg = tools[event.tool_id] = ToolMessage("Claude · " + (event.tool_name or "tool"))
                        card.add_tool(msg)
                    if event.kind == "tool_use" and event.tool_input is not None:
                        msg.set_args(json.dumps(event.tool_input))
                        path = (event.tool_input or {}).get("file_path") or (event.tool_input or {}).get("path")
                        if event.complete and path and "write" in (event.tool_name or "").lower():
                            writes.append(str(path))
                    elif event.kind == "tool_result":
                        msg.set_result(str(event.tool_result), not event.is_error)
                elif event.kind == "usage" and event.usage and event.usage.source == "message":
                    clock = claude_cache.clock_for(app, segment["id"])
                    if clock.observe(event.usage) and clock.used_at is not None:
                        ledger.note_cache(segment["id"], clock.used_at, app.model_id)
                elif event.kind == "error":
                    failure = event.detail or "Claude reported an error"
                elif event.kind == "result" and event.is_error:
                    failure = event.detail or event.text or failure or "Claude compaction turn failed"
                elif event.kind == "result" and event.text:
                    summary = event.text   # the turn's final answer: authoritative
        card.thinking_done()
        if app._stop_requested:
            failure = "stopped"
    except Exception as exc:  # noqa: BLE001 - a failed compaction changes nothing
        failure = str(exc) or type(exc).__name__
    summary = summary.strip()
    card.record_round({"round": 1, "model_s": time.perf_counter() - started,
                       "first_chunk_s": None, "tools_s": 0.0, "usage": None})
    if failure or not summary:
        app._autocompact_failed_at = app.ctx_used
        card.fail("failed — conversation unchanged")
        app._system(f"Compact failed — the Claude session is unchanged. {failure or 'No summary produced.'}")
        app._emit_compaction("failed", tokens_before=tokens_before,
                             tokens_before_exact=tokens_before is not None)
        return

    # The summary exists: end this session and seed the next one with it.
    seed = summary + (f"\n\n[Agent's pre-compaction handoff]\n{handoff}" if handoff else "")
    try:
        await backend.close()
    except Exception as exc:  # noqa: BLE001 - reported; the new segment still owns the summary
        app._system(f"Claude cleanup after compaction: {exc}")
    ledger.select_segment(segment.get("workspace") or str(paths.ROOT), new=True, seed=seed)

    pair = [
        {"role": "user", "content": "[Summary of earlier conversation, which has been compacted away]\n\n" + seed},
        {"role": "assistant", "content": "Understood — I have that context."},
    ]
    system = app.conversation[:1] if app.conversation and app.conversation[0].get("role") == "system" else []
    rebuilt = system + pair
    tokens_after = (app._msg_chars(rebuilt) + 3) // 4
    app._truncate(len(app.conversation), pair, "compact", measurements={
        "model": app.model_id, "auto": auto, "backend": "claude",
        "trigger": "agent" if handoff is not None else ("automatic" if auto else "manual"),
        "before_chars": before_chars, "after_chars": app._msg_chars(rebuilt),
        "before_count": before_count, "after_count": len(rebuilt),
        "tokens_before": tokens_before, "tokens_after": None,
        "tokens_after_estimate": tokens_after,
        "token_estimate_method": "chars/4 of the seeded summary",
        "claude_session_before": old_session,
        "duration_s": time.perf_counter() - started, "summary_chars": len(summary),
        "store_files": writes,
    })
    app.conversation = rebuilt
    app._emit_compaction("compacted", tokens_before=tokens_before, tokens_after=tokens_after,
                         tokens_before_exact=tokens_before is not None, tokens_after_exact=False,
                         messages_dropped=before_count - len(system),
                         messages_summarised=before_count - len(system), kept_recent=0)
    app.ctx_used = tokens_after
    app._autocompact_failed_at = None
    try:
        card.body.set_markdown(summary)
    except Exception:  # noqa: BLE001 - same fallback as the host card
        card.body.content = Text(summary)
    card.finish(f"done · Claude session {str(old_session)[:8]} closed · next message starts a "
                f"fresh session with a {len(summary):,}-char summary"
                + (f" · persisted: {', '.join(dict.fromkeys(writes))}" if writes else ""))
    app._system(f"Compacted: Claude session {old_session} closed. Your next message starts a fresh "
                f"Claude session (same model and effort) carrying the summary. Scrollback is untouched.")
    if app.settings.clear_screen_after_compact:
        app._clear_screen(note="Compacted into a summary; the next message starts a fresh Claude session "
                               "carrying it. The full transcript is still on disk in this conversation's file.")
    if app.settings.wake_after_compact or handoff is not None:
        # Through the submit door, not _wake_after_compact: a Claude turn must
        # be ADMITTED (claude_turn.prepare_input) before it can be sent.
        from litetui.app import WAKE_AFTER_COMPACT
        app.call_after_refresh(lambda: app._chat_running() or app._submit_text(
            WAKE_AFTER_COMPACT, alt_chord=False, source="compact"))


def native_compaction(app, event) -> None:
    """Claude compacted on its own (a hard limit, autocompact off): show it on
    the same card, from its compact_boundary. Nothing is summarised by LiteTUI."""
    from litetui.widgets import CompactionCard

    meta = (event.data or {}).get("compact_metadata") or {}
    pre = meta.get("pre_tokens")
    card = CompactionCard(plan="Claude compacted its own context (its limit, not LiteTUI's threshold)",
                          prompt_text="(Claude's own compaction prompt)", auto=meta.get("trigger") != "manual")
    app.query_one("#chat-log").mount(card)
    card.finish("done · by Claude" + (f" · {pre:,} tokens before" if isinstance(pre, int) else ""))
    app._system("Claude compacted its native context; display history is unchanged.")
    app._emit_compaction("compacted", tokens_before=pre, tokens_before_exact=isinstance(pre, int))
