"""Five merged-preview contracts: T0250 admission + T0245 recovery + T0253 identity."""
import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest
from test_card_summary import _Chunk, _probe, _Stream
from test_claude_backend import _identity_app
from test_claude_compact import Backend, Session, app_for, summary_events
from test_claude_turn import _compaction_watch, turn_app
from test_pause import _agentic_app

from litetui import app as app_mod
from litetui import claude_backend, claude_compact, hook_host
from litetui.app import WAKE_AFTER_COMPACT, LiteTUI
from litetui.claude_events import ClaudeEvent, ClaudeUsage
from litetui.claude_persistence import ClaudeLedger
from litetui.claude_turn import (
    command,
    hold_input,
    ledger_for,
    prepare_input,
    session_for,
    stream_turn,
)


@pytest.mark.asyncio
async def test_startup_resolution_then_threshold_keeps_new_input_unsent(tmp_path, monkeypatch):
    monkeypatch.setattr('litetui.claude_turn.launch_workspace', lambda _: str(tmp_path))
    app = turn_app(tmp_path)
    ledger = ledger_for(app)
    old = app._claude_active_input['_claude_entry']
    ledger.update_delivery(old['id'], 'submitted')
    ledger.update_delivery(old['id'], 'uncertain')
    before = ledger.file.read_bytes()
    hook_host.initialize(app)
    app._hook_workspace = tmp_path
    app._pending_input = []
    started = []
    app._stream = lambda: started.append(app._claude_active_input)
    app._flush_pending_input = lambda: LiteTUI._flush_pending_input(app)
    hook_host.start_prompt(app, {'content': 'NEW startup instruction', 'source': 'harness'})
    command(app, 'continue')
    assert not started and not app.appended and ledger.file.read_bytes() == before
    command(app, 'resolve')
    command(app, 'continue')
    assert len(started) == 1 and started[0]['content'] == 'NEW startup instruction'
    new_id = started[0]['_claude_entry']['id']
    assert new_id != old['id'] and ledger.pending(ledger.selected['id'])[0]['state'] == 'prepared'

    async def context():
        return {'maxTokens': 200_000, 'totalTokens': 160_000}

    session = app.backend.session
    session.get_context_usage = context
    scheduled, commands = _compaction_watch(app, 80)
    await stream_turn(app)
    assert session.queried == [] and app._claude_compact_resume[4] == 'NEW startup instruction'
    assert ledger.pending(ledger.selected['id'])[0]['id'] == new_id
    assert ledger.pending(ledger.selected['id'])[0]['state'] == 'prepared'
    for callback in scheduled:
        callback()
    assert commands == ['/compact']
    # A failed real maintenance attempt cannot turn the prepared prompt into a send.
    maintenance, _ = app_for(tmp_path, [[ClaudeEvent(kind='result', is_error=True)]], monkeypatch=monkeypatch)
    maintenance._claude_active_input = app._claude_active_input
    maintenance._claude_compact_resume = (maintenance.backend, maintenance.convo_id,
                                          ledger.selected['id'], app._claude_active_input,
                                          'NEW startup instruction')
    summary_session = maintenance.backend.session
    await claude_compact.compact(maintenance, auto=True)
    assert summary_session.queried == [claude_compact.request('')]
    reopened = ClaudeLedger(tmp_path)
    assert maintenance.emitted[-1][0] == 'failed'
    assert reopened.pending(reopened.selected['id'])[0]['state'] == 'prepared'
    assert [e['content'] for e in reopened.pending(reopened.selected['id'])] == ['NEW startup instruction']
    assert session.queried == []


