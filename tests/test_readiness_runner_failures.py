"""Runner failure paths, with synthetic inventories and no child execution."""
import os
import pytest
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_all


@pytest.fixture(autouse=True)
def isolate_runner_environment(monkeypatch):
    # main changes child environment; do not leak those changes to other tests.
    monkeypatch.setattr(run_all.os, "environ", dict(os.environ))


def test_empty_inventory_is_not_green(monkeypatch, capsys):
    monkeypatch.setattr(run_all, "explicit_inventory", lambda: ([], []))
    monkeypatch.setattr(sys, "argv", ["run_all.py"])
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected child")))
    assert run_all.main() == 1
    assert "empty" in capsys.readouterr().out.lower()


def test_script_timeout_is_reported_and_remaining_scripts_run(monkeypatch, capsys):
    files = [Path("test_hang.py"), Path("test_after.py")]
    monkeypatch.setattr(run_all, "explicit_inventory", lambda: ([], files))
    monkeypatch.setattr(sys, "argv", ["run_all.py"])
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(run_all.subprocess, "run", fake_run)
    assert run_all.main() == 1
    assert len(calls) == 2
    output = capsys.readouterr().out.lower()
    assert "test_hang.py" in output and "timeout" in output
    assert "all green" not in output


def test_pytest_timeout_is_bounded_and_scripts_still_run(monkeypatch, capsys):
    monkeypatch.setattr(run_all, "explicit_inventory", lambda: ([Path("test_unit.py")], [Path("test_script.py")]))
    monkeypatch.setattr(sys, "argv", ["run_all.py"])
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] > 0
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(run_all.subprocess, "run", fake_run)
    assert run_all.main() == 1
    assert len(calls) == 2
    assert "timeout" in capsys.readouterr().out.lower()
