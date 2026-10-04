"""D3 policy integration; canonical OSS owns the complete literal corpus."""
import pytest

from litetui import tool_policy as tp


@pytest.mark.parametrize('profile', [tp.STRICT, tp.INTERACTIVE, tp.AUTONOMOUS])
@pytest.mark.parametrize('command', [
    '<# c #> run', '$x=run', 'return run', 'saps -Wait run',
    'Start-Process -NoNewWindow run', 'env X=1 nohup ./run.bat',
    'timeout 5 ./run.bat', 'cmd /v:on /c run',
])
def test_d3_cannot_be_lifted_by_profile(tmp_path, monkeypatch, profile, command):
    owner = tmp_path / 'run.bat'
    owner.write_text('@echo off\n', encoding='utf-8')
    monkeypatch.setenv('PATHEXT', '.COM;.EXE;.BAT;.CMD')
    decision = tp.evaluate(profile, tp.SHELL_POLICY, {'command': command}, tmp_path,
                           tool_name='powershell')
    assert decision.action == tp.DENY
    assert '[owner-launcher]' in decision.reason


@pytest.mark.parametrize('command', [
    'Start-Process ordinary.exe -Wait', 'nohup ordinary.exe',
    'env X=1 ordinary.exe', '$x = "run"', 'return "run"',
])
def test_d3_does_not_restore_foreign_process_approvals(tmp_path, command):
    decision = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, tmp_path,
                           tool_name='powershell')
    assert decision.action == tp.ALLOW


@pytest.mark.parametrize('command, refused', [
    ('X=run', False), ('env X=run cat file', False),
    ('env X=./run.bat cat file', False), ('env X=run Y=./run.bat cat file', False),
    ('env X="run.bat" cat file', False), ('X=run Y=./run.bat', False),
    ('X=1 run', True), ('env X=1 run', True),
    ('env X=run Y=./run.bat run', True), ('X=run; ./run.bat', True),
])
def test_d3_bash_assignment_value_policy(tmp_path, monkeypatch, command, refused):
    owner = tmp_path / 'run.bat'
    owner.write_text('@echo off\n', encoding='utf-8')
    monkeypatch.setenv('PATHEXT', '.COM;.EXE;.BAT;.CMD')
    decision = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, tmp_path,
                           tool_name='bash')
    assert (decision.action == tp.DENY) is refused
    if refused:
        assert '[owner-launcher]' in decision.reason