@pytest.mark.asyncio
async def test_delivered_threshold_without_terminal_has_no_compaction_or_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr('litetui.claude_turn.launch_workspace', lambda _: str(tmp_path))
    app = turn_app(tmp_path, messages=['usage'])
    session = app.backend.session
    session.session_id = 'native-old'  # Same native owner used by the compact UI helper.
    scheduled, commands = _compaction_watch(app, 80)
    app._events = [[ClaudeEvent(kind='usage', usage=ClaudeUsage(source='message', context_tokens=160_000))]]
    await stream_turn(app)
    entry_id = app._claude_active_input['_claude_entry']['id']
    assert session.queried == [entry_id] and session.interrupted == 1
    assert ledger_for(app).pending(ledger_for(app).selected['id'])[0]['state'] == 'uncertain'
    assert not getattr(app, '_claude_compact_resume', None)
    for callback in scheduled:
        callback()
    assert commands == ['maybe'], 'generic budget check is not a synthetic recovery'
    evidence = ledger_for(app).file.read_bytes()
    maintenance, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    before_segment = ledger_for(maintenance).selected
    before_conversation = copy.deepcopy(maintenance.conversation)
    native = maintenance.backend.session
    await claude_compact.compact(maintenance, auto=True)
    assert native.queried == [] and maintenance.emitted[-1][0] == 'failed'
    maintenance._pending_input = []
    maintenance._flush_pending_input = lambda: pytest.fail('uncertain delivery must not continue')
    command(maintenance, 'continue')
    app._pending_input = []
    app._flush_pending_input = lambda: pytest.fail('original owner must not continue uncertain input')
    command(app, 'continue')
    assert not getattr(maintenance, '_claude_compact_resume', None)
    assert ledger_for(maintenance).selected == before_segment
    assert maintenance.conversation == before_conversation
    assert ledger_for(app).file.read_bytes() == evidence
    assert ClaudeLedger(tmp_path).pending(ledger_for(app).selected['id'])[0]['state'] == 'uncertain'


