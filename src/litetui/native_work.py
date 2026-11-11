"""Bounded owned Work inspection and reviewed scheduler pause / Task Stop.

Inspection reads frozen facts without process probes or durable mutations.
Explicit reviewed controls own their effects and verify independent readback.
"""
# Calendar dates follow the canonical scheduler's local wall clock.
# ruff: noqa: DTZ011
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from litetui import backend_catalog, backend_transition, calendar_view, scheduler, tasks
from litetui.agent_store import _unlinked, valid_id, valid_name
from litetui.file_observations import stable_read
from litetui.goal_loop import GOAL_FILENAME, GoalState

if TYPE_CHECKING:
    from textual.worker import Worker

    from litetui.background_input import Bridge, Producer

VERSION = 1
MAX_WIRE = 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_CAPTURE = 8 * 1024 * 1024
MAX_ROWS = 4096
MAX_ROW = 128 * 1024
MAX_TAIL = 65536
COMMON = ('version', 'session_id', 'agent_id', 'backend', 'model')
SECTIONS = ('tasks', 'provider_tasks', 'jobs', 'loops')
OPERATIONS = ('snapshot', 'page', 'tail', 'calendar', 'pause_validate', 'pause_apply', 'pause_readback',
              'stop_validate', 'stop_apply', 'stop_settled')
CONSEQUENCE = 'Pause future scheduled prompts only; active tasks, goals, queued input and saved jobs remain unchanged.'
CLOSE_REQUIRED = 'Scheduler pause outcome is uncertain; close this runtime and reopen explicitly.'


@dataclass
class Capture:
    owner: tuple
    instance: str = field(default_factory=lambda: secrets.token_hex(32))
    snapshot: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)
    review: bytes | None = None
    receipt: bytes | None = None


def encoded(value, maximum=MAX_CAPTURE):
    return backend_catalog._encoded(value, maximum)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def text(value, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value.encode('utf-8')) > maximum:
        raise ValueError('Work text is missing, malformed or exceeds its byte budget')
    return value


def integer(value, minimum=0, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Work integer is outside its exact bounds')
    return value


def boolean(value):
    if type(value) is not bool:
        raise TypeError('Work flag must be an exact boolean')
    return value


def finite(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError('Work time must be finite and nonnegative')
    return value


def iso(value):
    text(value, 128)
    datetime.fromisoformat(value)
    return value


def _identity(app):
    backend_transition.require_open(app)
    session = getattr(app, '_agent_session', None)
    store = app.store
    current = valid_id(app.convo_id)
    if (session is None or store.agent_session is not session or store.convo_id != current
            or store.loading or store.persist_error or not store.pending and not store.owned):
        raise ValueError('Work inspection requires the current owned conversation')
    authority = session.authority
    directory = session.conversation_directory(current)
    if store.convo_dir != directory or store.convo_path != directory / 'convo.jsonl':
        raise ValueError('Work conversation path differs from its owned capability')
    backend = text(app.backend.name, 128)
    model = app.model_id
    if model is not None:
        text(model, 4096)
    if authority.backend != backend or authority.model != model:
        raise ValueError('Work identity disagrees with owned agent authority')
    return {'version': VERSION, 'session_id': current, 'agent_id': valid_id(authority.agent_id),
            'backend': backend, 'model': model}


def _owner(app, identity):
    return (*identity.values(), id(app._agent_session), id(app.store), id(app.backend), id(app.settings))


def _common(app, cmd, keys):
    if set(cmd) != {*COMMON, 'type', 'id', *keys}:
        raise ValueError('Work request has missing or unsupported fields')
    text(cmd.get('id'), 128)
    encoded(cmd, MAX_WIRE)
    identity = _identity(app)
    if type(cmd.get('version')) is not int or any(cmd.get(k) != v for k, v in identity.items()):
        raise ValueError('Work request belongs to another owned runtime or version')
    return identity


def _wire(cmd, value):
    # The actual emitter uses ensure_ascii=False; counting both encodings also
    # bounds compatible emitters which escape Unicode or retain default spaces.
    envelope = {'type': 'response', 'id': cmd['id'], 'ok': True, 'result': value}
    if len(json.dumps(envelope, allow_nan=False).encode('utf-8')) > MAX_WIRE:
        raise ValueError('Work response exceeds the complete 1 MiB envelope budget')
    return value


def _stamp(info, *, birthtime=False):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_birthtime_ns if birthtime else info.st_ctime_ns, stat.S_IFMT(info.st_mode))


def _read(path):
    path = _unlinked(Path(path))
    try:
        before = path.stat()
    except FileNotFoundError:
        return None, 'absent'
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
        raise ValueError('Work document is not a bounded regular file')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_FILE + 1)
        finished = os.fstat(stream.fileno())
    after = _unlinked(path).stat()
    if len(raw) > MAX_FILE or not stable_read(before, opened, finished, after, stamp=_stamp):
        raise ValueError('Work document changed during bounded read')
    return raw, hashlib.sha256(raw).hexdigest()


