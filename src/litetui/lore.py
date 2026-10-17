"""The lore file: a few short stories every seat is told at the start of a conversation.

One markdown file, `## ` sections, hard-capped so it costs every boot the same
few hundred tokens. LiteTUI snapshots it into the system prompt next to the
store (see appsvc.lore_block). The same convention -- file, 2,400-char cap, four
sections, cut on a line boundary with one warning line, missing/empty means
nothing -- is implemented independently by the liteharness package (SessionStart
brief), which this app does not import. Change one, change the other.
"""
from __future__ import annotations

import os
from pathlib import Path

LORE_ENV = "LITEHARNESS_LORE_FILE"
#: Longer than this and the prompt copy is cut on a line boundary (with a warning line).
LORE_CAP = 2_400
#: More `## ` sections than this and the rest are dropped (with a warning line).
LORE_SECTIONS = 4


def lore_path() -> Path:
    """LITEHARNESS_LORE_FILE, else ~/.liteharness/lore.md. LiteTUI reads no harness config."""
    override = os.environ.get(LORE_ENV)
    return Path(override).expanduser() if override else Path.home() / ".liteharness" / "lore.md"


def read_lore() -> tuple[Path, str, list[str]] | None:
    """(path, text within the section limit and the cap, warning lines), or None.

    None when the file is missing, unreadable or empty. Undecodable bytes
    degrade to U+FFFD: a stray cp1252 byte must not raise out of a turn.
    """
    path = lore_path()
    try:
        text = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    where = str(path).replace("\\", "/")
    warnings: list[str] = []
    lines = text.split("\n")  # not splitlines(): the text keeps its own line endings
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if len(starts) > LORE_SECTIONS:
        text = "\n".join(lines[: starts[LORE_SECTIONS]]).rstrip()
        warnings.append(f"[... lore {where}: {len(starts)} sections, only the first {LORE_SECTIONS} are injected]")
    if len(text) > LORE_CAP:
        size = len(text)
        cut = text[:LORE_CAP]
        # whole lines only, so no story is cut mid-sentence; no newline at all -> hard cut
        if text[LORE_CAP] != "\n" and "\n" in cut:
            cut = cut[: cut.rfind("\n")]
        text = cut.rstrip()
        warnings.append(f"[... lore {where} truncated at {LORE_CAP} of {size} chars]")
    return path, text, warnings
