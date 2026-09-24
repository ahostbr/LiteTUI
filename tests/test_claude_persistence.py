"""Contract tests for the Claude delivery ledger (claude-persistence).

Every test here is a restart test or a refusal test, because those are the only
two things the ledger exists for: an input accepted while busy must still be
there after a crash, and an input that MIGHT already have executed must be
impossible to send again by accident.
"""

import json
import os

import pytest

from litetui.claude_persistence import (
    ACKNOWLEDGED,
    LEDGER_NAME,
    PREPARED,
    SUBMITTED,
    TERMINAL,
    UNCERTAIN,
    ClaudeLedger,
    LedgerError,
)

WORKSPACE = "C:/Projects/LiteTUI"


@pytest.fixture
def convo(tmp_path):
    directory = tmp_path / "convo"
    directory.mkdir()
    return directory


def prepared(ledger, segment_id, content="hello", operation_id=None):
    return ledger.prepare(
        segment_id,
        content,
        profile="developer",
        source="input",
        operation_id=operation_id,
    )


def test_missing_ledger_is_fresh_state_not_an_error(convo):
    ledger = ClaudeLedger(convo)
    assert ledger.selected is None
    assert not (convo / LEDGER_NAME).exists()


def test_a_file_instead_of_the_conversation_directory_is_refused(tmp_path):
    stray = tmp_path / "convo.jsonl"
    stray.write_text("{}", encoding="utf-8")
    with pytest.raises(LedgerError) as caught:
        ClaudeLedger(stray)
    assert str(stray) in str(caught.value)


def test_segment_and_its_entries_survive_a_restart_on_the_same_path(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    ledger.bind_session(segment["id"], "native-1")
    entry = prepared(ledger, segment["id"], "queued while busy")

    restarted = ClaudeLedger(convo)
    assert restarted.selected is not None
    assert restarted.selected["id"] == segment["id"]
    assert restarted.selected["session_id"] == "native-1"
    assert restarted.selected["workspace"] == WORKSPACE
    held = restarted.pending(segment["id"])
    assert [e["id"] for e in held] == [entry["id"]]
    assert held[0]["state"] == PREPARED
    assert held[0]["content"] == "queued while busy"
    assert held[0]["profile"] == "developer"
    assert held[0]["source"] == "input"


def test_selection_resumes_the_same_segment_and_new_starts_another(convo):
    ledger = ClaudeLedger(convo)
    first = ledger.select_segment(WORKSPACE)
    assert ledger.select_segment(WORKSPACE)["id"] == first["id"]

    second = ledger.select_segment(WORKSPACE, new=True)
    assert second["id"] != first["id"]
    assert ClaudeLedger(convo).selected["id"] == second["id"]
    # The old segment is kept, not replaced: its records are still readable.
    assert ClaudeLedger(convo).segment(first["id"])["id"] == first["id"]


def test_a_different_workspace_does_not_resume_the_selected_segment(convo):
    ledger = ClaudeLedger(convo)
    first = ledger.select_segment(WORKSPACE)
    elsewhere = ledger.select_segment("C:/Projects/LiteImage")
    assert elsewhere["id"] != first["id"]
    assert elsewhere["workspace"] == "C:/Projects/LiteImage"


@pytest.mark.parametrize("reached", [SUBMITTED, ACKNOWLEDGED])
def test_an_in_flight_delivery_becomes_uncertain_on_restart(convo, reached):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED)
    if reached == ACKNOWLEDGED:
        ledger.update_delivery(entry["id"], ACKNOWLEDGED)

    restored = ClaudeLedger(convo).pending(segment["id"])[0]
    assert restored["state"] == UNCERTAIN
    assert restored["uncertain_from"] == reached


def test_an_uncertain_delivery_cannot_be_submitted_again(convo):
    """The enforceable half of "never replay automatically"."""
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED)

    restarted = ClaudeLedger(convo)
    with pytest.raises(LedgerError) as caught:
        restarted.update_delivery(entry["id"], SUBMITTED)
    assert UNCERTAIN in str(caught.value)
    # ...and it did not quietly revert to a sendable state either.
    assert restarted.pending(segment["id"])[0]["state"] == UNCERTAIN
    # Resolving it explicitly is the only way out.
    assert restarted.update_delivery(entry["id"], TERMINAL)["state"] == TERMINAL


