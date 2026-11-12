"""Real entry doors with synthetic owned stores; no live registry or provider."""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import test_restart_checkpoint as helpers

from litetui import gui_rpc, hook_host
from litetui import restart_checkpoint as checkpoint
from litetui.app import LiteTUI

prepare = helpers.prepare
runtime = helpers.runtime
checkpoint_contract = helpers.checkpoint_contract


@pytest.mark.parametrize('operation', ['conversations.edit', 'conversations.retry', 'conversations.create',
    'conversations.open', 'images.clear', 'settings.apply', 'memory.write', 'goals.resume', 'thinking.set'])
def test_gui_mutations_refused_before_side_effects(runtime, operation):
    prepare(runtime)
    original = list(runtime.conversation)
    with pytest.raises(ValueError, match='hold'):
        gui_rpc.dispatch(runtime, {'type': 'gui.' + operation, 'index': 0, 'message': 'replacement'})
    assert runtime.conversation == original


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['backend.set', 'models.load', 'tools.execute', 'mcp.connect'])
async def test_async_management_cannot_bypass_hold(runtime, operation):
    prepare(runtime)
    with pytest.raises(ValueError, match='hold'):
        await gui_rpc.async_dispatch(runtime, {'type': 'gui.' + operation})
    assert not getattr(runtime, '_gui_management_busy', False)


def test_control_dispatch_roundtrip_and_no_release_replay(checkpoint_contract, runtime):
    ident = str(uuid4())
    state = gui_rpc.dispatch(runtime, {'type': 'gui.checkpoint.prepare', 'checkpoint_id': ident, 'disposition': 'idle'})
    assert state['held']
    assert gui_rpc.dispatch(runtime, {'type': 'gui.checkpoint.flush', 'checkpoint_id': ident})['boundary_ready']
    runtime._pending_input.append({'content': 'accepted earlier'})
    state = gui_rpc.dispatch(runtime, {'type': 'gui.checkpoint.release', 'checkpoint_id': ident, 'reconciled': True})
    assert not state['held']
    assert runtime._pending_input == [{'content': 'accepted earlier'}]


@pytest.mark.asyncio
async def test_stream_and_hook_admission_do_not_start_work(runtime):
    from litetui.turn_deferral import STREAM_DEFERRED
    prepare(runtime)
    runtime._await_mcp_maintenance = AsyncMock()
    assert await LiteTUI._stream.__wrapped__(runtime) is STREAM_DEFERRED
    runtime._await_mcp_maintenance.assert_not_called()
    item = {'content': 'already accepted'}
    assert not await hook_host.admit_prompt(runtime, item)
    assert not hook_host.accept_prompt(runtime, item)
    hook_host.start_prompt(runtime, item)
    assert runtime._pending_input == [item]
    assert not await hook_host.queued_prompt(runtime)
    LiteTUI._flush_pending_input(runtime)
    assert runtime._pending_input == [item]


@pytest.mark.asyncio
async def test_hold_during_maintenance_wait_prevents_stream(runtime):
    from litetui.turn_deferral import STREAM_DEFERRED
    async def maintenance():
        prepare(runtime)
    runtime._await_mcp_maintenance = maintenance
    assert await LiteTUI._stream.__wrapped__(runtime) is STREAM_DEFERRED


def test_explicit_compaction_permit_is_handoff_bound_and_single_use(runtime):
    state = prepare(runtime)
    handoff = runtime._agent_session.memory_root / 'handoff.md'
    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    assert not checkpoint.compaction_permitted(runtime)
    with pytest.raises(ValueError):
        checkpoint.authorize_compaction(runtime, state['checkpoint_id'], 'wrong')
    checkpoint.authorize_compaction(runtime, state['checkpoint_id'], digest)
    assert checkpoint.compaction_permitted(runtime)
    handoff.write_text('Changed handoff', encoding='utf-8')
    assert not checkpoint.compaction_permitted(runtime)
    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    checkpoint.authorize_compaction(runtime, state['checkpoint_id'], digest)
    assert checkpoint.compaction_permitted(runtime, consume=True)
    assert not checkpoint.compaction_permitted(runtime)


