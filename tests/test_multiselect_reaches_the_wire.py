"""T644 — the ask says on the wire whether more than one option may be ticked.

🔴 THE FLAG WAS NOT DROPPED. IT NEVER EXISTED HERE. LiteSuite's card renders
single-select unless `question.multiSelect === true` (session-logic.ts, T634),
and litetui's emitted question had no such key — so EVERY litetui ask rendered
as one-of-N in the Frontier Chat pane. The tester read that as "the toggle
replaces"; the toggle was doing exactly what the payload told it to.

⚠️ AND THE MISMATCH RAN THE OPPOSITE WAY TO EVERYONE'S FIRST GUESS. This tool has
been multi-select since it was written — the schema says "Options are
MULTI-SELECT: any number of them can be correct", `QuestionState.selected` is a
`set[int]`, and the widget ticks freely. So the producer's real behaviour was
MULTI and the consumer's default was SINGLE, and neither side was wrong about
itself. Nothing on the wire carried the disagreement.
    A DEFAULT ON EACH SIDE OF A PROCESS BOUNDARY IS NOT A CONTRACT. Both were
    reasonable; they were never compared, because the field that would have
    compared them was absent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import ask_user_question as auq

SCHEMA = Path(__file__).resolve().parents[1] / "src" / "litetui" / "schemas" / "ask_user_question.json"


def one(**over) -> dict:
    q = {"label": "Pick", "question": "Which?", "options": [{"title": "A"}, {"title": "B"}]}
    q.update(over)
    return {"questions": [q]}


def test_the_emitted_question_carries_multiSelect_true_by_default():
    """🔴 The card. Absent used to mean "no key at all"; it now means MULTI."""
    states = auq._parse_questions(one())
    assert states[0].to_dict()["multiSelect"] is True


def test_an_explicit_false_narrows_it_to_one_of_N():
    states = auq._parse_questions(one(multiSelect=False))
    assert states[0].to_dict()["multiSelect"] is False


def test_an_explicit_true_is_carried_as_true():
    states = auq._parse_questions(one(multiSelect=True))
    assert states[0].to_dict()["multiSelect"] is True


def test_a_junk_value_reads_as_MULTI_rather_than_failing_the_call():
    """Lenient where a local model drifts — the file's own stated rule.

    Only an explicit `false` narrows the widget; a string, a number or a null
    is not a considered "no", and failing the whole tool call over one would
    lose the question entirely.
    """
    for junk in ("false", 0, None, "no"):
        states = auq._parse_questions(one(multiSelect=junk))
        assert states[0].to_dict()["multiSelect"] is True, junk


def test_the_wire_name_is_camelCase_because_the_consumer_is_TypeScript():
    """🔴 A snake_case key here would be dropped SILENTLY.

    LiteSuite rebuilds each question field by field (`parseUserInputQuestions`),
    so a key it does not name never arrives and nothing reports the omission —
    which is precisely how T634's flag went missing one process further on.
    """
    d = auq._parse_questions(one())[0].to_dict()
    assert "multiSelect" in d
    assert "multi_select" not in d


def test_the_TOOL_SCHEMA_exposes_it_so_the_model_can_actually_set_it():
    """🔴 The other half, and the half that makes the rest reachable.

    A parser that reads a field no schema advertises is a parser for input no
    model will ever send. This is the arm that would have caught the original
    state: the emit and the schema have to agree that the field exists.
    """
    spec = json.loads(SCHEMA.read_text(encoding="utf-8"))
    props = spec["function"]["parameters"]["properties"]["questions"]["items"]["properties"]
    assert "multiSelect" in props
    assert props["multiSelect"]["type"] == "boolean"
    # It must say which way absent falls, or a model will guess.
    assert "true" in props["multiSelect"]["description"].lower()


def test_CONTROL_the_other_question_fields_still_travel():
    """Without this, deleting to_dict's body would pass every arm above."""
    d = auq._parse_questions(one())[0].to_dict()
    assert d["label"] == "Pick"
    assert d["question"] == "Which?"
    assert [o["title"] for o in d["options"]] == ["A", "B"]
    assert d["answered"] is False
