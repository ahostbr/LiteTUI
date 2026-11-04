"""Reviewed durable Work configuration. No operation in this module starts work."""
# Scheduler timestamps intentionally use canonical local wall time.
# ruff: noqa: DTZ005
from __future__ import annotations

import copy
import json
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from litetui import (
    background_input,
    conversation_action,
    goal_loop,
    scheduler,
    seat_authority,
)
from litetui import native_work as work

OPERATIONS = ('control_validate', 'control_apply', 'control_readback')
ACTIONS = ('job_create', 'job_update', 'job_delete', 'loop_enable', 'goal_pause', 'goal_clear')
EXECUTION_FIELDS = frozenset(('run_count', 'last_fired_slot', 'next_run_at'))
CLOSE_REQUIRED = 'Work configuration outcome is uncertain; close this runtime and reopen explicitly.'


@dataclass
class Ticket:
    capture: work.Capture
    wire: bytes
    before: dict | None
    after: dict | None
    receipt: dict | None = None
    published: dict | None = None


def _guard(app):
    from litetui.gui_rpc import _idle

    _idle(app)
    conversation_action.require_open(app)
    app._validate_owned_execution()
    if getattr(app, '_gui_quitting', False):
        raise ValueError('This runtime is closing; configuration is unavailable')
    value = background_input.bridge(app)
    if value is not None:
        value.check()


def _goal_guard(app):
    if getattr(getattr(app, 'backend', None), 'owns_native_turns', False):
        raise ValueError('This backend owns its goal loop; host goal controls are unsupported')
    if getattr(getattr(app, 'goal_runtime', None), '_evaluating', False):
        raise ValueError('Goal evaluator is active; wait before changing the goal')
    if any(item.get('goal_continuation') for item in app._pending_input):
        raise ValueError('Goal continuation is pending; wait before changing the goal')
    value = background_input.bridge(app)
    if value is not None:
        if any(p.ref['kind'] == 'goal' and p.phase != 'retired' for p in value.producers.values()):
            raise ValueError('Goal producer is active; wait for exact retirement')
        if any(i.producer.ref['kind'] == 'goal' and i.state not in background_input.TERMINAL for i in value.inputs.values()):
            raise ValueError('Goal input is pending; settle it before changing the goal')


def read_jobs(path):
    raw, revision = work._read(path)
    rows = [] if raw is None else background_input.strict_json(raw.decode('utf-8-sig'))
    if not isinstance(rows, list) or len(rows) > work.MAX_ROWS:
        raise ValueError('Jobs must be a bounded list')
    seen = set()
    for row in rows:
        parsed = work._job(row)
        key = parsed['row']['id']
        if key in seen:
            raise ValueError('Jobs contain duplicate identities')
        seen.add(key)
    return rows, revision


def _goal(path):
    raw, revision = work._read(path)
    value = None if raw is None else background_input.strict_json(raw.decode('utf-8-sig'))
    if value is not None and not isinstance(value, dict):
        raise ValueError('Goal document is malformed')
    return value, revision


def _fields(draft, allowed, required=()):
    if not isinstance(draft, dict) or set(draft) - set(allowed) or not set(required) <= draft.keys():
        raise ValueError('Invalid editable configuration fields')


def _prompt(value):
    work.text(value, 65536)
    if not value.strip():
        raise ValueError('Scheduled prompt must be nonempty')
    return value


def _patch(draft):
    for key, value in draft.items():
        if key == 'prompt':
            _prompt(value)
        elif key in ('enabled', 'new_conversation'):
            work.boolean(value)
        elif key == 'label':
            work.text(value, 4096, empty=True)
        elif key == 'schedule':
            work.text(value, 1024)
            scheduler.Cron.parse(value)
        elif key == 'interval_minutes':
            work.integer(value, 1, 525600)
        elif key == 'tool_profile':
            work.text(value, 4096)


