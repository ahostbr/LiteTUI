"""Source-owned Work reads and pause; actual owned app, forbidden external effects."""
from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as controlled_app

from litetui import calendar_view, gui_rpc, native_work, row_store, scheduler, tasks
from litetui.goal_loop import GoalState, save_goal

switch_app = controlled_app
PREFIX = 'gui.work.native_'


def common(app):
    return {'version': 1, 'session_id': app.convo_id, 'agent_id': app._agent_session.authority.agent_id,
            'backend': app.backend.name, 'model': app.model_id}


async def call(app, operation, **fields):
    return await gui_rpc.async_dispatch(app, {'type': PREFIX + operation, 'id': 'work-' + operation, **common(app), **fields})


def export_fixture(name, value):
    if output := os.environ.get('LITETUI_WORK_CONTRACT_FIXTURE'):
        path = Path(output)
        document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        document[name] = value
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding='utf-8')


def selected(snapshot):
    return {key: snapshot[key] for key in ('instance', 'snapshot')}


def add_task(app, *, state='done', **changes):
    task = tasks.Task(id='t-' + 'a' * 32, tool='powershell', label='Controlled task', convo_id=app.convo_id,
                      started=1.0, state=state, ended=None if state == 'running' else 2.0,
                      ok=None if state == 'running' else True, prompt='Preserve task prompt',
                      owner_pid=os.getpid(), owner_instance=tasks._INSTANCE_ID, **changes)
    app.bg_tasks[task.id] = task
    return task


def save_jobs(app, rows):
    path = scheduler.jobs_path(app._agent_session.store.data_root)
    path.write_text(json.dumps([asdict(row) if isinstance(row, scheduler.Job) else row for row in rows]), encoding='utf-8')
    return path


def protected(app):
    return (disk(app), copy.deepcopy(app.conversation), copy.deepcopy(app._pending_input),
            asdict(app.settings), asdict(app._agent_session.authority),
            [t.to_row() for t in app.bg_tasks.values()], copy.deepcopy(row_store._BASELINE),
            getattr(app, '_gui_quitting', False))


@pytest.mark.asyncio
async def test_busy_snapshot_is_pure_and_freezes_owned_rows_without_refresh_or_probes(switch_app, monkeypatch):
    app = switch_app
    task = add_task(app)
    foreign = copy.copy(task)
    foreign.id, foreign.convo_id = 'foreign', '11111111-2222-4333-8444-555555555555'
    app.bg_tasks[foreign.id] = foreign
    job = scheduler.Job(prompt='scheduled', schedule='*/5 * * * *', id='job-one')
    loop = scheduler.Job.loop(prompt='loop', interval_minutes=15, owner_convo_id=app.convo_id)
    save_jobs(app, [job, loop])
    save_goal(app.convo_dir, GoalState('Preserve goal', id='goal-one'))
    monkeypatch.setattr(app, '_chat_running', lambda: True)
    monkeypatch.setattr(scheduler, 'refresh', forbidden)
    monkeypatch.setattr(scheduler, 'load', forbidden)
    monkeypatch.setattr(tasks, '_owner_alive', forbidden)
    before = protected(app)
    s = await call(app, 'snapshot')
    assert s['busy'] and s['pause']['allowed']
    assert s['counts'] == {'tasks': 1, 'provider_tasks': 0, 'jobs': 1, 'loops': 1}
    assert not s['sources']['provider_tasks']['available'] and s['sources']['provider_tasks']['reason']
    assert s['goal']['status'] == 'present' and s['goal']['value']['objective'] == 'Preserve goal'
    rows = await call(app, 'page', **selected(s), section='tasks', offset=0, limit=64)
    assert rows['rows'][0]['row'] == task.to_row() and rows['rows'][0]['owner'] == 'current'
    assert protected(app) == before
    if output := os.environ.get('LITETUI_WORK_CONTRACT_FIXTURE'):
        fixture = {'snapshot': s, 'tasks': rows,
                   'jobs': await call(app, 'page', **selected(s), section='jobs'),
                   'loops': await call(app, 'page', **selected(s), section='loops'),
                   'calendar': await call(app, 'calendar', **selected(s), month='2026-11')}
        Path(output).write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding='utf-8')
    task.label = 'New live label'
    assert (await call(app, 'page', **selected(s), section='tasks'))['rows'] == rows['rows']
    s2 = await call(app, 'snapshot')
    assert s2['snapshot'] != s['snapshot'] and s2['instance'] == s['instance']
    with pytest.raises(ValueError, match='stale'):
        await call(app, 'page', **selected(s), section='tasks')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['version', 'id', 'session_id', 'agent_id', 'backend', 'model', 'extra', 'missing'])
