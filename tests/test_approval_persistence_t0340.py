"""Second-round shipped repository/error-latch and indefinite-wait controls."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import approval_authority, approval_relay, harness, seat_authority, tool_approval, tool_policy
from litetui.app import LiteTUI
from litetui.conversation import ConversationRepository


@pytest.fixture
def real_app(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'AGENTS_DIR', tmp_path / 'agents')
    harness.AGENTS_DIR.mkdir()
    (harness.AGENTS_DIR / 'leader.json').write_text(json.dumps({'agent_id': 'leader'}))
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: 'leader')
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: 'own')
    app = LiteTUI()
    app.seat.agent_id = 'worker'
    app.conversation = [{'role': 'user', 'content': 'test'}]
    store = ConversationRepository()
    store.convo_id = 'origin'
    store.convo_dir = tmp_path / 'convos' / 'origin'
    store.convo_path = store.convo_dir / 'convo.jsonl'
    # Preserve record_edit/write_record/open; fake only lease acquisition for
    # isolation. Failure arms deliberately replace acquire/open below.
    monkeypatch.setattr(store, 'acquire', lambda *args: None)
    app.store = store
    app.events = []
    app._rpc_emit = app.events.append
    app._system = lambda *args: None
    yield app
    store.release()


async def rpc(app, timeout=0):
    task = asyncio.create_task(tool_approval.approve_over_rpc(
        app, 'shell', {}, SimpleNamespace(reason='test'), timeout=timeout))
    await asyncio.sleep(0)
    return task, app.events[-1]['id'] if app.events else None


def assert_clean(app):
    assert not app._approval_authority_records
    assert not app._approval_waiters
    assert not app._rpc_approval_spawners


def fail_repository(app, monkeypatch, fault):
    if fault == 'acquire':
        def fail(*args):
            raise OSError('acquire unavailable')
        monkeypatch.setattr(app.store, 'acquire', fail)
    else:
        actual = Path.open
        def fail(path, *args, **kwargs):
            if path == app.store.convo_path and args and args[0] == 'a+b':
                raise OSError('write unavailable')
            return actual(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', fail)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['create', 'settle'])
@pytest.mark.parametrize('fault', ['acquire', 'open'])
async def test_actual_edit_swallowed_oserror_never_approves(real_app, monkeypatch, stage, fault):
    app = real_app
    if stage == 'create':
        fail_repository(app, monkeypatch, fault)
    task, ident = await rpc(app)
    if stage == 'settle':
        initial = app.store.convo_path.read_bytes()
        fail_repository(app, monkeypatch, fault)
        assert not tool_approval.resolve_over_rpc(app, ident, True)
        assert not tool_approval.resolve_over_rpc(app, ident, True)
    assert await asyncio.wait_for(task, 0.5) is None
    assert app.store.persist_error is not None
    assert_clean(app)
    if stage == 'settle':
        assert app.store.convo_path.read_bytes() == initial
        assert app.conversation[0]['approval_delivery'][ident]['outcome'] == 'pending'
    else:
        assert not app.events
        assert not app.conversation[0].get('approval_delivery')
    assert not tool_approval.resolve_over_rpc(app, ident or 'missing', True)


@pytest.mark.asyncio
async def test_real_store_valid_indefinite_wait_and_durable_answer(real_app):
    task, ident = await rpc(real_app)
    await asyncio.sleep(0.12)
    assert not task.done(), 'cadence is not a human timeout'
    assert tool_approval.resolve_over_rpc(real_app, ident, True)
    assert await asyncio.wait_for(task, 0.5) is tool_approval.ONCE
    records = [json.loads(line) for line in real_app.store.convo_path.read_text().splitlines()]
    assert records[-1]['message']['approval_delivery'][ident]['outcome'] == 'approved'
    assert real_app.store.persist_error is None
    assert_clean(real_app)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['list', 'message', 'store-id'])
async def test_silent_origin_change_wakes_timeout_zero(real_app, change):
    app = real_app
    task, ident = await rpc(app)
    original = app.store.convo_path.read_bytes()
    if change == 'list':
        app.conversation = list(app.conversation)
    elif change == 'message':
        app.conversation[0] = dict(app.conversation[0])
    else:
        app.store.convo_id = 'foreign'
    assert await asyncio.wait_for(task, 0.5) is None
    assert app.store.convo_path.read_bytes() == original
    assert_clean(app)
    assert not tool_approval.resolve_over_rpc(app, ident, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['prior-error', 'pending', 'loading', 'path', 'no-user', 'missing-signal'])
async def test_no_durable_signal_fails_closed_without_erasing_evidence(real_app, fault):
    app = real_app
    if fault == 'prior-error':
        app.store.persist_error = 'prior disk error'
    elif fault == 'no-user':
        app.conversation = []
    elif fault == 'path':
        app.store.convo_path = None
    elif fault == 'missing-signal':
        del app.store.persist_error
    else:
        setattr(app.store, fault, True)
    task, ident = await rpc(app)
    assert await asyncio.wait_for(task, 0.5) is None
    assert ident is None
    assert_clean(app)
    if fault == 'prior-error':
        assert app.store.persist_error == 'prior disk error'


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['waiting', 'after-answer'])
async def test_outer_cancellation_during_observation_cleans(real_app, when):
    task, ident = await rpc(real_app)
    if when == 'after-answer':
        assert tool_approval.resolve_over_rpc(real_app, ident, True)
    task.cancel()
    assert await task is None
    assert_clean(real_app)
    assert not tool_approval.resolve_over_rpc(real_app, ident, True)


@pytest.mark.asyncio
async def test_finite_deadline_and_positive(real_app):
    task, ident = await rpc(real_app, timeout=0.5)
    assert tool_approval.resolve_over_rpc(real_app, ident, True)
    assert await task is tool_approval.ONCE
    real_app.events.clear()
    task, ident = await rpc(real_app, timeout=0.02)
    assert await asyncio.wait_for(task, 0.5) is None
    assert not tool_approval.resolve_over_rpc(real_app, ident, True)
    assert_clean(real_app)


@pytest.mark.asyncio
@pytest.mark.parametrize('offset', [0, 0.001])
async def test_done_future_at_or_after_absolute_deadline_refused(real_app, monkeypatch, offset):
    app = real_app
    task, ident = await rpc(app, timeout=1)
    record = app._approval_authority_records[ident]
    deadline = record['authority'].deadline
    # Complete without yielding, then make the observation clock reach expiry.
    app._approval_waiters[ident].set_result(tool_approval.ONCE)
    monkeypatch.setattr(approval_authority.time, 'monotonic', lambda: deadline + offset)
    assert await task is None
    assert_clean(app)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['cancelled', 'audit-error'])
async def test_actual_authorize_consumer_refuses_new_status(real_app, monkeypatch, tmp_path, status):
    app = real_app
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: 'spawner')
    async def refused(*args):
        return status
    monkeypatch.setattr(approval_relay, 'ask_spawner', refused)
    app._active_tool_profile = tool_policy.INTERACTIVE
    app._hook_source = 'harness'
    result = await app._authorize_action('powershell', {'command': 'Remove-Item old'},
        tool_policy.SHELL_POLICY, workspace=tmp_path, stop_on_denial=False)
    assert result[1] is False
    assert app._stop_requested and app._stop_cause == 'approval'
    assert 'not approved' in app._stop_reason
    assert 'denied' not in app._stop_reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['pending', 'no-user', 'missing-signal'])
async def test_actual_supervised_child_with_no_durable_origin_is_refused(real_app, fault):
    app = real_app
    app._rpc = True
    if fault == 'pending':
        app.store.pending = True
    elif fault == 'no-user':
        app.conversation = []
    else:
        del app.store.persist_error
    request = {'tool': 'powershell', 'input': {'command': 'Remove-Item old'},
               'profile': 'interactive', 'why': 'child destructive confirmation'}
    assert await asyncio.wait_for(app.approve_for_child(request), 0.5) is False
    assert not app.events
    assert_clean(app)
