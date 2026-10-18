"""Owner mode: one seat per memory folder, by `<DIR>/.owner.lock`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from litetui import cli, owner_mode
from litetui.owner_mode import LOCK_NAME, OwnerLock, OwnerModeError, pid_alive


@pytest.fixture
def owner_dir(tmp_path):
    folder = tmp_path / "widgets-memory"
    folder.mkdir()
    for name in ("SOUL.md", "NOW.md", "DOMAIN.md"):
        (folder / name).write_text("x\n", encoding="utf-8")
    return folder


def _dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def _write_lock(folder, pid, agent_id="agent-old"):
    (folder / LOCK_NAME).write_text(json.dumps({"pid": pid, "agent_id": agent_id, "started": "then"}), encoding="utf-8")


# -- the liveness check -------------------------------------------------------------

def test_pid_alive_knows_itself_a_dead_process_and_nonsense():
    assert pid_alive(os.getpid())
    assert not pid_alive(_dead_pid())
    for bad in (0, -1, None, "12", True):
        assert not pid_alive(bad)


# -- the lock -----------------------------------------------------------------------------

def test_the_lock_is_created_with_pid_agent_and_start(owner_dir):
    lock = OwnerLock(owner_dir)
    assert lock.acquire("agent-1") is None
    data = json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["agent_id"] == "agent-1" and data["started"]


def test_a_second_launch_on_a_live_lock_is_refused_naming_the_holder(owner_dir):
    first = OwnerLock(owner_dir)
    first.acquire("agent-1")
    with pytest.raises(OwnerModeError) as exc:
        OwnerLock(owner_dir).acquire("agent-2")
    text = str(exc.value)
    assert "agent-1" in text and f"pid {os.getpid()}" in text
    assert str(owner_dir / LOCK_NAME) in text, "says which file to delete if the holder is not running"
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] == "agent-1"


def test_a_stale_lock_is_taken_over_with_a_note(owner_dir):
    _write_lock(owner_dir, _dead_pid(), "agent-old")
    lock = OwnerLock(owner_dir)
    note = lock.acquire("agent-new")
    assert note and "agent-old" in note and "no longer running" in note
    data = json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["agent_id"] == "agent-new"


def test_a_taker_that_loses_the_race_does_not_remove_the_winners_lock(owner_dir, monkeypatch):
    """A and B both judge the old lock stale. B replaces it first; A must not delete B's live lock."""
    _write_lock(owner_dir, _dead_pid(), "agent-old")
    real = owner_mode.pid_alive
    calls = []

    def racing(pid):
        if not calls:
            calls.append(pid)
            _write_lock(owner_dir, os.getpid(), "agent-racer")  # the other launch wins here
            return False  # ... after this launch had already judged the old holder dead
        return real(pid)
    monkeypatch.setattr(owner_mode, "pid_alive", racing)
    with pytest.raises(OwnerModeError, match="agent-racer"):
        OwnerLock(owner_dir).acquire("agent-late")
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] == "agent-racer"


def test_release_removes_the_lock_and_is_idempotent(owner_dir):
    lock = OwnerLock(owner_dir)
    lock.acquire("agent-1")
    lock.release()
    lock.release()
    assert not (owner_dir / LOCK_NAME).exists()
    OwnerLock(owner_dir).acquire("agent-2")  # free again


def test_release_leaves_a_lock_that_is_no_longer_ours(owner_dir):
    lock = OwnerLock(owner_dir)
    lock.acquire("agent-1")
    _write_lock(owner_dir, os.getpid() + 100000, "someone-else")
    lock.release()
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] == "someone-else"


def test_two_different_folders_run_side_by_side(tmp_path, owner_dir):
    other = tmp_path / "gadgets-memory"
    other.mkdir()
    OwnerLock(owner_dir).acquire("agent-1")
    OwnerLock(other).acquire("agent-2")
    assert (owner_dir / LOCK_NAME).exists() and (other / LOCK_NAME).exists()


def test_a_lock_being_written_is_not_mistaken_for_a_stale_one(owner_dir):
    (owner_dir / LOCK_NAME).write_text("", encoding="utf-8")  # created, not yet written
    with pytest.raises(OwnerModeError, match="being written"):
        OwnerLock(owner_dir).acquire("agent-2")
    assert (owner_dir / LOCK_NAME).exists()


