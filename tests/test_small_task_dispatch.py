"""Bounded ordinary-seat dispatch. Real owned storage/Git, fake launch boundary."""
import json
import socket
import sqlite3
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from litetui import llm_backend, plugins, router_record, subagent_local
from litetui import small_task_dispatch as dispatch
from litetui.agent_launch_context import create
from litetui.llm_backend import BACKEND_NAMES, BackendError
from litetui.settings import Settings

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
REAL_CLI = dispatch.harness._cli
ROUTE = {'backend': 'claude', 'model': 'claude-fixture-1', 'cognitive': 'Gamma', 'thinking_level': 'low'}


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    with create(tmp_path, 'ParentFixture', agent_id=PARENT, backend='codex',
                model='parent-model', thinking_level='high') as session:
        app = SimpleNamespace(_agent_session=session, convo_id=session.initial_conversation_id,
                              seat=SimpleNamespace(registered=True, agent_id=PARENT, tier='leader'),
                              settings=Settings(small_task_route=deepcopy(ROUTE)), backend=object())
        session.conversation_directory(app.convo_id).joinpath('convo.jsonl').touch()
        calls = []
        def launch(argv, timeout):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout=json.dumps({'agent_id': CHILD}), stderr='')
        monkeypatch.setattr(dispatch.harness, '_cli', launch)
        monkeypatch.setattr(dispatch.harness, '_liteharness_exe', lambda: 'fixture-launcher')
        monkeypatch.setattr(dispatch.harness, 'harness_disabled', lambda: False)
        yield app, calls


@pytest.fixture
def request_data(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'small.txt').write_text('before\n')
    git(repo, 'add', 'small.txt')
    git(repo, 'commit', '-m', 'fixture')
    worktree = tmp_path / 'isolated'
    git(repo, 'worktree', 'add', '-b', 'small-task', str(worktree))
    return {'task_id': 'T-fixture', 'purpose': 'One bounded change', 'scope': 'Only small.txt',
                'production_line_budget': 10, 'allowed_files': ['small.txt'], 'acceptance_checks': ['python check.py'],
                'worktree': str(worktree), 'worker_name': 'Gamma-Fixture', 'pane': 'pane-fixture', 'leader_id': PARENT}


def call(app, action='dispatch', **kwargs):
    return json.loads(dispatch.run(app, dict(action=action, **kwargs)))


def test_dispatch_is_reachable():
    # This assertion fails on the baseline without a missing import/fixture error.
    assert 'litetui.plugins.small_task_plugin' in plugins.PLUGIN_LOAD_ORDER


def test_registered_tool_policy_and_claude_exposure(owned):
    from litetui.claude_tools import HOST_TOOLS
    from litetui.plugins.small_task_plugin import _register
    from litetui.tool_policy import CAPABILITIES
    app, calls = owned
    entries = []
    ctx = SimpleNamespace(app=app, tool=lambda spec, run, **kw: entries.append((spec, run, kw)))
    _register(ctx)
    spec, run, options = entries[0]
    assert spec['function']['name'] == 'small_task'
    assert 'small_task' in HOST_TOOLS
    assert options['policy'].capabilities == frozenset(CAPABILITIES)
    assert json.loads(run({'action': 'dispatch', 'request': {}}))['state'] == 'unavailable'
    assert calls == []


@pytest.mark.parametrize('route', [None, {}, {'extra': 'x'}, {**ROUTE, 'backend': 'no-such-backend'},
    {**ROUTE, 'model': 'haiku'}, {**ROUTE, 'model': ''}, {**ROUTE, 'cognitive': '../Gamma'}, {**ROUTE, 'cognitive': 'Gamma.md'},
    {**ROUTE, 'thinking_level': 'guess'}, {**ROUTE, 'backend': []}])
def test_off_or_invalid_route_never_launches(owned, route):
    app, calls = owned
    app.settings.small_task_route = route
    before = asdict(app.settings), app.backend, app._agent_session.authority
    assert call(app, request={})['state'] == 'unavailable'
    assert calls == []
    assert (asdict(app.settings), app.backend, app._agent_session.authority) == before


@pytest.mark.parametrize('patch', [{'production_line_budget': 101}, {'production_line_budget': True},
    {'allowed_files': ['../escape']}, {'allowed_files': ['/escape']}, {'allowed_files': ['a/../../b']},
    {'allowed_files': ['.git/config']}, {'allowed_files': ['C:/escape']}, {'allowed_files': ['a\\b']},
    {'allowed_files': ['a//b']}, {'allowed_files': ['small.txt', 'small.txt']}, {'extra': 'unknown'},
    {'acceptance_checks': []}, {'worker_name': '--resume'}, {'leader_id': CHILD}, {'pane': ''}])
def test_invalid_request_never_launches(owned, request_data, patch):
    app, calls = owned
    assert call(app, request={**request_data, **patch})['state'] == 'unavailable'
    assert calls == []


