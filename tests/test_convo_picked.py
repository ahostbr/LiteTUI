"""`_on_convo_picked` — the conversation picker's callback.

WHY THIS FILE EXISTS, MEASURED RATHER THAN INFERRED. Gutted to a bare `return`
at `9090aae` the FULL suite still passed: 1,141 tests, 97 pytest files + 13
scripts, REAL EXIT 0. Picking a conversation could do nothing at all and the
suite would not notice. The member is reached only as a `push_screen` callback
(`plugins/convo.py:111`) and never called by name, so nothing covered it by
accident -- but the ZERO test files naming it is NOT what establishes that; the
mutation is. A symbol count is not a coverage measurement.

Two arms of the guard, and they are not the same arm:
  falsy path  -> silent no-op (the user pressed Esc)
  same path   -> ANNOUNCES "Already in that conversation." and does not resume
A test that only checked "did not resume" would pass on a dead method for both.
"""
from pathlib import Path
from types import SimpleNamespace

from litetui.app import LiteTUI

picked = LiteTUI._on_convo_picked


class ConvoDouble(SimpleNamespace):
    def __init__(self, current="/convos/current.jsonl"):
        super().__init__(convo_path=Path(current), resumed=[], said=[])

    def _system(self, message):
        self.said.append(message)

    def _resume(self, path):
        self.resumed.append(path)


def test_picking_another_conversation_resumes_it():
    d = ConvoDouble()
    picked(d, "/convos/other.jsonl")
    assert d.resumed == [Path("/convos/other.jsonl")]
    assert d.said == []


def test_it_resumes_with_a_path_not_the_raw_string():
    """`_resume` is handed a Path. Passing the string through would still look
    right in most assertions and break path comparison downstream."""
    d = ConvoDouble()
    picked(d, "/convos/other.jsonl")
    assert isinstance(d.resumed[0], Path)


def test_escaping_the_picker_does_nothing_at_all():
    d = ConvoDouble()
    picked(d, None)
    assert d.resumed == [] and d.said == []


def test_an_empty_choice_does_nothing_at_all():
    d = ConvoDouble()
    picked(d, "")
    assert d.resumed == [] and d.said == []


def test_repicking_the_current_conversation_says_so_and_does_not_resume():
    """The distinguishing arm: this one is NOT silent. A dead method satisfies
    'did not resume' -- only the announcement separates it from doing nothing."""
    d = ConvoDouble("/convos/current.jsonl")
    picked(d, "/convos/current.jsonl")
    assert d.resumed == []
    assert d.said == ["Already in that conversation."]
