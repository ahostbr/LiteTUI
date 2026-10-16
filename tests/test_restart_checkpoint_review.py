"""Regression evidence for the two T0558-B integration review findings."""
import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_restart_checkpoint as helpers

from litetui import hook_host, rpc
from litetui import restart_checkpoint as checkpoint
from litetui.app import LiteTUI
from litetui.turn_deferral import STREAM_DEFERRED

runtime = helpers.runtime
prepare = helpers.prepare


@pytest.mark.parametrize('command', [
    {'type': 'prompt', 'message': 'must not disappear', 'tool_profile': 'autonomous'},
    {'type': 'run_command', 'line': '/new'},
    {'type': 'use_skill', 'name': 'fixture'},
    {'type': 'set', 'profile': 'autonomous', 'mode': 'plan'},
    {'type': 'set_model', 'slug': 'another-model'},
    {'type': 'set_thinking', 'level': 'low'},
    {'type': 'set_settings', 'patch': {'model': 'another-model', 'thinking_level': 'low'}},
    {'type': 'jobs.create', 'prompt': 'new work'},
    {'type': 'jobs.delete', 'job_id': 'existing'},
    {'type': 'tasks.kill', 'task_id': 'existing'},
    {'type': 'abort'}, {'type': 'shutdown'},
])
def test_legacy_dispatch_refuses_before_side_effects(runtime, monkeypatch, command):
    prepare(runtime)
    responses = []
    monkeypatch.setattr(rpc, 'rpc_emit', responses.append)
    runtime.set_tool_profile = Mock(side_effect=AssertionError('profile changed'))
    runtime._submit_text = Mock(side_effect=AssertionError('input was falsely admitted'))
    runtime._handle_command = Mock(side_effect=AssertionError('command was falsely acknowledged'))
    runtime.jobs = [SimpleNamespace(id='existing')]
    transcript = runtime.store.convo_path.read_bytes()
    authority = runtime._agent_session.authority
    monkeypatch.setattr(runtime.store, 'acquire', Mock(side_effect=AssertionError('store acquired')))
    rpc._dispatch(runtime, {'id': 'held-request', **command})
    assert len(responses) == 1 and responses[0]['ok'] is False
    assert 'hold' in responses[0]['error']
    assert responses[0]['id'] == 'held-request'
    runtime.set_tool_profile.assert_not_called()
    runtime._submit_text.assert_not_called()
    runtime._handle_command.assert_not_called()
    runtime.store.acquire.assert_not_called()
    assert runtime._agent_session.authority == authority
    assert runtime.store.convo_path.read_bytes() == transcript
    assert [job.id for job in runtime.jobs] == ['existing']


@pytest.mark.asyncio
async def test_legacy_dispatch_keeps_reads_and_pending_callback_completion(runtime, monkeypatch):
    from litetui.tool_approval import ONCE
    prepare(runtime)
    responses = []
    monkeypatch.setattr(rpc, 'rpc_emit', responses.append)
    runtime.jobs = []
    runtime.skills = []
    runtime._rpc_model_state = lambda: {'models': ['fixture-model']}
    future = asyncio.get_running_loop().create_future()
    runtime._approval_waiters = {'approval': future}
    done, result = threading.Event(), []
    runtime._rpc_pending_asks = {'question': (done, result, [])}
    for command in [
        {'type': 'list_models'}, {'type': 'jobs.list'}, {'type': 'list_skills'},
        {'type': 'approve', 'approval_id': 'approval', 'allow': True},
        {'type': 'answer', 'ask_id': 'question', 'action': 'submit', 'answers': []},
    ]:
        rpc._dispatch(runtime, command)
    assert all(response['ok'] for response in responses)
    assert future.done() and future.result() == ONCE
    assert done.is_set() and result
    assert checkpoint.is_held(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['success', 'changed-before', 'changed-during-wait',
                                 'changed-after-step', 'owner-failure', 'compact-failure'])
