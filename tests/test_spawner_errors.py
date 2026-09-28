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
    assert seen == [('Compact failed - HTTP 400', 'system message')]
