"""Reviewed durable Work controls with actual owned source storage."""
# Scheduler timestamps intentionally use canonical local wall time.
# ruff: noqa: DTZ005
from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_rust_backend_transition import forbidden
from test_rust_backend_transition import switch_app as _switch_app
from test_rust_work_inspection import common, save_jobs, selected

from litetui import goal_loop, native_work, scheduler
from litetui import native_work_controls as controls

switch_app = _switch_app


def snapshot(app):
    return native_work.snapshot(app, {'type':'gui.work.native_snapshot', 'id':'snapshot', **common(app)})


def review(app, snap, action, target=None, draft=None):
    target_id = target['row']['id'] if target and 'row' in target else target['value']['id'] if target else None
    return controls.validate(app, {'type':'gui.work.native_control_validate', 'id':'review', **common(app),
        **selected(snap), 'action':action, 'target_id':target_id,
        'target_revision':target['revision'] if target else None, 'draft':draft or {}})


def apply(app, value):
    return controls.apply(app, {'type':'gui.work.native_control_apply', 'id':'apply',
        **{k:v for k,v in value.items() if k != 'kind'}})


def readback(app, result):
    return controls.readback(app, {'type':'gui.work.native_control_readback', 'id':'readback',
        **common(app), **selected(result), **{k:result[k] for k in ('review','action_id','receipt')}})


def row(app, section='jobs'):
    return app._native_work_inspection.rows[section][0]


@pytest.mark.asyncio
async def test_job_edit_preserves_siblings_unknown_fields_and_counters_without_execution(switch_app, monkeypatch):
    app=switch_app
    original={**asdict(scheduler.Job(prompt='old', schedule='0 9 * * *', id='selected', run_count=7)), 'future_extension':{'keep':[1,2]}}
    path=save_jobs(app,[original])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'prompt':'edited'})
    assert json.loads(path.read_text()) == [original]
    sibling={**asdict(scheduler.Job(prompt='peer',schedule='* * * * *',id='sibling')), 'peer_extension':True}
    path.write_text(json.dumps([original,sibling]),encoding='utf-8')
    monkeypatch.setattr(app,'_fire_job',forbidden)
    result=apply(app,value)
    assert result['status']=='applied'
    final=json.loads(path.read_text())
    assert final[0]=={**original,'prompt':'edited'} and final[1]==sibling
    assert readback(app,result)['verified'] is True
    scheduler.save(app.jobs,app._agent_session.store.data_root)
    assert json.loads(path.read_text())==final


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['counter','delete','unknown'])
async def test_selected_raw_drift_refuses_before_write(switch_app,change):
    app=switch_app
    original=asdict(scheduler.Job(prompt='old',schedule='0 9 * * *',id='selected'))
    path=save_jobs(app,[original])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'label':'changed'})
    changed=copy.deepcopy(original)
    if change=='counter': changed['run_count']=1
    if change=='unknown': changed['future_extension']='changed'
    path.write_text(json.dumps([] if change=='delete' else [changed]),encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='changed|stale'):
        apply(app,value)
    assert path.read_bytes()==before
    with pytest.raises(ValueError,match='review'):
        apply(app,value)


@pytest.mark.asyncio
async def test_loop_resume_rearms_at_apply_and_goal_activity_refuses(switch_app,monkeypatch):
    app=switch_app
    loop=scheduler.Job.loop(prompt='loop',interval_minutes=15,owner_convo_id=app.convo_id)
    loop.enabled=False
    loop.next_run_at=(datetime.now()-timedelta(days=1)).isoformat()
    save_jobs(app,[loop])
    snap=snapshot(app)
    value=review(app,snap,'loop_enable',row(app,'loops'),{'enabled':True})
    before=datetime.now()
    result=apply(app,value)
    fresh=scheduler.load(app._agent_session.store.data_root)[0]
    assert fresh.next_run_at is not None
    assert datetime.fromisoformat(fresh.next_run_at)>=before+timedelta(minutes=14,seconds=59)
    assert readback(app,result)['verified']
    goal_loop.save_goal(app.convo_dir,goal_loop.GoalState('Preserve goal'))
    snap=snapshot(app)
    monkeypatch.setattr(app,'goal_runtime',SimpleNamespace(_evaluating=True),raising=False)
    before=(app.convo_dir/'goal.json').read_bytes()
    with pytest.raises(ValueError,match='goal|Goal'):
        review(app,snap,'goal_clear',snap['goal'])
    assert (app.convo_dir/'goal.json').read_bytes()==before