@pytest.mark.parametrize('damage', ['delete', 'corrupt'])
def test_missing_or_corrupt_hold_does_not_ack_or_release(checkpoint_contract, runtime, damage):
    state = prepare(runtime)
    checkpoint.flush(runtime, state['checkpoint_id'])
    path = runtime._restart_checkpoint.path
    if damage == 'delete':
        path.unlink()
    else:
        path.write_text('{broken', encoding='utf-8')
    assert checkpoint.is_held(runtime)
    observed = checkpoint.flush(runtime, state['checkpoint_id'])
    assert not observed['boundary_ready'] and 'hold-record-changed' in observed['blockers']
    with pytest.raises(ValueError, match='record changed'):
        checkpoint.release(runtime, state['checkpoint_id'], reconciled=True)


def test_failed_release_preserves_memory_gate(runtime, monkeypatch):
    state = prepare(runtime)
    monkeypatch.setattr(checkpoint, '_write', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        checkpoint.release(runtime, state['checkpoint_id'], reconciled=True)
    assert checkpoint.is_held(runtime)
    assert not checkpoint.status(runtime)['boundary_ready']


def test_crash_between_boundary_and_state_does_not_publish_ack(checkpoint_contract, runtime, monkeypatch):
    state = prepare(runtime)
    writer = checkpoint._write
    def fail_state(path, value):
        if path.name == checkpoint.STATE_NAME:
            raise OSError('state write failed')
        writer(path, value)
    monkeypatch.setattr(checkpoint, '_write', fail_state)
    with pytest.raises(OSError):
        checkpoint.flush(runtime, state['checkpoint_id'])
    assert len(list(runtime.convo_dir.glob('restart-boundary-*.json'))) == 1
    assert not checkpoint.status(runtime)['boundary_ready']
    del runtime._restart_checkpoint
    assert checkpoint.is_held(runtime)
    assert not checkpoint.status(runtime)['boundary_ready']
    monkeypatch.setattr(checkpoint, '_write', writer)
    assert checkpoint.flush(runtime, state['checkpoint_id'])['boundary_ready']


def test_retry_rejects_preexisting_incorrect_boundary(checkpoint_contract, runtime):
    state = prepare(runtime)
    _, revision = checkpoint._files(runtime)
    path = runtime.convo_dir / f"restart-boundary-{state['checkpoint_id']}-{runtime._restart_checkpoint.runtime_id}-{revision}.json"
    path.write_text(json.dumps({'observed_at': 'forged', 'files': []}), encoding='utf-8')
    with pytest.raises(ValueError, match='Existing boundary'):
        checkpoint.flush(runtime, state['checkpoint_id'])
    assert not checkpoint.status(runtime)['boundary_ready']


@pytest.mark.asyncio
async def test_inbox_hold_does_not_claim_mail(runtime, monkeypatch):
    from litetui import app as app_module
    from litetui import harness, local_rpc
    prepare(runtime)
    monkeypatch.setattr(app_module, '_INBOX_SETTLE_S', 0)
    monkeypatch.setattr(app_module, '_sync_seat_resolution', lambda app: None)
    monkeypatch.setattr(harness, 'POLL_SECONDS', 0.001)
    monkeypatch.setattr(local_rpc.LocalRpc, 'start', AsyncMock())
    runtime._register_owned_startup = AsyncMock(return_value=True)
    runtime._resumed_seat_name = None
    runtime._system = lambda text: None
    runtime._update_header = lambda: None
    runtime._sync_fleet_identity = lambda: None
    runtime._fleet_identity_sentence = lambda: 'fixture'
    unread = ['unread native mail']
    runtime.seat.poll = lambda: unread.pop()
    refreshed = asyncio.Event()
    loop = asyncio.get_running_loop()
    runtime.seat.refresh_name = lambda: loop.call_soon_threadsafe(refreshed.set)
    worker = asyncio.create_task(LiteTUI._inbox_monitor.__wrapped__(runtime))
    try:
        await asyncio.wait_for(refreshed.wait(), 3)
        assert unread == ['unread native mail']
        assert runtime._restart_inbox_ready
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert not runtime._restart_inbox_ready


def test_inflight_native_poll_blocks_boundary(runtime):
    runtime._restart_poll_inflight = True
    state = prepare(runtime)
    assert 'unsupported-pending:_restart_poll_inflight' in checkpoint.flush(runtime, state['checkpoint_id'])['blockers']
    runtime._restart_poll_inflight = False
    runtime._pending_input.append({'content': 'mail claimed before hold'})
    assert not checkpoint.flush(runtime, state['checkpoint_id'])['boundary_ready']

@pytest.mark.asyncio
async def test_authenticated_local_rpc_uses_existing_dispatcher(runtime):
    from litetui.local_rpc import LocalRpc
    rpc = LocalRpc(runtime)
    rpc.endpoint = {'agent_id': runtime.seat.agent_id, 'nonce': str(uuid4())}
    reader = asyncio.StreamReader()
    auth = {**rpc.endpoint, 'token': rpc.secret}
    cmd = {'id': 'fixture-prepare', 'type': 'gui.checkpoint.prepare', 'checkpoint_id': str(uuid4()), 'disposition': 'idle'}
    reader.feed_data((json.dumps(auth) + '\n' + json.dumps(cmd) + '\n').encode())
    reader.feed_eof()
    class Writer:
        def __init__(self):
            self.responses = []
        def get_extra_info(self, name):
            return ('127.0.0.1', 1)
        def write(self, raw):
            self.responses.append(json.loads(raw))
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    writer = Writer()
    await rpc._client(reader, writer)
    assert writer.responses[0]['type'] == 'authenticated'
    assert writer.responses[1]['ok']
    assert writer.responses[1]['result']['held']
    assert not rpc.clients and not rpc.requests


@pytest.mark.asyncio
async def test_native_provider_input_doors_refuse_new_work(runtime):
    from litetui.claude_turn import prepare_input, queue_ready
    from litetui.codex_steering import HostSteering
    prepare(runtime)
    item = {'content': 'held native input'}
    runtime._pending_input = [item]
    server = type('Server', (), {'request': AsyncMock()})()
    host = HostSteering(runtime, server, 'thread', 'turn', {}, lambda: None)
    await host.run()
    server.request.assert_not_called()
    assert '_codex_entry' not in item
    assert not queue_ready(runtime, item)
    with pytest.raises(ValueError, match='hold'):
        prepare_input(runtime, item['content'], 'strict', 'queued')


@pytest.mark.parametrize('message,blocker', [
    ({'role': 'user', 'codex_delivery': {'state': 'uncertain'}}, 'uncertain-codex-delivery'),
    ({'role': 'user', 'provider_metadata': {'steering': [{'state': 'sending'}]}}, 'pending-codex-delivery'),
])
def test_uncertain_native_delivery_blocks_even_without_live_worker(runtime, message, blocker):
    runtime.conversation.append(message)
    runtime.store.record_msg(message)
    state = prepare(runtime)
    assert blocker in checkpoint.flush(runtime, state['checkpoint_id'])['blockers']


def test_submit_handlers_retain_draft(runtime):
    from types import SimpleNamespace
    prepare(runtime)
    draft = SimpleNamespace(value='draft')
    runtime.query_one = lambda *a: draft
    runtime._refuse_submit = lambda *a: None
    LiteTUI.handle_submit(runtime, SimpleNamespace(value='draft', input=draft))
    LiteTUI.action_submit_alt(runtime)
    assert draft.value == 'draft'


def test_missing_contract_fails_without_boundary(runtime, monkeypatch):
    import sys
    state = prepare(runtime)
    monkeypatch.setitem(sys.modules, 'liteharness.checkpoint_contract', None)
    with pytest.raises(checkpoint.CheckpointError, match='contract is unavailable'):
        checkpoint.flush(runtime, state['checkpoint_id'])
    assert checkpoint.is_held(runtime)
    assert not checkpoint.status(runtime)['boundary_ready']
    assert not list(runtime.convo_dir.glob('restart-boundary-*.json'))


@pytest.mark.asyncio
async def test_hold_while_poll_is_inflight_preserves_claimed_input(runtime, monkeypatch):
    from litetui import app as app_module
    from litetui import harness, local_rpc
    monkeypatch.setattr(app_module, '_INBOX_SETTLE_S', 0)
    monkeypatch.setattr(app_module, '_sync_seat_resolution', lambda app: None)
    monkeypatch.setattr(harness, 'POLL_SECONDS', 0.001)
    monkeypatch.setattr(local_rpc.LocalRpc, 'start', AsyncMock())
    runtime._register_owned_startup = AsyncMock(return_value=True)
    runtime._resumed_seat_name = None
    runtime._system = lambda text: None
    runtime._update_header = lambda: None
    runtime._sync_fleet_identity = lambda: None
    runtime._fleet_identity_sentence = lambda: 'fixture'
    runtime.seat.poll = lambda: [{'content': 'claimed before hold'}]
    runtime.seat.refresh_name = lambda: None
    entered, finish, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def thread_call(fn, *args):
        if fn is runtime.seat.poll:
            entered.set()
            await finish.wait()
        return fn(*args)
    monkeypatch.setattr(app_module.asyncio, 'to_thread', thread_call)
    def receive(item):
        hook_host.start_prompt(runtime, item)
        received.set()
    runtime._receive_mail = receive
    worker = asyncio.create_task(LiteTUI._inbox_monitor.__wrapped__(runtime))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        state = prepare(runtime)
        assert 'unsupported-pending:_restart_poll_inflight' in checkpoint.flush(runtime, state['checkpoint_id'])['blockers']
        finish.set()
        await asyncio.wait_for(received.wait(), 3)
        assert runtime._pending_input == [{'content': 'claimed before hold'}]
        assert not checkpoint.flush(runtime, state['checkpoint_id'])['boundary_ready']
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_compact_dispatch_requires_pinned_handoff_before_existing_command(runtime):
    state = prepare(runtime)
    commands = []
    runtime._handle_command = commands.append
    cmd = {'type': 'gui.conversations.compact', 'checkpoint_id': state['checkpoint_id'],
           'handoff_sha256': hashlib.sha256((runtime._agent_session.memory_root / 'handoff.md').read_bytes()).hexdigest()}
    assert gui_rpc.dispatch(runtime, cmd)['accepted']
    assert commands == ['/compact']
    assert checkpoint.compaction_permitted(runtime, consume=True)
    assert not checkpoint.compaction_permitted(runtime)


@pytest.mark.parametrize('terminal', ['done', 'failed', 'killed', 'lost'])
def test_terminal_task_requires_native_row_and_log(checkpoint_contract, runtime, terminal):
    from litetui import tasks
    task = tasks.Task(id=str(uuid4()), tool='powershell', label='settled',
                      convo_id=runtime.convo_id, started=1, state=terminal, ended=2)
    runtime.bg_tasks = {task.id: task}
    state = prepare(runtime)
    assert 'task-persistence-not-ready' in checkpoint.flush(runtime, state['checkpoint_id'])['blockers']
    tasks.save([task], runtime.convo_dir, agent_session=runtime._agent_session)
    path = tasks.log_path(task, runtime._agent_session.store.data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('settled outcome', encoding='utf-8')
    assert checkpoint.flush(runtime, state['checkpoint_id'])['boundary_ready']
    task.label = 'not durably saved'
    assert 'task-persistence-not-ready' in checkpoint.status(runtime)['blockers']
    assert not checkpoint.status(runtime)['boundary_ready']


def test_native_task_migration_marker_is_covered(checkpoint_contract, runtime):
    from litetui import tasks
    (runtime.convo_dir / tasks.MIGRATED).write_text('{}', encoding='utf-8')
    state = prepare(runtime)
    saved = checkpoint.flush(runtime, state['checkpoint_id'])
    assert saved['boundary_ready']
    from pathlib import Path
    evidence = json.loads(Path(saved['receipt']['reference']).read_text(encoding='utf-8'))
    assert tasks.MIGRATED in [Path(f['path']).name for f in evidence['files']]


@pytest.mark.asyncio
async def test_already_running_local_management_blocks_ack_without_cancellation(runtime, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    async def management(app, cmd):
        entered.set()
        await finish.wait()
        return {'completed': True}
    monkeypatch.setattr(gui_rpc, '_async_dispatch', management)
    worker = asyncio.create_task(gui_rpc.async_dispatch(runtime, {'type': 'gui.models.load'}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        state = prepare(runtime)
        assert state['disposition'] == 'held'
        assert 'unsupported-pending:_gui_management_busy' in checkpoint.flush(runtime, state['checkpoint_id'])['blockers']
        assert not worker.done()
        finish.set()
        assert (await worker)['completed']
        assert not runtime._gui_management_busy
    finally:
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