def _source(reader):
    try:
        rows, revision = reader()
        return rows, {'available': True, 'reason': None, 'revision': revision}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return [], {'available': False, 'reason': (str(exc) or type(exc).__name__)[:2048], 'revision': None}


def _row(value):
    encoded(value, MAX_ROW)
    return {**value, 'revision': digest(value)}


def _budget_rows(rows):
    total = 0
    seen = set()
    for count, row in enumerate(rows):
        if count >= MAX_ROWS:
            raise ValueError('Work source exceeds 4096 rows; no rows were omitted silently')
        key = row['row']['id']
        if key in seen:
            raise ValueError('Work source has duplicate row identities')
        seen.add(key)
        total += len(encoded(row, MAX_ROW))
        if total > MAX_CAPTURE // 2:
            raise ValueError('Work source exceeds its 4 MiB capture budget')
        yield row


def _task_row(task, session_id):
    if not isinstance(task, tasks.Task):
        raise TypeError('Host task is not a canonical Task')
    row = task.to_row()
    text(valid_name(row['id']), 128)
    if valid_id(row['convo_id']) != session_id:
        raise ValueError('Task belongs to another conversation')
    for key, cap in (('tool', 256), ('label', 4096), ('prompt', 65536), ('log', 4096)):
        text(row[key], cap, empty=key != 'tool')
    finite(row['started'])
    if row['ended'] is not None:
        finite(row['ended'])
    if row['ok'] is not None:
        boolean(row['ok'])
    for key in ('tokens', 'owner_pid'):
        if row[key] is not None:
            integer(row[key], int(key == 'owner_pid'))
    for key in ('owner_instance', 'owner_created'):
        if row[key] is not None:
            text(row[key], 4096)
    if row['state'] not in {tasks.RUNNING, tasks.DONE, tasks.FAILED, tasks.KILLED, tasks.LOST}:
        raise ValueError('Task has an unknown state')
    if row['owner_instance'] == tasks._INSTANCE_ID and row['owner_pid'] == os.getpid():
        owner = 'current'
    elif row['owner_instance'] is not None or row['owner_pid'] not in (None, os.getpid()):
        owner = 'other'
    else:
        owner = 'unknown'
    tail = row['state'] != tasks.RUNNING
    return _row({'kind': 'host_task', 'owner': owner, 'tail_available': tail,
                 'tail_reason': None if tail else 'Output is available when the task finishes.', 'row': row})


def _host_tasks(app, session_id):
    held = getattr(app, 'bg_tasks', {})
    if not isinstance(held, dict) or len(held) > MAX_ROWS:
        raise ValueError('Held host task collection exceeds the bounded inventory')
    if any(not isinstance(row, tasks.Task) for row in held.values()):
        raise TypeError('Held host task inventory contains a noncanonical row')
    rows = list(_budget_rows(_task_row(t, session_id) for t in tasks.host_tasks_for_app(app)))
    rows.sort(key=lambda r: (-r['row']['started'], r['row']['id']))
    return rows, digest(rows)


def _provider_tasks(app, session_id):
    from litetui.codex_tasks import NativeActivity
    if app.backend.name != 'codex':
        raise ValueError('This provider has no canonical native task inventory exposed here.')
    held = getattr(app, 'codex_native_activity', {})
    if not isinstance(held, dict) or len(held) > MAX_ROWS:
        raise ValueError('Provider task collection exceeds the bounded inventory')
    def bounded_rows():
        for task in held.values():
            if not isinstance(task, NativeActivity):
                raise TypeError('Provider activity is not a canonical NativeActivity')
            if task.convo_id != session_id:
                continue
            row = {key: getattr(task, key) for key in NativeActivity.__dataclass_fields__ if key != 'clock'}
            for key in ('id', 'thread_id', 'turn_id', 'item_id', 'tool'):
                text(row[key], 4096)
            text(row['label'], 16384, empty=True)
            finite(row['started'])
            if row['tokens'] is not None:
                integer(row['tokens'])
            if row['state'] != 'running':
                raise ValueError('Canonical provider activity has an unsupported state')
            yield _row({'kind': 'provider_task', 'owner': 'provider', 'tail_available': False,
                              'tail_reason': 'Provider-owned activity has no host task log or process ownership.', 'row': row})
    rows = list(_budget_rows(bounded_rows()))
    rows.sort(key=lambda r: (-r['row']['started'], r['row']['id']))
    return rows, digest(rows)


