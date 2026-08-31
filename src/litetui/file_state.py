"""Read-state tracker that feeds edit's read-before-edit guard.

The harness has no observable tool events (the only emit() call site is an
intensity/channel event), so cross-tool state needs explicit hooks: this module
snapshots a file when core_tools' `read`/`write` handlers serve it, and
file_tools.tool_edit consults the snapshot before touching anything.

The snapshot is (st_mtime_ns, st_size) at read time, keyed by RESOLVED path so
different spellings of the same file (relative vs absolute) map to one entry.
A change after the read — by anyone — voids it and forces a fresh look before
the next edit.

Deliberately neutral: imports only pathlib, no litetui modules, so core_tools
can import it without a cycle. Process-global by design — a LiteTUI restart
empties it, which is intended (fresh eyes), not a bug to fix.
"""
from __future__ import annotations

from pathlib import Path

_SEEN: dict[str, tuple[int, int]] = {}


def _key(path) -> str:
    return str(Path(path).expanduser().resolve())


def record_read(path) -> None:
    """Snapshot the file's (mtime_ns, size) now. Swallows OSError — a file that
    vanishes between read and stat simply records nothing."""
    try:
        st = Path(path).stat()
    except OSError:
        return
    _SEEN[_key(path)] = (st.st_mtime_ns, st.st_size)


def is_fresh_read(path) -> bool:
    """True only if this file was read AND has not changed since."""
    snap = _SEEN.get(_key(path))
    if snap is None:
        return False
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return (st.st_mtime_ns, st.st_size) == snap


def forget_all() -> None:
    """Drop every snapshot. Test isolation; also the honest shape of a restart."""
    _SEEN.clear()
