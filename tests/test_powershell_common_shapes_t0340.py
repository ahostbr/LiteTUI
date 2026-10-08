"""T0340 bounded literal arrays/Test-Path guards. Pure policy, no execution."""
from pathlib import Path

import pytest

from litetui import tool_policy as tp

WS = Path('C:/work/project')


def decide(command):
    return tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, WS,
                       shell='powershell', tool_name='powershell')


@pytest.mark.parametrize('command', [
    "& 'E:/project/hidden_commands.ps1' -Action python -CommandArgs @('E:/project/x.py')",
    "python script.py @('one', 'two words', 'it''s literal')",
    'python script.py @("one", "two")',
    'Get-ChildItem src; git log -4 --oneline; if(Test-Path x.jsonl){Get-Content x.jsonl}',
    "if (Test-Path 'x y.jsonl') { Get-Content 'x y.jsonl' } else { Get-ChildItem src }",
    "if(Test-Path -LiteralPath 'x.jsonl'){Get-Content 'x.jsonl'}",
    "if(Test-Path -Path '-ErrorVariable:x'){Get-Content y}",
    "if(Test-Path -LiteralPath '-ev:x'){Get-Content y}",
    'Set-Location own; node -e "console.log(1)"',
    'Get-ChildItem | Select-Object -First 5', 'git status',
])
def test_bounded_shapes_are_ordinary(command):
    assert decide(command).action == tp.ALLOW


@pytest.mark.parametrize('command', [
    "Remove-Item @('old')", "if(Test-Path missing){Remove-Item old}",
    'if(Test-Path missing){Get-Content x}else{Stop-Process -Id 42}',
    'if(Test-Path missing){Get-Content x}else{git reset --hard}',
    "& 'Remove-Item' @('old')",
])
def test_receiving_command_and_every_branch_keep_danger(command):
    assert decide(command).action == tp.CONFIRM


@pytest.mark.parametrize('command', [
    "@('git') status", "& @('git') status", "python script.py @($x)",
    'python script.py @("$env:HOME")', 'python script.py @("$(Remove-Item x)")',
    "python script.py @(Get-Content x)", "python script.py @('one',)",
    'if(Test-Path $x){Get-Content x}', 'if(Test-Path "$(Get-Content x)"){Get-Content x}',
    'if(Test-Path x){python script.py}', 'if(Test-Path x){Get-Content x}else{python script.py}',
    'if(Test-Path x){Get-Content x > log}', 'if(Test-Path x){Get-Content x -OutVariable y}',
    'if(Test-Path x){Get-Content x}; if($true){Get-Content y}',
    'if(Test-Path x){Get-Content x} elseif(Test-Path y){Get-Content y}',
    'if(Test-Path x -IsValid){Get-Content x}', 'if(Test-Path x){Get-Content x',
    'if(Test-Path "$env:HOME"){Get-Content x}',
    'python script.py @(\'one\',"$x")',
    'if(Test-Path x){Get-Content x -ErrorV y}',
    'if(Test-Path x){Get-Content x -WarningVariable:y}',
    'if(Test-Path x){Get-Content x -PipelineV y}',
    'if(Test-Path x){Get-Content x -ov y}',
    'if(Test-Path x){Get-Content x -InformationV y}',
    'if(Test-Path -ErrorVariable:x){Get-Content y}',
    'if(Test-Path -OutVariable:x){Get-Content y}',
    'if(Test-Path -ev:x){Get-Content y}',
    'if(Test-Path -Path -ErrorVariable:x){Get-Content y}',
    'if(Test-Path -LiteralPath -ev:x){Get-Content y}',
])
def test_near_miss_still_asks(command):
    assert decide(command).action == tp.CONFIRM


def test_existing_run_launcher_floor_is_not_relaxed():
    owner = Path('C:/ExampleProjects/LiteTUI/run.bat')
    if not owner.is_file():
        pytest.skip('actual protected owner launcher is absent')
    for command in ("& 'C:/ExampleProjects/LiteTUI/run.bat' @('x')", "C:/ExampleProjects/LiteTUI/run.bat x"):
        assert decide(command).action == tp.DENY
