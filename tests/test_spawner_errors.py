import json
from types import SimpleNamespace

import pytest

from litetui.spawner_errors import SpawnerErrorReporter
from litetui.app import LiteTUI


def _seat(root, spawner='parent'):
    directory = root / 'agents'
    directory.mkdir(exist_ok=True)
    (directory / 'seat-id.json').write_text(json.dumps({
        'agent_id': 'seat-id', 'spawned_by': spawner, 'name': 'OpenBolt',
    }), encoding='utf-8')
    sent = []
    seat = SimpleNamespace(agent_id='seat-id', name='OpenBolt',
                           send=lambda to, body: sent.append((to, body)) or True)
    return seat, sent


def test_sent_from_own_seat_to_spawner_with_context(tmp_path):
    seat, sent = _seat(tmp_path)
    reporter = SpawnerErrorReporter()
    assert reporter.send(seat, 'convo-1', 'HTTP 400', 'compaction', root=tmp_path, now=10)
    assert sent[0][0] == 'parent'
    assert all(piece in sent[0][1] for piece in ('OpenBolt', 'seat-id', 'convo-1', 'compaction', 'HTTP 400'))


def test_identical_error_throttled_for_five_minutes(tmp_path):
    seat, sent = _seat(tmp_path)
    reporter = SpawnerErrorReporter()
    assert reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path, now=1)
    assert not reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path, now=299)
    assert reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path, now=301)
    assert len(sent) == 2


def test_no_spawner_no_mail_and_send_failure_does_not_throttle(tmp_path):
    seat, sent = _seat(tmp_path, None)
    reporter = SpawnerErrorReporter()
    assert not reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path)
    assert sent == []
    seat, sent = _seat(tmp_path)
    seat.send = lambda *_: False
    assert not reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path, now=1)
    seat.send = lambda to, body: sent.append((to, body)) or True
    assert reporter.send(seat, 'c', 'bad', 'turn', root=tmp_path, now=2)


def test_reporter_never_raises(tmp_path):
    seat, _ = _seat(tmp_path)
    seat.send = lambda *_: 1 / 0
    assert not SpawnerErrorReporter().send(seat, 'c', 'bad', 'turn', root=tmp_path)


@pytest.mark.asyncio
async def test_system_message_routes_errors_and_ignores_success():
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    seen = []
    app._report_spawner_error = lambda text, activity: seen.append((text, activity))
    async with app.run_test():
        app.system_message('Compact failed - HTTP 400')
        app.system_message('Completed successfully')
    assert seen == [('Compact failed - HTTP 400', 'compaction')]


@pytest.mark.asyncio
async def test_real_missing_read_tool_result_reaches_reporter(tmp_path, monkeypatch):
    from litetui import model_transport, tool_policy
    from litetui.plugins.core_tools import tool_read

    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.tools_enabled = True
    app.settings.tool_iterations = 1
    app.settings.autocompact_enabled = False
    app._active_tool_profile = tool_policy.AUTONOMOUS
    app._authorize_action = lambda *a, **kw: _allow()
    app._dispatch_for = lambda name: tool_read if name == 'read' else None
    app.plugins.policy_for = lambda name: tool_policy.READ_POLICY
    missing = tmp_path / 'nothing.txt'
    seen = []
    app._report_spawner_error = lambda text, activity: seen.append((text, activity))

    class Stream:
        def __aiter__(self):
            async def chunks():
                function = SimpleNamespace(name='read', arguments=json.dumps({'path': str(missing)}))
                tc = SimpleNamespace(index=0, id='call-read', function=function)
                delta = SimpleNamespace(content=None, reasoning=None, reasoning_content=None,
                                        tool_calls=[tc])
                yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)
            return chunks()

        async def close(self):
            pass

    async def create(**kwargs):
        return Stream()

    async def _allow():
        return None

    monkeypatch.setattr(model_transport, 'for_app',
                        lambda _app: SimpleNamespace(create=create))
    async with app.run_test() as pilot:
        app._append({'role': 'user', 'content': 'read missing path'})
        app._stream()
        for _ in range(100):
            await pilot.pause()
            if not app._chat_running():
                break
    assert any('[error] file not found' in text and activity == 'read tool'
               for text, activity in seen)


def test_first_line_names_the_failure(tmp_path):
    seat, sent = _seat(tmp_path)
    SpawnerErrorReporter().send(seat, 'c', '[error] file not found: C:/x/nothing.txt',
                                'read tool', root=tmp_path, now=1)
    assert sent[0][1].splitlines()[0] == (
        '[LiteTUI error] OpenBolt: read tool failed: [error] file not found: C:/x/nothing.txt'
    )


def test_distinct_errors_are_capped_per_seat_with_one_rollover_summary(tmp_path):
    seat, sent = _seat(tmp_path)
    reporter = SpawnerErrorReporter()
    for n in range(9):
        reporter.send(seat, 'c', f'[error] missing path {n}', 'read tool',
                      root=tmp_path, now=n)
    assert len(sent) == 5
    assert reporter.send(seat, 'c', '[error] missing path 9', 'read tool',
                         root=tmp_path, now=301)
    assert len(sent) == 7
    assert sent[-2][1].splitlines()[0] == (
        '[LiteTUI error] OpenBolt: 4 more errors suppressed in the last 5 min'
    )
    assert '[error] missing path 9' in sent[-1][1]


def test_reporter_dedupes_same_error_even_if_activity_changes(tmp_path):
    seat, sent = _seat(tmp_path)
    reporter = SpawnerErrorReporter()
    assert reporter.send(seat, 'c', 'failure', 'stream', root=tmp_path, now=1)
    assert not reporter.send(seat, 'c', 'failure', 'system message', root=tmp_path, now=2)
    assert len(sent) == 1
