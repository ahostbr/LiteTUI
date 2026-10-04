"""The per-project AGENT_INDEX.md: which file, and how much of it reaches the prompt.

One committed file at a repo root tells an agent what to read before touching an
area. LiteTUI snapshots it into the system prompt next to the store (see
appsvc.index_block). The same convention -- file name, walk up to the git root,
12k cap -- is implemented independently by the liteharness package
(`liteharness index --check`, SessionStart brief), which this app does not
import. Change one, change the other.
"""
from __future__ import annotations

import os
from pathlib import Path

INDEX_NAME = "AGENT_INDEX.md"
#: Longer than this and the prompt copy is cut (with a warning naming the file).
INDEX_CAP = 12_000


def find_index(start: str | os.PathLike) -> Path | None:
    """The AGENT_INDEX.md of the repo containing `start`, or None.

    Walks up from `start` and stops at the first directory holding a `.git`
    (a file in a worktree): a repo never borrows the index of the repo around
    it. With no git root above `start`, only `start` itself is consulted.
    """
    here = Path(start).resolve()
    if here.is_file():
        here = here.parent
    chain = (here, *here.parents)
    root = next((d for d in chain if (d / ".git").exists()), None)
    if root is None:
        candidate = here / INDEX_NAME
        return candidate if candidate.is_file() else None
    for d in chain[: chain.index(root) + 1]:
        candidate = d / INDEX_NAME
        if candidate.is_file():
            return candidate
    return None


def read_index(start: str | os.PathLike) -> tuple[Path, str, int] | None:
    """(path, text cut to INDEX_CAP, full length) for the repo around `start`.

    None when there is no index, it is unreadable, or it is empty. Undecodable
    bytes degrade to U+FFFD: a stray cp1252 byte must not raise out of a turn.
    """
    path = find_index(start)
    if path is None:
        return None
    try:
        # raw decode, no newline folding: the length matches `liteharness index --check`
        text = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    return path, text[:INDEX_CAP], len(text)
