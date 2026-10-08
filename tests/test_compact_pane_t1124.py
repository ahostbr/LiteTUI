"""Responsive compact pane: measured geometry, expansion, recap and board ownership."""
import asyncio
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
    app.settings.footer_show_key_hints = True  # T0247 made them opt-in; these tests measure their layout
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
@pytest.mark.parametrize('rounds,verdicts,want', [
    (['Draft.\n<recap>Rejected draft</recap>', 'Final.\n<recap>Accepted final</recap>'],
     ['retry', 'allow'], 'Accepted final'),
    (['Draft.\n<recap>Rejected draft</recap>'], ['pause'], 'Previous recap'),
    (['Tool preface.\n<recap>Not final</recap>', 'Final.\n<recap>Accepted final</recap>'],
     ['allow'], 'Accepted final'),
])
async def test_only_accepted_terminal_answer_publishes_recap(tmp_path, monkeypatch, rounds, verdicts, want):
    from litetui import app as app_module, hook_host
    from litetui.tool_policy import READ_POLICY
    from test_card_summary import _Chunk, _Stream, _TC

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    presence = tmp_path / '.liteharness' / 'agents' / 'test-agent.json'
    presence.parent.mkdir(parents=True)
    presence.write_text(json.dumps({'agent_id': 'test-agent', 'last_recap': 'Previous recap'}), encoding='utf-8')
    app = app_for_pilot()
    app.settings.tool_iterations = 3
    app.tools_enabled = True
    app._active_tool_profile = 'autonomous'
    app.settings.tool_policy_profile = 'autonomous'
    app.settings.tool_auto_background_s = 0
    app._resync_ctx_if_stale = lambda: None
    app._maybe_autocompact = lambda: None
    app._report_spawner_error = lambda *args: None
    async def ready():
        pass
    app._ensure_chat_ready = ready
    app.plugins.add_tool('test', {'type': 'function', 'function': {'name': 'probe',
        'description': 'fixture', 'parameters': {'type': 'object', 'properties': {}}}},
        lambda args: 'ok', policy=READ_POLICY)
    streams = []
    for i, answer in enumerate(rounds):
        chunks = [_Chunk(content=answer)]
        if len(rounds) == 2 and i == 0 and answer.startswith('Tool'):
            chunks += [_Chunk(tool_calls=[_TC(0, id='tool1', name='probe')]),
                       _Chunk(tool_calls=[_TC(0, arguments='{}')])]
        streams.append(_Stream(chunks))
    stream_iter = iter(streams)
    requests = []
    async def create(**kwargs):
        purpose = kwargs.get('purpose', 'turn')
        requests.append(purpose)
        if purpose == 'card-summary':
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='summary'))])
        return next(stream_iter)
    monkeypatch.setattr(app_module.model_transport, 'for_app',
                        lambda app: SimpleNamespace(create=create))
    decisions = iter(verdicts)
    completions = []
    async def completion(app, answer):
        completions.append(answer)
        # A rejected draft or tool round must leave presence untouched at the gate.
        assert json.loads(presence.read_text(encoding='utf-8'))['last_recap'] == 'Previous recap'
        return next(decisions)
    monkeypatch.setattr(hook_host, 'completion', completion)
    async with app.run_test(size=(46, 22)) as pilot:
        app.last_recap = 'Previous recap'
        app.query_one(NowCard).last = 'Previous recap'
        app._append({'role': 'user', 'content': 'go'})
        app._stream()
        for _ in range(200):
            await pilot.pause(.05)
            if not app._chat_running():
                break
        assert not app._chat_running(), f"turn still running: {requests}, {completions}"
        assert app.last_recap == want, (requests, completions, app.conversation[-3:])
        assert app.query_one(NowCard).last == want
        assert json.loads(presence.read_text(encoding='utf-8'))['last_recap'] == want


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_width', [46, 120])
async def test_real_turn_read_tool_finishes_as_compact_row(monkeypatch, initial_width):
    from litetui import app as app_module
    from test_card_summary import _Chunk, _Stream, _TC

    app = app_for_pilot()
    app.settings.tool_iterations = 3
    app.tools_enabled = True
    app._active_tool_profile = 'autonomous'
    app.settings.tool_policy_profile = 'autonomous'
    app._resync_ctx_if_stale = lambda: None
    app._maybe_autocompact = lambda: None
    app._report_spawner_error = lambda *args: None

    async def ready():
        pass

    app._ensure_chat_ready = ready
    app._execute_tool = lambda *args, **kwargs: asyncio.sleep(0, result=('README contents', True))
    streams = iter([
        _Stream([_Chunk(tool_calls=[_TC(0, id='read1', name='read')]),
                 _Chunk(tool_calls=[_TC(0, arguments='{"path":"C:/ExampleProjects/LiteTUI/README.md","offset":1,"limit":40}')])]),
        _Stream([_Chunk(content='Read the requested README lines.')]),
    ])

    async def create(**kwargs):
        if kwargs.get('purpose') == 'card-summary':
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='summary'))])
        return next(streams)

    monkeypatch.setattr(app_module.model_transport, 'for_app',
                        lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(initial_width, 22)) as pilot:
        await pilot.pause(.2)
        assert app._compact_mode == (initial_width == 46)
        app._append({'role': 'user', 'content': 'read README'})
        app._stream()
        for _ in range(100):
            await pilot.pause(.05)
            if not app._chat_running():
                break
        assert not app._chat_running()
        tools = list(app.query(ToolMessage))
        assert len(tools) == 1
        tool = tools[0]
        assert tool._result is not None
        assert tool.tool_name == 'read'
        assert json.loads(tool._args) == {'path': 'C:/ExampleProjects/LiteTUI/README.md', 'offset': 1, 'limit': 40}
        if initial_width != 46:
            await pilot.resize_terminal(46, 22)
            await pilot.pause(.3)
        assert app._compact_mode and app.query_one(NowCard).display
        assert app.query_one('.compact-footer-hints').display
        assert tool.header.content.plain.startswith('▸ read  README.md:1 +40'), (
            tool.header.content.plain, tool._compact, tool.expanded, tool._explicit_expansion)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,tool_name,server,as_text', [
    ('dynamicToolCall', 'litetui_read', None, False),
    ('mcpToolCall', 'read', 'workspace', True),
])
async def test_native_codex_item_read_row_is_compact(kind, tool_name, server, as_text):
    from litetui.codex_tool_ui import CodexToolUI
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        assert app._compact_mode
        ui = CodexToolUI(app, thread_id='thread', turn_id='turn')
        args = {'path': 'C:/ExampleProjects/LiteTUI/README.md', 'offset': 1, 'limit': 40}
        item = {'type': kind, 'id': 'read1', 'tool': tool_name,
                'arguments': json.dumps(args) if as_text else args}
        if server:
            item['server'] = server
        await ui.item(item)
        await ui.item({**item, 'success': True, 'durationMs': 10,
                       'contentItems': [{'type': 'inputText', 'text': 'README contents'}]}, True)
        tool = list(app.query(ToolMessage))[-1]
        assert tool.tool_name == ('workspace/read' if server else 'read')
        assert json.loads(tool._args) == args
        assert tool.header.content.plain.startswith('▸ read  README.md:1 +40'), (
            tool.header.content.plain, tool._compact, tool.expanded, tool._explicit_expansion)


@pytest.mark.asyncio
@pytest.mark.parametrize('item,expected', [
    ({'type': 'commandExecution', 'id': 'native', 'command': 'echo hello'}, '$ command  echo'),
    ({'type': 'fileChange', 'id': 'native', 'changes': [{'path': 'src/a.py'}]}, '✎ file changes  files 1 changes'),
    ({'type': 'webSearch', 'id': 'native', 'query': 'LiteTUI'}, '⌕ web search  LiteTUI'),
])
async def test_native_codex_builtin_items_have_compact_summary(item, expected):
    from litetui.codex_tool_ui import CodexToolUI
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        ui = CodexToolUI(app, thread_id='thread', turn_id='turn')
        await ui.item(item)
        await ui.item({**item, 'status': 'completed', 'durationMs': 25}, True)
        tool = list(app.query(ToolMessage))[-1]
        assert tool._compact and not tool.expanded
        assert tool.header.content.plain.startswith(expected), (tool._args, tool.header.content.plain)


@pytest.mark.asyncio
@pytest.mark.parametrize('resize_while_running', [False, True])
async def test_native_codex_read_started_wide_then_resized_compact(resize_while_running):
    from litetui.codex_tool_ui import CodexToolUI
    app = app_for_pilot()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(.2)
        assert not app._compact_mode
        ui = CodexToolUI(app, thread_id='thread', turn_id='turn')
        item = {'type': 'dynamicToolCall', 'id': 'read1', 'tool': 'litetui_read',
                'arguments': {'path': 'C:/ExampleProjects/LiteTUI/README.md', 'offset': 1, 'limit': 40}}
        await ui.item(item)
        tool = list(app.query(ToolMessage))[-1]
        if resize_while_running:
            await pilot.resize_terminal(80, 25)
            await pilot.resize_terminal(46, 22)
            await pilot.pause(.3)
        await ui.item({**item, 'success': True, 'durationMs': 10,
                       'contentItems': [{'type': 'inputText', 'text': 'README contents'}]}, True)
        if not resize_while_running:
            await pilot.resize_terminal(80, 25)
            await pilot.resize_terminal(46, 22)
            await pilot.pause(.3)
        assert app._compact_mode and app.has_class('compact-profile')
        assert app.query_one(NowCard).display
        assert app.query_one('.compact-footer-hints').display
        assert tool.header.content.plain.startswith('▸ read  README.md:1 +40'), (
            tool.header.content.plain, tool._compact, tool.expanded)


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
        app._clear_retry_wait()  # first resumed chunk, terminal error, or stream cancellation
        assert card.wait is None
        app._retry_notice('Codex rate limited (HTTP 429); retrying in 2s.')
        app._retry_notice('Codex connection reset; retrying in 2s.')
        assert card.wait is None


@pytest.mark.asyncio
async def test_now_card_wait_survives_modal_repaint_and_pop():
    from textual.screen import ModalScreen
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app._now_card()
        token = app._begin_wait('host', 'question')
        await app.push_screen(ModalScreen())
        await pilot.pause(.6)  # two elapsed repaint ticks with a modal on top
        assert app._now_card() is card
        assert card.wait is not None and 'WAITING ON host' in card.content
        app.pop_screen()
        await pilot.pause(.1)
        assert 'WAITING ON host' in card.content
        app._end_wait(token)
        assert card.wait is None


@pytest.mark.asyncio
async def test_overlapping_waits_keep_newest_owner_and_clock():
    import time
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app._now_card()
        first = app._begin_wait('you', 'question')
        second = app._begin_wait('KeyStone', 'approval')
        card._waits[second] = ('KeyStone', 'approval', time.monotonic() - 2)
        card.wait = card._waits[second]
        app._paint_now()
        assert 'WAITING ON KeyStone' in card.content and '0:02' in card.content
        app._end_wait(first)
        assert 'WAITING ON KeyStone' in card.content and '0:02' in card.content
        app._end_wait(second)
        assert card.wait is None
        app._end_wait(None)
        app._end_wait(second)  # a settled token cannot clear another wait

        first = app._begin_wait('you', 'question')
        card._waits[first] = ('you', 'question', time.monotonic() - 8)
        card.wait = card._waits[first]
        second = app._begin_wait('KeyStone', 'approval')
        app._end_wait(second)
        assert card.wait[:2] == ('you', 'question')
        assert 'WAITING ON you' in card.content and '0:08' in card.content
        app._end_wait(second)
        assert 'WAITING ON you' in card.content
        app._end_wait(first)
        assert card.wait is None


@pytest.mark.asyncio
async def test_failed_spawner_send_clears_only_its_wait():
    from litetui import approval_relay, tool_policy
    app = app_for_pilot()
    app._spawner_id = 'KeyStone'
    app.seat.send = lambda *_: False
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        question = app._begin_wait('you', 'question')
        decision = tool_policy.PolicyDecision(tool_policy.CONFIRM, 'interactive', frozenset(), 'approval')
        result = await approval_relay.ask_spawner(app, 'read', {}, decision, 'tool')
        assert result == 'absent'
        assert app._now_card().wait[:2] == ('you', 'question')
        app._end_wait(question)
        assert app._now_card().wait is None


@pytest.mark.asyncio
async def test_worker_thread_wait_token_clears_its_own_banner():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        token = await asyncio.to_thread(app._begin_wait, 'host', 'question')
        assert token is not None and 'WAITING ON host' in app._now_card().content
        await asyncio.to_thread(app._end_wait, token)
        assert app._now_card().wait is None


