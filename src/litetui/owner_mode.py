"""Owner mode: `litetui --owner NAME --memory DIR`.

A LiteTUI agent whose long memory is a folder it does not own: `DIR` holds the
three files below and outlives every conversation. They are injected INSTEAD of
the agent home's own memory.md / soul.md / handoff.md, on every backend, and a
compaction rewrites NOW.md and DOMAIN.md and commits them through
`liteharness owner-handoff` (always a subprocess; liteharness is never imported).

Per launch only: nothing here is saved in the agent's settings.json, so owner
mode adds no standing authority. Validation lives in this module so the CLI can
refuse BEFORE any agent home or lock is created.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from litetui.agent_store import StoreError, _unlinked

#: Injection order and caps, in characters. DOMAIN.md's cap is the setting
#: `owner_domain_cap` (it is long, and its newest rows are first).
SOUL, NOW, DOMAIN = "SOUL.md", "NOW.md", "DOMAIN.md"
OWNER_FILES = (SOUL, NOW, DOMAIN)
SOUL_CAP, NOW_CAP = 8000, 6000
DEFAULT_DOMAIN_CAP = 30000

#: The refusal prefix `liteharness owner-handoff` prints on every refusal. The
#: availability probe requires it: a bare non-zero exit could be any failure.
HANDOFF_REFUSAL = "HANDOFF REFUSED"


class OwnerModeError(ValueError):
    """A launch refusal with a message fit to print as-is."""


def store_files(settings) -> tuple[tuple[str, int], ...]:
    """(file name, injection cap) in injection order."""
    cap = getattr(settings, "owner_domain_cap", DEFAULT_DOMAIN_CAP)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        cap = DEFAULT_DOMAIN_CAP
    return ((SOUL, SOUL_CAP), (NOW, NOW_CAP), (DOMAIN, cap))


def validate_memory_dir(raw: str) -> Path:
    """The folder named by --memory, or a refusal. Creates nothing."""
    if not raw or not str(raw).strip():
        raise OwnerModeError("--memory needs a folder")
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise OwnerModeError(f"--memory must be an absolute path: {raw}")
    try:
        _unlinked(path)
    except (StoreError, OSError):
        raise OwnerModeError(f"--memory must not be, or sit under, a link or junction: {raw}") from None
    if not path.is_dir():
        raise OwnerModeError(f"--memory is not an existing folder: {raw}")
    for name in OWNER_FILES:
        try:
            _unlinked(path / name)
        except (StoreError, OSError):
            raise OwnerModeError(f"--memory file must not be a link: {path / name}") from None
        if not (path / name).is_file():
            raise OwnerModeError(f"--memory folder has no {name}: {path}")
    return path


def validate_owner_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise OwnerModeError("--owner needs a name")
    return name


def handoff_command() -> list[str]:
    """The argv prefix that runs the liteharness CLI as a subprocess."""
    found = shutil.which("liteharness")
    if found:
        return [found]
    return [sys.executable, "-m", "liteharness.cli"]


def require_handoff() -> None:
    """Fail fast, with one message, when `liteharness owner-handoff` is not there.

    `owner-handoff` has no --help (it refuses unknown flags). So the probe runs
    it at a folder that cannot exist and requires BOTH a non-zero exit and its
    own refusal text: a missing executable, a liteharness without the command
    ("Unknown command") and a crash all fail this, and only the real command
    passes it. Nothing is committed: the folder check refuses first.
    """
    missing = Path(tempfile.gettempdir()) / "litetui-owner-probe-does-not-exist"
    try:
        probe = subprocess.run([*handoff_command(), "owner-handoff", "--memory", str(missing)],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        probe = None
    text = "" if probe is None else (probe.stdout or "") + (probe.stderr or "")
    if probe is None or probe.returncode == 0 or HANDOFF_REFUSAL not in text:
        raise OwnerModeError(
            "owner mode needs `liteharness owner-handoff` (it commits NOW.md and DOMAIN.md) and "
            "it was not found or is too old: install or update liteharness, then launch again")


def run_handoff(memory_dir: Path, agent_id: str | None = None, timeout: float = 120.0) -> tuple[bool, str]:
    """Run `liteharness owner-handoff --memory DIR`; (ok, the reason or its output).

    Outside the tool policy on purpose: this is the host committing, not the
    model running git. Exact paths only: the command itself commits NOW.md and
    DOMAIN.md and never anything else, so `.owner.lock` is never staged.
    """
    env = dict(os.environ)
    if agent_id:
        env["LITEHARNESS_AGENT_ID"] = agent_id
    try:
        done = subprocess.run([*handoff_command(), "owner-handoff", "--memory", str(memory_dir)],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run liteharness owner-handoff: {exc}"
    output = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode != 0:
        return False, output or f"liteharness owner-handoff exited {done.returncode}"
    return True, output


# -- one seat per memory folder -----------------------------------------------------------

LOCK_NAME = ".owner.lock"
_UNREADABLE_GRACE_S = 10.0


def pid_alive(pid: int) -> bool:
    """Is process `pid` running? The agent lease is a kernel lock with no pid in it,
    so there is no helper there to reuse; on Windows this is jobkill.alive."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        from litetui import jobkill
        return jobkill.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class OwnerLock:
    """`<DIR>/.owner.lock`, created exclusively and holding {pid, agent_id, started}.

    The agent lease protects only the agent home; a foreign memory folder has no
    lease, so two seats on one folder could clobber NOW.md. A second launch is
    refused naming the holder; a lock whose pid is dead is taken over and the
    caller is handed a one-line note to show. Removed on clean exit and never
    committed (owner-handoff stages NOW.md and DOMAIN.md by exact path).

    Pid reuse can make a dead holder look alive: the refusal names the file so a
    person can delete it. Like the agent lease this is not a defence against a
    hostile process, only against two honest launches.

    The takeover of a stale lock is read, compare, unlink: it re-reads the file and
    removes it only if it is unchanged since it was judged stale, which narrows the
    window in which two launches both judge the same crashed holder stale, but it is
    not atomic. In that narrow window both can end up holding the lock.
    """

    def __init__(self, directory: Path):
        self.path = Path(directory) / LOCK_NAME
        self._mine: tuple[int, str] | None = None

    def _read(self) -> tuple[str, dict | None]:
        import json
        import time
        raw = ""
        for _ in range(5):
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return "", None
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("pid"), int):
                return raw, data
            time.sleep(0.02)  # the creator may be between create and write
        return raw, None

    def acquire(self, agent_id: str | None = None) -> str | None:
        """Take the lock. Returns a takeover note, or None; raises OwnerModeError if held."""
        import json
        import time
        from datetime import datetime, timezone
        note = None
        for _ in range(4):
            started = datetime.now(timezone.utc).isoformat()
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                try:
                    raw, holder = self._read()
                    age = time.time() - self.path.stat().st_mtime if holder is None and self.path.exists() else 0
                except OSError as exc:  # e.g. a directory sits where the lock file should be
                    raise OwnerModeError(f"cannot read the owner lock {self.path}: {exc}") from None
                if not raw and holder is None and not self.path.exists():
                    continue  # released between our create and read: try again
                if holder is None:
                    if age < _UNREADABLE_GRACE_S:
                        raise OwnerModeError(f"this memory folder's lock {self.path} is being written by another "
                                             "launch: try again in a moment") from None
                    gone = "an unreadable lock"
                elif pid_alive(holder["pid"]):
                    who = holder.get("agent_id") or "another LiteTUI"
                    raise OwnerModeError(
                        f"this memory folder is in use by {who} (pid {holder['pid']}): close that session first. "
                        f"If no such session is running, delete {self.path}") from None
                else:
                    gone = f"{holder.get('agent_id') or 'a LiteTUI'} (pid {holder['pid']}, no longer running)"
                try:
                    if self.path.read_text(encoding="utf-8") == raw:  # unchanged since judged stale
                        self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise OwnerModeError(f"cannot clear the stale lock {self.path}: {exc}") from None
                note = f"took over the owner lock left by {gone}"
                continue
            except OSError as exc:  # no folder permission, a full disk, ...
                raise OwnerModeError(f"cannot take the owner lock {self.path}: {exc}") from None
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "agent_id": agent_id, "started": started}, handle)
            self._mine = (os.getpid(), started)
            return note
        raise OwnerModeError(f"could not take the owner lock {self.path}: another launch keeps winning it")

    def set_agent(self, agent_id: str) -> None:
        """Record the agent id once the seat exists (the lock is taken before it does)."""
        import json
        _raw, data = self._read()
        if self._mine is None or data is None or (data.get("pid"), data.get("started")) != self._mine:
            return
        data["agent_id"] = agent_id
        try:
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def release(self) -> None:
        """Remove the lock if it is still ours; idempotent, never raises."""
        if self._mine is None:
            return
        try:
            _raw, data = self._read()
            if data is not None and (data.get("pid"), data.get("started")) == self._mine:
                self.path.unlink()
        except OSError:
            pass
        self._mine = None


