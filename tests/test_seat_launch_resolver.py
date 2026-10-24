r"""Opening another seat must not launch through an interpreter that lacks liteharness.

Ryan 2026-10-24 (screenshot, LiteTUI-9ecb4b7a): "Own-seat launch refused: C:\Projects\LiteTUI\.venv\Scripts\python.exe:
Error while finding module specification for 'liteharness.cli' (ModuleNotFoundError: No module named 'liteharness')".
agent_seat_launch and owner_mode spelled `[sys.executable, "-m", "liteharness.cli"]` by hand; sys.executable is the
locked project venv, which deliberately has no liteharness (harness._liteharness_exe documents it). Both now go
through harness.liteharness_command().
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from litetui import agent_seat_launch, harness, owner_mode
from litetui.agent_store import StoreError


@pytest.fixture(scope="module")
def bare_python(tmp_path_factory):
    """A real interpreter shaped like the locked project venv: no site packages, so no liteharness."""
    home = tmp_path_factory.mktemp("bare")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(home)], check=True, timeout=120)
    return home / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def test_the_premise_a_bare_venv_cannot_run_liteharness_cli(bare_python):
    done = subprocess.run([str(bare_python), "-m", "liteharness.cli", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert "No module named 'liteharness'" in done.stderr


def test_resolved_through_a_bare_venv_it_is_the_console_script_never_the_venv_interpreter(bare_python):
    if harness._liteharness_exe() is None:
        pytest.skip("no liteharness console script on this machine to resolve")
    src = str(__import__("pathlib").Path(harness.__file__).resolve().parents[1])
    done = subprocess.run(
        [str(bare_python), "-c", "from litetui import harness; import json; print(json.dumps(harness.liteharness_command()))"],
        capture_output=True, text=True, timeout=60, env={**__import__("os").environ, "PYTHONPATH": src})
    assert done.returncode == 0, done.stderr
    command = __import__("json").loads(done.stdout.strip().splitlines()[-1])
    assert command and command[0].lower().endswith(("liteharness.exe", "liteharness"))
    assert str(bare_python) not in command


def test_nothing_reachable_is_none_not_a_guess(monkeypatch):
    monkeypatch.setattr(harness, "_liteharness_exe", lambda: None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert harness.liteharness_command() is None


def test_the_module_form_only_when_this_interpreter_can_import_liteharness(monkeypatch):
    monkeypatch.setattr(harness, "_liteharness_exe", lambda: None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert harness.liteharness_command() == [sys.executable, "-m", "liteharness.cli"]


def test_the_console_script_wins_over_the_module_form(monkeypatch):
    monkeypatch.setattr(harness, "_liteharness_exe", lambda: "/x/liteharness")
    assert harness.liteharness_command() == ["/x/liteharness"]


def _app():
    return SimpleNamespace(_agent_session=None, seat=SimpleNamespace(registered=True, agent_id="a" * 8, tier="worker"))


@pytest.mark.asyncio
async def test_open_seat_without_a_launcher_says_so_in_plain_words_and_spawns_nothing(tmp_path, monkeypatch):
    from test_agent_picker_tasks import AID, BID, CID, home, transcript
    with home(tmp_path, "OtherAgent", BID) as other:
        transcript(other, "separate")
    with home(tmp_path, "QuietHelm", AID) as session:
        app = SimpleNamespace(_agent_session=session,
                              seat=SimpleNamespace(registered=True, agent_id=AID, tier="worker"))
        spawned = []

        async def spawn(*args, **kw):
            spawned.append(args)

        monkeypatch.setattr(agent_seat_launch.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(harness, "liteharness_command", lambda: None)
        with pytest.raises(StoreError, match="liteharness command was not found.*pip install liteharness"):
            await agent_seat_launch.open_seat(app, BID, CID)
        assert spawned == []


def test_owner_mode_uses_the_same_resolver(monkeypatch):
    monkeypatch.setattr(harness, "liteharness_command", lambda: ["/x/liteharness"])
    assert owner_mode.handoff_command() == ["/x/liteharness"]
    monkeypatch.setattr(harness, "liteharness_command", lambda: None)
    assert owner_mode.handoff_command()[1:] == ["-m", "liteharness.cli"]  # probe then fails in plain words