def _job(raw):
    if not isinstance(raw, dict) or not {'id', 'prompt', 'schedule', 'created'} <= raw.keys():
        raise ValueError('Job is missing canonical persisted identity or fields')
    job = scheduler.Job(**{k: v for k, v in raw.items() if k in scheduler.Job.__dataclass_fields__})
    row = asdict(job)
    for key, cap in (('id', 128), ('prompt', 65536), ('schedule', 1024), ('created', 128),
                     ('label', 4096), ('tool_profile', 4096), ('owner_convo_id', 128)):
        text(row[key], cap, empty=key in ('prompt', 'schedule', 'label', 'owner_convo_id', 'tool_profile'))
    for key in ('enabled', 'new_conversation'):
        boolean(row[key])
    for key in ('run_count', 'interval_minutes'):
        integer(row[key])
    iso(row['created'])
    for key in ('last_fired_slot', 'next_run_at'):
        if row[key] is not None:
            iso(row[key])
    if row['kind'] not in ('cron', 'loop'):
        raise ValueError('Unknown scheduler job kind')
    if row['kind'] == 'loop':
        integer(row['interval_minutes'], 1)
        valid_id(row['owner_convo_id'])
        iso(row['next_run_at'])
    return _row({'kind': row['kind'], 'effective_tool_profile': scheduler.level_of(job.tool_profile), 'row': row})


def _jobs(app):
    raw, revision = _read(scheduler.jobs_path(app._agent_session.store.data_root))
    if raw is None:
        return [], revision
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError('Jobs must be a bounded canonical list')
    return list(_budget_rows(_job(row) for row in value)), revision


def goal_row(value):
    if not isinstance(value, dict) or not {'id', 'objective', 'created', 'updated'} <= value.keys():
        raise ValueError('Goal is missing canonical persisted identity or fields')
    goal = GoalState(**{k: v for k, v in value.items() if k in GoalState.__dataclass_fields__})
    row = asdict(goal)
    for key, cap in (('id', 128), ('objective', 65536), ('last_reason', 65536), ('last_verdict', 128),
                     ('tool_profile', 4096), ('started_by', 4096)):
        text(row[key], cap, empty=key not in ('id', 'objective'))
    for key in ('created', 'updated'):
        iso(row[key])
    for key in ('evaluated_turns', 'max_turns', 'no_progress_count'):
        integer(row[key])
    if row['status'] not in ('active', 'paused', 'met', 'blocked', 'impossible'):
        raise ValueError('Goal has an unknown status')
    if not isinstance(row['missing_evidence'], list) or len(row['missing_evidence']) > 256:
        raise ValueError('Goal evidence exceeds its bounded list')
    for item in row['missing_evidence']:
        text(item, 4096, empty=True)
    encoded(row, MAX_ROW)
    return row


def _goal(app):
    try:
        raw, revision = _read(app._agent_session.conversation_directory(app.convo_id) / GOAL_FILENAME)
        if raw is None:
            return {'status': 'absent', 'revision': revision, 'value': None, 'reason': None}
        value = json.loads(raw.decode('utf-8-sig'))
        row = goal_row(value)
        return {'status': 'present', 'revision': revision, 'value': row, 'reason': None}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {'status': 'unavailable', 'revision': None, 'value': None,
                'reason': (str(exc) or type(exc).__name__)[:2048]}


def snapshot(app, cmd):
    identity = _common(app, cmd, ())
    owner = _owner(app, identity)
    previous = getattr(app, '_native_work_inspection', None)
    current = Capture(owner, previous.instance) if isinstance(previous, Capture) and previous.owner == owner else Capture(owner)
    app._native_work_inspection = None
    rows, sources = {}, {}
    rows['tasks'], sources['tasks'] = _source(lambda: _host_tasks(app, identity['session_id']))
    rows['provider_tasks'], sources['provider_tasks'] = _source(lambda: _provider_tasks(app, identity['session_id']))
    jobs, source = _source(lambda: _jobs(app))
    rows['jobs'] = [row for row in jobs if row['kind'] == 'cron']
    rows['loops'] = [row for row in jobs if row['kind'] == 'loop']
    sources['jobs'], sources['loops'] = source, copy.deepcopy(source)
    paused = boolean(getattr(app, '_gui_schedules_paused', False))
    quitting = boolean(getattr(app, '_gui_quitting', False))
    scheduler_notice = getattr(app, '_native_work_scheduled_transition_hold', None)
    if scheduler_notice is not None:
        scheduler_notice = text(scheduler_notice, 2048)
    value = {'kind': 'work_snapshot', **identity, 'instance': current.instance, 'captured_at': finite(time.time()),
             'busy': boolean(app._chat_running()), 'calendar_month': date.today().strftime('%Y-%m'),
             'schedules_paused': paused, 'quitting': quitting, 'scheduler_notice': scheduler_notice,
             'counts': {key: len(rows[key]) for key in SECTIONS}, 'sources': sources, 'goal': _goal(app),
             'pause': {'allowed': not paused and not quitting,
                       'reason': 'This runtime is closing.' if quitting else 'Future scheduled prompts are already paused.' if paused else None}}
    from litetui import native_deferred, native_schedule, native_work_execution
    if native_work_execution.enabled(app):
        value["execution_receipt"] = native_work_execution.recovery(app)
    if native_schedule.enabled(app):
        value['deferred_job'] = native_deferred.snapshot_evidence(app)
    value['snapshot'] = digest({'metadata': value, 'rows': rows})
    if _owner(app, _identity(app)) != owner:
        raise ValueError('Work ownership changed during inspection')
    _wire(cmd, value)
    current.snapshot, current.rows = copy.deepcopy(value), copy.deepcopy(rows)
    app._native_work_inspection = current
    return value


def _capture(app, cmd, keys):
    identity = _common(app, cmd, ('instance', 'snapshot', *keys))
    current = getattr(app, '_native_work_inspection', None)
    if (not isinstance(current, Capture) or current.owner != _owner(app, identity)
            or cmd['instance'] != current.instance or cmd['snapshot'] != current.snapshot.get('snapshot')):
        raise ValueError('Work snapshot is stale; refresh before continuing')
    return current


def _base(current, kind):
    return {'kind': kind, **{key: current.snapshot[key] for key in COMMON},
            'instance': current.instance, 'snapshot': current.snapshot['snapshot']}


def _page(cmd, value, rows):
    offset = integer(cmd.get('offset', 0), 0, len(rows))
    limit = integer(cmd.get('limit', 32), 1, 64)
    value.update(offset=offset, total=len(rows), next_offset=None, rows=[])
    for row in rows[offset:offset + limit]:
        value['rows'].append(row)
        try:
            _wire(cmd, value)
        except ValueError:
            value['rows'].pop()
            if not value['rows']:
                raise
            break
    end = offset + len(value['rows'])
    value['next_offset'] = end if end < len(rows) else None
    return _wire(cmd, value)


def page(app, cmd):
    current = _capture(app, cmd, ('section', *(key for key in ('offset', 'limit') if key in cmd)))
    section = cmd['section']
    if not isinstance(section, str) or section not in SECTIONS:
        raise ValueError('Unknown Work section')
    if not current.snapshot['sources'][section]['available']:
        raise ValueError(current.snapshot['sources'][section]['reason'])
    return _page(cmd, {**_base(current, 'work_page'), 'section': section}, current.rows[section])


