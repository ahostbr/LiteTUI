"""D3 policy integration; canonical OSS owns the complete literal corpus."""
import pytest

from litetui import deny_floor
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
    monkeypatch.setattr(deny_floor, '_OWNER_LAUNCHER', owner)
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
