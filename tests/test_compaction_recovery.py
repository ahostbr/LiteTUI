"""T0245: actual Claude window, threshold interruption, and one-shot recovery."""
import asyncio
from types import SimpleNamespace

import pytest
from test_claude_compact import app_for, summary_events
from test_claude_session import Client
from test_claude_turn import _compaction_watch, turn_app

from litetui import claude_compact
from litetui.claude_events import ClaudeEvent, ClaudeUsage
from litetui.claude_session import ClaudeSession
from litetui.claude_turn import ledger_for, stream_turn


@pytest.mark.asyncio
async def test_context_measurement_runs_on_sdk_owner():
    client = Client(None)

    async def context():
        assert asyncio.current_task() is client.owner
        return {"maxTokens": 200_000, "rawMaxTokens": 200_000}

    client.get_context_usage = context
    session = ClaudeSession(None, lambda _: client)
    try:
        assert (await session.get_context_usage())["maxTokens"] == 200_000
        assert not client.sent
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("window,used,interrupts", [(200_000, 160_000, 1), (1_000_000, 160_000, 0)])
async def test_actual_window_decides_when_a_native_turn_is_interrupted(tmp_path, window, used, interrupts):
    app = turn_app(tmp_path, messages=["usage", "result"])
    session = app.backend.session
    app.settings.autocompact_enabled = True
    app.settings.autocompact_at_percent = 80
    app.ctx_max, app.ctx_loaded = 1_000_000, True  # stale prior model window

    async def context():
        return {"maxTokens": window, "rawMaxTokens": window, "totalTokens": 1000}

    session.get_context_usage = context
    from litetui.app import LiteTUI
    app._autocompact_due = lambda: LiteTUI._autocompact_due(app)
    scheduled, commands = [], []
    app.call_after_refresh = scheduled.append
    app._handle_command = commands.append
    app._maybe_autocompact = lambda: commands.append("maybe")
    app._events = [[ClaudeEvent(kind="usage", usage=ClaudeUsage(source="message", context_tokens=used))],
                   [ClaudeEvent(kind="result", is_error=False)]]
    await stream_turn(app)
    assert app.ctx_max == window
    assert session.interrupted == interrupts
    assert bool(getattr(app, "_claude_compact_resume", None)) == bool(interrupts)
    for callback in scheduled:
        callback()
    assert commands == (["/compact"] if interrupts else ["maybe"])


@pytest.mark.asyncio
async def test_interrupted_turn_resumes_once_even_with_optional_wake_off(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    item = {"content": "original task"}
    app._claude_active_input = item
    old = ledger_for(app).selected
    app._claude_compact_resume = (app.backend, app.convo_id, old["id"], item, None)
    callbacks, sent = [], []
    app.call_after_refresh = callbacks.append
    app._submit_text = lambda text, **kw: sent.append((text, kw))
    await claude_compact.compact(app, auto=True)
    assert app.emitted[-1][0] == "compacted"
    assert len(callbacks) == 1
    callbacks[0]()
    callbacks[0]()
    assert len(sent) == 1 and "resume" in sent[0][0]
    assert sent[0][1]["source"] == "compact"


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["failed", "stop", "queued", "conversation", "backend", "segment", "new_turn"])
async def test_recovery_never_revives_failed_stopped_or_replaced_work(tmp_path, monkeypatch, block):
    events = ([[ClaudeEvent(kind="result", is_error=True, detail="failed")]]
              if block == "failed" else summary_events())
    app, _ = app_for(tmp_path, events, monkeypatch=monkeypatch)
    item = {"content": "original task"}
    app._claude_active_input = item
    old = ledger_for(app).selected
    app._claude_compact_resume = (app.backend, app.convo_id, old["id"], item, None)
    callbacks, sent = [], []
    app.call_after_refresh = callbacks.append
    app._submit_text = lambda *a, **kw: sent.append(a)
    await claude_compact.compact(app, auto=True)
    if block == "stop":
        app._turn_abandoned = True
    elif block == "queued":
        app._pending_input = [{"content": "new instruction"}]
    elif block == "conversation":
        app.convo_id = "other"
    elif block == "backend":
        app.backend = SimpleNamespace(name="other")
    elif block == "segment":
        ledger_for(app).select_segment(str(tmp_path), new=True)
    elif block == "new_turn":
        app._claude_active_input = {"content": "new task"}
    for callback in callbacks:
        callback()
    assert sent == []