def tail(app, cmd):
    current = _capture(app, cmd, ('task_id', 'task_revision'))
    selected = next((row for row in current.rows['tasks'] if row['row']['id'] == cmd['task_id']), None)
    if selected is None or selected['revision'] != cmd['task_revision']:
        raise ValueError('Task tail requires the exact selected host row')
    task = getattr(app, 'bg_tasks', {}).get(cmd['task_id'])
    if task is None or _task_row(task, app.convo_id) != selected:
        raise ValueError('Task changed after capture; refresh its Work snapshot')
    root = _unlinked(Path(app._agent_session.store.data_root))
    path = _unlinked(tasks.log_path(task, root))
    if not path.is_relative_to(root):
        raise ValueError('Task log is outside its canonical data root')
    value = {**_base(current, 'work_tail'), 'task_id': cmd['task_id'], 'task_revision': cmd['task_revision'],
             'status': 'running', 'reason': 'Output is available when the task finishes.', 'path': str(path),
             'text': '', 'total_bytes': 0, 'offset': 0, 'bytes_read': 0, 'truncated': False, 'lossy': False, 'revision': 'absent'}
    if task.state != tasks.RUNNING:
        try:
            before = path.stat()
        except FileNotFoundError:
            value.update(status='absent', reason='No canonical task log is present.')
        else:
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Task log must be a regular file')
            offset = max(0, before.st_size - MAX_TAIL)
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                stream.seek(offset)
                data = stream.read(MAX_TAIL)
                finished = os.fstat(stream.fileno())
            after = _unlinked(path).stat()
            if not stable_read(before, opened, finished, after, stamp=_stamp):
                raise ValueError('Task log changed during bounded tail read')
            try:
                decoded, lossy = data.decode('utf-8'), False
            except UnicodeDecodeError:
                decoded, lossy = data.decode('utf-8', errors='replace'), True
            value.update(status='available', reason=None, text=decoded, total_bytes=before.st_size,
                         offset=offset, bytes_read=len(data), truncated=offset > 0, lossy=lossy,
                         revision=digest({'stamp': _stamp(before), 'suffix': hashlib.sha256(data).hexdigest()}))
    if (getattr(app, 'bg_tasks', {}).get(cmd['task_id']) is not task
            or _task_row(task, app.convo_id) != selected or _owner(app, _identity(app)) != current.owner):
        raise ValueError('Task or owned identity changed during tail read')
    return _wire(cmd, value)


class _CalendarJob:
    """Read-only canonical calendar input with one bounded parse per request."""

    def __init__(self, job: scheduler.Job):
        self.id, self.label, self.prompt = job.id, job.label, job.prompt
        self.kind, self.enabled = job.kind, job.enabled
        self._parsed: scheduler.Cron | scheduler.CronError
        try:
            self._parsed = job.cron()
        except scheduler.CronError as exc:
            self._parsed = exc

    def cron(self) -> scheduler.Cron:
        if isinstance(self._parsed, scheduler.CronError):
            raise self._parsed
        return self._parsed


def calendar(app, cmd):
    optional = tuple(key for key in ('date', 'offset', 'limit') if key in cmd)
    current = _capture(app, cmd, ('month', *optional))
    if not current.snapshot['sources']['jobs']['available']:
        raise ValueError(current.snapshot['sources']['jobs']['reason'])
    month = cmd['month']
    if not isinstance(month, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month):
        raise ValueError('Calendar month must be YYYY-MM')
    year, number = map(int, month.split('-'))
    date(year, number, 1)
    jobs = [_CalendarJob(scheduler.Job(**row['row'])) for row in current.rows['jobs']]
    value = {**_base(current, 'work_calendar'), 'month': month, 'date': cmd.get('date'),
             'days': [], 'offset': 0, 'total': 42, 'next_offset': None, 'rows': []}
    if cmd.get('date') is not None:
        selected = date.fromisoformat(text(cmd['date'], 10))
        if selected.isoformat() != cmd['date'] or (selected.year, selected.month) != (year, number):
            raise ValueError('Calendar date must belong to the selected month')
        rows = [asdict(entry) for entry in calendar_view.entries_for(jobs, selected, cap=2)]
        return _page(cmd, value, rows)
    if any(key in cmd for key in ('offset', 'limit')):
        raise ValueError('Calendar month overview has no pagination offset')
    for week in calendar_view.month_matrix(year, number, start_monday=True):
        for day in week:
            when = date(year, number, day) if day else None
            entries = calendar_view.entries_for(jobs, when, cap=2) if when else []
            value['days'].append({'date': when.isoformat() if when else None, 'total': len(entries),
                                  'entries': [asdict(entry) for entry in entries[:2]]})
    return _wire(cmd, value)


def _pause_ready(app, current):
    if boolean(getattr(app, '_gui_quitting', False)):
        raise ValueError('This runtime is closing; scheduler controls are unavailable')
    if boolean(getattr(app, '_gui_schedules_paused', False)):
        raise ValueError('Future scheduled prompts are already paused')
    if not current.snapshot['pause']['allowed']:
        raise ValueError(current.snapshot['pause']['reason'])