def export_flow(name, **value):
    if destination := os.environ.get('LITETUI_WORK_CONTROLS_FIXTURE'):
        path=Path(destination)
        document=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        document[name]=value
        path.write_text(json.dumps(document,indent=2,ensure_ascii=False),encoding='utf-8')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['cron','loop'])
async def test_create_has_source_identity_profile_and_no_execution(switch_app,monkeypatch,kind):
    app=switch_app
    snap=snapshot(app)
    draft={'kind':kind,'prompt':'exact\nconfiguration ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â½ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©','label':'Created by review','enabled':False}
    draft.update({'schedule':'0 9 * * 1-5'} if kind=='cron' else {'interval_minutes':15})
    value=review(app,snap,'job_create',draft=draft)
    assert value['before'] is None and value['after']['run_count']==0
    monkeypatch.setattr(app,'_fire_job',forbidden)
    result=apply(app,value)
    final=readback(app,result)
    assert final['verified'] and final['row']['prompt']==draft['prompt']
    assert final['row']['id']==value['target_id'] and not final['row']['enabled']
    assert not getattr(app,'_native_work_jobs',{})
    export_flow(kind+'_create',snapshot=snap,review=value,result=result,readback=final)


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['goal_pause','goal_clear'])
async def test_goal_controls_preserve_evidence_and_unknown_data(switch_app,action):
    app=switch_app
    goal=goal_loop.GoalState('Objective',id='goal-owned',evaluated_turns=4,last_reason='Evidence retained',missing_evidence=['missing'])
    goal_loop.save_goal(app.convo_dir,goal)
    path=app.convo_dir/goal_loop.GOAL_FILENAME
    original={**json.loads(path.read_text()),'future_extension':{'keep':True}}
    path.write_text(json.dumps(original),encoding='utf-8')
    snap=snapshot(app)
    value=review(app,snap,action,snap['goal'])
    result=apply(app,value)
    final=readback(app,result)
    assert final['verified']
    if action=='goal_clear':
        assert not path.exists() and final['row'] is None
    else:
        assert json.loads(path.read_text())=={**original,'status':'paused'}
        assert final['row']['evaluated_turns']==4
    export_flow(action,snapshot=snap,review=value,result=result,readback=final)


@pytest.mark.asyncio
@pytest.mark.parametrize('action,draft,section',[
    ('job_update',{'label':'Reviewed label'},'jobs'),
    ('job_delete',{},'jobs'),
    ('job_update',{'prompt':'Loop edited'},'loops'),
    ('job_delete',{},'loops'),
    ('loop_enable',{'enabled':False},'loops'),
    ('loop_enable',{'enabled':True},'loops'),
],ids=['cron-edit','cron-delete','loop-edit','loop-delete','loop-pause','loop-resume'])
async def test_export_selected_control_flow(switch_app,action,draft,section):
    app=switch_app
    job=scheduler.Job(prompt='Selected prompt',schedule='0 9 * * *',run_count=7) if section=='jobs' else scheduler.Job.loop(prompt='Selected loop',interval_minutes=15,owner_convo_id=app.convo_id)
    save_jobs(app,[job])
    snap=snapshot(app)
    target=copy.deepcopy(row(app,section))
    value=review(app,snap,action,target,draft)
    result=apply(app,value)
    final=readback(app,result)
    assert final['verified']
    export_flow(section+'_'+action+('_on' if draft.get('enabled') else '_off'),snapshot=snap,target=target,review=value,result=result,readback=final)


@pytest.mark.asyncio
@pytest.mark.parametrize('draft',[
    {'enabled':1},{'prompt':'   '},{'schedule':'bad cron'}, {'run_count':4},
    {'prompt':'x'*65537},{'label':'ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â½ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©'*2049},{'new_conversation':'true'},
],ids=['bool','blank','cron','counter','prompt-budget','label-byte-budget','new-type'])
async def test_bad_drafts_are_pure(switch_app,draft):
    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    before=path.read_bytes()
    with pytest.raises((TypeError,ValueError)):
        review(app,snap,'job_update',row(app),draft)
    assert path.read_bytes()==before