def test_the_agent_id_is_recorded_once_the_seat_exists(owner_dir):
    lock = OwnerLock(owner_dir)
    lock.acquire()
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] is None
    lock.set_agent("agent-1")
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] == "agent-1"
    with pytest.raises(OwnerModeError, match="agent-1"):
        OwnerLock(owner_dir).acquire("agent-2")


# -- the CLI ------------------------------------------------------------------------------

class _FakeApp:
    built: list = []
    fail = False

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)
        self.lock_during_run = None

    def run(self, **_kwargs):
        self.lock_during_run = json.loads((self.owner_dir() / LOCK_NAME).read_text(encoding="utf-8"))
        type(self).seen_during_run = self.lock_during_run
        if type(self).fail:
            raise RuntimeError("the app died")

    def owner_dir(self):
        return type(self).built[-1]["owner_memory_dir"]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    _FakeApp.built = []
    _FakeApp.fail = False
    _FakeApp.seen_during_run = None
    monkeypatch.setattr("litetui.app.LiteTUI", _FakeApp)
    monkeypatch.setattr("litetui.shared_state.check_data_version", lambda _root: None)
    monkeypatch.setattr(owner_mode, "require_handoff", lambda: None)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["litetui", *argv])
        cli.main()
    return run


def test_a_launch_holds_the_lock_while_running_and_releases_it_on_clean_exit(owner_dir, cli_env):
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    held = _FakeApp.seen_during_run
    assert held["pid"] == os.getpid() and held["agent_id"], "the lock names the seat once it exists"
    assert not (owner_dir / LOCK_NAME).exists()


def test_the_lock_is_released_when_the_app_dies(owner_dir, cli_env):
    _FakeApp.fail = True
    with pytest.raises(RuntimeError):
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert not (owner_dir / LOCK_NAME).exists()


def test_a_second_launch_exits_non_zero_naming_the_holder_and_creates_no_home(tmp_path, owner_dir, cli_env, capsys):
    OwnerLock(owner_dir).acquire("agent-1")
    with pytest.raises(SystemExit) as exc:
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert exc.value.code == 2 and not _FakeApp.built
    err = capsys.readouterr().err
    assert "in use by agent-1" in err and f"pid {os.getpid()}" in err
    assert not (tmp_path / ".agents").exists()
    assert json.loads((owner_dir / LOCK_NAME).read_text(encoding="utf-8"))["agent_id"] == "agent-1", \
        "the refused launch did not remove the holder's lock"


def test_a_stale_lock_is_taken_over_and_the_app_is_told(owner_dir, cli_env):
    _write_lock(owner_dir, _dead_pid(), "agent-old")
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    note = _FakeApp.built[0]["owner_notice"]
    assert note and "agent-old" in note and "took over" in note
    assert not (owner_dir / LOCK_NAME).exists()


def test_no_lock_means_no_notice(owner_dir, cli_env):
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert _FakeApp.built[0]["owner_notice"] is None


def test_a_refused_flag_leaves_no_lock(owner_dir, cli_env):
    (owner_dir / "NOW.md").unlink()
    with pytest.raises(SystemExit):
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert not (owner_dir / LOCK_NAME).exists()


def test_a_busy_home_releases_the_folder_lock(tmp_path, owner_dir, cli_env):
    import dataclasses
    from litetui import settings
    from litetui.agent_launch_context import ordinary
    held = ordinary(tmp_path, dataclasses.replace(settings.load(), seat_name="Alpha"))
    try:
        with pytest.raises(SystemExit):
            cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    finally:
        held.release()
    assert not (owner_dir / LOCK_NAME).exists()


# -- the lock is never committed ---------------------------------------------------------

NOW = """# Updated
2026-10-11 10:00
# Doing now
widgets
# Next 3 steps
1. one
# Waiting on
nothing
# Crew alive
none
# Do NOT redo
nothing
"""


@pytest.mark.skipif(shutil.which("liteharness") is None or shutil.which("git") is None,
                    reason="liteharness and git are needed for the real handoff")
