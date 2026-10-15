"""Owned runtime restart barrier. No launcher, provider replay, or fleet policy.

A hold stops NEW admissions, never the running turn. Unsupported in-memory state
is a blocker, not silently discarded. The boundary proves only the listed local
files; external provider durability is explicitly outside this implementation.
All calls run on the app's event loop under its existing AgentSession capability.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from litetui.agent_store import StoreError, _unlinked, valid_id

STATE_NAME = 'restart-checkpoint.json'
BOUNDARY_PREFIX = 'restart-boundary-'


class CheckpointError(ValueError):
    """A checkpoint cannot be trusted or the caller does not own its hold."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError('Duplicate checkpoint JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise CheckpointError('Invalid checkpoint JSON constant')


def _read(path):
    value = json.loads(_unlinked(path).read_text(encoding='utf-8'),
                       object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise CheckpointError('Checkpoint object required')
    return value


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, value):
    _unlinked(path)
    fd, name = tempfile.mkstemp(prefix='.restart-pending-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        _unlinked(path)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _controller(app):
    session = getattr(app, '_agent_session', None)
    if session is None:
        raise CheckpointError('Restart checkpoints require an owned agent session')
    existing = getattr(app, '_restart_checkpoint', None)
    if existing is None:
        existing = RuntimeCheckpoint(session)
        app._restart_checkpoint = existing
    if existing.session is not session:
        raise CheckpointError('Checkpoint ownership capability changed')
    return existing


def is_held(app):
    """Admission gate: corrupt/unreadable owned hold state fails closed."""
    if getattr(app, '_agent_session', None) is None:
        return False
    try:
        runtime = _controller(app)
        runtime.session.authority
        return bool(runtime.state and runtime.state['phase'] != 'released')
    except (ValueError, OSError, AttributeError, TypeError):
        return True


def retain_prompt(app, item):
    """Do not drop a prompt already removed from a producer's queue.

    This is not a new durable queue: unsupported accepted input stays visible in
    the existing queue and prevents a checkpoint until its owner reconciles it.
    """
    if not is_held(app):
        return False
    pending = app._pending_input
    if not any(existing is item for existing in pending):
        pending.append(item)
    return True


def _activity(app):
    from litetui.plugin_reload_activity import produce_activity
    from litetui.plugin_reload_children import children_pending
    report = produce_activity(app, children_pending=lambda: children_pending(
        Path.home() / '.litetui-agents', app.convo_id))
    return sorted({f'active:{reason.source}' for reason in report.reasons})


def inventory(app):
    """Read-only inventory: never answers approvals, replays, kills or wakes."""
    blockers = _activity(app)
    if app._chat_running():
        blockers.append('active-turn')
    for field in ('_pending_input', 'pending_image', '_pending_tool_images', 'rejected_prompts',
                  '_rpc_pending_asks', '_gui_model_pending', '_gui_host_pending', '_hook_lifecycle',
                  '_claude_compact_resume', '_interrupted_compact_resume', '_claude_closing',
                  '_codex_recovery_pending', '_restart_poll_inflight'):
        if getattr(app, field, None):
            blockers.append(f'unsupported-pending:{field}')
    for field in ('_approval_waiters', '_rpc_approval_spawners', '_approval_authority_records'):
        values = getattr(app, field, {})
        if values and (field != '_approval_waiters' or any(
                not callable(getattr(v, 'done', None)) or not v.done() for v in values.values())):
            blockers.append(f'unresolved-approval:{field}')
    try:
        if app.query_one('#message-input').value:
            blockers.append('draft-input')
    except Exception:  # absent/unreadable UI cannot prove it has no draft
        blockers.append('draft-input-unavailable')
    for task in getattr(app, 'bg_tasks', {}).values():
        if task.state not in ('done', 'failed', 'cancelled'):
            blockers.append('background-outcome-unresolved')
    goal = getattr(app, 'goal_runtime', None)
    if goal is not None and getattr(goal, '_evaluating', False):
        blockers.append('goal-evaluation-active')
    store = app.store
    if store.pending or store.loading or not store.owned or store.persist_error:
        blockers.append('conversation-persistence-not-ready')
    if getattr(app, '_settings_persist_error', None) or getattr(app, '_background_persist_error', None):
        blockers.append('companion-persistence-failed')
    for message in app.conversation:
        delivery = message.get('codex_delivery')
        if delivery and delivery.get('state') != 'accepted':
            blockers.append('uncertain-codex-delivery')
        steering = (message.get('provider_metadata') or {}).get('steering', [])
        if not isinstance(steering, list):
            blockers.append('malformed-codex-delivery')
        else:
            for entry in steering:
                if not isinstance(entry, dict) or entry.get('state') not in ('accepted', 'denied'):
                    blockers.append('pending-codex-delivery')
    ledger = getattr(app, '_claude_ledger', None)
    if ledger is not None:
        try:
            segment = ledger.selected
            if segment and ledger.pending(segment['id']):
                blockers.append('pending-claude-delivery')
        except Exception:
            blockers.append('claude-ledger-unreadable')
    backend = getattr(app.backend, 'name', '')
    if getattr(app.backend, 'owns_native_turns', False) or hasattr(app.backend, 'app_server'):
        blockers.append(f'provider-durability-unsupported:{backend}')
    return sorted(set(blockers))


def _walk(directory):
    _unlinked(directory)
    if not directory.exists():
        return
    for path in sorted(directory.iterdir()):
        _unlinked(path)  # before recursion: never follow a junction or symlink
        if path.is_dir():
            yield from _walk(path)
        elif path.is_file():
            yield path


def _files(app, *, sync=False):
    session, store = app._agent_session, app.store
    authority = session.authority
    expected = session.conversation_directory(app.convo_id)
    if store.convo_dir != expected or app.convo_dir != expected or store.convo_id != app.convo_id:
        raise CheckpointError('Checkpoint conversation does not match owned store')
    if store.convo_path != expected / 'convo.jsonl':
        raise CheckpointError('Checkpoint transcript does not match owned store')
    home = session.memory_root
    required = [home / 'settings.json', home / 'handoff.md', home / 'memory.md', home / 'soul.md',
                expected / 'convo.jsonl', expected / 'settings.json']
    files = set(required)
    for path in _walk(expected):
        if path.name == '.session.lease' or path.name.startswith(BOUNDARY_PREFIX):
            continue
        if path.name.startswith('.'):
            raise CheckpointError('Unsettled conversation companion file')
        files.add(path)
    files.update(_walk(home / 'memories'))
    from litetui import tasks
    for task in getattr(app, 'bg_tasks', {}).values():
        if getattr(task, 'convo_id', None) == app.convo_id:
            files.add(tasks.log_path(task, session.store.data_root))
    records = []
    for path in sorted(files):
        _unlinked(path)
        if not path.is_relative_to(session.store.data_root):
            raise CheckpointError('Checkpoint file escaped owned data root')
        with path.open('r+b' if sync else 'rb') as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise CheckpointError('Checkpoint source changed during read')
        records.append({'path': str(path), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    # Ordinary append can report an error without raising: compare with the
    # authoritative reader as well as checking its sticky persistence latch.
    if store.read(store.convo_path)[1] != app.conversation:
        raise CheckpointError('In-memory transcript is not durably represented')
    source = {'agent': asdict(authority), 'conversation_id': app.convo_id, 'files': records}
    return records, hashlib.sha256(_json_bytes(source)).hexdigest()


class RuntimeCheckpoint:
    def __init__(self, session):
        self.session = session
        self.runtime_id = str(uuid4())
        self.path = _unlinked(session.memory_root / STATE_NAME)
        self.state = _read(self.path) if self.path.exists() else None
        self.durable = True
        if self.state is not None:
            required = {'schema_version', 'checkpoint_id', 'agent_id', 'conversation_id', 'phase',
                        'disposition', 'prepared_at', 'receipt', 'runtime_id'}
            if (set(self.state) != required or type(self.state['schema_version']) is not int
                    or self.state['schema_version'] != 1 or self.state['phase'] not in ('held', 'checkpointed', 'released')
                    or self.state['disposition'] not in ('idle', 'waiting', 'held')):
                raise CheckpointError('Unsupported or corrupt restart hold')
            for field in ('checkpoint_id', 'agent_id', 'conversation_id', 'runtime_id'):
                valid_id(self.state[field])
            if self.state['agent_id'] != session.authority.agent_id:
                raise CheckpointError('Restart hold belongs to another seat')

    def persist(self):
        self.session.authority
        self.durable = False
        _write(self.path, self.state)
        self.durable = True

    def require(self, checkpoint_id, app):
        valid_id(checkpoint_id)
        if not self.state or self.state['checkpoint_id'] != checkpoint_id:
            raise CheckpointError('Caller does not own this restart hold')
        if self.state['conversation_id'] != app.convo_id:
            raise CheckpointError('Restart hold belongs to another conversation')
        self.session.authority


def prepare(app, checkpoint_id, *, disposition):
    valid_id(checkpoint_id)
    if disposition not in ('idle', 'waiting', 'held'):
        raise CheckpointError('Explicit idle/waiting/held disposition required')
    runtime = _controller(app)
    if runtime.state and runtime.state['phase'] != 'released':
        runtime.require(checkpoint_id, app)
        if not runtime.durable:
            runtime.persist()
        return status(app)
    valid_id(app.convo_id)
    if app.store.pending or not app.store.owned:
        raise CheckpointError('An owned materialized conversation is required')
    runtime.state = {'schema_version': 1, 'checkpoint_id': checkpoint_id,
        'agent_id': runtime.session.authority.agent_id, 'conversation_id': app.convo_id,
        'phase': 'held', 'disposition': 'held' if app._chat_running() else disposition,
        'prepared_at': _now(), 'receipt': None, 'runtime_id': runtime.runtime_id}
    # Memory gate closes BEFORE fsync; a failed write never ACKs preparation.
    runtime.persist()
    return status(app)


def status(app):
    runtime = _controller(app)
    state = runtime.state
    held = bool(state and state['phase'] != 'released')
    blockers = inventory(app)
    receipt = state['receipt'] if state else None
    boundary_ready = False
    if held:
        runtime.require(state['checkpoint_id'], app)
        if runtime.durable and (not runtime.path.exists() or _read(runtime.path) != state):
            blockers.append('hold-record-changed')
        if not runtime.durable:
            blockers.append('hold-not-durable')
        elif receipt:
            try:
                _, revision = _files(app)
                expected = app.convo_dir / f"{BOUNDARY_PREFIX}{state['checkpoint_id']}-{state['runtime_id']}-{receipt['source_revision']}.json"
                if receipt['reference'] != str(expected):
                    raise CheckpointError('Boundary reference mismatch')
                boundary = _read(expected)
                if (hashlib.sha256(_json_bytes(boundary)).hexdigest() != receipt['sha256']
                        or boundary['checkpoint_id'] != state['checkpoint_id']
                        or boundary['agent_id'] != state['agent_id']
                        or boundary['conversation_id'] != app.convo_id
                        or boundary['runtime_id'] != state['runtime_id']):
                    raise CheckpointError('Boundary evidence changed')
                if revision != receipt['source_revision']:
                    blockers.append('boundary-changed')
                elif state['runtime_id'] != runtime.runtime_id:
                    blockers.append('boundary-from-previous-runtime')
                else:
                    boundary_ready = not blockers
            except (OSError, ValueError, KeyError, TypeError):
                blockers.append('boundary-unreadable')
    authority = runtime.session.authority
    connected = (getattr(app, '_gui_connection_success', False)
                 and app.backend.name == authority.backend and app.model_id == authority.model
                 and getattr(app, 'thinking_level', None) == authority.thinking_level)
    registered = getattr(app, 'seat', None)
    identity_ready = (registered is not None and registered.registered
                      and registered.agent_id == authority.agent_id and registered.name == authority.name)
    recovery_blockers = [b for b in blockers if not b.startswith(('provider-durability-unsupported:',
                          'boundary-from-previous-runtime', 'boundary-changed'))]
    readiness = 'blocked'
    if held and connected and identity_ready and getattr(app, '_restart_inbox_ready', False) and not recovery_blockers:
        readiness = 'ready-held' if state['disposition'] == 'held' else 'ready-idle'
    return {'schema_version': 1, 'checkpoint_id': state['checkpoint_id'] if state else None,
            'agent_id': authority.agent_id, 'conversation_id': app.convo_id, 'runtime_id': runtime.runtime_id,
            'held': held, 'disposition': state['disposition'] if state else None,
            'boundary_ready': boundary_ready, 'receipt': receipt if boundary_ready else None,
            'blockers': sorted(set(blockers)), 'readiness': readiness,
            'readiness_scope': 'owned-runtime-only; placement/resources/coordinator-ack excluded'}


def flush(app, checkpoint_id):
    runtime = _controller(app)
    runtime.require(checkpoint_id, app)
    if not is_held(app):
        raise CheckpointError('Checkpoint flush requires its active hold')
    before = status(app)
    if before['boundary_ready']:
        return before
    if inventory(app):
        return before
    # Lazy dependency: ordinary LiteTUI startup does not require liteharness.
    try:
        from liteharness.checkpoint_contract import Receipt, wire
    except ImportError as exc:
        raise CheckpointError('Compatible liteharness checkpoint contract is unavailable') from exc
    records, revision = _files(app, sync=True)
    observed = _now()
    path = _unlinked(app.convo_dir / f'{BOUNDARY_PREFIX}{checkpoint_id}-{runtime.runtime_id}-{revision}.json')
    boundary = {'schema_version': 1, 'checkpoint_id': checkpoint_id,
        'agent_id': runtime.session.authority.agent_id, 'conversation_id': app.convo_id,
        'runtime_id': runtime.runtime_id, 'source_revision': revision, 'observed_at': observed,
        'coverage': 'litetui-owned-files-only', 'files': records}
    if path.exists():
        boundary = _read(path)  # a retry never rewrites an already published boundary
        observed = boundary['observed_at']
    else:
        _write(path, boundary)
    if _files(app)[1] != revision or inventory(app):
        raise CheckpointError('Runtime changed before checkpoint acknowledgement')
    receipt = Receipt(str(uuid4()), checkpoint_id, runtime.session.authority.agent_id, 'boundary',
                      revision, str(path), hashlib.sha256(_json_bytes(boundary)).hexdigest(),
                      True, 'native', observed)
    runtime.state = {**runtime.state, 'phase': 'checkpointed', 'receipt': wire(receipt),
                     'runtime_id': runtime.runtime_id}
    runtime.persist()  # ACK only after boundary AND hold record are durable
    return status(app)


def release(app, checkpoint_id, *, reconciled):
    runtime = _controller(app)
    runtime.require(checkpoint_id, app)
    if reconciled is not True:
        raise CheckpointError('Explicit owner reconciliation is required before releasing a hold')
    if runtime.state['phase'] != 'released' or not runtime.durable:
        previous = runtime.state
        runtime.state = {**previous, 'phase': 'released', 'receipt': None}
        try:
            runtime.persist()
        except BaseException:
            runtime.state = previous  # failure must not open the admission gate
            raise
    return status(app)  # no flush, wake, replay, stop, cancellation or service recreation


def authorize_compaction(app, checkpoint_id, handoff_sha256):
    """One explicit maintenance permission; uses the existing compactor unchanged."""
    runtime = _controller(app)
    runtime.require(checkpoint_id, app)
    if not is_held(app):
        raise CheckpointError('Compaction permission requires the active checkpoint hold')
    blockers = [b for b in inventory(app) if not b.startswith('provider-durability-unsupported:')]
    if blockers:
        raise CheckpointError('Resolve checkpoint blockers before compaction')
    handoff = _unlinked(runtime.session.memory_root / 'handoff.md')
    with handoff.open('r+b') as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
        if handoff_sha256 != digest:
            raise CheckpointError('Explicit handoff digest does not match current handoff')
        stream.flush()
        os.fsync(stream.fileno())
    runtime.compaction_permit = (checkpoint_id, app.convo_id, digest)


def compaction_permitted(app, *, consume=False):
    if not is_held(app):
        return True
    runtime = _controller(app)
    permit = getattr(runtime, 'compaction_permit', None)
    if not permit or permit[:2] != (runtime.state['checkpoint_id'], app.convo_id):
        return False
    digest = hashlib.sha256(_unlinked(runtime.session.memory_root / 'handoff.md').read_bytes()).hexdigest()
    if consume:
        runtime.compaction_permit = None
    return digest == permit[2]