def pause_validate(app, cmd):
    previous = getattr(app, '_native_work_inspection', None)
    if isinstance(previous, Capture):
        previous.review = None
    current = _capture(app, cmd, ('action',))
    if cmd['action'] != 'pause_schedules':
        raise ValueError('This review only pauses future scheduled prompts')
    _pause_ready(app, current)
    value = {**_base(current, 'work_pause_review'), 'action': 'pause_schedules',
             'review': secrets.token_hex(32), 'action_id': str(uuid.uuid4()),
             'paused_before': False, 'quitting_before': False, 'scope': 'runtime', 'consequence': CONSEQUENCE}
    _wire(cmd, value)
    current.review = encoded(value)
    current.receipt = None
    return value


def pause_apply(app, cmd):
    current = getattr(app, '_native_work_inspection', None)
    stored = current.review if isinstance(current, Capture) else None
    if isinstance(current, Capture):
        current.review = None
    if stored is None:
        raise ValueError('Scheduler pause needs a fresh unconsumed review')
    review = json.loads(stored)
    expected = {key: value for key, value in review.items() if key != 'kind'}
    if encoded({k: v for k, v in cmd.items() if k not in ('type', 'id')}) != encoded(expected):
        raise ValueError('Scheduler pause must exactly echo its reviewed effect')
    current = _capture(app, cmd, tuple(k for k in expected if k not in (*COMMON, 'instance', 'snapshot')))
    _pause_ready(app, current)
    try:
        app._gui_schedules_paused = True
        if (_owner(app, _identity(app)) != current.owner or not boolean(app._gui_schedules_paused)
                or boolean(getattr(app, '_gui_quitting', False))):
            raise ValueError('Scheduler pause did not retain its owned runtime')
        value = {**_base(current, 'work_pause_result'), 'action': review['action'], 'review': review['review'],
                 'action_id': review['action_id'], 'status': 'paused', 'mutated': True, 'close_required': False,
                 'schedules_paused': True, 'quitting': False}
        value['receipt'] = digest(value)
        _wire(cmd, value)
        current.receipt = encoded(value)
        return value
    except BaseException:
        app._backend_transition_error = CLOSE_REQUIRED
        app._owned_launch_error = CLOSE_REQUIRED
        app._cli_launch_error = CLOSE_REQUIRED
        raise


def pause_readback(app, cmd):
    current = _capture(app, cmd, ('review', 'action_id', 'receipt'))
    receipt = json.loads(current.receipt) if current.receipt is not None else None
    if receipt is None or any(cmd[key] != receipt[key] for key in ('review', 'action_id', 'receipt')):
        raise ValueError('Pause readback does not identify this runtime publication')
    if not boolean(getattr(app, '_gui_schedules_paused', False)) or boolean(getattr(app, '_gui_quitting', False)):
        raise ValueError('Scheduler pause readback changed; no verified success')
    return _wire(cmd, {**_base(current, 'work_pause_readback'), **{key: receipt[key] for key in ('review', 'action_id', 'receipt')},
                       'schedules_paused': True, 'quitting': False, 'busy': boolean(app._chat_running()), 'verified': True})


STOP_CONSEQUENCE = 'Stop only this owned background task. Its final output may still enter this conversation.'
STOP_UNCERTAIN = 'Task Stop retirement or persistence is uncertain; close this runtime and inspect its retained evidence.'


@dataclass
class StopReview:
    wire: bytes
    owner: tuple
    task: tasks.Task
    producer: Producer
    worker: Worker
    bridge: Bridge
    process: object = None
    result: bytes | None = None


def _stop_actor(app, current, selected):
    from litetui import background_input
    from litetui.task_supervisor import process_creation_identity
    if getattr(app, '_gui_quitting', False):
        raise ValueError('Runtime is closing; new Task Stop requests are unavailable')
    task = app.bg_tasks.get(selected['row']['id'])
    if (task is None or _task_row(task, app.convo_id) != selected or selected['owner'] != 'current'
            or task.owner_pid != os.getpid() or task.owner_instance != tasks._INSTANCE_ID
            or task.owner_created != process_creation_identity(os.getpid()) or task.state != tasks.RUNNING):
        raise ValueError('Task changed or lacks exact current owner; refresh Work before Stop')
    value = background_input.bridge(app)
    if value is None or not value.active:
        raise ValueError('Task Stop requires captured worker ownership')
    value.check(allow_error=True)
    records = [record for record in value.producers.values() if record.task is task]
    if (len(records) != 1 or records[0].ref['kind'] != 'host_task'
            or records[0].ref['object_id'] != task.id or records[0].ref['producer_session_id'] != task.convo_id
            or records[0].ref['owner_instance'] != task.owner_instance or records[0].ref['owner_created'] != task.owner_created
            or records[0].phase != 'running' or records[0].worker is None
            or records[0].worker.is_finished or records[0].observer is None):
        raise ValueError('Task Stop lacks its exact live producer and worker')
    if task.proc is None and not tasks.pending_cancellable(task):
        raise ValueError('Task has no owned process or cancellable pending spawn')
    if task.proc is not None:
        integer(getattr(task.proc, 'pid', None), 1)
        if not callable(getattr(task.proc, 'poll', None)):
            raise ValueError('Task process handle has no verifiable lifecycle')
    if _owner(app, _identity(app)) != current.owner:
        raise ValueError('Task Stop changed its owned inspection identity')
    return task, value, records[0]


