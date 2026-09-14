"""The ownership record for a SHARED llama-server router.

Both LiteSuite and LiteTUI now default to port 7470, and both know how to
spawn a router there. Without a record, whoever starts second sees a healthy
router on its own port, decides it is plausibly its own crashed orphan, and
regenerates the ini — restarting the other app's server out from under it.

`~/.litesuite/llm/router.json` is the one fact that ends that: whoever spawned
the router writes down that it did. The other app reads it, sees a live owner
that is not itself, and ATTACHES instead of managing.

🔴 ABSENCE IS MEANINGFUL AND MUST STAY MEANINGFUL. A router nobody wrote a
record for is a foreign server started by hand, and `owner` deliberately has
no "external" value — the missing file IS that case. So this module never
writes a record for a process it did not spawn, and never leaves one behind
for a pid it did not own.

Schema (frozen by LiteSuite WS1 T0, `packages/contracts/src/localRuntime.ts`,
`LocalRouterRecordSchema`) — camelCase on disk, snake_case in Python:

    {"version": 1, "owner": "litesuite"|"litetui", "pid": int, "port": int,
     "ini": str, "startedAt": ISO-8601, "buildTag": str?}
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RECORD_VERSION = 1
OWNER_SELF = "litetui"
_OWNERS = ("litesuite", "litetui")


def record_path() -> Path:
    """`~/.litesuite/llm/router.json` — the same path LiteSuite resolves.

    Deliberately NOT configurable. Two apps agreeing on a port but not on
    where the record lives would be worse than no record at all: each would
    read an empty answer and conclude the other was not there.
    """
    return Path.home() / ".litesuite" / "llm" / "router.json"


@dataclass(frozen=True)
class RouterRecord:
    version: int
    owner: str
    pid: int
    port: int
    ini: str
    started_at: str
    build_tag: str | None = None

    @property
    def is_mine(self) -> bool:
        return self.owner == OWNER_SELF


def read(path: Path | None = None) -> RouterRecord | None:
    """The record, or `None` for every kind of "there isn't one".

    NEVER RAISES. Missing, unreadable, truncated mid-write, hand-edited to
    nonsense — the caller does the same thing in all of them, which is to
    treat the router as unowned. A reader that threw would turn a cosmetic
    problem with a JSON file into an app that cannot start its engine.
    """
    p = record_path() if path is None else path
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        version = raw["version"]
        owner = raw["owner"]
        pid = raw["pid"]
        port = raw["port"]
        ini = raw["ini"]
        started_at = raw["startedAt"]
    except (KeyError, TypeError):
        return None
    # `True` is an int in Python and would sail through an isinstance check,
    # which is how a bool ends up being used as a pid.
    if version != RECORD_VERSION or owner not in _OWNERS:
        return None
    if isinstance(pid, bool) or isinstance(port, bool):
        return None
    if not isinstance(pid, int) or not isinstance(port, int) or pid <= 0:
        return None
    if not isinstance(ini, str) or not isinstance(started_at, str):
        return None
    build_tag = raw.get("buildTag")
    if build_tag is not None and not isinstance(build_tag, str):
        build_tag = None
    return RouterRecord(
        version=version,
        owner=owner,
        pid=pid,
        port=port,
        ini=ini,
        started_at=started_at,
        build_tag=build_tag,
    )


def is_live(record: RouterRecord | None) -> bool:
    """Is the process named by this record still running?

    🔴 THIS DOES NOT CALL `jobkill.alive`, AND THE DIFFERENCE IS THE WHOLE
    POINT. That one answers "did my kill land?", so it returns False when the
    handle cannot be opened — gone, or not ours to see, same answer. Here the
    question is "may I take this router over?", and *not ours to see* is the
    strongest possible NO: some other user or an elevated process owns it.

    ⭐ UNOPENABLE MEANS ALIVE. Reading access-denied as dead would have
    LiteTUI restart an elevated LiteSuite's router — the exact accident this
    record exists to prevent — and it is the same rule LiteSuite's own reader
    applies from the other side (`isPidAlive`, where EPERM counts as alive).
    """
    if record is None:
        return False
    return pid_is_live(record.pid)


def pid_is_live(pid: int) -> bool:
    """Is this pid running? Extracted from `is_live` so the same rule — and
    especially the same reading of "unopenable" — answers for the agent
    registry as well as for the router record (T690).

    ⚠️ EXTRACTED, NOT REWRITTEN. A second liveness test would be a second place
    for "access denied" to be read as "dead", and that misreading is precisely
    what makes one app steal another's server. There is one of these.
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    import ctypes
    from ctypes import wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    STILL_ACTIVE = 259
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return True   # cannot even ask: assume alive rather than steal
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ONLY "no such process" is dead. Access-denied, and anything else we
        # did not anticipate, mean somebody has it.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        code = wt.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def write(
    *,
    pid: int,
    port: int,
    ini: str,
    owner: str = OWNER_SELF,
    build_tag: str | None = None,
    path: Path | None = None,
) -> Path | None:
    """Claim ownership of the router at `port`, atomically.

    tmp + `os.replace` IN THE SAME DIRECTORY, because the other app may read
    this file at any moment and a half-written record is indistinguishable
    from a corrupt one. `os.replace` is atomic on Windows and POSIX alike; a
    cross-directory move would not be.

    🔴 RETURNS `None` — WITHOUT WRITING — WHEN THE FILE ALREADY HOLDS A LIVE
    FOREIGN CLAIM ON A DIFFERENT PORT (T217). There is ONE record for a path
    that is deliberately not configurable, so two routers on two ports cannot
    both be recorded: whoever wrote last erased the other's claim, and the
    erased app then looked UNOWNED to everybody. The next app to start would
    see a healthy router with no record, decide it was a plausible orphan of
    its own, and restart somebody's live server — which is the exact accident
    this module exists to prevent, reintroduced by the bookkeeping meant to
    prevent it.

    ⭐ THE REFUSAL IS NOT A FAILURE, AND THE SPAWN STILL STANDS. Our router is
    up on our port either way; all we lose is the announcement. That is why
    this returns None rather than raising — the caller logs it and carries on,
    exactly as it already does for an unwritable file.

    ⬜ WHY "DIFFERENT PORT" IS PART OF THE CONDITION. A live foreign claim on
    OUR port cannot be reached from here: `_ensure_running_sync` would have
    attached to their router and never spawned. Refusing that case too would
    only add an untestable branch, and a same-port foreign record we DID reach
    is stale bookkeeping about the port we are now serving, so ours is the
    truer answer.
    """
    p = record_path() if path is None else path
    existing = read(p)
    if (
        existing is not None
        and existing.pid != pid
        and existing.port != port
        and is_live(existing)
    ):
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": RECORD_VERSION,
        "owner": owner,
        "pid": pid,
        "port": port,
        "ini": ini,
        "startedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if build_tag:
        payload["buildTag"] = build_tag
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".router-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def remove_if_mine(pid: int, path: Path | None = None) -> bool:
    """Drop the record on the way out, but ONLY if it is still ours.

    The pid guard is not ceremony. Between our spawn and our shutdown another
    app may have taken the port over and written its own record; deleting
    that one would tell everybody the live router is unowned, and the next
    app to start would restart it. We only ever retract our own claim.
    """
    p = record_path() if path is None else path
    record = read(p)
    if record is None or record.pid != pid or not record.is_mine:
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True