@pytest.mark.asyncio
async def test_threshold_compaction_does_not_follow_user_stop(tmp_path):
    app = turn_app(tmp_path, messages=["usage", "result"])
    session = app.backend.session
    _compaction_watch(app, 80)
    app._events = [[ClaudeEvent(kind="usage", usage=ClaudeUsage(source="message", context_tokens=160_000))],
                   [ClaudeEvent(kind="result", is_error=False)]]
    original = session.interrupt

    async def interrupt():
        await original()
        app._stop_requested = True
        app._turn_abandoned = True

    session.interrupt = interrupt
    await stream_turn(app)
    assert not getattr(app, "_claude_compact_resume", None)


@pytest.mark.asyncio
async def test_context_api_failure_does_not_disconnect_owner():
    client = Client(None)

    async def context():
        raise RuntimeError("metadata unavailable")

    client.get_context_usage = context
    session = ClaudeSession(None, lambda _: client)
    try:
        with pytest.raises(RuntimeError, match="context usage unavailable"):
            await session.get_context_usage()
        assert session.lifecycle.failure is None
        await session.query("one", "still usable")
        assert client.sent == ["still usable"]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("measurement", [None, {}, {"maxTokens": 0}, {"maxTokens": True}])
async def test_unknown_window_refuses_before_query_and_discards_stale_window(tmp_path, measurement):
    app = turn_app(tmp_path)
    session = app.backend.session
    app.ctx_max, app.ctx_loaded = 1_000_000, True

    async def context():
        if measurement is None:
            raise RuntimeError("context usage unavailable")
        return measurement

    session.get_context_usage = context
    await stream_turn(app)
    assert not session.queried and app.ctx_max is None and not app.ctx_loaded
    assert app.backend.session is session
    assert not getattr(app, "_claude_compact_resume", None)
    assert ledger_for(app).pending(app._claude_active_input["_claude_segment"])[0]["state"] == "prepared"


