"""T0306 classifier-only regressions. No example commands are executed."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from litetui import settings
from litetui import tool_policy as tp
from litetui import trusted_executables as te

WORKSPACES = [Path(p) for p in ('E:/ExampleWorkspace/ExampleGame', 'C:/ExampleProjects/LiteSuite',
                              'C:/ExampleProjects/LiteTUI', 'C:/ExampleProjects', 'C:/Users/TestUser')]
VENV = 'E:/ExampleWorkspace/ExampleGame/.venv_mcp312/Scripts/python.exe'
SAMPLES = {
    'A': 'python C:/Users/TestUser/.claude/skills/ls-conversation-lookup/find_conversation.py --search "x" --mode hybrid -n 8',
    'B': 'git -C E:/ExampleWorkspace/ExampleGame log --oneline -3',
    'C': r'''"E:/ExampleWorkspace/ExampleGame/.venv_mcp312/Scripts/python.exe" -X utf8 -c "import subprocess,os; p=subprocess.run([r'C:/Users/TestUser/AppData/Local/Programs/Python/Python311/python.exe',r'E:/ExampleWorkspace/ExampleGame/.worktrees/_scratch/T0182/o/kaginawa_verify_launch.py'],capture_output=True,text=True,encoding='utf-8',errors='replace',env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW,timeout=25); print(p.stdout); print(p.stderr); raise SystemExit(p.returncode)"''',
    'D': r'''"E:/ExampleWorkspace/ExampleGame/.venv_mcp312/Scripts/python.exe" -X utf8 -c "import hashlib; from pathlib import Path; p=Path('E:/ExampleWorkspace/x.uasset'); print(hashlib.sha256(p.read_bytes()).hexdigest())"''',
    'E': '"C:/Users/TestUser/AppData/Local/Programs/Python/Python311/python.exe" C:/ExampleProjects/.scratch/x.py',
    'F': f'"{VENV}" script.py',
    'G': '''python -c "from pathlib import Path; p=Path('C:/ExampleProjects/liteharness-oss/.worktrees/X/.slot-output'); print(p.exists())"''',
    'appr-e315ccbbdd6a': f'{VENV} .worktrees/_scratch/T0182/o/compact_graph_evidence.py',
    'appr-c9f82b398b83': f'{VENV} .worktrees/_scratch/T0182/o/contact_rows_summary.py',
}


def decision(command, workspace, configured=()):
    return tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, workspace,
                       tool_name='bash', shell='bash', trusted_interpreters=configured)


@pytest.mark.parametrize('workspace', WORKSPACES)
@pytest.mark.parametrize('name', SAMPLES)
def test_reference_matrix_baseline_and_configured(workspace, name, monkeypatch):
    # Stable installed identity fixture; the real host probe measures filesystem/PATH.
    system = Path('C:/Users/TestUser/AppData/Local/Programs/Python/Python311/python.exe').resolve()
    monkeypatch.setattr(te, 'is_installed_tool', lambda path: path == system)
    # Corpus fixture only: exact explicit user identity, not a production path rule.
    monkeypatch.setattr(te, '_unlinked_absolute', lambda raw: Path(raw) if raw == VENV else None)
    # T0116 removes executable-location/opaque-launch approvals, not floors.
    baseline = decision(SAMPLES[name], workspace)
    configured = decision(SAMPLES[name], workspace, [VENV])
    assert baseline.action == configured.action == tp.ALLOW


@pytest.mark.parametrize('shell', [None, 'bash', 'powershell'])
@pytest.mark.parametrize('payload', [
    '''"from pathlib import Path; p=Path('C:/outside/data'); print(p.exists())"''',
    ''''from pathlib import Path; p=Path("C:/outside/data"); print(p.exists())' ''',
])
def test_python_path_data_is_not_a_launch(shell, payload):
    assert tp.danger('python -c ' + payload, Path('E:/other'), shell=shell) is None


@pytest.mark.parametrize('command,label', [
    ('python -c "import shutil; shutil.rmtree(\'x\')"', tp.DELETION),
    ('python -c "import zipfile; z.extractall(\'x\')"', tp.ARCHIVE),
    ('python -c "import os; os.system(cmd)"', None),
    ('python -c "import os as o; o.system(cmd)"', None),
    ('python -c "from os import system; system(cmd)"', None),
    ('python -c "import subprocess as s; s.run(cmd)"', None),
    ('python -c "import os; os.execv(target, args)"', None),
    ('python -c "print(\'$(C:/foreign/program.exe)\')"', None),
    ('python -c "print(\'$(echo ok; (true); C:/foreign/program.exe)\')"', None),
    ('python -c "print(\'$(echo $(true); C:/foreign/program.exe)\')"', None),
    ('python -c "print(\'`echo ok; (true); C:/foreign/program.exe`\')"', None),
    ('bash -c "C:/foreign/program.exe"', None),
    ('(C:/foreign/program.exe)', None),
    ('python -c "print(1)"; C:/foreign/program.exe', None),
    ('python -c "print(1)"; format C:', tp.DANGEROUS),
])
def test_payload_mask_never_masks_dangers_or_real_shell_launches(command, label, tmp_path, monkeypatch):
    # Generic danger labels must not depend on a developer cwd containing the
    # owner launcher. The floor also judges Path.cwd(), not only workspace.
    monkeypatch.chdir(tmp_path)
    assert tp.danger(command, Path('E:/other'), shell='bash') == label
    assert decision(command, Path('E:/other')).action == (tp.CONFIRM if label else tp.ALLOW)


def test_opaque_python_run_preserves_deny_floor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    command = 'python -c "from subprocess import run; run(cmd)"'
    assert tp.danger(command, Path('E:/other'), shell='bash') == None
    result = decision(command, Path('E:/other'))
    assert result.action == tp.DENY
    assert 'DENY FLOOR [owner-launcher]' in result.reason


@pytest.fixture
def interpreter(tmp_path, monkeypatch):
    exe = tmp_path / 'known' / 'python.exe'
    exe.parent.mkdir()
    exe.write_bytes(b'fixture only, never executed')
    monkeypatch.setattr(te, 'is_installed_tool', lambda path: False)
    return exe


def test_explicit_exact_identity_and_foreign_negatives(interpreter, tmp_path):
    exe = interpreter
    workspace = tmp_path / 'workspace'
    assert decision(f'"{exe}" script.py', workspace).action == tp.ALLOW
    assert decision(f'"{exe}" script.py', workspace, [str(exe)]).action == tp.ALLOW
    for other in (exe.parent / 'other.exe', tmp_path / 'foreign' / 'python.exe'):
        other.parent.mkdir(exist_ok=True)
        other.write_bytes(exe.read_bytes())
        assert decision(f'"{other}" script.py', workspace, [str(exe)]).action == tp.ALLOW
    assert decision('python.exe script.py', workspace, [str(exe)]).action == tp.ALLOW  # unchanged bare-tool baseline
    spaced = tmp_path / 'known (fixture)' / 'python.exe'
    spaced.parent.mkdir()
    spaced.write_bytes(exe.read_bytes())
    assert decision(f'"{spaced}" script.py', workspace).action == tp.ALLOW
    assert decision(f'"{spaced}" script.py', workspace, [str(spaced)]).action == tp.ALLOW


@pytest.mark.parametrize('configured', [None, '', 'PATH', {}, 123, [None], [''], ['python.exe'],
                                        ['../python.exe'], ['~/python.exe']])
def test_malformed_setting_grants_no_identity(interpreter, configured):
    assert not te.is_configured_interpreter(str(interpreter), configured)
    assert settings._coerce('tool_trusted_interpreters', configured, ['old']) == (
        configured if isinstance(configured, list) and all(isinstance(x, str) for x in configured) else [])


def test_one_bad_entry_invalidates_whole_setting(interpreter):
    for bad in (None, '../python.exe', str(interpreter.parent / 'absent.exe')):
        assert not te.is_configured_interpreter(str(interpreter), [str(interpreter), bad])
    assert not te.is_configured_interpreter(str(interpreter.parent / '..' / 'known' / 'python.exe'), [str(interpreter)])


def test_configured_and_candidate_links_and_retargeting(interpreter, tmp_path):
    alias = tmp_path / 'alias.exe'
    directory = tmp_path / 'alias_dir'
    try:
        alias.symlink_to(interpreter)
        directory.symlink_to(interpreter.parent, target_is_directory=True)
    except OSError:
        pytest.skip('host cannot create symlinks')
    for path in (alias, directory / 'python.exe'):
        assert not te.is_configured_interpreter(str(path), [str(interpreter)])
        assert not te.is_configured_interpreter(str(interpreter), [str(path)])
        assert not te.is_configured_interpreter(str(path), [str(path)])
    alias.unlink()
    alias.symlink_to(tmp_path / 'elsewhere.exe')
    assert not te.is_configured_interpreter(str(alias), [str(alias)])


def test_reparse_ancestor_fails_closed(interpreter, monkeypatch):
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == interpreter.parent:
            return NS(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert not te.is_configured_interpreter(str(interpreter), [str(interpreter)])


def test_configured_identity_does_not_override_danger_or_deny(interpreter, tmp_path):
    workspace = tmp_path / 'workspace'
    for body, label in (("import shutil; shutil.rmtree('x')", tp.DELETION),
                        ("z.extractall('x')", tp.ARCHIVE),
                        ('import subprocess; subprocess.run(cmd)', None)):
        result = decision(f'"{interpreter}" -c "{body}"', workspace, [str(interpreter)])
        assert result.action == (tp.CONFIRM if label else tp.ALLOW) and result.danger == (label or "")
    command = f'"{interpreter}" script.py'
    key = tp.rule_key('bash', {tp.PROCESS_EXECUTION})
    assert tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, workspace,
                       tool_name='bash', deny=frozenset({key}),
                       trusted_interpreters=[str(interpreter)]).action == tp.DENY


@pytest.mark.asyncio
async def test_host_authorization_passes_seat_setting(interpreter, tmp_path, monkeypatch):
    from litetui.app import LiteTUI
    original = tp.evaluate
    seen = []
    def evaluate(*args, **kwargs):
        seen.append(kwargs['trusted_interpreters'])
        return original(*args, **kwargs)
    monkeypatch.setattr(tp, 'evaluate', evaluate)
    app = NS(settings=NS(tool_always_allow=[], tool_deny=[],
                         tool_trusted_interpreters=[str(interpreter)]),
             _active_tool_profile=tp.INTERACTIVE)
    assert await LiteTUI._authorize_action(app, 'bash', {'command': f'"{interpreter}" script.py'},
                                          tp.SHELL_POLICY, workspace=tmp_path / 'workspace') is None
    assert seen == [[str(interpreter)]]
    # Native/backend authorization is the same host door, not a parallel policy.
    from litetui.codex_native_policy import NativePolicy
    app.tools_enabled = True
    app.settings.tools_disabled = []
    app.plugins = NS(policy_for=lambda name: None)
    app._hook_workspace = tmp_path / 'workspace'
    async def authorize(*args, **kwargs):
        return await LiteTUI._authorize_action(app, *args, **kwargs)
    app._authorize_action = authorize
    assert await NativePolicy(app).handle({
        'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
        'tool_input': {'command': f'"{interpreter}" script.py'},
    }) == {}
    assert seen[-1] == [str(interpreter)]
    # Claude native entry also exercises the shipped host policy, no SDK process.
    from litetui.claude_tools import ClaudeTools
    app._stop_requested = False
    app.convo_id = 'fixture'
    app.backend = NS(segment_id='segment')
    monkeypatch.setattr('litetui.claude_tools._deadlines', lambda app: (5, 10))
    claude = ClaudeTools(app, app.backend, 'segment', workspace=tmp_path / 'workspace')
    response = await claude.pre_tool({'tool_name': 'Bash',
                                     'tool_input': {'command': f'"{interpreter}" script.py'}}, 'tool', None)
    assert response['hookSpecificOutput']['permissionDecision'] == 'allow'
    assert seen[-1] == [str(interpreter)]
    # Lifecycle hooks also reach that same door; runner is mocked, never launched.
    from litetui import hook_host
    async def run_hook(*args, **kwargs):
        return NS(allowed=True)
    monkeypatch.setattr(hook_host.hooks, 'run_hook', run_hook)
    app.hook_results = {}
    hook = NS(executable=str(interpreter), argv=['script.py'], cwd=None, env={},
              scope='project', id='fixture', approval_name=lambda workspace: 'bash')
    assert (await hook_host.invoke(app, hook, {'event': 'tool_before'}, tp.INTERACTIVE)).allowed
    assert seen[-1] == [str(interpreter)]


def test_fleet_payload_receives_explicit_identity(interpreter, tmp_path):
    args = {'command': f'"{interpreter}" script.py'}
    assert tp.evaluate(tp.INTERACTIVE, tp.FLEET_MCP_POLICY, args, tmp_path / 'workspace',
                       trusted_interpreters=[str(interpreter)]).action == tp.ALLOW
    assert tp.evaluate(tp.INTERACTIVE, tp.FLEET_MCP_POLICY, args, tmp_path / 'workspace').action == tp.ALLOW