# -- the compaction contract ----------------------------------------------------------------

def _posix(path) -> str:
    return str(path).replace("\\", "/")


def compact_prompt(owner_dir, *, summary: bool = True) -> str:
    """STEP 1 / STEP 2 for an owner seat: rewrite NOW.md, append to DOMAIN.md, then summarise.

    `summary=False` is STEP 1 alone, for a backend that compacts natively and writes its
    own summary (Codex): the seat writes the files and confirms in one line.
    """
    from litetui.textfmt import load_prompt
    text = load_prompt("compact-owner", dir=_posix(owner_dir))
    if summary:
        return text
    return (text.split("\nSTEP 2")[0].rstrip()
            + "\n\nThen reply with one short line saying NOW.md is written. The host commits it "
            "and compacts the context after that; do not write a summary.")


def retry_text(reason: str, owner_dir, *, summary: bool = True) -> str:
    """The one retry turn after a refused handoff: names the reason and what to redo."""
    ending = "reply again with only the summary" if summary else "reply with one short line"
    return (f"NOW.md was NOT committed: {reason}\n\n"
            f"Fix it now. Rewrite {_posix(owner_dir)}/NOW.md whole, in the template (maximum 60 lines; the headings "
            "Updated, Doing now, Next 3 steps, Waiting on, Crew alive, Do NOT redo, each once and each with "
            f"something under it), and append anything new to DOMAIN.md. Then {ending}.")