@pytest.mark.parametrize('kind', ['parent', 'main', 'plain', 'dirty', 'missing', 'relative'])
def test_unisolated_worktree_never_launches(owned, request_data, tmp_path, monkeypatch, kind):
    app, calls = owned
    if kind == 'parent':
        monkeypatch.chdir(request_data['worktree'])
    elif kind == 'main':
        request_data['worktree'] = str(tmp_path / 'repo')
    elif kind == 'plain':
        request_data['worktree'] = str(tmp_path)
    elif kind == 'dirty':
        (Path(request_data['worktree']) / 'small.txt').write_text('dirty\n')
    elif kind == 'missing':
        request_data['worktree'] = str(tmp_path / 'absent')
    else:
        request_data['worktree'] = 'isolated'
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


def test_launcher_missing_no_fallback(owned, request_data, monkeypatch):
    app, calls = owned
    monkeypatch.setattr(dispatch.harness, '_liteharness_exe', lambda: None)
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_exact_ordinary_launch_parent_unchanged_and_durable_snapshot(owned, request_data, backend):
    app, calls = owned
    app.settings.small_task_route['backend'] = backend
    before = asdict(app.settings), app.backend, app.convo_id, app._agent_session.authority
    result = call(app, request=request_data)
    assert result['state'] == 'dispatched'
    assert result['child_id'] == CHILD
    assert result['parent'] == {'agent_id': PARENT, 'conversation_id': app.convo_id}
    assert len(calls) == 1
    argv = calls[0]
    expected = {'--cli': 'litetui', '--name': request_data['worker_name'], '--cwd': request_data['worktree'],
                '--tier': 'worker', '--cognitive': 'Gamma', '--backend': backend,
                '--model': ROUTE['model'], '--thinking-level': 'low', '--pane': request_data['pane'],
                '--spawned-by': PARENT}
    for flag, value in expected.items():
        assert argv[argv.index(flag) + 1] == value
    assert argv[:2] == ['spawn', '--split']
    assert '--resume' not in argv and '--takeover' not in argv
    assert 'self-review' in argv[-1] and 'No merge' in argv[-1]
    assert (asdict(app.settings), app.backend, app.convo_id, app._agent_session.authority) == before
    app.settings.small_task_route['model'] = 'changed-later'
    assert call(app, request=request_data) == result
    assert len(calls) == 1
    app.settings.small_task_route = None
    assert call(app, 'status', task_id=request_data['task_id']) == result


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'malformed'])
def test_ambiguous_launch_never_retried_and_does_not_leak_output(owned, request_data, monkeypatch, failure):
    app, calls = owned
    def launch(argv, timeout):
        calls.append(argv)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('secret-provider', 150)
        return SimpleNamespace(returncode=2 if failure == 'nonzero' else 0,
                               stdout='secret-provider-token', stderr='private body')
    monkeypatch.setattr(dispatch.harness, '_cli', launch)
    result = call(app, request=request_data)
    assert result['state'] == 'unknown'
    assert 'secret-provider' not in json.dumps(result)
    assert call(app, request=request_data) == result
    assert len(calls) == 1


def test_reservation_survives_interruption(owned, request_data, monkeypatch):
    app, calls = owned
    def launch(argv, timeout):
        calls.append(argv)
        saved = call(app, 'status', task_id=request_data['task_id'])
        assert saved['state'] == 'unknown'
        app.convo_id = CHILD  # An accepted record must not follow the new conversation.
        raise KeyboardInterrupt
    monkeypatch.setattr(dispatch.harness, '_cli', launch)
    original = app.convo_id
    with pytest.raises(KeyboardInterrupt):
        call(app, request=request_data)
    saved = call(app, request=request_data)
    assert saved['state'] == 'unknown' and saved['parent']['conversation_id'] == original
    assert len(calls) == 1


def test_concurrent_dispatch_one_launch(owned, request_data, monkeypatch):
    app, calls = owned
    barrier = Barrier(2)
    original = dispatch._workspace
    def together(*args):
        result = original(*args)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(dispatch, '_workspace', together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(app, request=request_data), range(2)))
    assert len(calls) == 1
    assert sorted(result['state'] for result in results) == ['dispatched', 'unavailable']


def test_same_task_changed_contract_and_reused_name_rejected(owned, request_data):
    app, calls = owned
    call(app, request=request_data)
    assert call(app, request={**request_data, 'scope': 'different'})['state'] == 'unavailable'
    assert call(app, request={**request_data, 'task_id': 'another'})['state'] == 'unavailable'
    assert len(calls) == 1


def candidate(app, request):
    record = call(app, request=request)
    root = Path(request['worktree'])
    (root / 'small.txt').write_text('after\n')
    git(root, 'add', 'small.txt')
    git(root, 'commit', '-m', 'fixture candidate')
    return {'operation_id': record['operation_id'], 'task_id': request['task_id'], 'agent_id': CHILD,
                'worktree': record['worktree'], 'branch': record['branch'], 'state': 'candidate-ready',
                'commit': git(root, 'rev-parse', 'HEAD'), 'changed_files': ['small.txt'],
                'checks': [{'command': 'python check.py', 'outcome': 'passed'}], 'limits': ['Reported checks, not rerun']}


def test_candidate_receipt_git_verified_not_merged(owned, request_data):
    app, calls = owned
    receipt = candidate(app, request_data)
    result = call(app, 'receipt', receipt=receipt)
    assert result['state'] == 'candidate-ready'
    assert result['result']['diff_stat'] == '1\t1\tsmall.txt'
    assert result['result']['reported_by'] == PARENT
    assert call(app, 'receipt', receipt=receipt) == result
    assert len(calls) == 1


