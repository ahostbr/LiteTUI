"""Wheel probe command isolation; never builds, installs, or loads a backend."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest


def gate_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "wheel_import_gate.py"
    spec = importlib.util.spec_from_file_location("wheel_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installed_probe_uses_isolated_python_and_external_cwd(tmp_path, monkeypatch):
    gate = gate_module()
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(gate, "_run", fake_run)
    python = tmp_path / "venv" / "python"
    gate.run_in_venv(python, tmp_path)
    assert calls[0][0][1] == "-I"
    assert calls[0][1]["cwd"] == tmp_path
    source = (tmp_path / "in_venv_checks.py").read_text()
    assert "importlib.metadata" in source
    assert "sys.prefix" in source
    assert "__file__" in source


def test_version_nonzero_is_failure_even_with_matching_text(tmp_path, monkeypatch):
    gate = gate_module()
    from litetui.version import __version__
    monkeypatch.setattr(gate, "_run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=__version__, stderr="failed"))
    with pytest.raises(SystemExit):
        gate.check_version(tmp_path / "python", tmp_path)