def test_reading_a_ledger_never_writes_one(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED)
    before = (convo / LEDGER_NAME).read_text(encoding="utf-8")

    reopened = ClaudeLedger(convo)
    assert reopened.pending(segment["id"])[0]["state"] == UNCERTAIN
    assert (convo / LEDGER_NAME).read_text(encoding="utf-8") == before
    assert json.loads(before)["segments"][segment["id"]]["entries"][0]["state"] == (
        SUBMITTED
    )


def test_uncertainty_is_re_derived_on_every_restart(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED)
    ledger.update_delivery(entry["id"], ACKNOWLEDGED)

    for _ in range(3):
        again = ClaudeLedger(convo)
        assert again.pending(segment["id"])[0]["state"] == UNCERTAIN
        assert again.pending(segment["id"])[0]["uncertain_from"] == ACKNOWLEDGED


def test_pending_is_isolated_per_segment(convo):
    ledger = ClaudeLedger(convo)
    first = ledger.select_segment(WORKSPACE)
    held = prepared(ledger, first["id"], "held for the first segment")
    second = ledger.select_segment(WORKSPACE, new=True)
    fresh = prepared(ledger, second["id"], "typed after the switch")

    for source in (ledger, ClaudeLedger(convo)):
        assert [e["id"] for e in source.pending(first["id"])] == [held["id"]]
        assert [e["id"] for e in source.pending(second["id"])] == [fresh["id"]]


def test_an_operation_id_cannot_be_prepared_under_a_second_segment(convo):
    ledger = ClaudeLedger(convo)
    first = ledger.select_segment(WORKSPACE)
    prepared(ledger, first["id"], operation_id="op-7")
    second = ledger.select_segment(WORKSPACE, new=True)

    with pytest.raises(LedgerError) as caught:
        prepared(ledger, second["id"], operation_id="op-7")
    assert first["id"] in str(caught.value)
    assert ledger.pending(second["id"]) == []


