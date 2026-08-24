"""`on_model_picked` — the model picker's callback, and the guard that keeps a
resident model from being reloaded for nothing.

WHY THIS FILE EXISTS. The member is reached ONLY as a `push_screen` callback
reference (`plugins/model_switch.py`, the `app.on_model_picked` argument) and
is never called by name anywhere in the tree. Nothing calls it, so nothing
accidentally covered it: when this file was written,
`git grep -l on_model_picked -- tests/` returned ZERO, on a branch where the
rename had already shipped. A green suite was never evidence for this member.

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