def test_the_real_handoff_commits_the_two_files_and_never_the_lock(owner_dir):
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                              cwd=owner_dir, capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("add", "--", "SOUL.md", "NOW.md", "DOMAIN.md")
    git("commit", "-q", "-m", "seed")
    (owner_dir / "NOW.md").write_text(NOW, encoding="utf-8")
    lock = OwnerLock(owner_dir)
    lock.acquire("agent-1")
    ok, output = owner_mode.run_handoff(owner_dir, agent_id="agent-1")
    assert ok, output
    assert sorted(git("show", "--name-only", "--format=", "HEAD").split()) == ["NOW.md"], \
        "DOMAIN.md is unchanged, so only NOW.md is in the commit"
    assert "?? .owner.lock" in git("status", "--porcelain")
    assert LOCK_NAME not in git("ls-files")
    lock.release()


# -- review round: a lock path that cannot be taken is a refusal, not a traceback -------------------

def test_a_directory_at_the_lock_path_is_a_refusal(owner_dir):
    (owner_dir / LOCK_NAME).mkdir()
    with pytest.raises(OwnerModeError, match="owner lock"):
        OwnerLock(owner_dir).acquire("agent-1")


def test_the_cli_refuses_a_lock_path_it_cannot_take(tmp_path, owner_dir, cli_env, capsys):
    (owner_dir / LOCK_NAME).mkdir()
    with pytest.raises(SystemExit) as exc:
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert exc.value.code == 2 and not _FakeApp.built
    assert "owner lock" in capsys.readouterr().err
    assert not (tmp_path / ".agents").exists()


def test_metadata_guard_is_persistent_but_released_after_a_transaction(owner_dir):
    from litetui.agent_ownership import _KernelLease
    lock = OwnerLock(owner_dir)
    lock.acquire("Alpha")
    guard = owner_dir / ".owner.guard"
    assert guard.is_file()
    with _KernelLease(guard):
        pass
    lock.release()
    assert guard.is_file() and not lock.path.exists()


def test_an_unreadable_lock_file_is_a_refusal(owner_dir, monkeypatch):
    _write_lock(owner_dir, os.getpid())

    def unreadable(self):
        raise PermissionError("denied")
    monkeypatch.setattr(OwnerLock, "_read", unreadable)
    with pytest.raises(OwnerModeError, match="cannot read the owner lock"):
        OwnerLock(owner_dir).acquire("agent-1")


def test_stale_takeover_serializes_the_compare_unlink_window(owner_dir, monkeypatch):
    from pathlib import Path
    from threading import Event, Thread, current_thread

    _write_lock(owner_dir, _dead_pid(), "old-holder")
    checked = Event()
    second_finished = Event()
    results = {}
    locks = {name: OwnerLock(owner_dir) for name in ("Alpha", "Beta")}
    original_read = Path.read_text
    reads = 0

    def interleaved_read(path, *args, **kwargs):
        nonlocal reads
        raw = original_read(path, *args, **kwargs)
        if path == owner_dir / LOCK_NAME and current_thread().name == "Alpha":
            reads += 1
            if reads == 2:  # stale comparison read, before its unlink
                checked.set()
                second_finished.wait(0.3)
        return raw

    monkeypatch.setattr(Path, "read_text", interleaved_read)

    def take(name):
        try:
            locks[name].acquire(name)
            results[name] = None
        except OwnerModeError as exc:
            results[name] = str(exc)
        finally:
            if name == "Beta":
                second_finished.set()

    first = Thread(target=take, args=("Alpha",), name="Alpha")
    second = Thread(target=take, args=("Beta",), name="Beta")
    first.start()
    assert checked.wait(2), "first taker never reached stale comparison"
    second.start()
    first.join(5)
    second.join(5)
    try:
        assert not first.is_alive() and not second.is_alive()
        winners = [name for name, error in results.items() if error is None]
        assert len(winners) == 1, f"both stale takers acquired: {results}"
        loser = next(name for name in results if name not in winners)
        assert winners[0] in results[loser]
        assert json.loads((owner_dir / LOCK_NAME).read_text())["agent_id"] == winners[0]
    finally:
        for lock in locks.values():
            lock.release()