async def test_request_identity_and_fields_are_strict(switch_app, fault):
    app = switch_app
    cmd = {'type': PREFIX + 'snapshot', 'id': 'test', **common(app)}
    if fault == 'extra':
        cmd['field'] = 'tasks'
    elif fault == 'missing':
        cmd.pop('agent_id')
    else:
        cmd[fault] = True if fault == 'version' else 'wrong' if fault != 'id' else ''
    before = protected(app)
    with pytest.raises((ValueError, TypeError)):
        await gui_rpc.async_dispatch(app, cmd)
    assert protected(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['bool-time', 'nan-time', 'bad-state', 'bool-pid', 'foreign-path', 'duplicate', 'oversize', 'wrong-row'])
async def test_malformed_task_source_is_unavailable_not_silently_empty(switch_app, fault):
    app = switch_app
    t = add_task(app)
    if fault == 'bool-time':
        t.started = True
    elif fault == 'nan-time':
        t.started = float('nan')
    elif fault == 'bad-state':
        t.state = 'finished'
    elif fault == 'bool-pid':
        t.owner_pid = True
    elif fault == 'foreign-path':
        t.id = '../escape'
    elif fault == 'duplicate':
        app.bg_tasks['duplicate-key'] = copy.copy(t)
    elif fault == 'oversize':
        t.prompt = 'x' * 65537
    else:
        app.bg_tasks['wrong'] = object()
    s = await call(app, 'snapshot')
    assert not s['sources']['tasks']['available'] and s['sources']['tasks']['reason']
    assert s['counts']['tasks'] == 0
    with pytest.raises(ValueError):
        await call(app, 'page', **selected(s), section='tasks')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['bad-json', 'object', 'bool-flag', 'bad-time', 'bad-loop', 'duplicate', 'oversize', 'missing'])
async def test_bad_jobs_never_disappear_as_an_empty_available_schedule(switch_app, fault):
    app = switch_app
    row = asdict(scheduler.Job(prompt='Keep me', schedule='* * * * *', id='job-one'))
    path = save_jobs(app, [row])
    if fault == 'bad-json':
        path.write_bytes(b'{')
    elif fault == 'object':
        path.write_bytes(b'{}')
    elif fault == 'oversize':
        path.write_bytes(b' ' * (native_work.MAX_FILE + 1))
    else:
        if fault == 'bool-flag':
            row['enabled'] = 1
        elif fault == 'bad-time':
            row['created'] = 'not a time'
        elif fault == 'bad-loop':
            row['kind'] = 'loop'
        elif fault == 'missing':
            row.pop('id')
        save_jobs(app, [row, row] if fault == 'duplicate' else [row])
    before = path.read_bytes(), copy.deepcopy(row_store._BASELINE)
    s = await call(app, 'snapshot')
    assert all(not s['sources'][key]['available'] for key in ('jobs', 'loops'))
    assert (path.read_bytes(), row_store._BASELINE) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['bad-json', 'no-objective', 'bool-count', 'bad-status', 'bad-evidence', 'oversize'])
async def test_goal_absence_and_unavailable_are_distinct(switch_app, fault):
    app = switch_app
    assert (await call(app, 'snapshot'))['goal']['status'] == 'absent'
    goal = GoalState('Preserve goal', id='goal-one')
    save_goal(app.convo_dir, goal)
    path = app.convo_dir / 'goal.json'
    row = asdict(goal)
    if fault == 'bad-json':
        path.write_bytes(b'{')
    elif fault == 'oversize':
        path.write_bytes(b' ' * (native_work.MAX_FILE + 1))
    else:
        if fault == 'no-objective':
            row.pop('objective')
        elif fault == 'bool-count':
            row['max_turns'] = True
        elif fault == 'bad-status':
            row['status'] = 'done'
        else:
            row['missing_evidence'] = [123]
        path.write_text(json.dumps(row), encoding='utf-8')
    before = path.read_bytes()
    result = (await call(app, 'snapshot'))['goal']
    assert result['status'] == 'unavailable' and result['value'] is None and result['reason']
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_calendar_matches_canonical_grid_and_entries_with_broken_disabled_and_loop_jobs(switch_app, monkeypatch):
    app = switch_app
    jobs = [scheduler.Job(prompt='every five minutes', schedule='*/5 * * * *', id='many'),
            scheduler.Job(prompt='Disabled stays visible', schedule='0 12 * * *', id='disabled', enabled=False),
            scheduler.Job(prompt='broken remains visible', schedule='nonsense', id='broken'),
            scheduler.Job.loop(prompt='Excluded loop', interval_minutes=1, owner_convo_id=app.convo_id)]
    save_jobs(app, jobs)
    monkeypatch.setattr(scheduler.Cron, 'next_after', forbidden)
    s = await call(app, 'snapshot')
    before = protected(app)
    result = await call(app, 'calendar', **selected(s), month='2026-11')
    assert len(result['days']) == 42 and result['total'] == 42
    day = next(cell for cell in result['days'] if cell['date'] == '2026-11-01')
    expected = [asdict(e) for e in calendar_view.entries_for(jobs, date(2026, 11, 1), cap=2)]
    assert day['entries'] == expected[:2] and day['total'] == 3
    first = await call(app, 'calendar', **selected(s), month='2026-11', date='2026-11-01', offset=0, limit=2)
    second = await call(app, 'calendar', **selected(s), month='2026-11', date='2026-11-01', offset=first['next_offset'], limit=2)
    assert first['rows'] + second['rows'] == expected and second['next_offset'] is None
    assert expected[0]['total'] == 288 and expected[1]['enabled'] is False and expected[2]['broken']
    assert protected(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'', b'hello\nworld\n', b'x' * 70000, ('\u96ea' * 30000).encode(), b'prefix\xfftail'],
                         ids=['empty', 'plain', 'large-ascii', 'large-utf8', 'lossy'])
async def test_tail_uses_bounded_canonical_path_and_declares_truncation(switch_app, body):
    app = switch_app
    t = add_task(app, log='../../must-not-read')
    path = tasks.log_path(t, app._agent_session.store.data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    s = await call(app, 'snapshot')
    row = (await call(app, 'page', **selected(s), section='tasks'))['rows'][0]
    before = protected(app)
    result = await call(app, 'tail', **selected(s), task_id=t.id, task_revision=row['revision'])
    suffix = body[-native_work.MAX_TAIL:]
    assert result['path'] == str(path) and result['status'] == 'available'
    assert result['text'] == suffix.decode('utf-8', errors='replace')
    assert result['total_bytes'] == len(body) and result['bytes_read'] == len(suffix)
    assert result['offset'] == len(body) - len(suffix) and result['truncated'] == (len(body) > native_work.MAX_TAIL)
    assert protected(app) == before
    if body == b'hello\nworld\n':
        export_fixture('tail_flow', {'snapshot': s, 'task': row, 'tail': result})
    t.label = 'changed'
    with pytest.raises(ValueError, match='changed'):
        await call(app, 'tail', **selected(s), task_id=t.id, task_revision=row['revision'])


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['running', 'done'])
async def test_tail_distinguishes_running_and_absent(switch_app, state):
    app = switch_app
    t = add_task(app, state=state)
    s = await call(app, 'snapshot')
    row = (await call(app, 'page', **selected(s), section='tasks'))['rows'][0]
    result = await call(app, 'tail', **selected(s), task_id=t.id, task_revision=row['revision'])
    assert result['status'] == ('running' if state == 'running' else 'absent')
    assert result['text'] == '' and result['revision'] == 'absent' and result['reason']
    export_fixture(result['status'] + '_tail_flow', {'snapshot': s, 'task': row, 'tail': result})


@pytest.mark.asyncio
async def test_busy_scheduler_pause_changes_only_latch_then_requires_exact_readback(switch_app, monkeypatch):
    app = switch_app
    add_task(app, state='running')
    app._pending_input = [{'content': 'Keep queued', 'tool_profile': 'strict'}]
    app._gui_schedules_paused = False
    monkeypatch.setattr(app, '_chat_running', lambda: True)
    s = await call(app, 'snapshot')
    before = protected(app)
    review = await call(app, 'pause_validate', **selected(s), action='pause_schedules')
    assert protected(app) == before and not app._gui_schedules_paused
    outcome = await call(app, 'pause_apply', **{k: v for k, v in review.items() if k != 'kind'})
    assert outcome['status'] == 'paused' and app._gui_schedules_paused
    assert protected(app) == before
    request = {k: outcome[k] for k in ('instance', 'snapshot', 'review', 'action_id', 'receipt')}
    result = await call(app, 'pause_readback', **request)
    assert result['verified'] and result['busy'] and not result['quitting']
    export_fixture('pause_flow', {'snapshot': s, 'review': review, 'result': outcome, 'readback': result})
    with pytest.raises(ValueError, match='unconsumed'):
        await call(app, 'pause_apply', **{k: v for k, v in review.items() if k != 'kind'})
    app._gui_schedules_paused = False
    with pytest.raises(ValueError, match='readback changed'):
        await call(app, 'pause_readback', **request)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['tamper', 'superseded', 'refresh', 'paused', 'quitting', 'identity', 'readback'])
async def test_pause_refuses_stale_or_changed_review_without_mutation(switch_app, fault):
    app = switch_app
    app._gui_schedules_paused = False
    s = await call(app, 'snapshot')
    review = await call(app, 'pause_validate', **selected(s), action='pause_schedules')
    if fault == 'tamper':
        review['consequence'] = 'Different action'
    elif fault == 'superseded':
        await call(app, 'pause_validate', **selected(s), action='pause_schedules')
    elif fault == 'refresh':
        await call(app, 'snapshot')
    elif fault == 'paused':
        app._gui_schedules_paused = True
    elif fault == 'quitting':
        app._gui_quitting = True
    elif fault == 'identity':
        review['agent_id'] = '11111111-2222-4333-8444-555555555555'
    else:
        with pytest.raises(ValueError):
            await call(app, 'pause_readback', **selected(s), review=review['review'], action_id=review['action_id'], receipt='a' * 64)
        return
    before = protected(app), app._gui_schedules_paused
    with pytest.raises(ValueError):
        await call(app, 'pause_apply', **{k: v for k, v in review.items() if k != 'kind'})
    assert (protected(app), app._gui_schedules_paused) == before