@pytest.mark.asyncio
async def test_goal_evaluator_started_after_review_prevents_stale_clear(switch_app,monkeypatch):
    app=switch_app
    goal=goal_loop.GoalState('Keep evaluated state',id='goal-one')
    goal_loop.save_goal(app.convo_dir,goal)
    app.conversation.append({'role':'user','content':'[goal goal-one] Keep evaluated state'})
    runtime=goal_loop.GoalRuntime(app)
    monkeypatch.setattr(app,'goal_runtime',runtime,raising=False)
    snap=snapshot(app)
    value=review(app,snap,'goal_clear',snap['goal'])
    entered,released=asyncio.Event(),asyncio.Event()
    async def evaluate(state,transcript):
        entered.set()
        await released.wait()
        state.status='paused'
        state.last_reason='Actual evaluator owns this state'
        goal_loop.save_goal(app.convo_dir,state)
    monkeypatch.setattr(runtime,'_evaluate',evaluate)
    task=asyncio.create_task(runtime.on_plain_answer())
    try:
        await asyncio.wait_for(entered.wait(),2)
        before=(app.convo_dir/goal_loop.GOAL_FILENAME).read_bytes()
        with pytest.raises(ValueError,match='evaluator'):
            apply(app,value)
        assert (app.convo_dir/goal_loop.GOAL_FILENAME).read_bytes()==before
    finally:
        released.set()
        await task
    final_goal=goal_loop.load_goal(app.convo_dir)
    assert final_goal is not None and final_goal.last_reason=='Actual evaluator owns this state'


@pytest.mark.asyncio
async def test_goal_queued_continuation_and_foreign_loop_refuse(switch_app):
    app=switch_app
    goal_loop.save_goal(app.convo_dir,goal_loop.GoalState('Keep'))
    snap=snapshot(app)
    app._pending_input.append({'content':'held','goal_continuation':True})
    with pytest.raises(ValueError,match='Goal continuation'):
        review(app,snap,'goal_pause',snap['goal'])
    app._pending_input.clear()
    foreign=scheduler.Job.loop(prompt='foreign',interval_minutes=5,owner_convo_id='11111111-2222-4333-8444-555555555555')
    save_jobs(app,[foreign])
    snap=snapshot(app)
    with pytest.raises(ValueError,match='another conversation'):
        review(app,snap,'loop_enable',row(app,'loops'),{'enabled':True})


@pytest.mark.asyncio
async def test_independent_readback_accepts_only_configuration_preserving_counter_advance(switch_app):
    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *',run_count=7)])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'label':'Changed'})
    result=apply(app,value)
    rows=json.loads(path.read_text())
    rows[0].update(run_count=8,last_fired_slot=scheduler.slot_of(datetime.now()))
    path.write_text(json.dumps(rows),encoding='utf-8')
    assert readback(app,result)['row']['run_count']==8
    rows[0]['prompt']='External change'
    path.write_text(json.dumps(rows),encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):
        readback(app,result)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['echo','refresh','closing','owner'])
async def test_control_review_is_bound_and_single_use(switch_app,monkeypatch,fault):
    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'label':'Changed'})
    before=path.read_bytes()
    if fault=='echo': value['draft']['label']='different'
    if fault=='refresh': snapshot(app)
    if fault=='closing': app._gui_quitting=True
    if fault=='owner': monkeypatch.setattr(app,'settings',copy.deepcopy(app.settings))
    with pytest.raises(ValueError):
        apply(app,value)
    assert path.read_bytes()==before
    with pytest.raises(ValueError,match='review'):
        apply(app,value)


@pytest.mark.asyncio
async def test_post_commit_read_failure_latches_uncertainty_without_success(switch_app,monkeypatch):
    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'label':'Persisted'})
    def fail(*args):
        raise OSError('Independent reload failed')
    monkeypatch.setattr(scheduler,'refresh',fail)
    with pytest.raises(OSError,match='reload'):
        apply(app,value)
    assert json.loads(path.read_text())[0]['label']=='Persisted'
    assert app._backend_transition_error==controls.CLOSE_REQUIRED
    assert app._native_work_control is None


@pytest.mark.asyncio
async def test_cas_is_checked_after_entering_the_canonical_transaction(switch_app,monkeypatch):
    from contextlib import contextmanager

    from litetui import shared_state

    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'label':'Reviewed'})
    original=shared_state.coordinated_write
    changed=None
    @contextmanager
    def locked(destination,*args,**kwargs):
        nonlocal changed
        with original(destination,*args,**kwargs):
            # A writer won before this transaction obtained its canonical file lock.
            rows=json.loads(path.read_text())
            rows[0]['run_count']=19
            path.write_text(json.dumps(rows),encoding='utf-8')
            changed=path.read_bytes()
            yield
    monkeypatch.setattr(shared_state,'coordinated_write',locked)
    with pytest.raises(ValueError,match='changed'):
        apply(app,value)
    assert path.read_bytes()==changed
    assert not getattr(app,'_backend_transition_error',None)


