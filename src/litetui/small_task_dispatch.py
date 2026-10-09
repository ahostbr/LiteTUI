"""Opt-in bounded dispatch through the ordinary named-seat launcher.

The scope is a checked handoff contract, NOT a sandbox or merge permission.
Only the owning leader records a worker's inbox receipt. No retries/recovery
launches: a transport failure can leave a real seat behind.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from litetui import harness, router_record, subagent_local
from litetui import settings as settings_mod
from litetui.agent_store import StoreError, _unlinked, valid_id, valid_name
from litetui.llm_backend import BACKEND_NAMES, BackendError, make_backend


class Unavailable(ValueError):
    """Invalid or unavailable dispatch; no alternate route is permitted."""


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Unavailable('Missing or unknown contract fields')


def _text(value, label, maximum=4000):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise Unavailable(f'Invalid {label}')
    return value


def _token(value, label):
    _text(value, label, 128)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise Unavailable(f'Invalid {label}')
    return value


def _texts(value, label):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise Unavailable(f'Invalid {label}')
    return tuple(_text(item, label) for item in value)


@dataclass(frozen=True)
class Route:
    backend: str
    model: str
    cognitive: str
    thinking_level: str

    @classmethod
    def parse(cls, raw):
        if raw is None:
            raise Unavailable('Small-task dispatch is disabled; configure small_task_route')
        _shape(raw, cls.__dataclass_fields__)
        model = _text(raw['model'], 'model', 128)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*(?:\[[0-9]+[km]\])?', model):
            raise Unavailable('Invalid exact model ID')
        route = cls(**{key: model if key == 'model' else _token(value, key)
                       for key, value in raw.items()})
        # No backend is refused by name: _prove_no_load decides, at dispatch, by what is running.
        if route.backend not in BACKEND_NAMES:
            raise Unavailable('Unknown backend; use a name LiteTUI lists under /backend')
        if route.cognitive.lower().endswith('.md'):
            raise Unavailable('Cognitive profile must be a bare name, not a filename')
        if route.thinking_level not in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise Unavailable('Unknown thinking level')
        if route.model.lower().split('[', 1)[0] in ('auto', 'default', 'inherit', 'local-auto', 'haiku', 'sonnet', 'opus', 'fable'):
            raise Unavailable('An exact model ID is required, not an alias')
        return route


@dataclass(frozen=True)
class Request:
    task_id: str
    purpose: str
    scope: str
    production_line_budget: int
    allowed_files: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    worktree: str
    worker_name: str
    pane: str
    leader_id: str

    @classmethod
    def parse(cls, raw):
        _shape(raw, cls.__dataclass_fields__)
        values = dict(raw)
        for key in ('task_id', 'pane'):
            values[key] = _token(raw[key], key)
        for key in ('purpose', 'scope', 'worktree'):
            values[key] = _text(raw[key], key)
        if type(raw['production_line_budget']) is not int or not 1 <= raw['production_line_budget'] <= 100:
            raise Unavailable('Production-line budget must be 1..100')
        values['worker_name'] = valid_name(_token(raw['worker_name'], 'fresh worker name'))
        values['leader_id'] = valid_id(raw['leader_id'])
        for key in ('allowed_files', 'acceptance_checks'):
            values[key] = _texts(raw[key], key)
        if len(set(values['allowed_files'])) != len(values['allowed_files']):
            raise Unavailable('Duplicate allowed files')
        for name in values['allowed_files']:
            path = PurePosixPath(name)
            if (path.is_absolute() or path.as_posix() != name or '\\' in name or ':' in name
                    or any(part.casefold() in ('.', '..', '.git', '.agents', '.convos') for part in path.parts)
                    or any(c in name for c in '*?[]') or not path.parts):
                raise Unavailable('Allowed files must be literal repo-relative paths without escapes')
            for part in path.parts:
                valid_name(part)
        if values['pane'].lower() in ('self', 'auto'):
            raise Unavailable('An explicit visible pane ID is required')
        return cls(**values)


def _git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            encoding='utf-8', errors='strict', timeout=15, check=False)
    if result.returncode:
        raise Unavailable('Git isolation/receipt verification unavailable')
    return result.stdout.strip()


def _workspace(request, parent):
    raw = Path(request.worktree)
    if not raw.is_absolute():
        raise Unavailable('Worktree must be absolute')
    worktree = _unlinked(raw).resolve()
    parent = Path(parent).resolve()
    if worktree == parent or worktree in parent.parents or parent in worktree.parents:
        raise Unavailable('Shared or nested parent workspace is not isolated')
    if not _unlinked(worktree / '.git').is_file() or Path(_git(worktree, 'rev-parse', '--show-toplevel')).resolve() != worktree:
        raise Unavailable('A caller-prepared separate Git worktree is required')
    gitdir = Path(_git(worktree, 'rev-parse', '--absolute-git-dir')).resolve()
    common = Path(_git(worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    _unlinked(gitdir)
    _unlinked(common)
    if gitdir == common or gitdir.parent != common / 'worktrees':
        raise Unavailable('Unverifiable Git worktree isolation')
    if Path((gitdir / 'gitdir').read_text(encoding='utf-8').strip()).resolve() != worktree / '.git':
        raise Unavailable('Worktree registration disagrees')
    branch = _git(worktree, 'symbolic-ref', '--short', 'HEAD')
    if _git(worktree, 'status', '--porcelain'):
        raise Unavailable('Prepared worktree must be clean')
    for name in request.allowed_files:
        target = _unlinked(worktree / name).resolve()
        if not target.is_relative_to(worktree) or target.is_dir():
            raise Unavailable('Allowed file escapes worktree or names a directory')
    return str(worktree), branch, _git(worktree, 'rev-parse', 'HEAD')


class Journal:
    """One owned-seat journal; unique task/name/worktree reserves before spawn."""
    def __init__(self, path):
        self.path = _unlinked(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS dispatch (task TEXT PRIMARY KEY, '
                       'name TEXT UNIQUE, worktree TEXT UNIQUE, body TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, task):
        with self.connect() as db:
            row = db.execute('SELECT body FROM dispatch WHERE task=?', (task,)).fetchone()
        if row is None:
            raise Unavailable('Unknown task; no launch attempted')
        return json.loads(row[0])

    def reserve(self, record):
        request = record['request']
        with self.connect() as db:
            try:
                db.execute('INSERT INTO dispatch VALUES (?,?,?,?)',
                           (request['task_id'], request['worker_name'].casefold(), record['worktree'].casefold(), json.dumps(record)))
            except sqlite3.IntegrityError:
                raise Unavailable('Task, name or worktree already reserved; needs owner, never retry') from None

    def save(self, record):
        with self.connect() as db:
            db.execute('UPDATE dispatch SET body=? WHERE task=?',
                       (json.dumps(record), record['request']['task_id']))

    def finalize(self, record, expected):
        """Commit one terminal receipt; a racing different report cannot replace it."""
        with self.connect() as db:
            changed = db.execute('UPDATE dispatch SET body=? WHERE task=? AND body=?',
                                 (json.dumps(record), record['request']['task_id'], expected)).rowcount
        if changed:
            return record
        current = self.get(record['request']['task_id'])
        if current['result'] == record['result']:
            return current
        raise Unavailable('A different receipt already exists; needs owner')


def _brief(record):
    return ('Implement ONLY this bounded task contract (not tool permission or merge authority): '
            + json.dumps(record, ensure_ascii=False)
            + '\nCheck inbox and declare purpose. Edit only allowed files, run the focused acceptance checks, '
            'self-review, then commit in your OWN worktree with Task-id, Agent-Tier: worker, '
            'Agent-Name and Agent-ID trailers; no Co-Authored-By. Stop and report if scope or '
            'production budget exceeds the contract. Never change model or expand scope. '
            'Return ONE JSON receipt by inbox to leader_id: operation_id, task_id, agent_id, worktree, branch, '
            'state (candidate-ready or failed), commit (full SHA or null for failed), changed_files '
            '(repo-relative list), checks (list of {command, outcome: passed|failed|not-run}), '
            'limits (list of strings). Include actual diff/stat in your message. '
            'No merge, Done, retry launch, seat closure or new delegation. Human merge authority remains intact.')


def _prove_no_load(route):
    """Refuse a route unless launching and using its seat is PROVEN not to load a model.

    A photograph, taken once: the seat repeats it before every request where the
    engine loads on a request (subagent_local.seat_refusal). Read-only: one state
    query, which is one GET (two to four for llama.cpp, which also reads /props).
    Never load, ensure_running, ensure_chat_ready or an SDK call.
    """
    name, model = route.backend, route.model
    if name in subagent_local.REMOTE_BACKENDS:
        return
    # What a fresh seat reads at startup (app.py: settings_mod.load()), not this seat's live values.
    settings = settings_mod.load()
    settings.backend, settings.default_model = name, model
    if name == 'custom':
        from litetui.custom_backend import api_base
        if not settings.custom_base_url:
            raise Unavailable('No custom server URL is saved for a worker to use')
        try:
            remote = not subagent_local.url_is_local(api_base(settings.custom_base_url))
        except ValueError as exc:
            raise Unavailable(str(exc)) from None
        if remote:
            return
    try:
        backend = make_backend(settings)
        states = backend.subagent_model_states()
        if name == 'custom' and isinstance(states, dict) and 'unknown' in states.values():
            raise ValueError('This custom server is on this machine or a private network and does not '
                             'report which models are loaded')
        subagent_local.require_sole_resident(states, model)
        if name == 'llamacpp' and not backend.single_model:
            # A single-model server has no load route (asleep reads as unloaded above).
            # A router loads on a request unless it was started --no-models-autoload,
            # which only ours is known to be: our record is for a port on THIS machine.
            record = router_record.read()
            if not (record is not None and record.is_mine and router_record.is_live(record)
                    and record.port == urlsplit(backend.host()).port
                    and subagent_local.url_is_this_machine(backend.host())):
                raise ValueError('This llama.cpp router was not started by LiteTUI, so it may load a model on a request')
    except (ValueError, BackendError) as exc:
        raise Unavailable(f'Loading needs approval: {exc}') from None
    except Exception:  # noqa: BLE001 - an answer nobody can read proves nothing; never a tool crash
        raise Unavailable('Loading needs approval: the model server could not be read') from None


def _context(app):
    session = getattr(app, '_agent_session', None)
    seat = getattr(app, 'seat', None)
    if (session is None or not getattr(seat, 'registered', False)
            or seat.agent_id != session.authority.agent_id or seat.tier not in ('leader', 'orchestrator')):
        raise Unavailable('A registered owned leader seat is required')
    return session, Journal(session.memory_root / 'small-task-dispatch.sqlite')


def _dispatch(app, journal, raw):
    # Parse/copy all configuration now. No mutable settings reference crosses launch.
    route = Route.parse(app.settings.small_task_route)
    request = Request.parse(raw)
    if request.leader_id != app.seat.agent_id:
        raise Unavailable('Return destination must be the owning leader')
    # Returning a durable record is idempotent, not another launch (even after a crash).
    try:
        previous = journal.get(request.task_id)
    except Unavailable:
        previous = None
    if previous is not None:
        if previous['request'] != json.loads(json.dumps(asdict(request))):
            raise Unavailable('Task already accepted with a different immutable contract')
        return previous
    session = app._agent_session
    parent = {'agent_id': session.authority.agent_id, 'conversation_id': valid_id(app.convo_id)}
    if not (session.conversation_directory(parent['conversation_id']) / 'convo.jsonl').is_file():
        raise Unavailable('A materialized owned parent conversation is required')
    cwd, branch, baseline = _workspace(request, Path.cwd())
    if harness.harness_disabled() or harness._liteharness_exe() is None:
        raise Unavailable('Ordinary liteharness launcher unavailable or disabled')
    # Before the reservation: a refused route launches nothing and reserves nothing.
    _prove_no_load(route)
    # The external launcher atomically checks persistent name ownership, provider,
    # fleet policy and bridge/owned launch. Never resume/takeover or replace it here.
    if (app._agent_session is not session or app.convo_id != parent['conversation_id']
            or app.seat.agent_id != parent['agent_id'] or not app.seat.registered):
        raise Unavailable('Parent context changed before acceptance; nothing launched')
    record = {'operation_id': str(uuid4()), 'parent': parent, 'request': asdict(request), 'route': asdict(route),
                  'worktree': cwd, 'branch': branch, 'baseline': baseline, 'child_id': None,
                  'state': 'unknown', 'detail': 'Reserved before launch; owner must reconcile if interrupted', 'result': None}
    journal.reserve(record)
    argv = ['spawn', '--split', '--cli', 'litetui', '--name', request.worker_name,
            '--cwd', cwd, '--tier', 'worker', '--cognitive', route.cognitive,
            '--backend', route.backend, '--model', route.model, '--thinking-level', route.thinking_level,
            '--pane', request.pane, '--spawned-by', request.leader_id, '--prompt', _brief(record)]
    try:
        result = harness._cli(argv, timeout=150)
        # Do not persist raw launcher/provider output: it may contain credentials.
        if result.returncode != 0:
            record['detail'] = 'Launcher unavailable/refused or ambiguous; owner must inspect; no retry'
        else:
            for line in reversed(result.stdout.splitlines()):
                try:
                    child = json.loads(line)
                    identity = valid_id(child['agent_id'])
                    if identity != parent['agent_id']:
                        record['child_id'] = identity
                        break
                except (ValueError, TypeError, KeyError):
                    continue
            if record['child_id']:
                record.update(state='dispatched', detail='Named seat dispatched; not completed or merged')
            else:
                record['detail'] = 'Launcher returned no verifiable child identity; no retry'
    except (OSError, subprocess.SubprocessError):
        record['detail'] = 'Launch transport failed; child may exist; no retry'
    journal.save(record)
    return record


@dataclass(frozen=True)
class Receipt:
    operation_id: str
    task_id: str
    agent_id: str
    worktree: str
    branch: str
    state: str
    commit: str | None
    changed_files: list[str]
    checks: list[dict]
    limits: list[str]

    @classmethod
    def parse(cls, raw):
        _shape(raw, cls.__dataclass_fields__)
        receipt = cls(**raw)
        valid_id(receipt.operation_id)
        valid_id(receipt.agent_id)
        _token(receipt.task_id, 'task ID')
        _text(receipt.worktree, 'worktree')
        _text(receipt.branch, 'branch')
        if receipt.state not in ('candidate-ready', 'failed'):
            raise Unavailable('Receipt cannot claim merged or Done')
        if receipt.commit is not None and (not isinstance(receipt.commit, str)
                or not re.fullmatch(r'[0-9a-f]{40}', receipt.commit)):
            raise Unavailable('Invalid commit SHA')
        for key in ('changed_files', 'limits'):
            value = raw[key]
            if not isinstance(value, list) or len(value) > 32:
                raise Unavailable(f'Invalid {key}')
            for item in value:
                _text(item, key)
        if not isinstance(receipt.checks, list) or not 1 <= len(receipt.checks) <= 32:
            raise Unavailable('Receipt requires focused check outcomes')
        for check in receipt.checks:
            _shape(check, ('command', 'outcome'))
            _text(check['command'], 'command')
            if check['outcome'] not in ('passed', 'failed', 'not-run'):
                raise Unavailable('Invalid check outcome')
        return receipt


def _receipt(journal, raw):
    receipt = Receipt.parse(raw)
    record = journal.get(receipt.task_id)
    expected = json.dumps(record)
    if (receipt.operation_id != record['operation_id'] or receipt.agent_id != record['child_id']
            or receipt.worktree != record['worktree'] or receipt.branch != record['branch']
            or record['state'] not in ('dispatched', 'candidate-ready', 'failed')):
        raise Unavailable('Receipt does not match dispatched identity/operation')
    if record['result'] is not None:
        if record['result']['receipt'] != asdict(receipt):
            raise Unavailable('A different receipt already exists; needs owner')
        return record
    request = Request.parse(record['request'])
    if not set(receipt.changed_files) <= set(request.allowed_files):
        raise Unavailable('Receipt exceeds allowed files')
    diff_stat = None
    if receipt.state == 'candidate-ready':
        cwd, branch, _ = _workspace(request, Path.cwd())
        if branch != record['branch'] or not receipt.commit or _git(cwd, 'rev-parse', 'HEAD') != receipt.commit:
            raise Unavailable('Candidate must be committed at the recorded worktree branch HEAD')
        _git(cwd, 'merge-base', '--is-ancestor', record['baseline'], receipt.commit)
        changed = _git(cwd, 'diff', '--no-renames', '--name-only', record['baseline'], receipt.commit).splitlines()
        if set(changed) != set(receipt.changed_files):
            raise Unavailable('Receipt changed files disagree with Git')
        diff_stat = _git(cwd, 'diff', '--no-renames', '--numstat', record['baseline'], receipt.commit)
        try:
            lines = sum(int(value) for row in diff_stat.splitlines() for value in row.split('\t')[:2])
        except ValueError:
            raise Unavailable('Binary candidate needs owner; cannot verify line budget') from None
        # Conservative first slice: count ALL changed lines, including tests.
        if lines > request.production_line_budget or not changed:
            raise Unavailable('Candidate exceeds conservative line budget or has no changes')
        outcomes = {check['command']: check['outcome'] for check in receipt.checks}
        if any(outcomes.get(check) != 'passed' for check in request.acceptance_checks):
            raise Unavailable('Candidate requires all focused checks reported passed')
    record.update(state=receipt.state, detail='Leader-recorded worker report; not independently tested or merged',
                  result={'receipt': asdict(receipt), 'diff_stat': diff_stat, 'reported_by': request.leader_id})
    return journal.finalize(record, expected)


def run(app, args):
    """Synchronous plugin runner; the host provides tool policy/approval dispatch."""
    try:
        if not isinstance(args, dict):
            raise Unavailable('Expected object')
        action = _text(args.get('action'), 'action', 20)
        key = {'dispatch': 'request', 'status': 'task_id', 'receipt': 'receipt'}.get(action)
        if key is None:
            raise Unavailable('Unknown action')
        _shape(args, ('action', key))
        _, journal = _context(app)
        if action == 'dispatch':
            record = _dispatch(app, journal, args[key])
        elif action == 'receipt':
            record = _receipt(journal, args[key])
        else:
            record = journal.get(_token(args[key], 'task ID'))
        return json.dumps(record, ensure_ascii=False)
    except (Unavailable, StoreError) as exc:
        return json.dumps({'state': 'unavailable', 'detail': str(exc)})
    except (OSError, sqlite3.Error, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        return json.dumps({'state': 'unavailable', 'detail': 'Storage/Git unavailable; inspect durable status before any action'})