def stop_validate(app, cmd):
    app._native_work_stop_review = None
    if getattr(app, '_native_work_stop_pending', False):
        raise ValueError('An owned Task Stop is still settling')
    current = _capture(app, cmd, ('task_id', 'task_revision'))
    selected = next((row for row in current.rows['tasks'] if row['row']['id'] == cmd['task_id']), None)
    if selected is None or selected['revision'] != cmd['task_revision']:
        raise ValueError('Task Stop requires the exact displayed host task row')
    task, bridge, producer = _stop_actor(app, current, selected)
    value = {**_base(current, 'work_stop_review'), 'task_id':task.id, 'task_revision':selected['revision'],
        'action':'stop_task', 'review':secrets.token_hex(32), 'action_id':str(uuid.uuid4()),
        'producer_id':producer.ref['producer_id'], 'worker_id':producer.worker_id,
        'lifecycle_instance':bridge.instance, 'owner_pid':task.owner_pid,
        'owner_instance':task.owner_instance, 'owner_created':task.owner_created,
        'stop_mode':'pending_spawn' if task.proc is None else 'process',
        'process_id':None if task.proc is None else task.proc.pid, 'scope':'task', 'consequence':STOP_CONSEQUENCE}
    _wire(cmd, value)
    app._native_work_stop_review = StopReview(encoded(value), current.owner, task, producer, producer.worker, bridge, task.proc)
    return value


def _stop_bound(app, attempt):
    from litetui import background_input
    review = json.loads(attempt.wire)
    if (attempt.producer.ref['producer_id'] != review['producer_id']
            or attempt.producer.worker_id != review['worker_id'] or attempt.bridge.instance != review['lifecycle_instance']
            or attempt.producer.ref['object_id'] != attempt.task.id
            or attempt.producer.ref['producer_session_id'] != app.convo_id
            or any(getattr(attempt.task, key) != review[key] for key in ('owner_pid','owner_instance','owner_created'))
            or attempt.producer.ref['owner_instance'] != review['owner_instance']
            or attempt.producer.ref['owner_created'] != review['owner_created']
            or _owner(app, _identity(app)) != attempt.owner or app.bg_tasks.get(attempt.task.id) is not attempt.task
            or background_input.bridge(app) is not attempt.bridge
            or attempt.bridge.producers.get(attempt.producer.ref['producer_id']) is not attempt.producer
            or attempt.producer.task is not attempt.task or attempt.producer.worker is not attempt.worker):
        raise ValueError('Task Stop lost its exact task, producer, worker or runtime identity')


def _stopped_readback(app, attempt):
    _stop_bound(app, attempt)
    if (not attempt.worker.is_finished or attempt.producer.phase != 'retired'
            or not attempt.producer.observer.done() or attempt.producer.observer.cancelled()
            or attempt.producer.observer.exception() is not None
            or attempt.task.state != tasks.KILLED or attempt.task.ended is None
            or not tasks.process_retired(attempt.task.proc)):
        raise ValueError('Task Stop has not proved worker, callback and process retirement')
    if attempt.process is not None and attempt.task.proc is not attempt.process:
        raise ValueError('Task Stop process handle changed after ownership was captured')
    attempt.process = attempt.task.proc
    raw, revision = _read(app.convo_dir / tasks.STORE)
    rows = json.loads(raw) if raw is not None else None
    if (not isinstance(rows, list)
            or [row for row in rows if isinstance(row, dict) and row.get('id') == attempt.task.id] != [attempt.task.to_row()]):
        raise ValueError('Stopped task row did not persist exactly in its owned store')
    row = _task_row(attempt.task, app.convo_id)
    proof = {'producer_id':attempt.producer.ref['producer_id'], 'worker_id':attempt.producer.worker_id,
        'lifecycle_instance':attempt.bridge.instance, 'worker_retired':True, 'callbacks_retired':True,
        'process_retired':True, 'pending_spawn_settled':True, 'task_persisted':True,
        'process_state':'not_spawned' if attempt.task.proc is None else 'retired',
        'process_id':None if attempt.task.proc is None else attempt.task.proc.pid}
    return row, revision, proof


