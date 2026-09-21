import subprocess
import sys
import pytest
from litetui import voice_install


def test_uv_targets_system_interpreter(monkeypatch):
    monkeypatch.setattr("litetui.optional_python.install_target", lambda: "system-python")
    monkeypatch.setattr(voice_install.shutil, 'which', lambda name: 'uv.exe')
    assert voice_install.install_command() == ['uv.exe', 'pip', 'install', '--python', 'system-python', 'edge-tts', 'playsound==1.2.2']


def test_pip_fallback_targets_system_interpreter(monkeypatch):
    monkeypatch.setattr("litetui.optional_python.install_target", lambda: "system-python")
    monkeypatch.setattr(voice_install.shutil, 'which', lambda name: None)
    assert voice_install.install_command()[:4] == ['system-python', '-m', 'pip', 'install']


def test_already_installed_does_not_run_installer(monkeypatch):
    monkeypatch.setattr(voice_install, 'edge_available', lambda: True)
    monkeypatch.setattr(voice_install.subprocess, 'run', lambda *a, **k: pytest.fail('unnecessary install'))
    assert 'already installed' in voice_install.install_edge()


def test_failure_reports_stderr(monkeypatch):
    monkeypatch.setattr(voice_install, 'edge_available', lambda: False)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['installer'], stderr='No module named pip')
    monkeypatch.setattr(voice_install.subprocess, 'run', fail)
    assert 'No module named pip' in voice_install.install_edge()


def test_success_requires_importable_dependencies(monkeypatch):
    monkeypatch.setattr(voice_install, 'edge_available', lambda: False)
    monkeypatch.setattr(voice_install.subprocess, 'run', lambda *a, **k: None)
    assert 'not importable' in voice_install.install_edge()
