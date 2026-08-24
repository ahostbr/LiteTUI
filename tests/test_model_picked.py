"""`on_model_picked` — the model picker's callback, and the guard that keeps a
resident model from being reloaded for nothing.

WHY THIS FILE EXISTS. The member is reached ONLY as a `push_screen` callback
reference (`plugins/model_switch.py`, the `app.on_model_picked` argument) and is
never called by name anywhere in the tree, so `git grep -l on_model_picked --
tests/` returns ZERO.

CORRECTED AFTER THE FACT, AND THE CORRECTION IS THE POINT. This docstring first
concluded from that ZERO that "nothing accidentally covered it". THAT WAS WRONG.
`tests/test_modals.py` drives the picker WIRING and never names the symbol; run
against the full suite with this file deselected, it already covers three of the
four arms:

    gut the body                     -> 2 RED in test_modals.py   already covered
    remove the whole guard           -> 1 RED in test_modals.py   already covered
    drop only the same-model clause  -> 1141 passed, GREEN        UNWATCHED

ZERO MENTIONS IS NOT ZERO COVERAGE. The original mutations were run against this
file alone, which measures THIS FILE'S sensitivity, not the codebase's coverage.

So what this file adds is one genuinely unwatched arm --
`test_repicking_the_current_model_does_not_reload_it` is the only test in the
tree that watches it -- plus explicit, named coverage beside implicit wiring
coverage, which a rename can silently detach. Not a closed hole.
(Not measured: whether the empty-string case is separately covered; `not
model_id` folds None and "" together and only the same-model clause was isolated.)

The guard is not cosmetic. `_apply_context_length` reloads the model on the
llama.cpp backend -- evicting resident weights -- so re-picking the model you
are already on must do nothing at all.
"""
from types import SimpleNamespace

from litetui.app import LiteTUI

picked = LiteTUI.on_model_picked


class PickDouble(SimpleNamespace):
    """Records the four effects a real switch is supposed to drive."""

    def __init__(self, model_id="alpha"):
        super().__init__(model_id=model_id, headers=0, fetches=0, applied=0, said=[])

    def _update_header(self):
        self.headers += 1

    def _fetch_ctx_window(self):
        self.fetches += 1

    def _system(self, message):
        self.said.append(message)

    def _apply_context_length(self):
        self.applied += 1


def test_picking_a_new_model_switches_and_drives_all_four_effects():
    d = PickDouble("alpha")
    picked(d, "beta")
    assert d.model_id == "beta"
    assert (d.headers, d.fetches, d.applied) == (1, 1, 1)
    assert d.said == ["Switched to: beta"]


def test_the_announcement_names_the_new_model_not_the_old():
    """The message reads `self.model_id` AFTER the assignment. Hoisting the
    announcement above the assignment would still print a plausible line --
    naming the model you just left."""
    d = PickDouble("alpha")
    picked(d, "beta")
    assert "beta" in d.said[0] and "alpha" not in d.said[0]


def test_escaping_the_picker_changes_nothing():
    """PickerScreen dismisses with None when the user presses Esc."""
    d = PickDouble("alpha")
    picked(d, None)
    assert d.model_id == "alpha"
    assert (d.headers, d.fetches, d.applied, d.said) == (0, 0, 0, [])


def test_an_empty_choice_changes_nothing():
    d = PickDouble("alpha")
    picked(d, "")
    assert d.model_id == "alpha"
    assert (d.headers, d.fetches, d.applied, d.said) == (0, 0, 0, [])


def test_repicking_the_current_model_does_not_reload_it():
    """The whole point of the second guard arm: on llama.cpp a re-apply evicts
    the resident weights, so picking what you already have must be free."""
    d = PickDouble("alpha")
    picked(d, "alpha")
    assert d.applied == 0
    assert (d.headers, d.fetches, d.said) == (0, 0, [])
