"""Responsive compact pane: measured geometry, expansion, recap and board ownership."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from litetui.app import LiteTUI
from litetui.compact_tools import summary
from litetui.now_card import NowCard
from litetui.recap import split_recap
from litetui.widgets import ToolMessage


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
        await pilot.pause(.3)
        status = app.query_one('.ctx-label')
        controls = app.query_one('.footer-second-row')
        assert status.region.y + 1 == controls.region.y
        assert status.region.height == controls.region.height == 1
        assert '9%' in status.content.plain and 'Carmack' in status.content.plain
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


def test_recap_stream_split_and_missing_malformed():
    text = 'Answer.\n<recap>Did work\nTests green</recap>'
    for i in range(len(text)):
        shown, _ = split_recap(text[:i])
        assert '<recap>' not in shown
        assert '</recap>' not in shown
    assert split_recap(text, final=True) == ('Answer.', 'Did work / Tests green')
    assert split_recap('Answer only', final=True) == ('Answer only', None)
    malformed = 'Answer <recap>unfinished'
    assert split_recap(malformed, final=True) == (malformed, None)


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


@pytest.mark.asyncio
async def test_slim_input_focus_and_text_growth():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.3)
        box = app.query_one('PromptBox')
        field = app.query_one('#message-input')
        assert box.region.height == 2
        assert field.region.height >= 2  # Textual Input intrinsic minimum with a full border.
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
        assert box.region.height == 2


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