@pytest.mark.parametrize('patch', [{'agent_id': PARENT}, {'operation_id': PARENT}, {'worktree': 'wrong'},
    {'branch': 'wrong'}, {'state': 'Done'}, {'commit': 10**39}, {'commit': '0'*40}, {'changed_files': ['other']},
    {'checks': [{'command': 'python check.py', 'outcome': 'failed'}]}])
def test_receipt_mismatch_rejected(owned, request_data, patch):
    app, _ = owned
    receipt = candidate(app, request_data)
    assert call(app, 'receipt', receipt={**receipt, **patch})['state'] == 'unavailable'
    assert call(app, 'status', task_id=request_data['task_id'])['state'] == 'dispatched'


def test_budget_receipt_and_failed_state(owned, request_data):
    app, _ = owned
    request_data['production_line_budget'] = 1
    receipt = candidate(app, request_data)
    assert call(app, 'receipt', receipt=receipt)['state'] == 'unavailable'
    receipt.update(state='failed', commit=None)
    assert call(app, 'receipt', receipt=receipt)['state'] == 'failed'


def test_released_or_unregistered_parent_cannot_dispatch(owned, request_data):
    app, calls = owned
    app.seat.registered = False
    assert call(app, request=request_data)['state'] == 'unavailable'
    app.seat.registered = True
    app._agent_session.release()
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


def test_settings_ownership_and_roundtrip(tmp_path):
    from litetui.settings import load, save
    from litetui.settings_scope import SETTING_SPECS, SettingScope, validate_registry
    validate_registry()
    assert Settings().small_task_route is None
    assert SETTING_SPECS['small_task_route'].scope == SettingScope.DEVICE
    save(Settings(small_task_route=ROUTE), root=tmp_path)
    assert load(root=tmp_path).small_task_route == ROUTE


def test_parent_preservation_guard_catches_mutation(owned, request_data, monkeypatch):
    app, _ = owned
    original = dispatch._dispatch
    def mutant(*args):
        result = original(*args)
        app.settings.backend = 'mutant'
        return result
    monkeypatch.setattr(dispatch, '_dispatch', mutant)
    # Prove the preservation assertion is sensitive to a plausible accidental switch.
    with pytest.raises(AssertionError):
        test_exact_ordinary_launch_parent_unchanged_and_durable_snapshot(owned, request_data, 'claude')


@pytest.mark.parametrize('args', [None, [], {}, {'action': []}, {'action': 'status', 'task_id': []},
                                  {'action': 'receipt', 'receipt': []}])
def test_malformed_envelope_never_launches(owned, args):
    app, calls = owned
    assert json.loads(dispatch.run(app, args))['state'] == 'unavailable'
    assert calls == []


def test_real_launcher_resolver_door_fake_process_boundary(owned, request_data, monkeypatch):
    app, calls = owned
    monkeypatch.setattr(dispatch.harness, '_cli', REAL_CLI)
    def process(argv, timeout):
        calls.append(argv)
        assert argv[0] == 'fixture-launcher'
        assert argv[1:3] == ['spawn', '--split']
        assert timeout == 150
        return SimpleNamespace(returncode=0, stdout=json.dumps({'agent_id': CHILD}), stderr='')
    monkeypatch.setattr(dispatch.harness.ttyguard, 'run', process)
    assert call(app, request=request_data)['state'] == 'dispatched'
    assert len(calls) == 1


def test_changed_parent_before_acceptance_never_launches(owned, request_data, monkeypatch):
    app, calls = owned
    original = dispatch._workspace
    def changed(*args):
        result = original(*args)
        app.convo_id = CHILD
        return result
    monkeypatch.setattr(dispatch, '_workspace', changed)
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


def test_scope_links_are_rejected_without_creating_links(owned, request_data, monkeypatch):
    from litetui.agent_store import StoreError
    app, calls = owned
    original = dispatch._unlinked
    def linked(path):
        if Path(path).name == 'small.txt':
            raise StoreError('Linked storage paths are not supported')
        return original(path)
    monkeypatch.setattr(dispatch, '_unlinked', linked)
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


@pytest.mark.asyncio
async def test_settings_route_control_roundtrip_and_validation(tmp_path):
    from textual.app import App
    from textual.widgets import Input

    from litetui import settings as settings_mod
    from litetui.settings_screen import SettingsBody, SettingsScreen
    settings_mod.save(Settings(small_task_route=ROUTE), root=tmp_path)
    saved_path = settings_mod.settings_path(tmp_path)
    saved = saved_path.read_bytes()
    results = []

    class Host(App):
        def on_mount(self):
            self.push_screen(SettingsScreen(Settings(small_task_route=deepcopy(ROUTE)), models=[]), results.append)

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen.query_one(SettingsBody)
        control = screen.query_one('#f-small_task_route', Input)
        assert screen._collect().small_task_route == ROUTE
        for raw in ('', 'null'):
            control.value = raw
            assert screen._collect().small_task_route is None
        for raw in ('{', '{}', '[]', '{"backend":"lmstudio"}'):
            control.value = raw
            with pytest.raises(ValueError):
                screen._collect()
            screen.action_save()
            await pilot.pause()
            assert results == []
            assert saved_path.read_bytes() == saved
            assert screen._start.small_task_route == ROUTE
        screen._set_dialog_values(Settings(), ('small_task_route',))
        assert control.value == ''
        control.value = json.dumps(ROUTE)
        assert screen._collect().small_task_route == ROUTE


