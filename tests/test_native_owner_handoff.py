"""Private owner lease, exact Git evidence, and containment of host commit helpers."""
import asyncio
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from litetui import native_owner_handoff as handoff
from litetui import owner_mode, ttyguard


@pytest.fixture
def owner(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.upper().startswith("GIT_"):
            monkeypatch.delenv(key)
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=" + str(memory / "SOUL.md"), *args],
                              cwd=memory, check=True, capture_output=True).stdout
    git("init", "--template=", "--initial-branch=owner-test")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "core.autocrlf", "false")
    for name in (*owner_mode.OWNER_FILES, "other.txt"):
        (memory / name).write_bytes(("Seed " + name).encode())
    git("add", "--", *owner_mode.OWNER_FILES, "other.txt")
    git("commit", "-m", "seed")
    lock = owner_mode.OwnerLock(memory)
    lock.acquire("private-agent")
    app = SimpleNamespace(_owner_memory_dir=memory, _owner_memory_lock=lock,
                          _agent_session=SimpleNamespace(authority=SimpleNamespace(name="Private", agent_id="private-agent")))
    try:
        yield app, git
    finally:
        lock.release()


def test_unchanged_now_never_starts_the_commit(owner, monkeypatch):
    app, _ = owner
    bound = handoff.Binding.capture(app)
    monkeypatch.setattr(owner_mode, "run_handoff", lambda *_a, **_k: pytest.fail("unexpected host write"))
    assert bound.commit(app, 0)[0] is False
    assert bound.readback(app) is None


@pytest.mark.parametrize("change", ["lease", "soul", "branch", "filter", "shadow"])
def test_changed_authority_cannot_reach_the_helper(owner, monkeypatch, change):
    app, git = owner
    bound = handoff.Binding.capture(app)
    monkeypatch.setattr(owner_mode, "run_handoff", lambda *_a, **_k: pytest.fail("unexpected host write"))
    if change == "lease":
        app._owner_memory_lock.set_agent("other")
    elif change == "soul":
        (bound.directory / "SOUL.md").write_text("Changed")
    elif change == "branch":
        git("switch", "-c", "other")
    elif change == "filter":
        (bound.directory / ".gitattributes").write_text("NOW.md filter=external\n")
    else:
        (bound.directory / "git.exe").write_bytes(b"untrusted")
    with pytest.raises(ValueError):
        bound.before(app)
    assert not bound.attempted


@pytest.mark.parametrize("effect", ["no-commit", "foreign-path", "false-success", "good"])
def test_helper_exit_requires_independent_path_limited_git_readback(owner, monkeypatch, effect):
    app, git = owner
    bound = handoff.Binding.capture(app)
    (bound.directory / "NOW.md").write_text("New owner memory")
    def commit(directory, agent, *, command, environment, contained):
        assert directory == bound.directory and agent == bound.agent_id and contained
        assert command == bound.helper and environment["GIT_CONFIG_VALUE_0"] == str(directory / "SOUL.md")
        if effect == "foreign-path":
            (directory / "other.txt").write_text("Foreign delta confined to fixture")
            git("commit", "-m", "unsafe fixture", "--", "NOW.md", "other.txt")
        elif effect in {"good", "false-success"}:
            git("commit", "-m", "owned", "--", "NOW.md", "DOMAIN.md")
            if effect == "false-success":
                (directory / "NOW.md").write_text("Different after commit")
        return True, "Untrusted command output"
    monkeypatch.setattr(owner_mode, "run_handoff", commit)
    if effect == "good":
        assert bound.commit(app, 0) == (True, "Owner handoff commit verified")
        assert bound.readback(app)["parent"] == bound.head
        (bound.directory / "DOMAIN.md").write_text("stale after commit")
        with pytest.raises(ValueError, match="readback"):
            bound.readback(app)
    else:
        with pytest.raises(ValueError):
            bound.commit(app, 0)
        with pytest.raises(ValueError, match="uncertain"):
            bound.readback(app)
        with pytest.raises(ValueError, match="already attempted"):
            bound.commit(app, 0)


@pytest.mark.asyncio
async def test_cancelled_gate_joins_owned_writer(owner, monkeypatch):
    app, _ = owner
    bound = handoff.Binding.capture(app)
    start, done = threading.Event(), threading.Event()
    def wait(_app, _t0):
        start.set()
        assert done.wait(5)
        return True, ""
    monkeypatch.setattr(bound, "commit", wait)
    task = asyncio.create_task(bound.gate(app, 0))
    assert await asyncio.to_thread(start.wait, 2)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    done.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(os.name != "nt", reason="Rust live uses a Windows kernel job")
def test_timed_out_writer_reaps_running_grandchild(tmp_path):
    marker = tmp_path / "started"
    unsafe = tmp_path / "late-write"
    child = tmp_path / "private-writer.py"
    child.write_text("from pathlib import Path\nimport time,sys\n"
                     "Path(sys.argv[1]).touch(); time.sleep(2); Path(sys.argv[2]).touch()\n")
    parent = tmp_path / "private-driver.py"
    parent.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,*sys.argv[1:]])\ntime.sleep(15)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        ttyguard.run_owned_writer([sys.executable, "-I", str(parent), str(child), str(marker), str(unsafe)],
                                  cwd=tmp_path, env=dict(os.environ), timeout=1)
    assert marker.is_file()
    time.sleep(1.3)
    assert not unsafe.exists()


@pytest.mark.asyncio
async def test_owner_rpc_cannot_invoke_ordinary_gate_outside_the_review(owner, monkeypatch):
    from litetui.app import LiteTUI
    app, _ = owner
    app._launch_options = SimpleNamespace(history_projection_version=1)
    monkeypatch.setattr(owner_mode, "owner_gate", lambda *a, **kw: pytest.fail("unreviewed host command"))
    with pytest.raises(ValueError, match="reviewed compaction"):
        await LiteTUI._owner_gate(app, 0)
