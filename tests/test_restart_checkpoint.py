"""Synthetic owned runtime: no provider, transport, registry or live fleet."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from litetui import restart_checkpoint as checkpoint
from litetui.agent_ownership import AgentSession
from litetui.agent_store import AgentStore
from litetui.conversation import ConversationRepository


@pytest.fixture
def checkpoint_contract():
    """Optional cross-repository contract: absence never becomes a fake receipt."""
    return pytest.importorskip("liteharness.checkpoint_contract")


@pytest.fixture
def runtime(tmp_path):
    session = AgentSession.create_fresh(AgentStore(tmp_path), name='Checkpoint-Fixture',
        agent_id=str(uuid4()), backend='lmstudio', model='fixture-model', thinking_level='high')
    store = ConversationRepository(agent_session=session)
    store.stage(session.initial_conversation_id)
    store.pending = False
    store.acquire()
    messages = [{'role': 'user', 'content': 'fixture'}, {'role': 'assistant', 'content': 'idle'}]
    store.write_record({'type': 'snapshot', 'messages': messages})
    (store.convo_dir / 'settings.json').write_text('{}', encoding='utf-8')
    (session.memory_root / 'handoff.md').write_text('Waiting; do not replay work.', encoding='utf-8')
    app = SimpleNamespace(_agent_session=session, store=store, convo_id=store.convo_id,
        convo_dir=store.convo_dir, conversation=messages, workers=[], screen_stack=[object()],
        bg_tasks={}, _pending_input=[], pending_image=None, _pending_tool_images=[], rejected_prompts=[],
        _chat_running=lambda: False, query_one=lambda *a: SimpleNamespace(value=''),
        backend=SimpleNamespace(name='lmstudio', owns_native_turns=False), model_id='fixture-model',
        thinking_level='high', _gui_connection_success=True, _restart_inbox_ready=True, is_running=True,
        seat=SimpleNamespace(registered=True, agent_id=session.authority.agent_id, name=session.authority.name))
    yield app
    store.release()
    session.release()


def prepare(app, ident=None):
    return checkpoint.prepare(app, ident or str(uuid4()), disposition='idle')


def test_idle_hold_flush_and_card_a_receipt(checkpoint_contract, runtime):
    from liteharness.checkpoint_contract import Receipt, decode
    result = prepare(runtime)
    assert result['held'] and not result['boundary_ready']
    saved = checkpoint.flush(runtime, result['checkpoint_id'])
    assert saved['boundary_ready']
    receipt = decode(Receipt, saved['receipt'])
    assert receipt.kind == 'boundary' and receipt.assurance == 'native' and receipt.durable
    assert receipt.seat_id == runtime.seat.agent_id
    boundary = json.loads(Path(receipt.reference).read_text())
    assert 'convo.jsonl' in [Path(f['path']).name for f in boundary['files']]
    assert boundary['coverage'] == 'litetui-owned-files-only'
    assert checkpoint.flush(runtime, result['checkpoint_id'])['receipt'] == saved['receipt']


def test_mid_turn_holds_new_work_without_cancellation(checkpoint_contract, runtime):
    runtime._chat_running = lambda: True
    runtime._stop_requested = False
    hold = prepare(runtime)
    assert hold['disposition'] == 'held' and checkpoint.is_held(runtime)
    assert not runtime._stop_requested
    assert not checkpoint.flush(runtime, hold['checkpoint_id'])['boundary_ready']
    runtime._chat_running = lambda: False
    assert checkpoint.flush(runtime, hold['checkpoint_id'])['boundary_ready']


@pytest.mark.parametrize('field,value', [('_pending_input', [{'content': 'accepted but unsent'}]),
    ('pending_image', 'in-memory-image'), ('_pending_tool_images', [('image', 'data')]),
    ('_approval_waiters', {'approval': object()}), ('_rpc_pending_asks', {'question': object()}),
    ('_claude_compact_resume', object()), ('rejected_prompts', [{'content': 'retained'}])])
def test_unserialized_state_is_a_named_blocker(runtime, field, value):
    setattr(runtime, field, value)
    hold = prepare(runtime)
    state = checkpoint.flush(runtime, hold['checkpoint_id'])
    assert not state['boundary_ready']
    assert any(field in b for b in state['blockers'])


def test_draft_is_not_silently_lost(runtime):
    runtime.query_one = lambda *a: SimpleNamespace(value='unsent draft')
    hold = prepare(runtime)
    state = checkpoint.flush(runtime, hold['checkpoint_id'])
    assert 'draft-input' in state['blockers']
    assert runtime.query_one().value == 'unsent draft'


def test_changed_file_invalidates_ack(checkpoint_contract, runtime):
    hold = prepare(runtime)
    assert checkpoint.flush(runtime, hold['checkpoint_id'])['boundary_ready']
    message = {'role': 'user', 'content': 'later'}
    runtime.conversation.append(message)
    runtime.store.write_record({'type': 'msg', 'message': message})
    state = checkpoint.status(runtime)
    assert not state['boundary_ready']
    assert 'boundary-changed' in state['blockers']


def test_cancel_ownership_and_no_automatic_resume(runtime):
    hold = prepare(runtime)
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.release(runtime, str(uuid4()), reconciled=True)
    with pytest.raises(checkpoint.CheckpointError):
        prepare(runtime)
    assert checkpoint.is_held(runtime)
    runtime._pending_input.append({'content': 'held'})
    state = checkpoint.release(runtime, hold['checkpoint_id'], reconciled=True)
    assert not state['held']
    assert runtime._pending_input == [{'content': 'held'}]
    assert checkpoint.release(runtime, hold['checkpoint_id'], reconciled=True) == state


def test_restart_keeps_gate_but_does_not_reuse_live_ready_ack(checkpoint_contract, runtime):
    hold = prepare(runtime)
    checkpoint.flush(runtime, hold['checkpoint_id'])
    del runtime._restart_checkpoint
    assert checkpoint.is_held(runtime)
    assert not checkpoint.status(runtime)['boundary_ready']
    assert checkpoint.status(runtime)['readiness'] == 'ready-idle'


def test_prepare_failure_has_no_ack_and_stays_held_in_process(runtime, monkeypatch):
    monkeypatch.setattr(checkpoint.os, 'fsync', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        prepare(runtime)
    assert checkpoint.is_held(runtime)


def test_failed_flush_never_claims_boundary_ready(checkpoint_contract, runtime, monkeypatch):
    hold = prepare(runtime)
    monkeypatch.setattr(checkpoint.os, 'fsync', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        checkpoint.flush(runtime, hold['checkpoint_id'])
    assert checkpoint.is_held(runtime)
    assert not checkpoint.status(runtime)['boundary_ready']


def test_native_provider_durability_is_explicitly_unsupported(runtime):
    runtime.backend = SimpleNamespace(name='claude', owns_native_turns=True)
    hold = prepare(runtime)
    state = checkpoint.flush(runtime, hold['checkpoint_id'])
    assert not state['boundary_ready']
    assert 'provider-durability-unsupported:claude' in state['blockers']


def test_readonly_status_never_initializes_state(runtime):
    before = set(runtime.convo_dir.iterdir())
    assert not checkpoint.status(runtime)['held']
    assert not (runtime._agent_session.memory_root / checkpoint.STATE_NAME).exists()
    assert set(runtime.convo_dir.iterdir()) == before