@pytest.mark.parametrize('allowed,budget,listed,expected,detail', [
    (['result.txt'], 10, ['result.txt'], 'unavailable', 'changed files'),
    (['small.txt', 'result.txt'], 1, ['small.txt', 'result.txt'], 'unavailable', 'budget'),
    (['small.txt', 'result.txt'], 2, ['small.txt', 'result.txt'], 'candidate-ready', ''),
])
def test_review_rename_scope_and_budget(owned, request_data, allowed, budget, listed, expected, detail):
    app, _ = owned
    request_data.update(allowed_files=allowed, production_line_budget=budget)
    record = call(app, request=request_data)
    root = Path(request_data['worktree'])
    git(root, 'mv', 'small.txt', 'result.txt')
    git(root, 'commit', '-m', 'rename fixture')
    receipt = {'operation_id': record['operation_id'], 'task_id': request_data['task_id'],
               'agent_id': CHILD, 'worktree': record['worktree'], 'branch': record['branch'],
               'state': 'candidate-ready', 'commit': git(root, 'rev-parse', 'HEAD'),
               'changed_files': listed,
               'checks': [{'command': 'python check.py', 'outcome': 'passed'}], 'limits': []}
    result = call(app, 'receipt', receipt=receipt)
    assert result['state'] == expected
    assert detail in result['detail']
    if expected == 'candidate-ready':
        assert set(result['result']['diff_stat'].splitlines()) == {'1\t0\tresult.txt', '0\t1\tsmall.txt'}
    else:
        assert call(app, 'status', task_id=request_data['task_id'])['state'] == 'dispatched'


@pytest.mark.parametrize('mode', ['different-failures', 'candidate-and-failure', 'identical'])
def test_review_terminal_receipt_concurrency(owned, request_data, monkeypatch, mode):
    app, _ = owned
    first = candidate(app, request_data)
    if mode != 'candidate-and-failure':
        first.update(state='failed', commit=None)
    second = deepcopy(first)
    if mode != 'identical':
        second.update(state='failed', commit=None, limits=['different terminal result'])
    barrier = Barrier(2)
    original = dispatch.Journal.get
    def together(journal, task):
        record = original(journal, task)
        if record['result'] is None:
            barrier.wait(timeout=10)
        return record
    monkeypatch.setattr(dispatch.Journal, 'get', together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda receipt: call(app, 'receipt', receipt=receipt), (first, second)))
    accepted = [result for result in results if result['state'] != 'unavailable']
    assert len(accepted) == (2 if mode == 'identical' else 1)
    saved = call(app, 'status', task_id=request_data['task_id'])
    assert all(saved['result'] == result['result'] for result in accepted)
    if mode != 'identical':
        refusal = next(result for result in results if result['state'] == 'unavailable')
        assert 'receipt' in refusal['detail']


@pytest.mark.parametrize('model', ['claude-sonnet-5[1m]', 'claude-fable-5-1[1m]'])
def test_review_context_qualified_exact_model_is_preserved(owned, request_data, model):
    app, calls = owned
    app.settings.small_task_route['model'] = model
    result = call(app, request=request_data)
    assert result['state'] == 'dispatched'
    assert result['route']['model'] == model
    assert calls[0][calls[0].index('--model') + 1] == model


@pytest.mark.parametrize('alias', ['fable', 'fable[1m]', 'opus[1m]', 'sonnet[1m]', 'haiku'])
def test_review_moving_model_alias_is_rejected(owned, request_data, alias):
    app, calls = owned
    app.settings.small_task_route['model'] = alias
    assert call(app, request=request_data)['state'] == 'unavailable'
    assert calls == []


# ── T0408-I: any backend, and a proof that the launch loads nothing ─────────
#
# NO MODEL IS LOADED AND NO SERVER IS CONTACTED BY ANYTHING BELOW. Backends are
# recording stubs, or the real classes over an in-process fake of their HTTP
# read. `sealed` fails the test on a socket, real DNS, a process that is not
# git, a load, a start or a POST.

MODEL = 'local-fixture-1'
NEEDS_APPROVAL = 'Loading needs approval: '
PUBLIC = 'https://openrouter.example.com/api/v1'
PUBLIC_ADDRESS = '104.18.2.115'


@pytest.fixture
def sealed(monkeypatch):
    """Every way a model could be loaded or a server reached, recorded and refused."""
    violations = []

    def forbid(label):
        def refuse(*args, **kwargs):
            violations.append(label)
            raise AssertionError(f'{label} is forbidden in a dispatch proof')
        return refuse

    real_popen = subprocess.Popen

    def only_git(args, *rest, **kwargs):
        if not (isinstance(args, list) and args and args[0] == 'git'):
            violations.append(f'process {args!r}')
            raise AssertionError('only git may be started')
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', only_git)
    monkeypatch.setattr(socket, 'getaddrinfo', forbid('real DNS'))
    monkeypatch.setattr(socket.socket, 'connect', forbid('socket connect'))
    monkeypatch.setattr(urllib.request, 'urlopen', forbid('urlopen'))
    monkeypatch.setattr(dispatch.harness.ttyguard, 'popen', forbid('ttyguard.popen'))
    monkeypatch.setattr(dispatch.harness.ttyguard, 'run', forbid('ttyguard.run'))
    for cls in (llm_backend.LMStudioBackend, llm_backend.LlamaCppBackend):
        for verb in ('load', 'unload', 'ensure_running', 'ensure_chat_ready', 'apply_load_settings'):
            monkeypatch.setattr(cls, verb, forbid(f'{cls.__name__}.{verb}'))
    monkeypatch.setattr(llm_backend.LMStudioBackend, '_sdk', forbid('LM Studio SDK'))
    monkeypatch.setattr(llm_backend.LlamaCppBackend, '_spawn', forbid('llama-server spawn'))
    return violations


