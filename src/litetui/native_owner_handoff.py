"""Source-owned evidence for reviewed host compaction of an explicitly selected owner store."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from litetui import owner_mode, ttyguard
from litetui.agent_store import _unlinked
from litetui.native_context import _regular_file

OID = re.compile(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _helper():
    command = tuple(owner_mode.handoff_command())
    if len(command) == 3 and command[1:] == ("-m", "liteharness.cli"):
        return (command[0], "-I", *command[1:])
    return command


def _git_environment(directory):
    # A command-scoped Git configuration reaches the canonical helper's children.
    # Bind the repo through cwd alone. SOUL.md is a validated regular file, so
    # it cannot contain hook executables.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    config = (("core.hooksPath", str(directory / owner_mode.SOUL)), ("core.fsmonitor", "false"),
              ("commit.gpgSign", "false"), ("gc.auto", "0"), ("maintenance.auto", "false"))
    env.update(GIT_CONFIG_COUNT=str(len(config)), GIT_TERMINAL_PROMPT="0")
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _files(directory):
    values = {}
    for name in owner_mode.OWNER_FILES:
        status, data = _regular_file(directory / name)
        if status != "content":
            raise ValueError("Owner documents must be unlinked regular files within 256 KiB each")
        values[name] = data
    return values


@dataclass
class Binding:
    directory: Path
    lock: object
    agent_id: str
    git: str
    helper: tuple
    head: str
    branch: bytes
    initial: dict
    attempted: bool = False
    receipt: dict | None = None

    def run_git(self, *args, check=True):
        proc = ttyguard.run([self.git, "--no-optional-locks", *args], cwd=self.directory,
                            env=_git_environment(self.directory), binary=True, timeout=10)
        if check and proc.returncode:
            raise ValueError("Could not verify the selected owner Git repository")
        return proc

    def repository(self):
        directory = self.directory
        if any((directory / name).exists() for name in ("git.exe", "git.com", "git.cmd", "git.bat")):
            raise ValueError("Owner folder must not shadow the selected Git executable")
        git_dir = _unlinked(directory / ".git")
        if (not git_dir.is_dir() or (git_dir / "commondir").exists()
                or (git_dir / "objects" / "info" / "alternates").exists()):
            raise ValueError("Reviewed handoff needs a dedicated Git root at the owner folder")
        toplevel = Path(os.fsdecode(self.run_git("rev-parse", "--show-toplevel").stdout.rstrip(b"\r\n")))
        if not directory.samefile(toplevel):
            raise ValueError("Owner folder must be the selected repository root")
        for path in ("config", "HEAD", "index", "objects", "refs", "packed-refs", "info/attributes"):
            _unlinked(git_dir / path)
        # Object writes choose a two-hex fanout. Inspect existing directories only.
        for entry in (git_dir / "objects").iterdir():
            if re.fullmatch("[a-fA-F0-9]{2}|pack|info", entry.name):
                _unlinked(entry)
        branch = self.run_git("symbolic-ref", "HEAD").stdout.strip()
        if not branch.startswith(b"refs/heads/") or len(branch) > 1024:
            raise ValueError("Reviewed handoff needs a local branch")
        ref = Path(os.fsdecode(branch))
        if ref.is_absolute() or ".." in ref.parts:
            raise ValueError("Owner branch path is outside the selected repository")
        for path in (git_dir / ref, git_dir / "logs" / ref, git_dir / "logs" / "HEAD"):
            _unlinked(path)
        head = self.run_git("rev-parse", "--verify", "HEAD").stdout.strip()
        if not OID.fullmatch(head):
            raise ValueError("Owner repository has no verifiable current commit")
        attrs = self.run_git("check-attr", "-z", "filter", "working-tree-encoding", "--", "NOW.md", "DOMAIN.md").stdout.split(b"\0")
        if len(attrs) != 13 or any(value not in {b"unspecified", b"unset"} for value in attrs[2:12:3]):
            raise ValueError("Owner handoff cannot run custom Git filters or encodings")
        return head.decode("ascii"), branch

    @classmethod
    def capture(cls, app):
        value = owner_mode.native_binding(app)["owner_store"]
        directory = Path(value["directory"])
        git = shutil.which("git")
        if not git:
            raise ValueError("Reviewed owner handoff needs Git")
        binding = cls(directory, app._owner_memory_lock, value["agent_id"], git,
                      _helper(), "", b"", _files(directory))
        binding.head, binding.branch = binding.repository()
        return binding

    def check(self, app):
        value = owner_mode.native_binding(app)["owner_store"]
        if (self.lock is not app._owner_memory_lock or value["agent_id"] != self.agent_id
                or Path(value["directory"]) != self.directory
                or _helper() != self.helper or shutil.which("git") != self.git):
            raise ValueError("Owner handoff lease, folder or host command changed")

    def plan(self):
        return {"directory": str(self.directory), "head": self.head,
                "files": {key: _hash(data) for key, data in self.initial.items()}}

    def before(self, app):
        self.check(app)
        if (self.head, self.branch) != self.repository() or self.initial != _files(self.directory):
            raise ValueError("Owner files or Git state changed after review")

    def readback(self, app):
        self.check(app)
        if self.receipt is None:
            if self.attempted:
                raise ValueError("Owner commit outcome is uncertain. Inspect the selected repository.")
            return None
        head, branch = self.repository()
        files = _files(self.directory)
        if (branch != self.branch or head != self.receipt["commit"]
                or {k: _hash(v) for k, v in files.items()} != self.receipt["files"]):
            raise ValueError("Owner handoff changed before independent readback")
        return self.receipt

    def commit(self, app, t0):
        self.check(app)
        if self.attempted or self.repository() != (self.head, self.branch):
            raise ValueError("Owner commit was already attempted or its Git base changed")
        files = _files(self.directory)
        if files["SOUL.md"] != self.initial["SOUL.md"]:
            raise ValueError("The reviewed owner SOUL.md changed")
        if (files["NOW.md"] == self.initial["NOW.md"] or
                (self.directory / "NOW.md").stat().st_mtime_ns + owner_mode.MTIME_SLACK_NS < t0):
            return False, "NOW.md has not been rewritten during this compaction"
        self.attempted = True
        ok, _ = owner_mode.run_handoff(self.directory, self.agent_id, command=self.helper,
                                        environment=_git_environment(self.directory), contained=True)
        if not ok:
            raise ValueError("Owner handoff failed. Inspect HEAD and the index before retrying.")
        self.check(app)
        head, branch = self.repository()
        parents = self.run_git("rev-list", "--parents", "-n", "1", head).stdout.strip().decode("ascii").split()
        if (branch != self.branch or head == self.head or parents != [head, self.head]
                or self.run_git("diff-tree", "-r", "--quiet", self.head, head, "--", ".",
                                ":(exclude)NOW.md", ":(exclude)DOMAIN.md", check=False).returncode != 0):
            raise ValueError("Owner handoff did not make exactly one path-limited commit")
        if _files(self.directory) != files:
            raise ValueError("Owner handoff files changed during the commit")
        for name in ("NOW.md", "DOMAIN.md"):
            obj = f"{head}:{name}"
            size = int(self.run_git("cat-file", "-s", obj).stdout)
            if size < 0 or size > len(files[name]):
                raise ValueError("Committed owner file exceeds the reviewed file size")
            blob = self.run_git("cat-file", "blob", obj).stdout
            if blob not in (files[name], files[name].replace(b"\r\n", b"\n")):
                raise ValueError("Committed owner file disagrees with the verified working file")
        self.receipt = {"parent": self.head, "commit": head,
                        "files": {key: _hash(data) for key, data in files.items()}}
        return True, "Owner handoff commit verified"

    async def gate(self, app, t0):
        task = asyncio.create_task(asyncio.to_thread(self.commit, app, t0))
        # Stop/Quit must outlive this host writer even if the provider worker is
        # cancelled while Git runs. Never schedule a second helper after uncertainty.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise
