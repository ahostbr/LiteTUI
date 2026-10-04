"""T0331: pure policy receipts, never execute the command strings."""
import pytest

from litetui import tool_policy as tp


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / 'T0331' / 'LiteTUI'
    root.mkdir(parents=True)
    (root / '.git').write_text('gitdir: /repo/.git/worktrees/seat\n')
    temp = tmp_path / 'temp'
    temp.mkdir()
    monkeypatch.setenv('TEMP', str(temp))
    monkeypatch.setenv('TMP', str(temp))
    return root, temp


def decision(command, tree, profile=tp.INTERACTIVE):
    return tp.evaluate(profile, tp.SHELL_POLICY, {'command': command}, tree,
                       tool_name='powershell', shell='powershell', seat_name='PowerShellShapesSol')


@pytest.mark.parametrize('command', [
    '& tool.exe status *> log; $code=$LASTEXITCODE; exit $code',
    '& tool.exe status 2>&1; exit $LASTEXITCODE',
    "$env:X='yes'; python -m pytest tests/test_tool_policy.py",
    'Set-Location "{root}"; python script.py',
    'lst run tasks action=list; git status > $env:TEMP/x',
    'Get-ChildItem | Select-Object -First 5',
    'Get-Content a | Select-String x',
    'Get-Content a | Tee-Object -FilePath log | Select-String x',
    'Get-Content a | Tee-Object -Variable result',
    'git status 6>&1', 'git status *>&1',
    "Write-Output '2>&1; Remove-Item x'",
])
def test_title_shapes_allow(command, tree):
    root, _ = tree
    command = command.format(root=root.as_posix())
    assert decision(command, root).action == tp.ALLOW


@pytest.mark.parametrize('command', [
    'git status > "{foreign}"', 'git status *> "{foreign}"',
    'Get-Content a | Tee-Object -FilePath "{foreign}"',
    'git status > ../LiteTUI-foreign/log',
    "$env:TEMP='{foreign}'; git status > $env:TEMP/log",
])
def test_foreign_output_confirms(command, tree):
    root, temp = tree
    command = command.format(foreign=(temp.parent / 'foreign' / 'log').as_posix())
    d = decision(command, root)
    assert d.action == tp.CONFIRM and d.danger == tp.OVERWRITE


@pytest.mark.parametrize('command', [
    '& $tool arg', '& (Get-Command git) status',
    "Write-Output @'\ntext\n'@", 'if ($true) { git status }',
    'git status 2>&2', 'git status 7>&1', 'git status >',
    'git status || git log', 'git status <<EOF',
    '$x += 1', '$x = [IO.File]::ReadAllText("a")',
    '$x = & $tool arg', '. $script',
])
def test_unknown_shapes_still_ask(command, tree):
    root, _ = tree
    d = decision(command, root)
    assert d.action == tp.CONFIRM and d.danger == tp.UNKNOWN_SHAPE


@pytest.mark.parametrize('command', [
    'Remove-Item old 2>&1', '$x = Remove-Item old',
    '& "Remove-Item" old', 'Get-ChildItem | Remove-Item',
    'Stop-Process -Id 42 > log', '$env:X = Stop-Process -Id 42',
    'if ($true) { Remove-Item old }', '& { Remove-Item old }',
    "Write-Output @'\nRemove-Item old\n'@", 'Remove-Item old *> $env:TEMP/log',
])
def test_destructive_does_not_ride_safe_output(command, tree):
    root, _ = tree
    # Existing own-tree deletion exemption is independently tested elsewhere;
    # main checkout must retain confirmation for these destructive commands.
    main = root.parent / 'main'
    main.mkdir()
    (main / '.git').mkdir()
    d = decision(command, main)
    assert d.action == tp.CONFIRM


def test_floor_remains_above_profiles(tree):
    root, _ = tree
    for profile in tp.PROFILE_NAMES:
        assert decision('Remove-Item $HOME -Recurse -Force 2>&1', root, profile).action == tp.DENY


def test_strict_retains_supervision(tree):
    root, _ = tree
    assert decision('git status 2>&1', root, tp.STRICT).action == tp.CONFIRM