@pytest.mark.asyncio
async def test_review_holds_no_os_lease_and_apply_respects_scheduler_owner(switch_app):
    from litetui.shared_state import Lease, OwnershipError

    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'enabled':False})
    before=path.read_bytes()
    with Lease(Path(str(path)+'.lock')):
        pass
    with Lease(app._agent_session.store.data_root/'.scheduler.lease'), pytest.raises(OwnershipError):
        apply(app,value)
    assert path.read_bytes()==before
    assert not getattr(app,'_backend_transition_error',None)


@pytest.mark.asyncio
async def test_failed_atomic_replace_preserves_document_and_has_no_publication(switch_app,monkeypatch):
    from litetui import row_store

    app=switch_app
    path=save_jobs(app,[scheduler.Job(prompt='Keep',schedule='0 9 * * *')])
    snap=snapshot(app)
    value=review(app,snap,'job_update',row(app),{'enabled':False})
    before=path.read_bytes()
    def fail(*args):
        raise OSError('replace failed before commit')
    monkeypatch.setattr(row_store.os,'replace',fail)
    with pytest.raises(OSError,match='replace failed'):
        apply(app,value)
    assert path.read_bytes()==before
    assert app._native_work_control is None
    assert not getattr(app,'_backend_transition_error',None)


@pytest.mark.asyncio
async def test_readback_identity_and_goal_raw_drift_never_publish_success(switch_app):
    app=switch_app
    goal_loop.save_goal(app.convo_dir,goal_loop.GoalState('Keep'))
    snap=snapshot(app)
    value=review(app,snap,'goal_pause',snap['goal'])
    result=apply(app,value)
    wrong=copy.deepcopy(result)
    wrong['receipt']='0'*64
    with pytest.raises(ValueError,match='publication'):
        readback(app,wrong)
    path=app.convo_dir/goal_loop.GOAL_FILENAME
    document=json.loads(path.read_text())
    document['future_extension']='drift'
    path.write_text(json.dumps(document),encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):
        readback(app,result)


@pytest.mark.asyncio
async def test_registered_gui_routes_preserve_review_apply_and_readback(switch_app):
    from litetui import gui_rpc

    app=switch_app
    snap=snapshot(app)
    cmd={'type':'gui.work.native_control_validate','id':'route-review',**common(app),**selected(snap),
         'action':'job_create','target_id':None,'target_revision':None,
         'draft':{'kind':'cron','prompt':'Route fixture','schedule':'0 9 * * *','enabled':False}}
    value=await gui_rpc.async_dispatch(app,cmd)
    result=await gui_rpc.async_dispatch(app,{'type':'gui.work.native_control_apply','id':'route-apply',
        **{k:v for k,v in value.items() if k!='kind'}})
    final=await gui_rpc.async_dispatch(app,{'type':'gui.work.native_control_readback','id':'route-readback',
        **common(app),**selected(result),**{k:result[k] for k in ('review','action_id','receipt')}})
    assert final['verified'] and final['row']['prompt']=='Route fixture'
    hello=await gui_rpc.async_dispatch(app,{'type':'gui.hello','id':'hello','protocol_version':1})
    assert all('gui.work.native_'+op in hello['operations'] for op in controls.OPERATIONS)
    export_flow('registered_routes',snapshot=snap,review=value,result=result,readback=final)


@pytest.mark.asyncio
async def test_duplicate_json_keys_are_not_configuration_authority(switch_app):
    app=switch_app
    original=asdict(scheduler.Job(prompt='Keep',schedule='0 9 * * *'))
    path=save_jobs(app,[original])
    encoded=json.dumps(original)
    path.write_text('['+encoded[:-1]+',"enabled":false}]',encoding='utf-8')
    snap=snapshot(app)
    before=path.read_bytes()
    with pytest.raises(ValueError,match='Duplicate'):
        review(app,snap,'job_update',row(app),{'label':'Changed'})
    assert path.read_bytes()==before


@pytest.mark.asyncio
async def test_concurrent_creation_cannot_exceed_source_row_capacity(switch_app,monkeypatch):
    app=switch_app
    monkeypatch.setattr(native_work,'MAX_ROWS',2)
    original=asdict(scheduler.Job(prompt='Keep',schedule='0 9 * * *'))
    path=save_jobs(app,[original])
    snap=snapshot(app)
    value=review(app,snap,'job_create',draft={'kind':'cron','prompt':'new','schedule':'0 9 * * *'})
    peer=asdict(scheduler.Job(prompt='Peer',schedule='0 9 * * *'))
    path.write_text(json.dumps([original,peer]),encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='capacity'):
        apply(app,value)
    assert path.read_bytes()==before
    assert not getattr(app,'_backend_transition_error',None)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['job', 'goal'])