def _stop_result(app, cmd, attempt, status, *, mutated=False, error=None, readback=None):
    value = {**json.loads(attempt.wire), 'kind':'work_stop_result', 'status':status,
        'mutated':mutated, 'close_required':status == 'uncertain',
        'error':None if error is None else (str(error) or type(error).__name__).encode('utf-8', 'replace')[:2048].decode('utf-8', 'ignore'),
        'row':None, 'store_revision':None, 'proof':None}
    if readback is not None:
        value['row'], value['store_revision'], value['proof'] = readback
    value['receipt'] = digest(value)
    _wire(cmd, value)
    attempt.result = encoded(value)
    app._native_work_stop_attempt = attempt
    return value


async def stop_apply(app, cmd):
    from litetui import background_input
    from litetui.shutdown_wait import join_task
    attempt = getattr(app, '_native_work_stop_review', None)
    app._native_work_stop_review = None
    if not isinstance(attempt, StopReview):
        # A missing/consumed review is invalid protocol state, not a caller type error.
        raise ValueError('Task Stop requires a fresh unconsumed review')  # noqa: TRY004
    review = json.loads(attempt.wire)
    expected = {key:value for key,value in review.items() if key != 'kind'}
    if encoded({key:value for key,value in cmd.items() if key not in ('type','id')}) != encoded(expected):
        raise ValueError('Task Stop must exactly echo its reviewed effect')
    try:
        current = _capture(app, cmd, tuple(key for key in expected if key not in (*COMMON,'instance','snapshot')))
        if getattr(app, '_native_work_stop_pending', False):
            raise ValueError('An owned Task Stop is still settling')
        _stop_bound(app, attempt)
        selected = next(row for row in current.rows['tasks'] if row['row']['id'] == review['task_id'])
        task, bridge, producer = _stop_actor(app, current, selected)
        if task is not attempt.task or bridge is not attempt.bridge or producer is not attempt.producer:
            raise ValueError('Task Stop producer was replaced after review')
        if attempt.process is not None and task.proc is not attempt.process:
            raise ValueError('Task process handle was replaced after review')
    # Every pre-effect validation failure is a refusal; no stop was attempted.
    except Exception as exc:  # noqa: BLE001
        return _stop_result(app, cmd, attempt, 'refused', error=exc)
    app._native_work_stop_pending = True
    try:
        reason = app._kill_background(task.id)
        if reason is not None:
            return _stop_result(app, cmd, attempt, 'refused', error=reason)
        background_input.mark_producer_stop(app, task)
        await join_task(producer.observer)
        return _stop_result(app, cmd, attempt, 'stopped', mutated=True, readback=_stopped_readback(app, attempt))
    # Cancellation after a stop request also requires retained uncertainty proof.
    except BaseException as exc:  # noqa: BLE001
        app._native_work_stop_error = STOP_UNCERTAIN
        attempt.bridge.error = STOP_UNCERTAIN
        return _stop_result(app, cmd, attempt, 'uncertain', mutated=True, error=exc)
    finally:
        app._native_work_stop_pending = False


def stop_settled(app, cmd):
    identity = _common(app, cmd, ('instance','snapshot','review','action_id','receipt'))
    attempt = getattr(app, '_native_work_stop_attempt', None)
    if not isinstance(attempt, StopReview) or attempt.result is None:
        raise ValueError('Task Stop has no retained receipt to verify')
    result = json.loads(attempt.result)
    if result['status'] != 'stopped' or any(cmd[key] != result[key] for key in ('instance','snapshot','review','action_id','receipt')):
        raise ValueError('Task Stop settlement does not name its exact successful receipt')
    row, revision, proof = _stopped_readback(app, attempt)
    if row != result['row'] or proof != result['proof']:
        raise ValueError('Task Stop retirement changed after its result')
    return _wire(cmd, {'kind':'work_stop_settled', **identity,
        **{key:result[key] for key in ('instance','snapshot','review','action_id','receipt','task_id','task_revision')},
        'verified':True, 'row':row, 'proof':proof, 'store_revision':revision})
