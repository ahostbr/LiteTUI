"""T0331 output-containment negatives. No command strings are executed."""
import pytest

from litetui import tool_policy as tp
from litetui import worktree_scope


@pytest.fixture
def paths(tmp_path, monkeypatch):
    root = tmp_path / 'own'
    root.mkdir()
    (root / '.git').write_text('gitdir: /repo/.git/worktrees/own\n')
    temp = tmp_path / 'temp'
    temp.mkdir()
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    monkeypatch.setenv('TEMP', str(temp))
    monkeypatch.setenv('TMP', str(temp))
    return root, temp, foreign


def decide(command, root):
    return tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, root,
                       shell='powershell', tool_name='powershell')


@pytest.mark.parametrize('shape', ['git status > {p}', 'git status 2> {p}',
                                  'git status *> {p}', 'git status >> {p}',
                                  'Get-Content a | Tee-Object -FilePath {p}'])
def test_all_file_output_forms_prove_target(paths, shape):
    root, temp, foreign = paths
    for target in (root / 'log', temp / 'log'):
        assert decide(shape.format(p='"' + target.as_posix() + '"'), root).action == tp.ALLOW
    assert decide(shape.format(p='"' + (foreign / 'log').as_posix() + '"'), root).action == tp.CONFIRM


@pytest.mark.parametrize('command', [
    'git status > $unknown/log', 'git status > $env:UNKNOWN/log',
    'git status > C:relative', 'git status > HKLM:log',
    'git status > log:stream', 'git status > ../own-other/log',
    '$env:TEMP=Get-Content config; git status > $env:TEMP/log',
    'Get-Content a | Tee-Object -Variable result -FilePath ../foreign/log',
    'Get-Content a | Tee-Object -FilePath ../foreign/log -Variable result',
    'Set-Location missing; git status > log', 'Pop-Location; git status > log',
    'git diff --output=../foreign/log > log',
    'Set-Variable env:TEMP ../foreign; git status > $env:TEMP/log',
    'Set-Item env:TEMP ../foreign; git status > $env:TEMP/log',
    'cmd /c "cd ../foreign & git status > log"',
    'Get-Content a | Tee-Object -FilePath `$env:TEMP/log',
    "Get-Content a | Tee-Object -FilePath '$env:TEMP/log'",
])
def test_unproved_output_never_uses_exception(paths, command):
    root, _, _ = paths
    assert decide(command, root).action == tp.CONFIRM


def test_literal_path_assignment_can_be_proved(paths):
    root, temp, _ = paths
    command = f"$log='{temp.as_posix()}/log'; git status > $log"
    assert decide(command, root).action == tp.ALLOW


@pytest.mark.parametrize('template', [
    "$HOME='{root}'; git status > $HOME/log",
    "$PID='{root}'; git status > $PID/log",
    "$log='{root}/log'; Get-Content config | Tee-Object -Variable log; git status > $log",
    "$log='{root}/log'; Get-Content config -OutVariable log; git status > $log",
    "$log='{root}/log'; Get-Content config -ov log; git status > $log",
    "$log='{root}/log'; Get-Content config -OutVariable:log; git status > $log",
    "$log='{root}/log'; Get-Content config -OV +log; git status > $log",
    "$log='{root}/log'; Get-Content config -PipelineVariable log; git status > $log",
    "$log='{root}/log'; Get-Content config -Pip log; git status > $log",
    "$log='{root}/log'; Get-Content config -Pipeline log; git status > $log",
    "$log='{root}/log'; Get-Content config | Tee-Object -Variable local:log; git status > $log",
    "$log='{root}/log'; Get-Content config | Tee-Object -Variable script:log; git status > $log",
    "$log='{root}/log'; Get-Content config | Tee-Object -Variable global:log; git status > $log",
    "$LASTEXITCODE='{root}/log'; git status > $LASTEXITCODE",
    'Microsoft.PowerShell.Utility\\Tee-Object -Variable log; git status > $env:TEMP/log',
    'Push-Location "{foreign}"; git status > log',
    'Microsoft.PowerShell.Management\\Set-Location "{foreign}"; git status > log',
    'Microsoft.PowerShell.Management\\Push-Location "{foreign}"; git status > log',
])
def test_review_unrepresented_state_never_proves_output(paths, template):
    root, _, foreign = paths
    command = template.format(root=root.as_posix(), foreign=foreign.as_posix())
    d = decide(command, root)
    assert d.action == tp.CONFIRM and d.danger == tp.OVERWRITE


@pytest.mark.parametrize('template', [
    "$log='{root}/log'; Get-Content config | Tee-Object -Variable other; git status > $log",
    'Push-Location "{root}"; git status > log',
    "$log='{root}/log'; Get-Content config -ReadCount 5; git status > $log",
])
def test_review_safe_state_controls(paths, template):
    root, _, _ = paths
    assert decide(template.format(root=root.as_posix()), root).action == tp.ALLOW


def test_cd_tracks_output_location(paths):
    root, temp, foreign = paths
    assert decide(f'Set-Location "{temp.as_posix()}"; git status > log', root).action == tp.ALLOW
    assert decide(f'Set-Location "{foreign.as_posix()}"; git status > log', root).action == tp.CONFIRM


def test_symlink_escape_not_a_contained_output(paths):
    root, _, foreign = paths
    link = root / 'linked'
    try:
        link.symlink_to(foreign, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'symlink creation unavailable: {exc}')
    assert decide('git status > linked/log', root).action == tp.CONFIRM


def test_card_scratch_derived_from_own_tree_only(paths, monkeypatch):
    root, _, foreign = paths
    base = root / 'scratch'
    card = base / 'T0331' / 'LiteTUI'
    monkeypatch.setenv('LITETUI_OUTPUT_SCRATCH_ROOTS', str(base))
    monkeypatch.setattr(worktree_scope, 'own_roots', lambda *args: [card])
    roots, _ = worktree_scope.output_context(root, None)
    assert card.parent.resolve() in roots
    assert (base / 'T0332').resolve() not in roots
    monkeypatch.setattr(worktree_scope, 'own_roots', lambda *args: [foreign])
    roots, _ = worktree_scope.output_context(root, None)
    assert foreign.parent not in roots
