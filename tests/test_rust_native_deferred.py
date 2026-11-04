"""Actual owned source discard preserves historical evidence and publication holds."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rust_background_input import app as _app
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_deferred_evidence import deferred as _deferred
from textual.worker import WorkerFailed

from litetui import background_input as work
from litetui import native_deferred as recovery
from litetui import native_schedule, native_work, rpc

app, switch_app, deferred = _app, _switch_app, _deferred


@pytest_asyncio.fixture
async def held(app, deferred):
    _, document, _ = deferred
    document['record']['target'] = {key: work.identity(app)[key] for key in ('session_id', 'agent_id')}
    document['record']['epoch']['identity']['agent_id'] = work.identity(app)['agent_id']
    path = app.convo_dir / work.DEFERRED_FILENAME
    path.write_bytes(work.encoded(document))
    app._launch_options.work_lifecycle_version = 1
    descriptor = work.capture(app, command(app))
    cmd = command(app, type='gui.work.native_activate', instance=descriptor['instance'],
                  activation_token=descriptor['activation_token'], scheduled_transition_version=1)
    result = work.activate(app, cmd)
    ticket = native_schedule.prepare_publication(app, cmd, result)
    native_schedule.finish_publication(app, ticket)
    return app, document, path


def snapshot(app):
    return native_work.snapshot(app, command(app, type='gui.work.native_snapshot'))


def reviewed(app):
    snap = snapshot(app)
    selected = snap['deferred_job']
    cmd = command(app, type=recovery.OPERATIONS[0], deferred_version=1,
                  instance=snap['instance'], snapshot=snap['snapshot'],
                  invocation_id=selected['record']['invocation_id'], journal_sha256=selected['journal_sha256'])
    return snap, recovery.validate(app, cmd)


def discard_command(app, review):
    return command(app, type=recovery.OPERATIONS[1], id='discard-exact', deferred_version=1,
                   **{key: review[key] for key in ('instance', 'snapshot', 'review', 'invocation_id', 'journal_sha256')})


def readback_command(app, result):
    return command(app, type=recovery.OPERATIONS[2], id='readback-exact', deferred_version=1,
                   **{key: result[key] for key in ('instance', 'operation_id', 'review', 'invocation_id', 'after_sha256')})


def publish(app, cmd, result):
    ticket = recovery.prepare_publication(app, cmd, result)
    recovery.finish_publication(app, ticket)
    return ticket


def export_flow(name, **value):
    if destination := os.environ.get('LITETUI_DEFERRED_FIXTURE'):
        path = Path(destination)
        document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        document[name] = value
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.asyncio
@pytest.mark.parametrize('state,input_id', [('pending', None), ('uncertain', '00000000-0000-0000-0000-000000000045')])
async def test_discard_preserves_input_and_record_until_published_independent_readback(held, state, input_id):
    app, document, path = held
    document.update(state=state, input_id=input_id)
    path.write_bytes(work.encoded(document))
    before, transcript = path.read_bytes(), app.convo_path.read_bytes()
    value = work.bridge(app)
    value.error = 'Independent provider uncertainty must survive'
    value.uncertain_inputs.add('independent-input')
    snap, review = reviewed(app)
    assert review['record'] == document['record']
    assert review['document_state'] == state and review['input_id'] == input_id
    assert path.read_bytes() == before
    cmd = discard_command(app, review)
    result = recovery.discard(app, cmd)
    assert result['status'] == 'discarded' and not result['close_required']
    final_document = json.loads(path.read_bytes())
    assert final_document['record'] == document['record']
    assert final_document['input_id'] == input_id
    assert final_document['version'] == 2 and final_document['state'] == 'discarded'
    assert work.deferred_evidence(app) == []
    assert recovery.mutation_hold(app) and snapshot(app)['deferred_job'] is not None
    read_cmd = readback_command(app, result)
    with pytest.raises(ValueError, match='published'):
        recovery.readback(app, read_cmd)
    publish(app, cmd, result)
    readback = recovery.readback(app, read_cmd)
    assert recovery.mutation_hold(app)
    ticket = publish(app, read_cmd, readback)
    assert recovery.mutation_hold(app) is None
    after = snapshot(app)
    assert after['deferred_job'] is None
    assert value.error == 'Independent provider uncertainty must survive'
    assert value.uncertain_inputs == {'independent-input'}
    assert app.convo_path.read_bytes() == transcript and not app._rewrite_test_requests
    with pytest.raises(ValueError, match='consumed'):
        recovery.finish_publication(app, ticket)
    with pytest.raises(ValueError, match='review'):
        recovery.discard(app, cmd)
    export_flow(state, snapshot=snap, review=review, result=result, readback=readback,
                after_snapshot=after, before_journal=before.decode(), after_journal=path.read_text(encoding='utf-8'))


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['bytes', 'record', 'capture', 'owner', 'review', 'version'])
async def test_review_drift_refuses_before_write_and_consumes_review(held, drift):
    app, document, path = held
    _, review = reviewed(app)
    cmd = discard_command(app, review)
    if drift == 'bytes':
        path.write_text(json.dumps(document, indent=2), encoding='utf-8')
    elif drift == 'record':
        document['record']['reason'] = 'Changed historical reason'
        path.write_bytes(work.encoded(document))
    elif drift == 'capture':
        snapshot(app)
    elif drift == 'owner':
        app.settings = copy.deepcopy(app.settings)
    elif drift == 'review':
        cmd['review'] = 'f' * 64
    else:
        cmd['deferred_version'] = True
    before = path.read_bytes()
    result = recovery.discard(app, cmd)
    assert result['status'] == 'refused' and result['after_sha256'] is None
    assert not result['close_required'] and path.read_bytes() == before
    assert recovery.mutation_hold(app) is None
    with pytest.raises(ValueError, match='review'):
        recovery.discard(app, cmd)


@pytest.mark.asyncio
@pytest.mark.parametrize('active', ['live', 'claimed', 'worker', 'callback', 'input', 'transition', 'review', 'capability'])
async def test_live_ownership_conflicting_review_and_unnegotiated_recovery_refuse(held, active):
    app, document, path = held
    before = path.read_bytes()
    if active in ('live', 'claimed'):
        app._native_work_deferred = work.DeferredJob(document, before, app._agent_session, app.store,
            path, object(), claimed=active == 'claimed')
    elif active == 'worker':
        app._native_work_jobs = {'job-a': SimpleNamespace(is_finished=False)}
    elif active == 'callback':
        app._native_work_job_owners = {'callback': object()}
    elif active == 'input':
        app._pending_input = [{'content': 'Reserved original input'}]
    elif active == 'transition':
        app._native_schedule_pending = object()
    elif active == 'capability':
        work.bridge(app).scheduled_transition_version = None
    snap = snapshot(app)
    if active == 'capability':
        assert 'deferred_job' not in snap
        return
    selected = snap['deferred_job']
    if active == 'review':
        app._native_work_inspection.review = b'conflicting review'
    else:
        assert not selected['discard']['eligible']
    with pytest.raises(ValueError):
        recovery.validate(app, command(app, type=recovery.OPERATIONS[0], deferred_version=1,
            instance=snap['instance'], snapshot=snap['snapshot'], invocation_id=selected['record']['invocation_id'],
            journal_sha256=selected['journal_sha256']))
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('after_replace', [False, True])
async def test_write_uncertainty_keeps_barrier_and_never_retries(held, monkeypatch, after_replace):
    app, _, path = held
    _, review = reviewed(app)
    before = path.read_bytes()
    original = work._write_deferred
    calls = []
    def fail(app, document, expected):
        calls.append(expected)
        if after_replace:
            original(app, document, expected)
        raise OSError('Controlled deferred disk failure')
    monkeypatch.setattr(work, '_write_deferred', fail)
    cmd = discard_command(app, review)
    result = recovery.discard(app, cmd)
    assert result['status'] == 'uncertain' and result['close_required']
    assert recovery.mutation_hold(app) and snapshot(app)['deferred_job'] is not None
    assert len(calls) == 1
    assert (path.read_bytes() != before) == after_replace
    with pytest.raises(ValueError, match='review'):
        recovery.discard(app, cmd)
    assert len(calls) == 1


@pytest.mark.parametrize('fault', ['version', 'state', 'missing', 'extra', 'boolean', 'reason', 'identity', 'hash', 'record', 'duplicate'])
def test_terminal_v2_is_strict_and_never_hides_malformed_evidence(deferred, fault):
    app, document, path = deferred
    document.update(version=2, state='discarded', retirement={
        'operation_id': 'discard-op', 'review': 'a'*64, 'previous_sha256': 'b'*64,
        'previous_state': 'pending', 'identity': work.identity(app), 'reason': 'user_discarded',
        'no_replay': True, 'effects_undone': False})
    if fault == 'version': document['version'] = 3
    elif fault == 'state': document['state'] = 'pending'
    elif fault == 'missing': document['retirement'].pop('effects_undone')
    elif fault == 'extra': document['retirement']['replayed'] = False
    elif fault == 'boolean': document['retirement']['no_replay'] = 1
    elif fault == 'reason': document['retirement']['reason'] = 'delivered'
    elif fault == 'identity': document['retirement']['identity']['session_id'] = '00000000-0000-0000-0000-000000000099'
    elif fault == 'hash': document['retirement']['previous_sha256'] = 'X'*64
    elif fault == 'record': document['record']['job_sha256'] = 'c'*64
    raw = work.encoded(document)
    if fault == 'duplicate': raw = raw.replace(b'"version":2', b'"version":2,"version":2')
    path.write_bytes(raw)
    with pytest.raises((ValueError, TypeError)):
        work.deferred_evidence(app)
    assert path.read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['result', 'readback'])
async def test_actual_rpc_response_write_failure_retains_per_operation_barrier(held, monkeypatch, phase):
    app, _, _ = held
    _, review = reviewed(app)
    cmd = discard_command(app, review)
    if phase == 'readback':
        result = recovery.discard(app, cmd)
        publish(app, cmd, result)
        cmd = readback_command(app, result)
    replies = []
    def emit(row):
        if row.get('id') == cmd['id']:
            assert row['ok'] is True, row
            assert recovery.mutation_hold(app)
            replies.append(copy.deepcopy(row))
            raise OSError('Controlled response publication failure')
    monkeypatch.setattr(rpc, 'rpc_emit', emit)
    rpc._dispatch(app, cmd)
    results = await asyncio.wait_for(asyncio.gather(*(w.wait() for w in app.workers
        if w.group == 'gui-management'), return_exceptions=True), 3)
    assert len(results) == len(replies) == 1
    assert isinstance(results[0], WorkerFailed)
    assert recovery.mutation_hold(app)
    assert snapshot(app)['deferred_job'] is not None
    assert not app._rewrite_test_requests

@pytest.mark.asyncio
async def test_discard_keeps_real_recovered_work_input_and_ledger_bytes(held):
    app, document, _ = held
    value = work.bridge(app)
    producer_ref = {'producer_id': work.ident(), 'kind': 'cron', 'object_id': 'job-a',
        'producer_session_id': app.convo_id, 'owner_instance': 'old-runtime', 'owner_created': None,
        'generation': 1, 'causal_action_id': None, 'causal_operation_id': None, 'causal_execution_id': None}
    producer = work.Producer(producer_ref, phase='retired')
    payload = copy.deepcopy(document['record']['payload'])
    input_ref = {'input_id': work.ident(), 'producer_id': producer_ref['producer_id'],
        'payload_sha256': work.digest(work.encoded(payload)), 'source': 'scheduled',
        'requested_profile': 'strict', 'delivery_session_id': app.convo_id, 'queue_revision': 1}
    entry = work.Input(input_ref, payload, {**payload, '_work_input_id': input_ref['input_id']}, producer,
        state='uncertain', reason='Prior delivery unknown', recovered=True,
        epoch={'instance': 'c'*64, 'identity': work.identity(app), 'provider': value.provider})
    value.inputs[input_ref['input_id']] = entry
    value.persist()
    work.validate_document(json.loads(value.path.read_bytes()))
    ledger = value.path.read_bytes()
    value.error = 'Prior delivery unknown'
    value.uncertain_inputs.add(input_ref['input_id'])
    _, review = reviewed(app)
    cmd = discard_command(app, review)
    result = recovery.discard(app, cmd)
    assert result['status'] == 'discarded'
    publish(app, cmd, result)
    read_cmd = readback_command(app, result)
    publish(app, read_cmd, recovery.readback(app, read_cmd))
    assert value.path.read_bytes() == ledger
    assert value.inputs[input_ref['input_id']] is entry and entry.state == 'uncertain'
    assert value.error == 'Prior delivery unknown' and value.uncertain_inputs == {input_ref['input_id']}
    assert work.prompt_refusal(app) == 'Prior delivery unknown'


@pytest.mark.asyncio
async def test_fresh_snapshot_can_replace_abandoned_review_and_invalidates_old_token(held):
    app, _, _ = held
    _, previous = reviewed(app)
    _, current = reviewed(app)
    assert previous['review'] != current['review']
    result = recovery.discard(app, discard_command(app, previous))
    assert result['status'] == 'refused'
    _, current = reviewed(app)
    result = recovery.discard(app, discard_command(app, current))
    assert result['status'] == 'discarded'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['result', 'readback'])
async def test_mutated_result_object_cannot_reserve_publication(held, phase):
    app, _, _ = held
    _, review = reviewed(app)
    cmd = discard_command(app, review)
    result = recovery.discard(app, cmd)
    if phase == 'readback':
        publish(app, cmd, result)
        cmd = readback_command(app, result)
        result = recovery.readback(app, cmd)
    result['review'] = 'd'*64
    with pytest.raises(ValueError, match='response'):
        recovery.prepare_publication(app, cmd, result)
    assert recovery.mutation_hold(app)

@pytest.mark.asyncio
async def test_journal_drift_during_temporary_flush_preserves_foreign_bytes(held, monkeypatch):
    app, document, path = held
    _, review = reviewed(app)
    changed = copy.deepcopy(document)
    changed['record']['reason'] = 'Concurrent external journal edit'
    foreign = work.encoded(changed)
    original = work.os.fsync
    def mutate(descriptor):
        original(descriptor)
        path.write_bytes(foreign)
    monkeypatch.setattr(work.os, 'fsync', mutate)
    cmd = discard_command(app, review)
    result = recovery.discard(app, cmd)
    assert result['status'] == 'uncertain' and result['close_required']
    assert result['after_sha256'] is None and recovery.mutation_hold(app)
    assert path.read_bytes() == foreign
    assert not list(path.parent.glob('.native-work-job-*.tmp'))
    assert not app._rewrite_test_requests