def _new_job(app, draft):
    if not isinstance(draft, dict) or draft.get('kind') not in ('cron', 'loop'):
        raise ValueError('Choose cron or loop')
    if draft['kind'] == 'loop':
        _fields(draft, ('kind', 'prompt', 'interval_minutes', 'label', 'enabled'), ('kind', 'prompt', 'interval_minutes'))
        _patch(draft)
        job = scheduler.Job.loop(prompt=draft['prompt'], interval_minutes=draft['interval_minutes'],
                                 owner_convo_id=app.convo_id, tool_profile=seat_authority.loop_level(app))
    else:
        _fields(draft, ('kind', 'prompt', 'schedule', 'label', 'enabled', 'new_conversation', 'tool_profile'), ('kind', 'prompt', 'schedule'))
        _patch(draft)
        job = scheduler.Job(prompt=draft['prompt'], schedule=draft['schedule'],
                            tool_profile=seat_authority.schedule_level(app, draft.get('tool_profile')),
                            new_conversation=draft.get('new_conversation', False))
    job.label = draft.get('label', '')
    job.enabled = draft.get('enabled', True)
    return asdict(job)


def _plain_goal(raw):
    return asdict(goal_loop.GoalState(**{k:v for k,v in raw.items() if k in goal_loop.GoalState.__dataclass_fields__}))


def validate(app, cmd):
    app._native_work_control = None
    previous = getattr(app, '_native_work_inspection', None)
    if isinstance(previous, work.Capture):
        previous.review = None
    current = work._capture(app, cmd, ('action', 'target_id', 'target_revision', 'draft'))
    _guard(app)
    action, target_id, draft = cmd['action'], cmd['target_id'], cmd['draft']
    if action not in ACTIONS:
        raise ValueError('Unsupported durable Work action')
    if not isinstance(draft, dict):
        raise TypeError('Configuration draft must be an object')
    rearm = None
    if action.startswith('goal_'):
        _goal_guard(app)
        _fields(draft, ())
        captured = current.snapshot['goal']
        if captured['status'] != 'present' or cmd['target_revision'] != captured['revision'] or target_id != captured['value']['id']:
            raise ValueError('Select the exact current goal')
        before, revision = _goal(app.convo_dir / goal_loop.GOAL_FILENAME)
        if revision != captured['revision'] or before is None:
            raise ValueError('Goal changed after capture')
        after = None if action == 'goal_clear' else {**before, 'status':'paused'}
        plain_before, plain_after = _plain_goal(before), None if after is None else _plain_goal(after)
        scope = 'conversation'
        consequence = 'Clear the saved goal only; no transcript is removed and no work is started.' if after is None else 'Pause future goal evaluation; no work is started and no transcript is changed.'
    else:
        source = current.snapshot['sources']['jobs']
        if not source['available']:
            raise ValueError('Saved jobs are unavailable; repair or refresh before editing')
        rows, store_revision = read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
        if store_revision != source['revision']:
            raise ValueError('Jobs changed after capture; refresh before reviewing')
        if action == 'job_create':
            if target_id is not None or cmd['target_revision'] is not None:
                raise ValueError('Create must not select an existing job')
            before, after, revision = None, _new_job(app, draft), 'absent'
            target_id = after['id']
            if len(rows) >= work.MAX_ROWS or any(r['id'] == target_id for r in rows):
                raise ValueError('Job capacity or identity conflict')
            if after['kind'] == 'loop':
                rearm = after['interval_minutes']
        else:
            captured = next((r for section in ('jobs','loops') for r in current.rows[section]
                             if r['row']['id'] == target_id and r['revision'] == cmd['target_revision']), None)
            if captured is None:
                raise ValueError('Select the exact captured job')
            before = next(r for r in rows if r['id'] == target_id)
            revision = work.digest(before)
            if before.get('kind', 'cron') == 'loop' and before.get('owner_convo_id') != app.convo_id:
                raise ValueError('Loop belongs to another conversation')
            after = copy.deepcopy(before)
            if action == 'job_delete':
                _fields(draft, ())
                after = None
            elif action == 'loop_enable':
                _fields(draft, ('enabled',), ('enabled',))
                work.boolean(draft['enabled'])
                if before.get('kind') != 'loop':
                    raise ValueError('Timer rearming requires a loop')
                after['enabled'] = draft['enabled']
                if draft['enabled']:
                    rearm = before['interval_minutes']
            elif action == 'job_update':
                allowed = ('prompt','label') if before.get('kind') == 'loop' else ('prompt','schedule','label','enabled','new_conversation')
                _fields(draft, allowed)
                if not draft:
                    raise ValueError('Configuration patch must be nonempty')
                _patch(draft)
                after.update(draft)
            else:
                raise ValueError('Unsupported job action')
        plain_before = None if before is None else work._job(before)['row']
        plain_after = None if after is None else work._job(after)['row']
        scope = 'data_root'
        consequence = ('Delete only the saved job; accepted work is not cancelled.' if after is None else
                       'Save this job configuration only. An enabled job may run on a later normal scheduler tick; this save starts no work.')
    result = {**work._base(current, 'work_control_review'), 'action':action, 'target_id':target_id,
              'target_revision':cmd['target_revision'], 'draft':copy.deepcopy(draft),
              'review':secrets.token_hex(32), 'action_id':str(uuid.uuid4()), 'scope':scope,
              'before':plain_before, 'after':plain_after, 'rearm_minutes':rearm,
              'consequence':consequence, 'raw_revision':revision}
    work._wire(cmd,result)
    current.review = work.encoded(result)
    app._native_work_control = Ticket(current,current.review,copy.deepcopy(before),copy.deepcopy(after))
    return result