def test_preparing_the_same_operation_id_twice_queues_it_once(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    first = prepared(ledger, segment["id"], operation_id="op-1")
    again = prepared(ledger, segment["id"], operation_id="op-1")
    assert again["id"] == first["id"]
    assert len(ledger.pending(segment["id"])) == 1


def test_terminal_entries_leave_pending_and_terminal_is_final(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED)
    ledger.update_delivery(entry["id"], TERMINAL, outcome="completed")

    assert ledger.pending(segment["id"]) == []
    assert ClaudeLedger(convo).pending(segment["id"]) == []
    assert ClaudeLedger(convo).segment(segment["id"])["entries"][0]["state"] == TERMINAL
    with pytest.raises(LedgerError):
        ledger.update_delivery(entry["id"], SUBMITTED)


@pytest.mark.parametrize(
    "chain",
    [
        (ACKNOWLEDGED,),          # prepared -> acknowledged skips the send
        (UNCERTAIN,),             # an input that never left is not ambiguous
        (PREPARED,),              # nothing goes backwards
        (SUBMITTED, PREPARED),
        (SUBMITTED, SUBMITTED),
        (SUBMITTED, "finished"),  # not a state this ledger knows
    ],
)
def test_invalid_transitions_are_rejected(convo, chain):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    *valid, bad = chain
    for state in valid:
        ledger.update_delivery(entry["id"], state)
    with pytest.raises(LedgerError) as caught:
        ledger.update_delivery(entry["id"], bad)
    assert entry["id"] in str(caught.value)
    expected = valid[-1] if valid else PREPARED
    assert ledger.pending(segment["id"])[0]["state"] == expected


def test_delivery_metadata_cannot_move_an_entry_to_another_segment(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    with pytest.raises(LedgerError):
        ledger.update_delivery(entry["id"], SUBMITTED, segment_id="somewhere-else")
    # `id` and `state` need no guard: they are named parameters, so the
    # signature itself refuses them and `**metadata` never sees them.
    with pytest.raises(TypeError):
        ledger.update_delivery(entry["id"], SUBMITTED, state=TERMINAL)
    stored = ledger.pending(segment["id"])[0]
    assert stored["state"] == PREPARED
    assert stored["segment_id"] == segment["id"]


def test_metadata_is_persisted_with_the_delivery(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    ledger.update_delivery(entry["id"], SUBMITTED, native_turn="t-1", model="opus")

    stored = ClaudeLedger(convo).pending(segment["id"])[0]
    assert stored["native_turn"] == "t-1"
    assert stored["model"] == "opus"
    assert "submitted_at" in stored


def test_unknown_delivery_and_unknown_segment_are_named_in_the_error(convo):
    ledger = ClaudeLedger(convo)
    with pytest.raises(LedgerError) as caught:
        ledger.update_delivery("no-such-entry", SUBMITTED)
    assert "no-such-entry" in str(caught.value)
    with pytest.raises(LedgerError) as caught:
        ledger.pending("no-such-segment")
    assert "no-such-segment" in str(caught.value)
    assert ledger.segment("no-such-segment") is None


def test_binding_the_same_session_twice_is_fine_and_a_second_one_is_refused(convo):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    ledger.bind_session(segment["id"], "native-1")
    assert ledger.bind_session(segment["id"], "native-1")["session_id"] == "native-1"

    with pytest.raises(LedgerError) as caught:
        ledger.bind_session(segment["id"], "native-2")
    assert "native-1" in str(caught.value)
    assert ClaudeLedger(convo).selected["session_id"] == "native-1"


def test_returned_records_are_copies(convo):
    """Every read hands out a copy, checked against the SAME live ledger.

    Re-opening the path would hide this: a caller mutating live ledger state
    corrupts what THIS instance answers next, whether or not it reaches disk.
    """
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    entry = prepared(ledger, segment["id"])
    segment_id = segment["id"]

    segment["session_id"] = "forged"
    entry["state"] = TERMINAL
    ledger.pending(segment_id)[0]["content"] = "rewritten"
    ledger.segment(segment_id)["entries"].clear()
    ledger.selected["entries"].clear()

    for source in (ledger, ClaudeLedger(convo)):
        assert source.selected["session_id"] is None
        assert source.segment(segment_id)["entries"][0]["id"] == entry["id"]
        held = source.pending(segment_id)
        assert [e["state"] for e in held] == [PREPARED]
        assert held[0]["content"] == "hello"


def test_a_failed_replace_leaves_the_previous_ledger_intact(convo, monkeypatch):
    ledger = ClaudeLedger(convo)
    segment = ledger.select_segment(WORKSPACE)
    prepared(ledger, segment["id"], "the record that must survive")
    before = (convo / LEDGER_NAME).read_text(encoding="utf-8")

    def refuse(src, dst):
        raise OSError("simulated crash between the temp write and the replace")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        prepared(ledger, segment["id"], "the record that is lost")
    monkeypatch.undo()

    assert (convo / LEDGER_NAME).read_text(encoding="utf-8") == before
    assert list(convo.glob(".claude_ledger-*")) == []
    survivors = ClaudeLedger(convo).pending(segment["id"])
    assert [e["content"] for e in survivors] == ["the record that must survive"]


@pytest.mark.parametrize(
    "written",
    [
        "{not json at all",
        '["a list is not a ledger"]',
        '{"schema_version": 1}',
        '{"schema_version": 999, "selected_segment": null, "segments": {}}',
        '{"schema_version": 1, "selected_segment": null, "segments": {"s": 3}}',
        (
            '{"schema_version": 1, "selected_segment": null, "segments":'
            ' {"s": {"id": "s", "entries": "not a list"}}}'
        ),
    ],
)
def test_a_corrupt_ledger_raises_naming_the_path_and_is_not_discarded(convo, written):
    (convo / LEDGER_NAME).write_text(written, encoding="utf-8")
    with pytest.raises(LedgerError) as caught:
        ClaudeLedger(convo)
    assert LEDGER_NAME in str(caught.value)
    assert (convo / LEDGER_NAME).read_text(encoding="utf-8") == written
