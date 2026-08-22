"""/rename gives a conversation a name, and every listing shows it.

Ryan: "for convos we need a naming feature aka /rename ... it should show the
convo name in the convo picker afterwards. this is good when i want to tag a
certain convo as X."

Two design points are asserted here because both could plausibly have gone the
other way:

  1. THE RENAME IS AN APPEND-ONLY RECORD, not a patch of the meta line. Every
     other mutation in convo.jsonl (edit, truncate, snapshot) appends and is
     folded on replay; rewriting a JSONL line in place is how a torn write
     loses a whole transcript. The last rename simply wins.

  2. ONE LABEL HELPER, TWO LISTINGS. /convos and the /resume picker already
     render the same column, and the code says in its own comment that the two
     must not drift. A name that appeared in one and not the other would be
     exactly that drift.

Nothing here writes to a real conversation: the read tests use tmp_path and the
command tests drive a stub, the same way test_message_queue drives the flush.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
from plugins.convo import _cmd_rename


def _write(tmp_path: Path, *records) -> Path:
    p = tmp_path / "convo.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


META = {"type": "meta", "v": 3, "id": "abc", "model": "m"}
SNAP = {"type": "snapshot", "messages": [{"role": "user", "content": "how do I do X"}]}


class _StubApp:
    """Enough app for /rename, and nothing that can reach a real conversation."""

    def __init__(self, path=None):
        self.convo_path = path
        self.records: list[dict] = []
        self.said: list[str] = []
        self.materialised = 0

    def _materialise_convo(self):
        self.materialised += 1

    def _write_record(self, rec):
        self.records.append(rec)

    def _system(self, text):
        self.said.append(text)

    _read_convo = staticmethod(m.LiteTUI._read_convo)


def test_a_rename_record_folds_into_meta_on_replay(tmp_path: Path) -> None:
    p = _write(tmp_path, META, SNAP, {"type": "rename", "name": "paywall audit"})
    meta, msgs = m.LiteTUI._read_convo(p)
    assert meta["name"] == "paywall audit"
    assert meta["id"] == "abc", "folding the name dropped the rest of the meta"
    assert len(msgs) == 1, "the rename record disturbed the messages"


def test_the_last_rename_wins(tmp_path: Path) -> None:
    p = _write(
        tmp_path, META, SNAP,
        {"type": "rename", "name": "first"},
        {"type": "rename", "name": "second"},
    )
    meta, _ = m.LiteTUI._read_convo(p)
    assert meta["name"] == "second"


def test_an_empty_rename_clears_the_name(tmp_path: Path) -> None:
    p = _write(
        tmp_path, META, SNAP,
        {"type": "rename", "name": "temporary"},
        {"type": "rename", "name": "   "},
    )
    meta, _ = m.LiteTUI._read_convo(p)
    assert "name" not in meta, "a cleared name lingered in meta"


def test_the_name_wins_over_the_derived_preview() -> None:
    msgs = SNAP["messages"]
    assert m.LiteTUI._convo_label({}, msgs) == m.LiteTUI._convo_title(msgs), (
        "with no name, the listing must still show the first-message preview"
    )
    assert m.LiteTUI._convo_label({"name": "paywall audit"}, msgs) == "paywall audit"
    # Whitespace-only is not a name.
    assert m.LiteTUI._convo_label({"name": "  "}, msgs) == m.LiteTUI._convo_title(msgs)


def test_rename_writes_the_record_and_materialises_first(tmp_path: Path) -> None:
    a = _StubApp(tmp_path / "convo.jsonl")
    _cmd_rename(a, "/rename", "paywall audit")
    assert a.materialised == 1, (
        "a conversation staged but not yet on disk has nowhere to put the record"
    )
    assert a.records == [{"type": "rename", "name": "paywall audit"}]
    assert "paywall audit" in a.said[0]


def test_a_name_is_collapsed_and_capped(tmp_path: Path) -> None:
    a = _StubApp(tmp_path / "convo.jsonl")
    _cmd_rename(a, "/rename", "  the   litesuite\n\npaywall   audit  ")
    assert a.records[0]["name"] == "the litesuite paywall audit"

    b = _StubApp(tmp_path / "convo.jsonl")
    _cmd_rename(b, "/rename", "x" * 200)
    # Bounded like the derived title, so one named row cannot blow the column
    # apart when every other row is capped.
    assert len(b.records[0]["name"]) <= 61


def test_rename_with_no_argument_reports_instead_of_clearing(tmp_path: Path) -> None:
    """Bare /rename must not wipe the name — it is the obvious typo."""
    p = _write(tmp_path, META, SNAP, {"type": "rename", "name": "paywall audit"})
    a = _StubApp(p)
    _cmd_rename(a, "/rename", "")
    assert a.records == [], "bare /rename wrote a record"
    assert "paywall audit" in a.said[0]


def test_rename_with_no_conversation_says_so(tmp_path: Path) -> None:
    a = _StubApp(None)
    _cmd_rename(a, "/rename", "something")
    assert a.records == []
    assert a.said, "renaming with no conversation said nothing at all"


def test_both_listings_go_through_the_label_helper() -> None:
    """The drift gate: neither listing may render the derived title directly."""
    src = (Path(__file__).resolve().parent.parent / "src" / "plugins" / "convo.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    assert "_convo_title(msgs)" not in src, (
        "a listing still renders the derived preview directly — a named "
        "conversation would show in one surface and not the other"
    )
    assert src.count("_convo_label(meta, msgs)") == 2, (
        "expected exactly two listings (/convos and the /resume picker)"
    )
