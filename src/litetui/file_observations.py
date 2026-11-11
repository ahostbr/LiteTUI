"""Validate a bounded read without mixing Windows timestamp meanings."""
from __future__ import annotations

import os
from typing import Any, Protocol

_MISSING = object()
_WINDOWS = os.name == "nt"


class Stamp(Protocol):
    def __call__(self, info: Any, *, birthtime: bool = False) -> tuple[int, ...]: ...


def stable_read(before: Any, opened: Any, finished: Any, after: Any, *,
                stamp: Stamp) -> bool:
    """Keep same-API change detection and cross-API file identity checks.

    On some Windows Python versions stat's ctime is creation time but fstat's
    ctime is metadata change time (CPython #157671). Both remain useful within
    their own API. Birthtime, when present throughout, supplies the common time
    domain without dropping either original before/after ctime comparison.
    """
    if stamp(before) != stamp(after) or stamp(opened) != stamp(finished):
        return False
    values = (before, opened, finished, after)
    if not _WINDOWS:
        return stamp(before) == stamp(opened)
    births = tuple(getattr(info, "st_birthtime_ns", _MISSING) for info in values)
    if any(value is not _MISSING and type(value) is not int for value in births):
        return False
    if any(value is _MISSING for value in births):
        # Older Python has no independent creation time. Keep the full original
        # comparison; partial availability is not permission to discard ctime.
        return stamp(before) == stamp(opened)
    # Preserve the caller's exact remaining fields (including its permission
    # mask): Windows path stat infers executable bits from a filename but fstat
    # cannot. The engine reader deliberately compares those bits path-to-path.
    return all(stamp(info, birthtime=True) == stamp(before, birthtime=True)
               for info in values[1:])