@pytest.mark.asyncio
async def test_wait_dispatch_teardown_race_does_not_hide_other_errors():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        original = app.call_from_thread
        try:
            def fail(*args, **kwargs):
                raise RuntimeError('Event loop is closed')
            app.call_from_thread = fail
            assert await asyncio.to_thread(app._begin_wait, 'host', 'approval') is None
            await asyncio.to_thread(app._end_wait, object())

            def unrelated(*args, **kwargs):
                raise RuntimeError('callback failed')
            app.call_from_thread = unrelated
            with pytest.raises(RuntimeError, match='callback failed'):
                await asyncio.to_thread(app._begin_wait, 'host', 'approval')
            with pytest.raises(RuntimeError, match='callback failed'):
                await asyncio.to_thread(app._end_wait, object())
        finally:
            app.call_from_thread = original


@pytest.mark.asyncio
async def test_now_step_eta_requires_reliable_sample_and_live_prefill():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app._now_card()
        app._active_turn_started_at = __import__('time').monotonic()
        app._rpc_emit({'type': 'turn_start'})
        assert 'est ~' not in card.content
        app._eta.last_prompt_tokens = 1000
        app._eta.samples = [100.0]
        app._elapsed.body = SimpleNamespace(content=None)
        app._paint_now()
        assert 'est ~10.0s' in card.content
        app._elapsed.stop_body()
        app._paint_now()
        assert 'est ~' not in card.content
        app._emit_turn_end('stop', None)


@pytest.mark.asyncio
async def test_turn_active_survives_body_timer_stop_and_clears_on_every_end():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app._now_card()
        for provider in ('codex', 'claude'):
            app._active_turn_started_at = __import__('time').monotonic()
            app._rpc_emit({'type': 'turn_start', 'provider': provider})
            app._elapsed.stop_body()  # the first answer token ends this timer
            app._paint_now()
            assert 'now responding' in card.content and 'idle since' not in card.content
            app._emit_turn_end('cancelled' if provider == 'claude' else 'stop', None)
            assert 'now idle' in card.content and 'idle since' in card.content


@pytest.mark.asyncio
async def test_real_codex_stream_keeps_responding_after_first_chunk(monkeypatch):
    from litetui import app as app_module
    from test_card_summary import _Chunk

    first_chunk = asyncio.Event()
    finish = asyncio.Event()
    app = app_for_pilot()
    app._resync_ctx_if_stale = lambda: None
    app._maybe_autocompact = lambda: None
    app._report_spawner_error = lambda *args: None

    async def ready():
        pass
    app._ensure_chat_ready = ready

    class Stream:
        async def __aiter__(self):
            await asyncio.sleep(.01)
            yield _Chunk(content='Answer begins. ')
            first_chunk.set()
            await finish.wait()
            yield _Chunk(content='Answer ends.')

        async def close(self):
            pass

    async def create(**kwargs):
        if kwargs.get('purpose') == 'card-summary':
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='summary'))])
        return Stream()

    monkeypatch.setattr(app_module.model_transport, 'for_app',
                        lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(46, 22)) as pilot:
        app._append({'role': 'user', 'content': 'go'})
        app._stream()
        await asyncio.wait_for(first_chunk.wait(), 5)
        await pilot.pause(.4)
        card = app._now_card()
        assert 'now responding' in card.content and 'idle since' not in card.content
        finish.set()
        for _ in range(100):
            await pilot.pause(.05)
            if not app._chat_running():
                break
        assert not app._chat_running()
        assert 'now idle' in card.content and 'idle since' in card.content


