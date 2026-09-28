"""Responsive compact pane: measured geometry, expansion, recap and board ownership."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui.app import LiteTUI
from litetui.claude_turn import replay_activity
from litetui.codex_trace import replay as replay_codex
from litetui.compact_tools import summary
from litetui.now_card import NowCard
from litetui.recap import split_recap
from litetui.widgets import AssistantMessage, ToolMessage


def app_for_pilot():
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.available_models = ["test"]
    app.model_id = "test"
    app.convo_id = "test"
    app.seat = SimpleNamespace(name="Carmack", agent_id="test-agent", registered=True, register=lambda: False)
    app.ctx_used, app.ctx_max, app.ctx_loaded = 22824, 258400, True
    return app


@pytest.mark.parametrize("width", [45, 46, 79, 101])
@pytest.mark.asyncio
async def test_footer_regions_and_compact_tool(width):
    app = app_for_pilot()
    async with app.run_test(size=(width, 22)) as pilot:
        for _ in range(40):
            await pilot.pause(.05)
            status_nodes = list(app.query('.ctx-label'))
            controls_nodes = list(app.query('.footer-second-row'))
            if status_nodes and controls_nodes and status_nodes[0].region.height and controls_nodes[0].region.y:
                break
        status = app.query_one('.ctx-label')
        controls = app.query_one('.footer-second-row')
        assert status.region.y + 1 == controls.region.y
        assert status.region.height == controls.region.height == 1
        assert '9%' in status.content.plain and 'Carmack' in status.content.plain
        for _ in range(20):
            if app.query_one('.compact-footer-hints').display == (width <= 93):
                break
            await pilot.pause(.05)
        assert app.query_one('.compact-footer-hints').display == (width <= 93)
        assert app.query_one('.footer-hints').display == (width >= 101)
        tool = ToolMessage('read')
        tool.set_args('{"path":"settings-store.ts","offset":297,"limit":16}')
        tool.set_result('the full result', True, elapsed=.1)
        await app.query_one('#chat-log').mount(tool)
        await pilot.pause(.2)
        if width <= 93:
            assert tool.region.height == 1
            assert 'settings-store.ts:297 +16' in tool.header.content.plain
            assert tool._body_content().plain == 'Arguments\n{"path":"settings-store.ts","offset":297,"limit":16}\n\nResult\nthe full result'
            app.action_expand_recent_tool()
            await pilot.pause(.2)
            assert tool.expanded
            assert tool.body.content.plain == tool._body_content().plain
            before = tool.body.content.plain
            await pilot.resize_terminal(120, 22)
            await pilot.pause(.3)
            assert tool.expanded and tool.body.content.plain == before
            assert '🔧 read' in tool.header.content.plain
        else:
            assert not app._compact_mode
            assert '🔧 read' in tool.header.content.plain


def test_tool_summaries_do_not_invent_counts():
    assert 'new 3 lines' in summary('write', json.dumps({'path':'x.py','content':'a\nb\nc'}), 'ok', True, 46, '0.1s')
    assert '+3' not in summary('edit', '{"path":"x.py","old_string":"a","new_string":"b"}', 'done', True, 46, '0.1s')
    assert summary('unknown', '{"path":"x"}', '', True, 46, '0.1s') is None
    assert summary('read', '{bad', '', True, 46, '0.1s') is None
    assert 'exit 1' in summary('shell', '{"command":"pytest tests"}', 'exit code 1; 4 passed, 1 failed', False, 46, '2s')
    assert 'hits/3 files' in summary('grep', '{"pattern":"foo"}', '7 hits in 3 files', True, 46, '.1s')
    assert 'server.tool' in summary('mcp__server__tool', '{}', 'ok', True, 46, '.1s')


@pytest.mark.parametrize('chunks, expected, recap', [
    (['Plain', ' text.'], 'Plain text.', None),
    (['Answer. ', '<re', 'cap>Did work\nTests green</recap>', ' trailing'], 'Answer.  trailing', None),
    (['Answer. ', '<re', 'cap>Did work\nTests green</recap>'], 'Answer.', 'Did work / Tests green'),
    (['Answer ', '<recap>one\ntwo\nthree</recap>'], 'Answer', None),
    (['Answer ', '<recap>one</recap> more ', '<re', 'cap>two</recap>'], 'Answer  more', 'two'),
])
def test_recap_stream_rpc_and_display_share_one_prefix(chunks, expected, recap):
    from litetui.recap import RecapStream
    stream = RecapStream()
    emitted = []
    for chunk in chunks:
        emitted.append(stream.feed(chunk))
        assert '<recap' not in stream.visible and '</recap>' not in stream.visible
    emitted.append(stream.finish())
    assert ''.join(emitted) == stream.visible == expected
    assert stream.recap == recap


@pytest.mark.asyncio
async def test_ask_worker_thread_wait_card_ticks_and_clears():
    import asyncio
    from litetui import ask_user_question as aq
    app = app_for_pilot()
    app._rpc = True
    app._rpc_emit = lambda event: None
    async with app.run_test(size=(46, 22)) as pilot:
        task = asyncio.create_task(asyncio.to_thread(aq.run, {
            'questions': [{'label': 'A', 'question': 'Proceed?', 'options': [{'title': 'Yes'}]}]
        }, app))
        try:
            card = app.query_one(NowCard)
            for _ in range(40):
                if card.wait:
                    break
                await pilot.pause(.05)
            assert card.wait is not None and 'WAITING ON host' in card.content
            first = card.content
            await pilot.pause(2.2)
            assert '0:02' in card.content and card.content != first
        finally:
            aq.cancel_pending_asks(app)
            await asyncio.wait_for(task, timeout=7)
            await pilot.pause(.1)
        assert card.wait is None


def test_recap_stream_split_and_missing_malformed():
    text = 'Answer.\n<recap>Did work\nTests green</recap>'
    for i in range(len(text)):
        shown, _ = split_recap(text[:i])
        assert '<recap>' not in shown
        assert '</recap>' not in shown
    assert split_recap(text, final=True) == ('Answer.', 'Did work / Tests green')
    assert split_recap('Answer only', final=True) == ('Answer only', None)
    malformed = 'Answer <recap>unfinished'
    assert split_recap(malformed, final=True) == ('Answer', None)
    assert split_recap('Answer <recap>too many words</recap> trailing', final=True) == ('Answer  trailing', None)


@pytest.mark.asyncio
async def test_recap_receipt_summary_now_and_presence(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    presence = tmp_path / '.liteharness' / 'agents' / 'test-agent.json'
    presence.parent.mkdir(parents=True)
    original = {'agent_id': 'test-agent', 'name': 'Carmack', 'tier': 'worker',
                'model': 'test', 'spawned_by': 'Marquee'}
    presence.write_text(json.dumps(original), encoding='utf-8')
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        card = AssistantMessage()
        card.set_answer('Answer.')
        card.recap = 'Changed renderer / Checks passed'
        app.query_one('#chat-log').mount(card)
        await pilot.pause(.1)
        summary_calls = []
        app._kick_card_summary = summary_calls.append
        app._record_recap(card.recap)
        app._settle_turn_stop_line(card, started_at=0, final_tps=2.0, stopped=False)
        assert not summary_calls
        assert card.recap in card.summary and card.recap in str(card.stop_line.content)
        assert card.recap in app.query_one(NowCard).content
        data = json.loads(presence.read_text(encoding='utf-8'))
        assert data['last_recap'] == card.recap and data['last_recap_at']
        assert all(data[key] == value for key, value in original.items())


def test_recap_presence_never_resurrects_missing_or_malformed_seat(tmp_path):
    from litetui.harness import merge_recap_presence

    missing = tmp_path / 'absent.json'
    assert not merge_recap_presence(missing, 'test-agent', 'Work complete')
    assert not missing.exists()
    malformed = tmp_path / 'broken.json'
    malformed.write_text('{not json', encoding='utf-8')
    assert not merge_recap_presence(malformed, 'test-agent', 'Work complete')
    assert malformed.read_text(encoding='utf-8') == '{not json'
    malformed.write_text('[]', encoding='utf-8')
    assert not merge_recap_presence(malformed, 'test-agent', 'Work complete')
    assert malformed.read_text(encoding='utf-8') == '[]'


@pytest.mark.asyncio
async def test_missing_recap_keeps_side_call_fallback():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        card = AssistantMessage()
        card.set_answer('No recap returned.')
        app.query_one('#chat-log').mount(card)
        await pilot.pause(.1)
        calls = []
        app._kick_card_summary = calls.append
        app._settle_turn_stop_line(card, started_at=0, final_tps=None, stopped=False)
        assert calls == [card]
        assert 'No recap returned' not in str(card.stop_line.content)


@pytest.mark.asyncio
async def test_saved_recaps_replay_without_tags(tmp_path):
    text = 'Answer.\n<recap>Did work\nGreen checks</recap>'
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        app.conversation = [{'role': 'assistant', 'content': text}]
        app._render_resumed(tmp_path / 'convo.jsonl')
        await pilot.pause(.2)
        card = list(app.query(AssistantMessage))[-1]
        assert card.answer_text == 'Answer.'
        assert card.recap == card.summary == 'Did work / Green checks'
        metadata = {'card_text_lengths': [len(text)], 'activities': []}
        claude_card = replay_activity(app, metadata, text)[0]
        await pilot.pause(.1)
        assert claude_card.answer_text == 'Answer.' and claude_card.recap == card.recap
        trace = {'display_trace': {'version': 1, 'items': [{'id': 'a', 'kind': 'agentMessage', 'result': text}]}}
        replay_codex(app, trace, set())
        await pilot.pause(.1)
        codex_card = list(app.query(AssistantMessage))[-1]
        assert codex_card.answer_text == 'Answer.' and codex_card.recap == card.recap


@pytest.mark.asyncio
async def test_retry_banner_lifetime_and_cancellation():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.15)
        card = app.query_one(NowCard)
        app._retry_notice('Codex rate limited (HTTP 429); retrying in 2s.')
        assert card.wait[:2] == ('provider', 'rate-limit retry')
        assert 'WAITING ON provider' in card.content
        app._end_wait()  # first resumed chunk, terminal error, or stream cancellation
        assert card.wait is None
        app._retry_notice('Codex rate limited (HTTP 429); retrying in 2s.')
        app._retry_notice('Codex connection reset; retrying in 2s.')
        assert card.wait is None


@pytest.mark.asyncio
async def test_card_reads_only_assignee_from_sqlite(tmp_path, monkeypatch):
    import litetui.now_card as now
    monkeypatch.setattr(now.Path, 'home', lambda: tmp_path)
    dbpath = tmp_path / '.litesuite' / 'harness' / 'tasks.db'
    dbpath.parent.mkdir(parents=True)
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE tasks (id TEXT,title TEXT,assignee TEXT,status TEXT,claimed_at TEXT)')
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', ('T1124','Compact pane','test-agent','building','2026-01-01'))
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', ('T9999','Other','another','building','2027-01-01'))
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app.query_one(NowCard)
        card.repaint(agent_id='test-agent', tool=SimpleNamespace(tool_name='read', _t0=0))
        assert 'T1124 Compact pane' in card.content
        card.begin_wait('Marquee', 'approval')
        assert 'WAITING ON Marquee' in card.content
        card.end_wait()
        assert card.wait is None
        assert 'T1124 Compact pane' in card.content  # idle still shows the claimed card


@pytest.mark.asyncio
async def test_idle_now_card_refreshes_assignment_without_turn(tmp_path, monkeypatch):
    import litetui.now_card as now
    monkeypatch.setattr(now.Path, 'home', lambda: tmp_path)
    dbpath = tmp_path / '.litesuite' / 'harness' / 'tasks.db'
    dbpath.parent.mkdir(parents=True)
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE tasks (id TEXT,title TEXT,assignee TEXT,status TEXT,claimed_at TEXT)')
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app.query_one(NowCard)
        assert 'no card claimed' in card.content
        with sqlite3.connect(dbpath) as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',
                       ('T1124', 'Compact pane', 'test-agent', 'building', '2026-01-01'))
        card._read_at -= 31  # simulate the 30-second board read interval
        await pilot.pause(1.2)  # no turn, input, resize, or explicit repaint
        assert 'T1124 Compact pane' in card.content


@pytest.mark.asyncio
async def test_slim_input_focus_and_text_growth():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.3)
        box = app.query_one('PromptBox')
        field = app.query_one('#message-input')
        assert box.region.height == field.region.height == 3  # Borders and one text row remain visible.
        field.focus()
        await pilot.pause(.2)
        assert box.region.height == 5
        field.value = 'draft'
        await pilot.pause(.1)
        app.set_focus(None)
        await pilot.pause(.1)
        assert box.region.height == 5
        field.value = ''
        box.sync_compact()
        await pilot.pause(.2)
        assert box.region.height == field.region.height == 3


@pytest.mark.asyncio
async def test_hysteresis_override_and_click_expansion():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.3)
        tool = ToolMessage('read')
        tool.set_args('{"path":"x.py"}')
        tool.set_result('hello', True)
        await app.query_one('#chat-log').mount(tool)
        await pilot.pause(.2)
        assert app._compact_mode
        await pilot.click(tool.header)
        await pilot.pause(.2)
        assert tool.expanded and 'hello' in tool.body.content.plain
        app.action_toggle_compact_view()
        await pilot.pause(.2)
        assert not app._compact_mode and tool.expanded
        app.action_toggle_compact_view()
        await pilot.pause(.2)
        assert app._compact_mode and tool.expanded
        await pilot.resize_terminal(98, 22)
        await pilot.pause(.2)
        assert app._compact_mode
        await pilot.resize_terminal(101, 22)
        await pilot.pause(.2)
        assert app._compact_mode  # session override persists across resize