@pytest.mark.asyncio
async def test_pages_reduce_rows_to_bound_full_envelope_without_losing_any(switch_app):
    app = switch_app
    template = add_task(app)
    template.prompt = '\u96ea' * 21000
    for index in range(20):
        row = copy.copy(template)
        row.id = f't-bounded-{index}'
        app.bg_tasks[row.id] = row
    s = await call(app, 'snapshot')
    offset = 0
    collected: list[str] = []
    while True:
        result = await call(app, 'page', **selected(s), section='tasks', offset=offset, limit=64)
        envelope = {'type': 'response', 'id': 'work-page', 'ok': True, 'result': result}
        assert len(json.dumps(envelope).encode()) <= native_work.MAX_WIRE
        assert len(result['rows']) < 21 and result['rows']
        collected.extend(row['row']['id'] for row in result['rows'])
        if result['next_offset'] is None:
            break
        assert result['next_offset'] == offset + len(result['rows'])
        offset = result['next_offset']
    assert len(collected) == len(set(collected)) == 21


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [{'offset': True}, {'limit': False}, {'offset': -1},
                                   {'offset': 2}, {'limit': 0}, {'limit': 65}])
async def test_page_bounds_require_exact_integers(switch_app, fields):
    app = switch_app
    add_task(app)
    s = await call(app, 'snapshot')
    with pytest.raises(ValueError):
        await call(app, 'page', **selected(s), section='tasks', **fields)


def test_provider_rows_use_canonical_live_activity_and_enforce_capture_budget_while_iterating(monkeypatch):
    from litetui.codex_tasks import NativeActivity
    session = '11111111-2222-4333-8444-555555555555'
    activity = NativeActivity('codex-one', 'thread', 'turn', 'item', session, 'inspect', 1.0, 999.0)
    foreign = copy.copy(activity)
    foreign.id, foreign.convo_id = 'foreign', None
    app = SimpleNamespace(backend=SimpleNamespace(name='codex'), codex_native_activity={'one': activity, 'foreign': foreign})
    rows, revision = native_work._provider_tasks(app, session)
    assert len(rows) == 1 and len(revision) == 64
    assert rows[0]['owner'] == 'provider' and not rows[0]['tail_available']
    assert 'clock' not in rows[0]['row'] and rows[0]['row']['convo_id'] == session
    activity.label = 'x' * 16384
    app.codex_native_activity = {}
    for index in range(400):
        row = copy.copy(activity)
        row.id = f'codex-{index}'
        app.codex_native_activity[index] = row
    calls = []
    original = native_work._row
    def counted(row):
        calls.append(row['row']['id'])
        return original(row)
    monkeypatch.setattr(native_work, '_row', counted)
    with pytest.raises(ValueError, match='4 MiB'):
        native_work._provider_tasks(app, session)
    assert len(calls) < 300  # Fail at the budget, not after constructing every row.


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['replace-task', 'modify-log'])
async def test_tail_rejects_live_row_replacement_and_file_change_during_read(switch_app, monkeypatch, fault):
    app = switch_app
    task = add_task(app)
    path = tasks.log_path(task, app._agent_session.store.data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'original')
    s = await call(app, 'snapshot')
    row = (await call(app, 'page', **selected(s), section='tasks'))['rows'][0]
    original = Path.open
    @contextmanager
    def intercept(target, *args, **kwargs):
        with original(target, *args, **kwargs) as stream:
            if target != path or args != ('rb',):
                yield stream
                return
            def changed_read(size):
                data = stream.read(size)
                if fault == 'replace-task':
                    app.bg_tasks[task.id] = copy.copy(task)
                else:
                    with original(path, 'ab') as writer:
                        writer.write(b'changed')
                return data
            yield SimpleNamespace(read=changed_read, fileno=stream.fileno, seek=stream.seek)
    monkeypatch.setattr(Path, 'open', intercept)
    with pytest.raises(ValueError, match='changed'):
        await call(app, 'tail', **selected(s), task_id=task.id, task_revision=row['revision'])


@pytest.mark.asyncio
async def test_calendar_parses_each_job_once_per_bounded_month(switch_app, monkeypatch):
    app = switch_app
    jobs = [scheduler.Job(prompt='frequent', schedule='* * * * *', id='every'),
            scheduler.Job(prompt='broken', schedule='bad', id='bad')]
    save_jobs(app, jobs)
    s = await call(app, 'snapshot')
    calls = []
    original = scheduler.Job.cron
    def counted(job):
        calls.append(job.id)
        return original(job)
    monkeypatch.setattr(scheduler.Job, 'cron', counted)
    result = await call(app, 'calendar', **selected(s), month='2026-11')
    assert len(result['days']) == 42 and calls == ['every', 'bad']