async def test_actual_held_codex_owner_compaction(case, tmp_path, monkeypatch):
    """Real mounted app, compact worker body, owner STEP 1, hook and stream.

    Provider transport and the external owner Git subprocess are synthetic. The
    admission/stream guards are NOT replaced, nor is _handle_command used as a
    stand-in for execution. No provider or process restart is performed.
    """
    from test_codex_native_tps_wiring import _app, _Chunk

    from litetui import app as app_module
    from litetui import owner_mode

    app = _app([_Chunk(content='Owner handoff written.')])
    app._apply_cli_args = lambda: None  # no live catalogue or launch prompt in this fixture
    transport = app._transport_for_test
    create = transport.create
    events = []
    captured = []
    provider_checked = []
    responses = []
    monkeypatch.setattr(rpc, 'rpc_emit', responses.append)
    monkeypatch.setattr(app_module.model_transport, 'for_app', lambda a: transport)

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app._materialise_convo()
        app.thinking_level = 'high'
        app._agent_session.update_execution(backend='codex', model=app.model_id, thinking_level='high')
        app._append({'role': 'user', 'content': 'Prior task'})
        app._append({'role': 'assistant', 'content': 'Prior task complete'})
        await hook_host.drain_lifecycle(app)
        await pilot.pause()
        app._owner_memory_dir = tmp_path / 'owner'
        app._owner_memory_dir.mkdir()
        handoff = app._agent_session.memory_root / 'handoff.md'
        handoff.write_text('Durable native handoff before same-seat compaction.', encoding='utf-8')
        state = prepare(app)
        assert state['blockers'] == ['provider-durability-unsupported:codex'], state['blockers']
        digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
        checkpoint.authorize_compaction(app, state['checkpoint_id'], digest)

        def handoff_process(memory_dir, agent_id=None, timeout=120.0):
            events.append('owner-gate')
            assert memory_dir == app._owner_memory_dir
            assert agent_id == app._agent_session.authority.agent_id
            if case == 'owner-failure':
                raise OSError('synthetic owner gate failure')
            return True, 'synthetic commit receipt'
        monkeypatch.setattr(owner_mode, 'run_handoff', handoff_process)

        async def provider_create(**kwargs):
            events.append('owner-turn')
            token = app._restart_checkpoint.maintenance
            captured.append(token)
            assert checkpoint.is_held(app)
            assert checkpoint.maintenance_permitted(app, token)
            # Even the compactor's task cannot start another turn without the
            # explicit capability. A child task cannot borrow that capability.
            assert await LiteTUI._stream.__wrapped__(app) is STREAM_DEFERRED
            assert await asyncio.create_task(LiteTUI._stream.__wrapped__(
                app, restart_maintenance=token)) is STREAM_DEFERRED
            rpc._dispatch(app, {'type': 'prompt', 'id': 'unrelated', 'message': 'not maintenance'})
            assert responses[-1]['ok'] is False
            if case == 'changed-after-step':
                handoff.write_text('Changed while existing maintenance turn was running.', encoding='utf-8')
            (app._owner_memory_dir / 'NOW.md').write_text('Synthetic provider STEP 1 write.', encoding='utf-8')
            (app._owner_memory_dir / 'DOMAIN.md').write_text('Synthetic domain update.', encoding='utf-8')
            provider_checked.append(True)
            return await create(**kwargs)
        transport.create = provider_create

        async def compact():
            events.append('native-compact')
            assert checkpoint.is_held(app)
            if case == 'compact-failure':
                raise app_module.model_transport.ProviderError('synthetic compact failure')
        transport.compact = compact
        if case == 'changed-before':
            handoff.write_text('Changed after permission, before worker execution.', encoding='utf-8')
        if case == 'changed-during-wait':
            async def maintenance_wait():
                handoff.write_text('Changed during the stream maintenance wait.', encoding='utf-8')
            app._await_mcp_maintenance = maintenance_wait

        if case == 'owner-failure':
            with pytest.raises(OSError, match='owner gate failure'):
                await LiteTUI._compact.__wrapped__(app)
        else:
            worker = app._compact()
            await worker.wait()
        await pilot.pause()
        assert checkpoint.is_held(app)
        assert not getattr(app._restart_checkpoint, 'maintenance', None)
        assert not checkpoint.compaction_permitted(app)
        assert not app._pending_input  # no refused internal STEP 1 prompt leaked
        assert len(provider_checked) == len(captured)  # provider assertions cannot be swallowed as stream errors
        assert all(not checkpoint.maintenance_permitted(app, token) for token in captured)
        assert not any(message.get('content') == 'not maintenance' for message in app.conversation)
        if case in ('success', 'compact-failure'):
            assert events == ['owner-turn', 'owner-gate', 'native-compact'], app.said
        elif case in ('changed-before', 'changed-during-wait'):
            assert events == []
        else:
            assert events == ['owner-turn', 'owner-gate']
        # A source label is not a capability, even after an authorized compact.
        item = {'content': 'forged maintenance', 'source': 'compact'}
        assert not hook_host.accept_prompt(app, item)
        assert app._pending_input == [item]
        assert await LiteTUI._stream.__wrapped__(app) is STREAM_DEFERRED


@pytest.mark.asyncio
async def test_compaction_capability_is_revoked_on_cancellation(runtime):
    state = prepare(runtime)
    handoff = runtime._agent_session.memory_root / 'handoff.md'
    checkpoint.authorize_compaction(runtime, state['checkpoint_id'], hashlib.sha256(handoff.read_bytes()).hexdigest())
    captured = None
    with pytest.raises(asyncio.CancelledError), checkpoint.compaction_scope(runtime) as permit:
        captured = permit
        assert checkpoint.maintenance_permitted(runtime, permit)
        assert 'compaction-maintenance-active' in checkpoint.inventory(runtime)
        raise asyncio.CancelledError
    assert checkpoint.is_held(runtime)
    assert not checkpoint.maintenance_permitted(runtime, captured)
    assert not checkpoint.compaction_permitted(runtime)
    assert runtime._restart_checkpoint.maintenance is None


@pytest.mark.asyncio
async def test_hold_blocks_new_and_already_scheduled_card_summary(runtime, monkeypatch):
    from litetui import app as app_module
    prepare(runtime)
    runtime.run_worker = Mock()
    runtime.settings = SimpleNamespace(lmstudio_graded_thinking_models=(), compact_max_tokens=10)
    card = SimpleNamespace(summary_done=False, summary='', answer_text='answer', model_name='fixture-model')
    transport = Mock()
    monkeypatch.setattr(app_module.model_transport, 'for_app', transport)
    LiteTUI._kick_card_summary(runtime, card)
    await LiteTUI._summarise_card(runtime, card, 'fixture-model', 'answer')
    runtime.run_worker.assert_not_called()
    transport.assert_not_called()
    assert not card.summary_done
