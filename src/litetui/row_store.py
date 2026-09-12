"""A JSON list-of-rows file that two processes can both write (T689).

`background-tasks.json` and `jobs.json` are the same shape: a list of dicts
with an `id`, held in memory from boot and rewritten IN FULL on every change.
That is correct for one process and lossy for two — the second to save erases
whatever the first added, which is the defect this module exists for.

🔴 A DELTA, NOT A MERGE, AND THE DIFFERENCE IS THE WHOLE POINT.
A merge keyed on id is the obvious fix and it is wrong in two directions:

    · it RESURRECTS. `jobs.json` has three real deletion paths (`/cron rm`,
      a loop stopping, the rpc `jobs.delete`). Under a merge, the instance that
      did not delete the job hands it back from its own memory on the next
      save — a worse bug than the one being fixed, and one no green suite can
      see, because every instance involved behaves exactly as written.
    · it lets a STALE copy win. Both instances hold the same historical rows.
      Under a merge, B's untouched copy of a row A just advanced overwrites
      A's newer one, purely because B saved last.

Both disappear once the question is "what did THIS instance change?" rather
than "what does this instance hold?". A row nobody touched is in nobody's
delta, so disk keeps the owner's last word on it; a removal IS a delta entry,
so it applies once and cannot come back.

⬜ THE BASELINE IS WHAT THE CALLER SAYS IT HOLDS, and it is stated rather than
inferred. Inferring it from the last read looked simpler and is a trap: a
loader that DROPS rows (an unparseable one, say) would have them counted as
deletions and erased, and a rebaseline taken from the merged result would
adopt a sibling's rows as ours and delete them on the next save. Both failures
are silent. `rebaseline` is therefore one explicit call next to each load.

⚠️ A FILE WITH NO BASELINE READS AS "we changed everything we hold" — the old
whole-file behaviour for our own rows, plus rows on disk we have never seen
left alone. That is the conservative direction: with no baseline there are no
removals, so nothing can be deleted by not knowing.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

#: path -> the rows this process believes are ITS OWN, as of its last load or
#: save. Not a cache of the file's contents: disk is re-read on every write.
#: This is only the thing the delta is computed against.
_BASELINE: dict[str, list[dict]] = {}


def _key_of(path: Path) -> str:
    return str(Path(path).resolve())


def forget(path: Path | None = None) -> None:
    """Drop the baseline for one file, or all of them.

    Exists for the suite: module state that survives between tests makes one
    test's answer depend on which tests ran before it — the class `conftest.py`
    already resets `_DEFAULT_VRAM_GATE` for.
    """
    if path is None:
        _BASELINE.clear()
    else:
        _BASELINE.pop(_key_of(path), None)


def rebaseline(path: Path, rows: list[dict]) -> None:
    """Record what this process now holds for `path`. Call it beside the load."""
    _BASELINE[_key_of(path)] = [dict(r) for r in rows]


def rows_on_disk(path: Path) -> list[dict]:
    """The file as raw rows. Unreadable, unparseable or not-a-list reads empty.

    A corrupt store must not stop the app: losing history is survivable, and
    refusing to write is not — the row for the task running right now would
    never land.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def apply_delta(
    baseline: list[dict], mine: list[dict], disk: list[dict], *, key: str = "id"
) -> list[dict]:
    """`disk`, with the changes `mine` made since `baseline`. Pure.

    Rows without `key` cannot be identified across instances, so disk's are
    kept where they are and ours are appended. Nothing invents an id: a row
    with no identity is not a row two processes can reconcile, and inventing
    one would make the reconciliation merely LOOK successful.
    """
    base = {r[key]: r for r in baseline if key in r}
    now = {r[key]: r for r in mine if key in r}

    removed = set(base) - set(now)
    changed = {k: v for k, v in now.items() if base.get(k) != v}

    out: list[dict] = []
    seen: set = set()
    for row in disk:
        rid = row.get(key)
        if rid is None:
            out.append(row)
            continue
        if rid in removed:
            continue
        seen.add(rid)
        out.append(changed.get(rid, row))

    for rid, row in now.items():
        if rid in seen:
            continue
        if rid in base:
            # Ours, in our baseline, and gone from disk: a sibling deleted it
            # while we held it. Deletion wins even when we also edited it — an
            # edit is a smaller claim than a removal, and resurrecting is the
            # one failure this module exists to prevent.
            continue
        out.append(row)

    out.extend(r for r in mine if key not in r)
    return out


def write(
    path: Path,
    rows: list[dict],
    *,
    key: str = "id",
    prefix: str,
    ensure_ascii: bool = True,
) -> None:
    """Re-read, apply this process's delta, replace atomically, rebaseline.

    Temp file in the SAME directory then `os.replace`: never open the real path
    for writing, because that truncates at open and lands any failure between
    the destructive step and the constructive one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = apply_delta(
        _BASELINE.get(_key_of(path), []), rows, rows_on_disk(path), key=key
    )

    payload = json.dumps(merged, indent=2, ensure_ascii=ensure_ascii)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Our own rows, NOT `merged`: adopting a sibling's rows as ours would make
    # them look deleted the next time we save without them.
    rebaseline(path, rows)