def _ticket(app, cmd, *, applying):
    value = getattr(app, '_native_work_control', None)
    if applying:
        app._native_work_control = None
    if value is None:
        raise ValueError('Control requires a fresh unconsumed review')
    if not isinstance(value, Ticket):
        raise TypeError('Control review has an invalid retained type')
    review = json.loads(value.wire)
    if applying:
        expected = {k:v for k,v in review.items() if k != 'kind'}
        valid = value.capture.review == value.wire
        value.capture.review = None
        if not valid or work.encoded({k:v for k,v in cmd.items() if k not in ('type','id')}) != work.encoded(expected):
            raise ValueError('Control must exactly echo its unconsumed review')
        fields = tuple(k for k in expected if k not in (*work.COMMON,'instance','snapshot'))
    else:
        fields = ('review','action_id','receipt')
    current = work._capture(app,cmd,fields)
    if current is not value.capture:
        raise ValueError('Control review belongs to a stale capture')
    return value, review


def _matches(raw, expected):
    if work.encoded(raw) != work.encoded(expected):
        raise ValueError('Selected raw configuration changed after review')


def apply(app, cmd):
    ticket, review = _ticket(app,cmd,applying=True)
    _guard(app)
    is_goal = review['action'].startswith('goal_')
    if is_goal:
        _goal_guard(app)
    elif review['action'] == 'job_create':
        profile = (seat_authority.loop_level(app) if ticket.after['kind'] == 'loop'
                   else seat_authority.schedule_level(app,review['draft'].get('tool_profile')))
        if profile != ticket.after['tool_profile']:
            raise ValueError('Job authority changed after review')
    after = copy.deepcopy(ticket.after)
    if review['rearm_minutes'] is not None:
        after['next_run_at'] = (datetime.now()+timedelta(minutes=review['rearm_minutes'])).isoformat(timespec='seconds')
    # Validation/CAS errors occur before the replace and do not poison runtime state.
    committed = False
    def mark_committed():
        nonlocal committed
        committed = True
    try:
        return _publish(app, cmd, ticket, review, after, is_goal, mark_committed)
    except BaseException:
        if committed:
            app._backend_transition_error = CLOSE_REQUIRED
        raise