@pytest.mark.asyncio
async def test_context_measurement_await_rechecks_segment_before_sending(tmp_path):
    app = turn_app(tmp_path)
    session = app.backend.session

    async def context():
        ledger_for(app).select_segment(str(tmp_path), new=True)
        return {"maxTokens": 200_000, "totalTokens": 1000}

    session.get_context_usage = context
    await stream_turn(app)
    assert not session.queried and not getattr(app, "_claude_compact_resume", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("used,successful", [(160_000, True), (160_000, False), (210_000, False)])
async def test_preflight_holds_original_unsent_prompt_through_compaction(tmp_path, monkeypatch, used, successful):
    app, _ = app_for(tmp_path, summary_events() if successful else [[
        ClaudeEvent(kind="result", is_error=True, detail="Prompt is too long")]], monkeypatch=monkeypatch)
    old = ledger_for(app).selected
    entry = ledger_for(app).prepare(old["id"], "unsent original task", "strict", "typed")
    item = {"content": entry["content"], "_claude_entry": entry, "_claude_segment": old["id"],
            "_claude_conversation": app.convo_id, "tool_profile": "strict", "source": "typed"}
    app._claude_active_input = item
    app._claude_compact_resume = (app.backend, app.convo_id, old["id"], item, item["content"])
    app.ctx_used = used
    callbacks, sent = [], []
    app._pending_input = []
    app._flush_pending_input = lambda: sent.extend(app._pending_input)
    app.call_after_refresh = callbacks.append
    await claude_compact.compact(app, auto=True)
    for callback in callbacks:
        callback()
    if successful:
        assert len(sent) == 1 and sent[0]["content"] == "unsent original task"
        fresh = ledger_for(app).pending(ledger_for(app).selected["id"])[0]
        assert (fresh["state"], fresh["profile"], fresh["source"]) == ("prepared", "strict", "typed")
        assert not ledger_for(app).pending(old["id"])
    else:
        assert not sent and ledger_for(app).selected["id"] == old["id"]
        assert ledger_for(app).pending(old["id"])[0]["state"] == "prepared"
        assert not app._claude_compact_resume


@pytest.mark.asyncio
async def test_already_due_preflight_does_not_query_original_input(tmp_path):
    app = turn_app(tmp_path)
    session = app.backend.session

    async def context():
        return {"maxTokens": 200_000, "totalTokens": 160_000}

    session.get_context_usage = context
    scheduled, commands = _compaction_watch(app, 80)
    await stream_turn(app)
    assert not session.queried
    assert app._claude_compact_resume[4] == "hello"
    assert ledger_for(app).pending(app._claude_active_input["_claude_segment"])[0]["state"] == "prepared"
    for callback in scheduled:
        callback()
    assert commands == ["/compact"]


@pytest.mark.asyncio
async def test_correlated_rpc_preflight_is_held_without_transfer(tmp_path):
    app = turn_app(tmp_path)
    session = app.backend.session
    app._claude_active_input["_claude_entry"]["operation_id"] = "rpc-original"

    async def context():
        return {"maxTokens": 200_000, "totalTokens": 160_000}

    session.get_context_usage = context
    scheduled, commands = _compaction_watch(app, 80)
    await stream_turn(app)
    assert not session.queried and not getattr(app, "_claude_compact_resume", None)
    assert not scheduled and not commands
    assert any("RPC operation" in n for n in app.notices)
    assert ledger_for(app).pending(app._claude_active_input["_claude_segment"])[0]["state"] == "prepared"


@pytest.mark.asyncio
async def test_interrupted_turn_without_terminal_stays_uncertain_and_never_resumes(tmp_path):
    app = turn_app(tmp_path, messages=["usage"])
    _compaction_watch(app, 80)
    app._events = [[ClaudeEvent(kind="usage", usage=ClaudeUsage(source="message", context_tokens=160_000))]]
    await stream_turn(app)
    assert not getattr(app, "_claude_compact_resume", None)
    pending = ledger_for(app).pending(app._claude_active_input["_claude_segment"])
    assert pending[0]["state"] == "uncertain"


@pytest.mark.asyncio
async def test_compact_refuses_uncertain_delivery_and_consumes_recovery(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    from litetui.claude_turn import prepare_input
    item = {"content": "task", **prepare_input(app, "task", "strict", "typed")}
    app._claude_active_input = item
    ledger = ledger_for(app)
    ledger.update_delivery(item["_claude_entry"]["id"], "submitted")
    ledger.update_delivery(item["_claude_entry"]["id"], "uncertain")
    app._claude_compact_resume = (app.backend, app.convo_id, ledger.selected["id"], item, None)
    await claude_compact.compact(app, auto=True)
    assert app.emitted[-1][0] == "failed" and not app._claude_compact_resume
    assert not app.backend.session.queried


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["stop", "new_input"])
async def test_unsent_prompt_stays_recoverable_if_continuation_is_cancelled(tmp_path, monkeypatch, block):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    ledger = ledger_for(app)
    old = ledger.selected
    entry = ledger.prepare(old["id"], "original unsent", "strict", "queued")
    item = {"content": entry["content"], "_claude_entry": entry, "_claude_segment": old["id"],
            "_claude_conversation": app.convo_id, "tool_profile": "strict", "source": "queued"}
    app._claude_active_input = item
    app._claude_compact_resume = (app.backend, app.convo_id, old["id"], item, item["content"])
    callbacks, flushed = [], []
    app._pending_input = []
    app._flush_pending_input = lambda: flushed.append(True)
    app.call_after_refresh = callbacks.append
    await claude_compact.compact(app, auto=True)
    if block == "stop":
        app._turn_abandoned = True
    else:
        app._claude_active_input = {"content": "new task"}
    callbacks[0]()
    assert not flushed
    fresh = ledger.pending(ledger.selected["id"])[0]
    assert (fresh["content"], fresh["state"], fresh["profile"], fresh["source"]) == (
        "original unsent", "prepared", "strict", "queued")
    assert not ledger.pending(old["id"])


@pytest.mark.asyncio
async def test_owner_change_during_compaction_does_not_seed_or_wake_wrong_owner(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    old = ledger_for(app).selected
    session = app.backend.session

    async def events():
        for batch in summary_events():
            yield batch
        app.convo_id = "different"

    session.events = events
    await claude_compact.compact(app, auto=True)
    assert app.emitted[-1][0] == "failed"
    assert ledger_for(app).selected["id"] == old["id"]