@pytest.mark.asyncio
async def test_newer_prompt_during_summary_retains_owner_fifo_and_recovers_once(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    monkeypatch.setattr('litetui.claude_turn.launch_workspace', lambda _: str(tmp_path))
    hook_host.initialize(app)
    app._hook_workspace = tmp_path
    ledger = ledger_for(app)
    old = ledger.selected
    original = {'content': 'original unsent', **prepare_input(app, 'original unsent', 'strict', 'typed')}
    app._claude_active_input = original
    app._claude_compact_resume = (app.backend, app.convo_id, old['id'], original, original['content'])
    busy = True
    app._chat_running = lambda: busy
    app._pending_input = []
    app.pending_image = None
    app.chosen_tool_profile = 'strict'
    app.tools_enabled = True
    app._mcp_maintenance = False
    app.settings.enter_interrupts = False
    app._split_image_path = lambda text: (None, text)
    app._looks_like_image_path = lambda text: False
    app._oversize_refusal = lambda content: None
    app._user_bubble = lambda *a, **k: SimpleNamespace(border_title='queued')
    app.notify = lambda *a, **k: None
    app._append = app.conversation.append
    app._flush_pending_input = lambda: LiteTUI._flush_pending_input(app)
    callbacks, delivered = [], []
    app.call_after_refresh = callbacks.append

    def native_execution():
        item = app._claude_active_input
        delivered.append((item['content'], item['_claude_segment'], item['_claude_entry']['id']))
        ledger.update_delivery(item['_claude_entry']['id'], 'submitted')
        ledger.update_delivery(item['_claude_entry']['id'], 'terminal', stop_reason='stop')

    app._stream = native_execution  # Only provider execution, not admission, is replaced.
    waiting, release = asyncio.Event(), asyncio.Event()

    async def events():
        for index, batch in enumerate(summary_events()):
            if index == 1:
                waiting.set()
                await release.wait()
            yield batch

    native = app.backend.session
    native.events = events
    task = asyncio.create_task(claude_compact.compact(app, auto=True))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        for text in ('newer first', 'newer second'):
            LiteTUI._submit_text(app, text, False, source='typed')
        queued_ids = [item['_claude_entry']['id'] for item in app._pending_input]
        assert [item['content'] for item in app._pending_input] == ['newer first', 'newer second']
        assert all(item['_claude_segment'] == old['id'] for item in app._pending_input)
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    busy = False
    assert ledger.selected['id'] == old['id'] and app.backend.session is native
    assert native.queried == [claude_compact.request('')]
    assert any('held' in notice.lower() or 'queued' in notice.lower() for notice in app.notices)
    assert not callbacks and app.emitted[-1][0] == 'failed'
    for _ in range(4):
        app._flush_pending_input()
    command(app, 'continue')
    for _ in range(4):
        command(app, 'continue')
        app._flush_pending_input()
    assert [row[0] for row in delivered] == ['newer first', 'newer second', 'original unsent']
    assert [row[2] for row in delivered[:2]] == queued_ids
    assert all(row[1] == old['id'] for row in delivered)
    assert len({row[2] for row in delivered}) == 3
    assert not app._pending_input and not ledger.pending(old['id'])


@pytest.mark.asyncio
async def test_local_tool_threshold_summary_wake_never_exposes_held_claude_input(tmp_path, monkeypatch):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: None)
    app.settings.autocompact_enabled = True
    app.settings.wake_after_compact = False
    app.settings.compact_keep_recent = 0
    app._kick_card_summary = lambda *a: None
    app._fetch_ctx_window = lambda: None
    calls, requests, wakes = [], [], []
    app._autocompact_due = lambda: 80 if len(calls) == 1 else None
    real_wake = app._wake_after_compact

    def observe_wake():
        wakes.append(True)
        real_wake()

    app._wake_after_compact = observe_wake

    async def ready(**kwargs):
        return True

    app._ensure_chat_ready = ready

    async def create(**kwargs):
        calls.append(kwargs.get('purpose', 'turn'))
        requests.append(copy.deepcopy(kwargs['messages']))
        if len(calls) == 1:
            return _Stream([_Chunk(content='working')] + _probe(1))
        if len(calls) == 2:
            return _Stream([_Chunk(content='Summary: completed probe; continue local task.')])
        return _Stream([_Chunk(content='local task completed')])

    monkeypatch.setattr(app_mod.model_transport, 'for_app', lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(100, 40)) as pilot:
        app._materialise_convo()
        convo = app.convo_id
        original = prepare_input(app, 'previous Claude send', 'strict', 'harness')
        ledger = ledger_for(app)
        ledger.update_delivery(original['_claude_entry']['id'], 'submitted')
        ledger.update_delivery(original['_claude_entry']['id'], 'uncertain')
        hold_input(app, {'content': 'PRIVATE CLAUDE HELD', 'source': 'harness'}, 'uncertain')
        held = app._pending_input[0]
        later = {'content': 'LATER LOCAL QUEUED', 'source': 'typed'}
        app._pending_input.append(later)
        evidence = ledger.file.read_bytes()
        app.ctx_max, app.ctx_used, app.ctx_loaded = 200_000, 160_000, True
        app._append({'role': 'user', 'content': 'run the local probe'})
        app._stream()
        for _ in range(120):
            await pilot.pause(.05)
            if wakes and not app._chat_running():
                break
        assert wakes == [True], 'real maintenance continuation reached the guarded wake'
        assert calls == ['turn', 'compaction']
        assert any(m['role'] == 'tool' for m in requests[1])
        assert any('Summary: completed probe; continue local task.' in str(m.get('content', ''))
                   for m in app.conversation), 'successful summary was installed'
        assert not any(m.get('content') == WAKE_AFTER_COMPACT for m in app.conversation)
        for _ in range(3):
            real_wake()
            app._flush_pending_input()
        await pilot.pause()
        assert calls == ['turn', 'compaction'], 'wrong-owner FIFO head suppresses wake and flush'
        assert app._interrupted_compact_resume is None
        assert app._pending_input == [held, later] and app.convo_id == convo
        assert 'PRIVATE CLAUDE HELD' not in str(requests) + str(app.conversation)
        assert 'LATER LOCAL QUEUED' not in str(requests) + str(app.conversation)
        assert ledger.file.read_bytes() == evidence
        assert ledger.selected['id'] == original['_claude_segment']
        assert app.backend.name != 'claude'