def _publish(app, cmd, ticket, review, after, is_goal, mark_committed):
    if is_goal:
        row, revision = goal_loop.reviewed_control(app.convo_dir, review['action'], ticket.before, _goal,
                                                  guard=lambda: (_guard(app),_goal_guard(app)), committed=mark_committed)
    else:
        def change(rows):
            current = next((r for r in rows if r['id'] == review['target_id']),None)
            _matches(current,ticket.before)
            output = [after if r['id'] == review['target_id'] else r for r in rows if after is not None or r['id'] != review['target_id']]
            if ticket.before is None:
                output.append(after)
            if len(output) > work.MAX_ROWS:
                raise ValueError('Saved jobs exceed row capacity')
            if len(json.dumps(output,indent=2,ensure_ascii=False,allow_nan=False).encode('utf-8')) > work.MAX_FILE:
                raise ValueError('Saved jobs exceed the bounded file budget')
            _guard(app)
            return output
        scheduler.reviewed_update(app._agent_session.store.data_root,lambda path:read_jobs(path)[0],change,committed=mark_committed)
        # Adopt canonical shared rows while retaining existing Job object identities.
        scheduler.refresh(app.jobs,app._agent_session.store.data_root)
        rows, revision = read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
        row = next((r for r in rows if r['id'] == review['target_id']),None)
    try:
        _matches(row,after)
        if work._owner(app,work._identity(app)) != ticket.capture.owner:
            raise ValueError('Work owner changed during publication')
        result = {**work._base(ticket.capture,'work_control_result'),
                  **{k:review[k] for k in ('action','target_id','review','action_id')},
                  'status':'applied','mutated':True,'close_required':False,
                  'row':None if row is None else _plain_goal(row) if is_goal else work._job(row)['row'],
                  'store_revision':revision}
        result['receipt'] = work.digest(result)
        work._wire(cmd,result)
    except BaseException:
        app._backend_transition_error = CLOSE_REQUIRED
        raise
    ticket.published, ticket.receipt = copy.deepcopy(row), copy.deepcopy(result)
    app._native_work_control = ticket
    return result


def readback(app, cmd):
    ticket, review = _ticket(app,cmd,applying=False)
    receipt = ticket.receipt
    if receipt is None or any(cmd[k] != receipt[k] for k in ('review','action_id','receipt')):
        raise ValueError('Readback does not identify this control publication')
    if review['action'].startswith('goal_'):
        raw, revision = _goal(app.convo_dir / goal_loop.GOAL_FILENAME)
        _matches(raw,ticket.published)
        row = None if raw is None else _plain_goal(raw)
    else:
        rows, revision = read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
        raw = next((r for r in rows if r['id'] == review['target_id']),None)
        expected = ticket.published
        if raw is None or expected is None:
            _matches(raw,expected)
        else:
            _matches({k:v for k,v in raw.items() if k not in EXECUTION_FIELDS},
                     {k:v for k,v in expected.items() if k not in EXECUTION_FIELDS})
            _execution_readback(raw, expected)
        row = None if raw is None else work._job(raw)['row']
    result = {**work._base(ticket.capture,'work_control_readback'),
              **{k:receipt[k] for k in ('action','target_id','review','action_id','receipt')},
              'verified':True,'row':row,'store_revision':revision}
    return work._wire(cmd,result)


def _execution_readback(raw, expected):
    count, prior_count = raw.get('run_count', 0), expected.get('run_count', 0)
    if count < prior_count:
        raise ValueError('Job execution count regressed after publication')
    if count == prior_count:
        _matches(raw, expected)
        return
    # A later canonical firing may advance counters during independent readback.
    # Its persisted slot and loop cadence must still describe that same firing.
    slot = raw.get('last_fired_slot')
    if not isinstance(slot, str):
        raise TypeError('Advanced job count lacks its canonical firing slot')
    fired = datetime.fromisoformat(slot)
    if fired.tzinfo is not None or scheduler.slot_of(fired) != slot:
        raise ValueError('Advanced job count changed canonical firing slot format')
    prior_slot = expected.get('last_fired_slot')
    if prior_slot is not None:
        prior = datetime.fromisoformat(prior_slot)
        if prior.tzinfo is not None or scheduler.slot_of(prior) != prior_slot or fired < prior:
            raise ValueError('Job firing slot regressed or has a noncanonical prior format')
    if raw.get('kind', 'cron') != 'loop':
        _matches(raw.get('next_run_at'), expected.get('next_run_at'))
        return
    next_run = raw.get('next_run_at')
    if not isinstance(next_run, str):
        raise TypeError('Advanced loop lacks its canonical next run')
    due = datetime.fromisoformat(next_run)
    if (due.tzinfo is not None or due.isoformat(timespec='seconds') != next_run
            or scheduler.slot_of(due - timedelta(minutes=max(1, raw['interval_minutes']))) != slot):
        raise ValueError('Advanced loop cadence differs from its firing slot')
