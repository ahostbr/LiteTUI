"""Owner-neutral emission and explicit legacy conversation-read compatibility."""
from types import SimpleNamespace

import pytest

from litetui import seat_authority
from litetui.claude_persistence import ClaudeLedger
from litetui.codex_steering import SteeringLedger
from litetui.legacy_goal_source import LEGACY_OWNER_GOAL_SOURCE


@pytest.mark.parametrize("origin, expected", [
    ("typed", "goal-owner"), ("gui", "goal-owner"),
    ("rpc", "goal"), ("", "goal"), ("unknown", "goal"),
])
def test_goal_emission_is_owner_neutral(origin, expected):
    assert seat_authority.goal_source(SimpleNamespace(started_by=origin)) == expected
    assert LEGACY_OWNER_GOAL_SOURCE not in seat_authority.ATTENDED_SOURCES
    assert LEGACY_OWNER_GOAL_SOURCE not in seat_authority.NARROWING_SOURCES


@pytest.mark.parametrize("source", [LEGACY_OWNER_GOAL_SOURCE, "goal-owner", "goal", "unlabelled"])
def test_codex_reads_legacy_origin_without_new_submission_alias(source):
    item = {"source": source, "content": "retained input"}
    entry = {"id": "kept", "state": "uncertain", "item": dict(item)}
    saves = []
    ledger = SteeringLedger([entry], lambda: saves.append(True))
    assert entry["item"]["source"] == ("goal-owner" if source == LEGACY_OWNER_GOAL_SOURCE else source)
    assert entry["state"] == "uncertain" and not saves
    new = ledger.enqueue(item, "thread", "turn")
    assert new["item"]["source"] == source  # normalization only on existing ledger reads


@pytest.mark.parametrize("source", [LEGACY_OWNER_GOAL_SOURCE, "goal-owner", "goal", "unlabelled"])
def test_claude_reads_legacy_origin_without_rewriting_delivery_state(tmp_path, source):
    # Independent old on-disk spelling: do not derive the compatibility fixture
    # from the production helper being tested.
    if source == LEGACY_OWNER_GOAL_SOURCE:
        source = bytes([103, 111, 97, 108, 45, 114, 121, 97, 110]).decode("ascii")
        assert source == LEGACY_OWNER_GOAL_SOURCE
        codex_entry = {"id": "legacy", "state": "uncertain", "item": {"source": source}}
        SteeringLedger([codex_entry], lambda: None)
        assert codex_entry["item"]["source"] == "goal-owner"
    ledger = ClaudeLedger(tmp_path)
    segment = ledger.select_segment(str(tmp_path))
    entry = ledger.prepare(segment["id"], "retained input", "autonomous", source)
    assert entry["source"] == source
    original = ledger.file.read_bytes()
    restored = ClaudeLedger(tmp_path)
    [pending] = restored.pending(segment["id"])
    assert pending["source"] == ("goal-owner" if source == LEGACY_OWNER_GOAL_SOURCE else source)
    assert pending["state"] == entry["state"]
    assert pending["id"] == entry["id"]
    assert restored.file.read_bytes() == original