@pytest.mark.asyncio
async def test_summary_new_segment_reopen_refreshes_one_identity_preserving_prose(monkeypatch):
    app = _identity_app(monkeypatch)
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    app.seat.registered = True
    app._seat_started = True
    app.model_id = 'sonnet'
    app.settings.wake_after_compact = False
    summary = 'Summary seed: preserve the exact parser plan and user prose.'
    ledger = ledger_for(app)
    old = ledger.select_segment(str(app.convo_dir))
    ledger.bind_session(old['id'], 'native-old')
    app.backend = Backend(Session(summary_events(summary)), old['id'])
    async with app.run_test(size=(100, 40)):
        app.conversation[:] = [{'role': 'user', 'content': 'User prose retained by summary'},
                               {'role': 'assistant', 'content': 'ready'}]
        app.ctx_used = 160_000
        await claude_compact.compact(app, auto=True)
        fresh = ledger.selected
        assert fresh['id'] != old['id'] and fresh['seed'] == summary
        assert app.conversation[0]['content'].startswith('[Summary of earlier conversation')
        assert summary in app.conversation[0]['content']
    stale = ('You are registered in the LiteHarness fleet as OldSeat '
             '(id 11111111-1111-1111-1111-111111111111, tier worker). ')
    prose = f'USER PROSE\nQuoted example: "{stale}"\nDOCUMENTED TOOL INVENTORY\n'
    saved = prose + stale + app._fleet_identity_sentence() + '\nTRUSTED SEED\n' + summary
    ledger.fix_system_prompt(fresh['id'], saved)
    options = []
    sdk = SimpleNamespace(ClaudeAgentOptions=lambda **kw: options.append(kw) or kw,
                          HookMatcher=lambda **kw: kw, tool=lambda name, *a: lambda fn: name,
                          create_sdk_mcp_server=lambda **kw: kw)
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', sdk)
    monkeypatch.setattr(claude_backend, 'sdk_module', lambda: sdk)

    class ExternalSession:
        def __init__(self, opts):
            self.session_id = 'native-new'
            self.lifecycle = SimpleNamespace(cleanup_errors=[])

        async def start(self):
            return {'session_id': self.session_id}

        async def close(self):
            pass

    monkeypatch.setattr(claude_backend, 'ClaudeSession', ExternalSession)
    backend = claude_backend.ClaudeBackend(app.settings)
    backend.models = {'sonnet': {'value': 'sonnet'}}
    app.backend = backend
    del app._claude_ledger  # Reopen from the actual persisted fresh segment.
    monkeypatch.setattr(app, '_system', lambda *a, **kw: None)  # Pilot has closed.
    for reopen in range(2):
        if reopen:
            # A subsequent harness registration must replace, not append, its identity.
            monkeypatch.setattr(app.seat, 'agent_id', '22222222-2222-2222-2222-222222222222')
        expected = prose + app._fleet_identity_sentence() + '\nTRUSTED SEED\n' + summary
        native, _, _ = await session_for(app, backend, ledger_for(app).selected, None)
        # Claude assigns native identity on the first turn, not session-open.
        ledger_for(app).bind_session(fresh['id'], native.session_id)
        assert options[-1]['system_prompt'] == expected
        assert expected.count(app._fleet_identity_sentence()) == 1
        assert ClaudeLedger(app.convo_dir).selected['seed'] == summary
        assert ClaudeLedger(app.convo_dir).selected['system_prompt'] == expected
        await backend.close()
    assert options[-1]['resume'] == 'native-new'
    assert options[0]['tools'] == options[1]['tools']
