"""The profile dropdown is DERIVED, so the UI and the engine cannot drift.

Sentinel's ruling, 2026-08-24: "make the collision impossible, not managed."

THE BUG THIS CLOSES, which existed independently of any new profile: the set of
profiles was written out three times — `PROFILES`, a `PROFILE_NAMES` tuple, and
a hardcoded `TOOL_PROFILE_CHOICES` list in settings_screen.py. Drift in one
direction was loud (a choice naming a profile that does not exist fails the
validator at save). Drift in the OTHER direction was silent: a profile added to
`PROFILES` alone EXISTS, is refused by that same validator, and can be selected
by nobody. Nothing went red.

🔴 THE TEST THAT MATTERS IS `test_a_new_profile_appears_without_touching_the_screen`.
Without it this file only proves the current two profiles render — which a
hardcoded list also does. Moving hardcoded data into a comprehension is not the
fix; being unable to add a profile the UI cannot see is the fix.
"""
from dataclasses import replace

import pytest

from litetui import tool_policy
from litetui.settings_screen import tool_profile_choices
from litetui.tool_policy import (
    AUTONOMOUS,
    INTERACTIVE,
    PROFILE_NAMES,
    PROFILES,
    SCHEDULED,
)


def test_the_original_labels_are_still_word_for_word():
    """The surviving half of the derivation's regression guard.

    It originally pinned the whole list, order included, so the derivation
    commit could prove it changed nothing visible. Adding AUTONOMOUS changed
    the list DELIBERATELY (a third entry, and a reorder to ascending
    authority), so the order assertion moved to its own test below. What must
    NOT drift is the wording of the two labels that already existed — a
    refactor is allowed to add an option, not to silently reword the others.
    """
    labels = dict((v, k) for k, v in tool_profile_choices())
    assert labels[INTERACTIVE] == "interactive — inspect freely, confirm sensitive actions"
    assert labels[SCHEDULED] == "scheduled — read-only tools only"


def test_the_dropdown_is_ordered_by_ascending_authority():
    """The order is a human-facing decision, not insertion order.

    scheduled (read-only) → interactive (asks) → autonomous (everything). The
    list reads as a scale of trust and the widest option sits visibly at the
    end. Insertion order would have put autonomous beside interactive, which
    hides that it is the extreme.
    """
    assert [v for _l, v in tool_profile_choices()] == [
        SCHEDULED, INTERACTIVE, AUTONOMOUS
    ]


def test_no_label_renders_a_double_separator():
    """The label is "<name> — <summary>", so an em dash inside a summary
    produces two clauses bolted together. Caught on autonomous's first draft."""
    for label, _value in tool_profile_choices():
        assert label.count("—") <= 1, label


def test_a_new_profile_appears_without_touching_the_screen(monkeypatch):
    """THE CLASS-KILLER.

    A profile is added to `PROFILES` and to nothing else. It must be offered.
    This is the drift that used to be silent, and it is why the choices are a
    function rather than a module constant computed once at import.
    """
    extra = replace(PROFILES[SCHEDULED], name="probeprofile", summary="a test profile")
    patched = dict(PROFILES)
    patched["probeprofile"] = extra
    monkeypatch.setattr(tool_policy, "PROFILES", patched)
    monkeypatch.setattr(tool_policy, "PROFILE_NAMES", tuple(patched))

    labels = dict((v, k) for k, v in tool_profile_choices())
    assert "probeprofile" in labels, "a profile the engine knows was not offered"
    assert labels["probeprofile"] == "probeprofile — a test profile"


def test_every_offered_value_is_a_real_profile():
    """The other direction, which was already loud — kept so BOTH are covered
    by a test rather than one by a test and one by a crash."""
    for _label, value in tool_profile_choices():
        assert value in PROFILES, f"{value} is offered but has no ToolProfile"
        assert value in PROFILE_NAMES, f"{value} is offered but fails the validator"


def test_no_profile_is_left_out_of_the_dropdown():
    offered = {value for _label, value in tool_profile_choices()}
    assert offered == set(PROFILES), "a profile exists that nobody can select"


def test_profile_names_is_derived_from_profiles_not_hand_written():
    """Both the membership AND the order, because the order is the dropdown."""
    assert PROFILE_NAMES == tuple(PROFILES)


def test_every_profile_explains_itself():
    """A dropdown option with no explanation is a control the user has to guess
    at. The summary lives on the ToolProfile, so this cannot be satisfied by
    editing the screen."""
    for name in PROFILE_NAMES:
        assert PROFILES[name].summary, f"{name} has no summary"


def test_a_profile_with_no_summary_still_renders_as_something(monkeypatch):
    """Degradation, not a crash or a bare em dash. A profile someone adds in a
    hurry must still be selectable."""
    bare = replace(PROFILES[SCHEDULED], name="bare", summary="")
    patched = dict(PROFILES)
    patched["bare"] = bare
    monkeypatch.setattr(tool_policy, "PROFILES", patched)
    monkeypatch.setattr(tool_policy, "PROFILE_NAMES", tuple(patched))

    labels = dict((v, k) for k, v in tool_profile_choices())
    assert labels["bare"] == "bare", labels["bare"]
    assert "—" not in labels["bare"], "rendered a dangling separator"
