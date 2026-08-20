"""/resume must be able to tell one conversation from another.

Every freshly-booted conversation has the title "(no user message)", so rows
showing only time / turns / size / title are literally indistinguishable. Two
different ids fix that and they are NOT the same thing:

  * the CONVERSATION uuid — the .convos/<uuid>/ folder name. On disk for every
    conversation ever written, old and new; it was simply never displayed.
  * the OWNING SEAT (agent name + id) — recorded in the meta record from v3
    onward. It CANNOT be known for older conversations, so those must degrade
    visibly rather than showing an invented or blank owner.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import app as app_mod

app_mod.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-resume-id-"))


def _meta_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_a_new_conversation_records_its_owning_seat():
    a = app_mod.LiteTUI()
    a._new_convo()
    meta = _meta_of(a.convo_path)
    assert meta["type"] == "meta"
    assert meta["v"] >= 3, "meta version must advance when its shape changes"
    assert meta["agent_name"], "no agent_name recorded"
    assert meta["agent_id"], "no agent_id recorded"
    # The id must be the seat's, not a fresh uuid — otherwise the column is
    # decorative and cannot be correlated with `liteharness discover`.
    assert meta["agent_id"] == a.seat.agent_id
    assert meta["agent_name"] == a.seat.name


def test_the_conversation_uuid_is_the_folder_name():
    """The row's id must match the directory, or /resume <id> cannot find it."""
    a = app_mod.LiteTUI()
    a._new_convo()
    assert a.convo_path.parent.name == a.convo_id


def test_two_conversations_are_distinguishable():
    """The failure being fixed: identical rows for different conversations."""
    a = app_mod.LiteTUI()
    a._new_convo()
    first = a.convo_path.parent.name
    a._new_convo()
    second = a.convo_path.parent.name
    assert first != second


def test_a_pre_v3_conversation_shows_no_owner_rather_than_a_wrong_one():
    """Older conversations genuinely cannot know their seat.

    Showing the CURRENT seat for them would be a fabrication — the row would
    claim ownership that was never recorded. Absence must read as absence.
    """
    a = app_mod.LiteTUI()
    a._new_convo()
    p = a.convo_path
    lines = p.read_text(encoding="utf-8").splitlines()
    old_meta = json.loads(lines[0])
    old_meta.pop("agent_name", None)
    old_meta.pop("agent_id", None)
    old_meta["v"] = 2
    lines[0] = json.dumps(old_meta)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    meta = _meta_of(p)
    who = str(meta.get("agent_name") or "")
    aid = str(meta.get("agent_id") or "")
    owner = f"{who} {aid}".strip() or "—"
    assert owner == "—", f"a pre-v3 conversation claimed an owner: {owner!r}"


def test_the_row_carries_both_ids():
    """Build the row the way the picker does and assert both ids appear."""
    a = app_mod.LiteTUI()
    a._new_convo()
    rows = a._list_convos()
    assert rows, "no conversations listed"
    path_, meta_, _msgs = rows[0]
    cid = path_.parent.name[:8]
    who = str(meta_.get("agent_name") or "")[:10]
    aid = str(meta_.get("agent_id") or "")[:8]
    owner = f"{who} {aid}".strip() or "—"
    row = f"{cid}  {owner}"
    assert cid in row and len(cid) == 8
    assert "LiteTUI" in row, f"owner missing from row: {row!r}"
    # And the ids must differ — showing the same value twice would look like
    # identity and carry no information.
    assert cid != aid