#: Owner mode shows this once each time the context rises through REMINDER_AT_PERCENT, on
#: every backend: the cue to run /compact (and so write NOW.md) before an automatic
#: compaction, or on Codex its own uninterruptible one, gets there first.
REMINDER_AT_PERCENT = 70
CONTEXT_REMINDER = "context 70%: run /compact to write NOW.md first"


def not_committed_note(reason: str) -> str:
    return f"NOW.md was NOT committed: {reason}"


#: File times tick coarsely (NTFS can stamp a write up to a few ms behind the clock that
#: produced t0), so a write made right after t0 can read as slightly older. A note written
#: this close to the start of a compaction was not written by STEP 1 anyway; anything older
#: than this is rejected as unchanged.
MTIME_SLACK_NS = 50_000_000


async def owner_gate(owner_dir, t0_ns: int, agent_id: str | None = None) -> tuple[bool, str]:
    """After STEP 1, before anything is truncated: did the seat write NOW.md, and did it commit?

    (a) NOW.md's mtime is at or after `t0_ns` (the seat actually rewrote it during
    this compaction); (b) `liteharness owner-handoff` runs as a subprocess, outside
    the tool policy, and validates the template and commits NOW.md and DOMAIN.md.
    Returns (ok, reason); the reason is the first thing that failed.
    """
    import asyncio
    now = Path(owner_dir) / NOW
    try:
        modified = now.stat().st_mtime_ns
    except OSError as exc:
        return False, f"NOW.md cannot be read: {exc}"
    if modified + MTIME_SLACK_NS < t0_ns:
        return False, "NOW.md was not rewritten during this compaction (it has not changed since the compaction began)"
    ok, output = await asyncio.to_thread(run_handoff, owner_dir, agent_id)
    if ok:
        return True, output
    return False, " ".join(output.split()) or "the handoff refused without a reason"