@pytest.fixture
def cheap_request(tmp_path, monkeypatch):
    """A valid contract without Git: these tests are about the route, not the worktree."""
    worktree = tmp_path / 'isolated'
    monkeypatch.setattr(dispatch, '_workspace', lambda request, parent: (str(worktree), 'small-task', '0' * 40))
    return {'task_id': 'T-fixture', 'purpose': 'One bounded change', 'scope': 'Only small.txt',
            'production_line_budget': 10, 'allowed_files': ['small.txt'], 'acceptance_checks': ['python check.py'],
            'worktree': str(worktree), 'worker_name': 'Gamma-Fixture', 'pane': 'pane-fixture', 'leader_id': PARENT}


def journal_rows(app):
    path = app._agent_session.memory_root / 'small-task-dispatch.sqlite'
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT count(*) FROM dispatch').fetchone()[0]


def save_child_settings(**values):
    """What a fresh worker seat reads at startup: the saved settings file."""
    from litetui import settings as settings_mod
    settings_mod.settings_path().write_text(json.dumps(values), encoding='utf-8')


def resolver(table):
    def resolve(host):
        if host not in table:
            raise AssertionError(f'unexpected DNS question: {host!r}')
        answer = table[host]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return resolve


class ReadOnlyBackend:
    """Recording stub: the state query and three facts are allowed, nothing else."""
    def __init__(self, name, states, *, single=False):
        self.name, self.single_model, self.calls, self._states = name, single, [], states

    def host(self):
        return 'http://127.0.0.1:7470'

    def subagent_model_states(self):
        self.calls.append('subagent_model_states')
        if isinstance(self._states, Exception):
            raise self._states
        return dict(self._states)

    def __getattr__(self, name):
        raise AssertionError(f'dispatch touched backend.{name}: only the read-only state query is allowed')


def stub_backends(monkeypatch, states, *, single=False):
    built = []

    def make(settings):
        backend = ReadOnlyBackend(settings.backend, states, single=single)
        built.append(backend)
        return backend
    monkeypatch.setattr(dispatch, 'make_backend', make, raising=False)
    return built


def own_router(monkeypatch, owner, port=7470):
    record = owner and router_record.RouterRecord(1, owner, 4242, port, 'fixture.ini', '2026-10-08T00:00:00+00:00')
    monkeypatch.setattr(router_record, 'read', lambda path=None: record)
    monkeypatch.setattr(router_record, 'is_live', lambda found: found is not None)


def test_route_parse_accepts_every_listed_backend_and_no_other():
    def parses(name):
        try:
            return dispatch.Route.parse({**ROUTE, 'backend': name}).backend == name
        except dispatch.Unavailable:
            return False
    assert {name: parses(name) for name in BACKEND_NAMES} == dict.fromkeys(BACKEND_NAMES, True)
    assert not parses('no-such-backend')


STATES = {
    'loaded': {MODEL: 'loaded'},
    'loading': {MODEL: 'loading'},
    'unloaded': {MODEL: 'unloaded'},
    'unknown': {MODEL: 'unknown'},
    'error': BackendError('fixture: the server answered with an error'),
    'unreachable': ConnectionRefusedError('fixture: nothing is listening'),
    'nothing': {},
    'another': {'other-model': 'loaded', MODEL: 'unloaded'},
    'second': {MODEL: 'loaded', 'other-model': 'loaded'},
    'other-loading': {MODEL: 'loaded', 'other-model': 'loading'},
}
# kind: (backend name, saved settings, single-model llama.cpp, router owner)
KINDS = {
    'claude': ('claude', {}, False, None),
    'codex': ('codex', {}, False, None),
    'cline': ('cline', {}, False, None),
    'free': ('free', {}, False, None),
    'custom-public': ('custom', {'custom_base_url': PUBLIC}, False, None),
    'custom-local': ('custom', {'custom_base_url': 'http://127.0.0.1:8080/v1'}, False, None),
    'lmstudio': ('lmstudio', {}, False, None),
    'strata': ('strata', {}, False, None),
    'ninfer': ('ninfer', {}, False, None),
    'llamacpp-single': ('llamacpp', {}, True, None),
    'llamacpp-own-router': ('llamacpp', {}, False, 'litetui'),
    'llamacpp-litesuite-router': ('llamacpp', {}, False, 'litesuite'),
    'llamacpp-unrecorded-router': ('llamacpp', {}, False, None),
}
NEVER_ASKED = {'claude', 'codex', 'cline', 'free', 'custom-public'}
ACCEPTED_WHEN_LOADED = {'custom-local', 'lmstudio', 'strata', 'ninfer', 'llamacpp-single', 'llamacpp-own-router'}


@pytest.mark.parametrize('state', sorted(STATES))
@pytest.mark.parametrize('kind', sorted(KINDS))
def test_accept_refuse_table_by_backend_and_state(owned, cheap_request, sealed, monkeypatch, kind, state):
    app, calls = owned
    name, saved, single, owner = KINDS[kind]
    app.settings.small_task_route = {**ROUTE, 'backend': name, 'model': MODEL}
    save_child_settings(**saved)
    built = stub_backends(monkeypatch, STATES[state], single=single)
    own_router(monkeypatch, owner)
    monkeypatch.setattr(subagent_local, 'resolve_host',
                        resolver({'openrouter.example.com': [PUBLIC_ADDRESS]}), raising=False)
    result = call(app, request=cheap_request)
    accepted = kind in NEVER_ASKED or (kind in ACCEPTED_WHEN_LOADED and state == 'loaded')
    if accepted:
        assert result['state'] == 'dispatched', result
        assert result['route']['backend'] == name and result['route']['model'] == MODEL
        assert len(calls) == 1 and calls[0][calls[0].index('--backend') + 1] == name
        assert journal_rows(app) == 1
    else:
        assert result['state'] == 'unavailable', result
        assert result['detail'].startswith(NEEDS_APPROVAL), result
        assert len(result['detail']) > len(NEEDS_APPROVAL) + 20, 'a refusal says why'
        assert calls == [], 'a refused route launches nothing'
        assert journal_rows(app) == 0, 'a refused route reserves nothing'
        assert call(app, 'status', task_id=cheap_request['task_id'])['detail'].startswith('Unknown task')
    queries = [query for backend in built for query in backend.calls]
    assert queries == ([] if kind in NEVER_ASKED else ['subagent_model_states']), queries
    assert len(built) == (0 if kind in NEVER_ASKED else 1)
    assert sealed == []


LOCAL, REMOTE = True, False
LOCALITY = [
    ('http://localhost:1234/v1', {}, LOCAL),
    ('http://LOCALHOST:1234/v1', {}, LOCAL),
    ('http://localhost.:1234/v1', {}, LOCAL),
    ('http://api.localhost/v1', {}, LOCAL),
    ('http://127.1:1234', {}, LOCAL),
    ('http://2130706433:1234', {}, LOCAL),
    ('http://0x7f.0.0.1:1234', {}, LOCAL),
    ('http://127.0.0.1:1234', {}, LOCAL),
    ('http://[::1]:8080/v1', {}, LOCAL),
    ('http://[::ffff:127.0.0.1]:8080/v1', {}, LOCAL),
    ('http://0.0.0.0:8000', {}, LOCAL),
    ('http://[::]:8000', {}, LOCAL),
    ('http://192.168.1.20:1234', {}, LOCAL),
    ('http://10.1.2.3:1234', {}, LOCAL),
    ('http://172.16.9.9:1234', {}, LOCAL),
    ('http://169.254.10.10:1234', {}, LOCAL),
    ('http://[fe80::1]:1234', {}, LOCAL),
    ('http://[fd00::1]:1234', {}, LOCAL),
    ('http://100.64.0.7:1234', {}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': ['127.0.0.1']}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': ['::1']}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': ['192.168.1.20']}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': [PUBLIC_ADDRESS, '10.0.0.5']}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': ['fe80::1%eth0']}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': socket.gaierror('fixture: no such host')}, LOCAL),
    ('http://rig.example.com:1234', {'rig.example.com': []}, LOCAL),
    ('http://rig.example.com.:1234', {'rig.example.com': ['127.0.0.1']}, LOCAL),
    ('http://workstation:1234', {'workstation': ['192.168.1.20']}, LOCAL),
    (PUBLIC, {'openrouter.example.com': [PUBLIC_ADDRESS]}, REMOTE),
    ('https://openrouter.example.com./api/v1', {'openrouter.example.com': [PUBLIC_ADDRESS]}, REMOTE),
    ('https://openrouter.example.com', {'openrouter.example.com': [PUBLIC_ADDRESS, '2606:4700::6812:273']}, REMOTE),
    (f'https://{PUBLIC_ADDRESS}/v1', {}, REMOTE),
    ('https://[2606:4700::6812:273]/v1', {}, REMOTE),
]


@pytest.mark.parametrize('url,names,local', LOCALITY)
def test_custom_url_locality_table(owned, cheap_request, sealed, monkeypatch, url, names, local):
    """A custom URL is remote only when EVERY address it resolves to is public."""
    app, calls = owned
    app.settings.small_task_route = {**ROUTE, 'backend': 'custom', 'model': MODEL}
    save_child_settings(custom_base_url=url)
    built = stub_backends(monkeypatch, {MODEL: 'unknown'})   # a plain server: no state per model
    monkeypatch.setattr(subagent_local, 'resolve_host', resolver(names), raising=False)
    result = call(app, request=cheap_request)
    if local:
        assert result['state'] == 'unavailable', result
        assert result['detail'].startswith(NEEDS_APPROVAL), result
        assert [backend.calls for backend in built] == [['subagent_model_states']]
        assert calls == [] and journal_rows(app) == 0
    else:
        assert result['state'] == 'dispatched', result
        assert built == [], 'a public server is never asked for its state'
        assert len(calls) == 1 and journal_rows(app) == 1
    assert sealed == []