@pytest.mark.parametrize('phase', ['apply', 'readback'])
@pytest.mark.parametrize('before,after', [(True, 1), (1, 1.0)])
async def test_raw_cas_distinguishes_equal_python_json_types(switch_app, kind, phase, before, after):
    app = switch_app
    if kind == 'job':
        path = save_jobs(app, [{**asdict(scheduler.Job(prompt='Keep', schedule='0 9 * * *')),
                               'future_extension': {'value': before}}])
    else:
        goal_loop.save_goal(app.convo_dir, goal_loop.GoalState('Keep'))
        path = app.convo_dir / goal_loop.GOAL_FILENAME
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['future_extension'] = {'value': before}
        path.write_text(json.dumps(raw), encoding='utf-8')
    snap = snapshot(app)
    value = review(app, snap, 'job_update' if kind == 'job' else 'goal_pause',
                   row(app) if kind == 'job' else snap['goal'], {'label': 'Reviewed'} if kind == 'job' else {})
    result = apply(app, value) if phase == 'readback' else None
    raw = json.loads(path.read_text(encoding='utf-8'))
    (raw[0] if kind == 'job' else raw)['future_extension']['value'] = after
    path.write_text(json.dumps(raw), encoding='utf-8')
    foreign = path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        readback(app, result) if phase == 'readback' else apply(app, value)
    assert path.read_bytes() == foreign


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,fault', [
    ('cron', 'missing'), ('cron', 'regressed'), ('cron', 'noncanonical'), ('cron', 'cron_timer'),
    ('loop', 'stale_cadence'), ('loop', 'null_timer'), ('loop', 'noncanonical_timer'),
])
async def test_advanced_execution_requires_consistent_canonical_slot_and_cadence(switch_app, kind, fault):
    app = switch_app
    job = (scheduler.Job.loop(prompt='Keep', interval_minutes=15, owner_convo_id=app.convo_id)
           if kind == 'loop' else scheduler.Job(prompt='Keep', schedule='0 9 * * *'))
    job.run_count, job.last_fired_slot = 7, '2026-11-04T12:00'
    if kind == 'loop': job.next_run_at = '2026-11-04T12:15:00'
    path = save_jobs(app, [job])
    snap = snapshot(app)
    value = review(app, snap, 'job_update', row(app, 'loops' if kind == 'loop' else 'jobs'), {'label':'Changed'})
    result = apply(app, value)
    rows = json.loads(path.read_text(encoding='utf-8'))
    rows[0].update(run_count=8, last_fired_slot='2026-11-04T12:20')
    if kind == 'loop': rows[0]['next_run_at'] = '2026-11-04T12:35:04'
    if fault == 'missing': rows[0]['last_fired_slot'] = None
    elif fault == 'regressed': rows[0]['last_fired_slot'] = '2026-11-04T11:59'
    elif fault == 'noncanonical': rows[0]['last_fired_slot'] += ':00'
    elif fault == 'cron_timer': rows[0]['next_run_at'] = '2026-11-04T12:35:04'
    elif fault == 'stale_cadence': rows[0]['next_run_at'] = '2026-11-04T12:15:00'
    elif fault == 'null_timer': rows[0]['next_run_at'] = None
    elif fault == 'noncanonical_timer': rows[0]['next_run_at'] += '.123'
    path.write_text(json.dumps(rows), encoding='utf-8')
    with pytest.raises((ValueError, TypeError)):
        readback(app, result)


@pytest.mark.asyncio
async def test_manual_early_loop_can_rearm_before_previous_due_time(switch_app):
    app = switch_app
    job = scheduler.Job.loop(prompt='Keep', interval_minutes=15, owner_convo_id=app.convo_id)
    job.run_count, job.last_fired_slot, job.next_run_at = 7, '2026-11-04T12:00', '2026-11-04T13:00:00'
    path = save_jobs(app, [job])
    snap = snapshot(app)
    value = review(app, snap, 'job_update', row(app, 'loops'), {'label':'Changed'})
    result = apply(app, value)
    rows = json.loads(path.read_text(encoding='utf-8'))
    rows[0].update(run_count=8, last_fired_slot='2026-11-04T12:10', next_run_at='2026-11-04T12:25:04')
    path.write_text(json.dumps(rows), encoding='utf-8')
    assert readback(app, result)['row']['run_count'] == 8