@pytest.mark.asyncio
async def test_management_only_engine_allows_inspection_and_pause_without_execution(switch_app):
    from litetui import native_engine
    app = switch_app
    app._native_engine_state = native_engine.State(app.backend, session_id=app.convo_id,
        desired_model=app.model_id, session=app._agent_session, agent_id=app._agent_session.authority.agent_id)
    before = protected(app)
    assert native_engine.submission_refusal(app)
    s = await call(app, 'snapshot')
    review = await call(app, 'pause_validate', **selected(s), action='pause_schedules')
    outcome = await call(app, 'pause_apply', **{k: v for k, v in review.items() if k != 'kind'})
    result = await call(app, 'pause_readback', **{k: outcome[k] for k in ('instance', 'snapshot', 'review', 'action_id', 'receipt')})
    assert result['verified'] and native_engine.submission_refusal(app)
    assert app._native_engine_state.inspection_depth == 0 and protected(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', ['_backend_transition_error', '_conversation_transition_error', '_conversation_rewrite_error'])
async def test_existing_transition_fail_closed_guards_still_reject_inspection(switch_app, flag):
    app = switch_app
    setattr(app, flag, 'Existing transition is uncertain')
    before = protected(app)
    with pytest.raises(ValueError, match='uncertain'):
        await call(app, 'snapshot')
    assert protected(app) == before


@pytest.mark.asyncio
async def test_lifecycle_capture_and_activation_routes_preserve_existing_guards_and_disk(switch_app):
    from litetui import background_input
    app = switch_app
    before = protected(app)
    captured = await call(app, 'capture')
    assert captured['kind'] == 'work_capture' and not background_input.enabled(app)
    active = await call(app, 'activate', instance=captured['instance'], activation_token=captured['activation_token'])
    assert active['kind'] == 'work_active' and background_input.enabled(app)
    assert active['instance'] == captured['instance'] and active['sequence'] == 0
    assert protected(app) == before
    with pytest.raises(ValueError):
        await call(app, 'settled', instance=captured['instance'], admission_id='11111111-2222-4333-8444-555555555555')
    app._conversation_rewrite_error = 'Uncertain captured action'
    with pytest.raises(ValueError):
        await call(app, 'capture')
    with pytest.raises(ValueError):
        await call(app, 'activate', instance=captured['instance'], activation_token=captured['activation_token'])


@pytest.mark.asyncio
async def test_only_exact_settled_route_reaches_source_join_without_action_busy_or_management_lock(switch_app, monkeypatch):
    from litetui import background_input
    app = switch_app
    app._conversation_rewrite_error = 'Existing action blocks new execution'
    app._gui_management_busy = True
    observed = []
    async def source_join(current, cmd):
        observed.append((current, cmd['type'], gui_rpc.OPERATION_CONTEXT.get()))
        raise ValueError('Unknown exact admission')
    monkeypatch.setattr(background_input, 'settled', source_join)
    with pytest.raises(ValueError, match='Unknown exact admission'):
        await call(app, 'settled', instance='a' * 64, admission_id='11111111-2222-4333-8444-555555555555')
    assert observed == [(app, 'gui.work.native_settled', 'work-settled')]
    assert app._gui_management_busy and gui_rpc.OPERATION_CONTEXT.get() is None
    with pytest.raises(ValueError):
        await call(app, 'snapshot')
    app._conversation_transition_error = 'Uncertain conversation transition'
    with pytest.raises(ValueError):
        await call(app, 'settled', instance='a' * 64, admission_id='11111111-2222-4333-8444-555555555555')
    assert len(observed) == 1