@pytest.mark.parametrize('saved,reason', [
    ({}, 'No custom server URL'),
    ({'custom_base_url': ''}, 'No custom server URL'),
    ({'custom_base_url': 'ftp://rig.example.com'}, 'Server URL'),
    ({'custom_base_url': 'http://user:secret@rig.example.com'}, 'Server URL'),
])
def test_custom_route_without_a_usable_saved_url_is_refused(owned, cheap_request, sealed, monkeypatch, saved, reason):
    app, calls = owned
    app.settings.small_task_route = {**ROUTE, 'backend': 'custom', 'model': MODEL}
    save_child_settings(**saved)
    built = stub_backends(monkeypatch, {MODEL: 'loaded'})
    monkeypatch.setattr(subagent_local, 'resolve_host', resolver({}), raising=False)
    result = call(app, request=cheap_request)
    assert result['state'] == 'unavailable' and reason in result['detail'], result
    assert 'secret' not in json.dumps(result)
    assert calls == [] and journal_rows(app) == 0 and built == [] and sealed == []


def test_the_route_is_read_from_saved_settings_not_from_the_leaders_own(owned, cheap_request, sealed, monkeypatch):
    """The leader's live URL is public; the file a fresh worker reads says this machine."""
    app, calls = owned
    app.settings.small_task_route = {**ROUTE, 'backend': 'custom', 'model': MODEL}
    app.settings.custom_base_url = PUBLIC
    save_child_settings(custom_base_url='http://127.0.0.1:1234/v1')
    stub_backends(monkeypatch, {MODEL: 'unknown'})
    monkeypatch.setattr(subagent_local, 'resolve_host', resolver({}), raising=False)
    result = call(app, request=cheap_request)
    assert result['state'] == 'unavailable' and result['detail'].startswith(NEEDS_APPROVAL), result
    assert calls == [] and sealed == []


def test_an_accepted_task_is_returned_again_without_a_second_proof(owned, cheap_request, sealed, monkeypatch):
    app, calls = owned
    app.settings.small_task_route = {**ROUTE, 'backend': 'lmstudio', 'model': MODEL}
    save_child_settings()
    states = {MODEL: 'loaded'}
    built = stub_backends(monkeypatch, states)
    first = call(app, request=cheap_request)
    assert first['state'] == 'dispatched', first
    states.clear()   # the model has since been unloaded
    assert call(app, request=cheap_request) == first
    assert len(calls) == 1 and len(built) == 1 and sealed == []


class StubServer:
    """In-process stand-in for a model server's HTTP reads. No socket exists."""
    def __init__(self, routes, violations):
        self.routes, self.requests, self.violations = routes, [], violations

    def read(self, url, body=None):
        method = 'GET' if body is None else 'POST'
        self.requests.append((method, urlsplit(url).path))
        if body is not None:
            self.violations.append(f'POST {url}')
            raise AssertionError('a proof never POSTs')
        if urlsplit(url).path not in self.routes:
            raise OSError(f'fixture: nothing answers {url}')
        return self.routes[urlsplit(url).path]


def serve(monkeypatch, sealed, routes):
    """Put one StubServer under every backend's own HTTP read."""
    import httpx

    from litetui import gpu_gate
    from litetui.ninfer_backend import NInferBackend
    from litetui.strata_backend import StrataBackend
    server = StubServer(routes, sealed)

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            try:
                return Response(server.read(url))
            except OSError as exc:
                raise httpx.ConnectError(str(exc)) from exc

        def __getattr__(self, name):
            sealed.append(f'httpx.Client.{name}')
            raise AssertionError(f'httpx.Client.{name} is not a read')

    def get_json(self, url, timeout=10.0):
        try:
            return server.read(url)
        except OSError as exc:
            raise BackendError('fixture: the engine did not answer') from exc

    monkeypatch.setattr(llm_backend, '_http_json', lambda url, body=None, timeout=10.0: server.read(url, body))
    monkeypatch.setattr(httpx, 'Client', Client)
    monkeypatch.setattr(NInferBackend, '_get_json', get_json)
    monkeypatch.setattr(StrataBackend, '_get_json', get_json)
    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', lambda: True)
    return server