@pytest.mark.asyncio
async def test_unexpected_stream_worker_error_clears_now_card(monkeypatch):
    from litetui import app as app_module

    app = app_for_pilot()
    app._resync_ctx_if_stale = lambda: None
    app._report_spawner_error = lambda *args: None

    async def ready():
        pass
    app._ensure_chat_ready = ready

    def fail(*args, **kwargs):
        raise RuntimeError('unexpected request construction')
    monkeypatch.setattr(app_module.TurnEngine, 'chat_request', fail)
    async with app.run_test(size=(46, 22)) as pilot:
        app._append({'role': 'user', 'content': 'go'})
        worker = app._stream()
        worker.exit_on_error = False  # inspect the failed worker without exiting the pilot
        for _ in range(80):
            await pilot.pause(.05)
            if worker.is_finished:
                break
        assert worker.is_finished and worker.error is not None
        assert 'now idle' in app._now_card().content
        assert app._now_turn_active_at is None


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
        token = card.begin_wait('Marquee', 'approval')
        assert 'WAITING ON Marquee' in card.content
        card.end_wait(token)
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
async def test_now_card_long_fields_stay_in_three_cells_and_wait_keeps_clock():
    from rich.cells import cell_len
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        card = app.query_one(NowCard)
        for _ in range(20):
            if card.region.width:
                break
            await pilot.pause(.05)
        assert card.region.width == 46 and card.region.height == 3 and card.display
        card.card_label = 'T1124 ' + 'long-title-' * 12
        card.last = 'result ' + 'long recap words ' * 30
        card.repaint(agent_id='test-agent', tool=SimpleNamespace(tool_name='long-tool-' * 10, _t0=0))
        lines = card.content.splitlines()
        assert len(lines) == card.region.height == 3
        assert all(cell_len(line) <= card.content_region.width for line in lines)
        assert lines[2].startswith('last ')
        token = card.begin_wait('LongOwnerNameThatDoesNotFitAcrossThisPane', 'approval')
        waiting = card.content.splitlines()[0]
        assert 'WAITING ON' in waiting and '0:00' in waiting
        assert cell_len(waiting) <= card.content_region.width
        card._waits[token] = ('very-long-owner-' * 12, 'approval', __import__('time').monotonic() - 2)
        card.wait = card._waits[token]
        card.repaint(agent_id='test-agent')
        assert '0:02' in card.content.splitlines()[0]
        assert cell_len(card.content.splitlines()[0]) <= card.content_region.width
        await pilot.resize_terminal(101, 22)
        await pilot.pause(.3)
        assert not card.display and card.region.height == 0
        await pilot.resize_terminal(46, 22)
        await pilot.pause(.3)
        assert card.display and card.region.height == 3
        await pilot.resize_terminal(101, 22)
        await pilot.pause(.3)
        assert not card.display and card.region.height == 0


@pytest.mark.asyncio
async def test_focused_empty_input_survives_resize_before_slimming():
    app = app_for_pilot()
    async with app.run_test(size=(120, 22)) as pilot:
        field = app.query_one('#message-input')
        box = app.query_one('PromptBox')
        field.focus()
        await pilot.pause(.2)
        assert field.has_focus and box.region.height == 5
        await pilot.resize_terminal(46, 22)
        await pilot.pause(.3)
        assert field.has_focus and box.region.height == 5
        app.set_focus(None)
        await pilot.pause(.2)
        assert box.region.height == 3
        field.focus()
        await pilot.pause(.2)
        assert field.has_focus and box.region.height == 5


@pytest.mark.asyncio
async def test_slim_input_focus_and_text_growth():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.3)
        box = app.query_one('PromptBox')
        field = app.query_one('#message-input')
        assert field.has_focus and box.region.height == 5
        app.set_focus(None)
        await pilot.pause(.2)
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
async def test_keyboard_selects_older_tool_without_collapsing_newer():
    app = app_for_pilot()
    async with app.run_test(size=(46, 22)) as pilot:
        await pilot.pause(.2)
        older, newer = ToolMessage('read'), ToolMessage('read')
        older.set_args('{"path":"older.py"}')
        older.set_result('older full output', True)
        newer.set_args('{"path":"newer.py"}')
        newer.set_result('newer full output', True)
        await app.query_one('#chat-log').mount(older, newer)
        await pilot.pause(.2)
        expected = older._body_content().plain
        await pilot.press('ctrl+b')  # first selection: latest
        await pilot.press('ctrl+b')  # then previous/older
        assert app.focused is older.header
        await pilot.press('ctrl+e')
        await pilot.pause(.1)
        assert older.expanded and older.body.content.plain == expected
        assert not newer.expanded
        await pilot.resize_terminal(120, 22)
        await pilot.pause(.3)
        field = app.query_one('#message-input')
        assert app.focused is field and not older.header.can_focus
        field.value = 'draft text'
        field.cursor_position = 0
        await pilot.press('ctrl+e')
        assert field.cursor_position == len(field.value)
        assert older.expanded and not newer.expanded


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
        tool.scroll_visible()
        await pilot.pause(.1)
        field = app.query_one('#message-input')
        assert field.has_focus
        assert await pilot.click(tool.header)
        await pilot.pause(.2)
        assert app.focused is field  # click does not blur and reflow the prompt
        assert app.query_one('PromptBox').region.height == 5
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