ROUTER_PROPS = {'role': 'router', 'model_path': 'none'}
# (case, backend, saved settings, router owner, server routes, accepted, the only paths it may read)
REAL = [
    ('lmstudio-exact-model-loaded', 'lmstudio', {}, None,
     {'/api/v0/models': {'data': [{'id': MODEL, 'state': 'loaded', 'loaded_context_length': 8192},
                                  {'id': 'other-model', 'state': 'not-loaded'}]}}, True, {'/api/v0/models'}),
    ('lmstudio-nothing-loaded', 'lmstudio', {}, None,
     {'/api/v0/models': {'data': [{'id': MODEL, 'state': 'not-loaded'}]}}, False, {'/api/v0/models'}),
    ('lmstudio-another-model-loaded', 'lmstudio', {}, None,
     {'/api/v0/models': {'data': [{'id': MODEL, 'state': 'not-loaded'},
                                  {'id': 'other-model', 'state': 'loaded', 'loaded_context_length': 4096}]}},
     False, {'/api/v0/models'}),
    ('lmstudio-model-loading', 'lmstudio', {}, None,
     {'/api/v0/models': {'data': [{'id': MODEL, 'state': 'loading'}]}}, False, {'/api/v0/models'}),
    ('lmstudio-not-running', 'lmstudio', {}, None, {}, False, {'/api/v0/models'}),
    ('llamacpp-single-model-serving-it', 'llamacpp', {}, None,
     {'/props': {'model_path': 'C:/models/fixture.gguf'}, '/models': {'data': [{'id': MODEL}]}},
     True, {'/props', '/models'}),
    ('llamacpp-single-model-serving-another', 'llamacpp', {}, None,
     {'/props': {'model_path': 'C:/models/other.gguf'}, '/models': {'data': [{'id': 'other-model'}]}},
     False, {'/props', '/models'}),
    ('llamacpp-own-router-model-loaded', 'llamacpp', {}, 'litetui',
     {'/props': ROUTER_PROPS, '/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}},
                                                    {'id': 'other-model', 'status': {'value': 'unloaded'}}]}},
     True, {'/props', '/models'}),
    ('llamacpp-own-router-model-unloaded', 'llamacpp', {}, 'litetui',
     {'/props': ROUTER_PROPS, '/models': {'data': [{'id': MODEL, 'status': {'value': 'unloaded'}}]}},
     False, {'/props', '/models'}),
    ('llamacpp-litesuite-router-model-loaded', 'llamacpp', {}, 'litesuite',
     {'/props': ROUTER_PROPS, '/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}}]}},
     False, {'/props', '/models'}),
    ('llamacpp-unrecorded-router-model-loaded', 'llamacpp', {}, None,
     {'/props': ROUTER_PROPS, '/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}}]}},
     False, {'/props', '/models'}),
    ('llamacpp-nothing-running', 'llamacpp', {}, None, {}, False, {'/props', '/models'}),
    ('custom-local-router-reports-loaded', 'custom', {'custom_base_url': 'http://127.0.0.1:7470'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}},
                              {'id': 'other-model', 'status': {'value': 'unloaded'}}]}}, True, {'/v1/models'}),
    ('custom-local-router-reports-unloaded', 'custom', {'custom_base_url': 'http://127.0.0.1:7470'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'status': {'value': 'unloaded'}}]}}, False, {'/v1/models'}),
    ('custom-local-plain-server-no-state', 'custom', {'custom_base_url': 'http://127.0.0.1:1234'}, None,
     {'/v1/models': {'data': [{'id': MODEL}]}}, False, {'/v1/models'}),
    ('custom-local-one-row-without-state', 'custom', {'custom_base_url': 'http://127.0.0.1:7470'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}}, {'id': 'other-model'}]}},
     False, {'/v1/models'}),
    ('strata-model-loaded', 'strata', {'strata_host': 'http://127.0.0.1:8090'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'status': {'value': 'loaded'}}]}}, True, {'/v1/models'}),
    ('strata-model-unloaded', 'strata', {'strata_host': 'http://127.0.0.1:8090'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'status': {'value': 'unloaded'}}]}}, False, {'/v1/models'}),
    ('strata-not-running', 'strata', {'strata_host': 'http://127.0.0.1:8090'}, None, {}, False, {'/v1/models'}),
    ('ninfer-serving-the-exact-model', 'ninfer', {'ninfer_host': 'http://127.0.0.1:8091'}, None,
     {'/v1/models': {'data': [{'id': MODEL, 'max_model_len': 8192}]}}, True, {'/v1/models'}),
    ('ninfer-serving-another-model', 'ninfer', {'ninfer_host': 'http://127.0.0.1:8091'}, None,
     {'/v1/models': {'data': [{'id': 'other-model', 'max_model_len': 8192}]}}, False, {'/v1/models'}),
    ('ninfer-not-running', 'ninfer', {'ninfer_host': 'http://127.0.0.1:8091'}, None, {}, False, {'/v1/models'}),
]


@pytest.mark.parametrize('case,name,saved,owner,routes,accepted,readable', REAL, ids=[row[0] for row in REAL])
def test_real_backend_classes_prove_by_reads_alone(owned, cheap_request, sealed, monkeypatch,
                                                   case, name, saved, owner, routes, accepted, readable):
    """The shipped state queries, over a stub server: reads only, and the table holds."""
    app, calls = owned
    app.settings.small_task_route = {**ROUTE, 'backend': name, 'model': MODEL}
    save_child_settings(**saved)
    own_router(monkeypatch, owner)
    monkeypatch.setattr(subagent_local, 'resolve_host', resolver({}), raising=False)
    server = serve(monkeypatch, sealed, routes)
    result = call(app, request=cheap_request)
    if accepted:
        assert result['state'] == 'dispatched', result
        assert len(calls) == 1 and journal_rows(app) == 1
    else:
        assert result['state'] == 'unavailable', result
        assert result['detail'].startswith(NEEDS_APPROVAL), result
        assert calls == [] and journal_rows(app) == 0
    assert server.requests, 'the proof asked the server nothing'
    assert {method for method, _ in server.requests} == {'GET'}
    assert {path for _, path in server.requests} <= readable, server.requests
    assert sealed == []
