"""T642 — a headless child uses what is LOADED instead of refusing.

🔴 THE INCIDENT. A fresh Frontier Chat thread on LiteTUI (Auto) refused its first
prompt: the persisted `default_model` qwen/qwen3.8-27b is cold, while
qwen3.5-4b-claude-4.6-opus-reasoning-distilled AND minicpm5-2b were resident
(artifacts/acceptance-2026-09-11/T631-auto-blocked.png). Ryan's standing rule is
"when a model is already loaded, USE THAT ONE" — refusing with two models in VRAM
gets the rule exactly backwards.

⚠️ THE OLD BEHAVIOUR WAS DELIBERATE AND IS BEING OVERRULED, NOT CORRECTED. T594
refused whenever several were resident and none was the one asked for, reasoning
that "substituting would be picking one on the user's behalf … guessing is how a
consult panel silently reports the wrong model's opinion". That is a real hazard;
the answer is a STATED tie-break plus a note that says what happened, not a
refusal. Refuse now means one thing only: NOTHING is resident.

⬜ NO SIZE ANYWHERE. "Prefer the largest" is not measurable from what the app
holds — `ModelRow` has no size, LM Studio rows have no path, and the native
listing reports context lengths, not bytes (and a context length is not a size:
minicpm5-2b advertises 131072). Sentinel's ruling (d1e7d2cd) is therefore an
explicit ordering rather than a measurement, and these arms pin it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui.model_residency import substitute_main_model

# The incident's own names, so a reader can match these arms to the screenshot.
COLD_DEFAULT = "qwen/qwen3.8-27b"
BIG = "qwen3.5-4b-claude-4.6-opus-reasoning-distilled"
SUBAGENT = "minicpm5-2b"


def pick(resident, *, want=COLD_DEFAULT, subagent=None, summary=None, default=COLD_DEFAULT):
    return substitute_main_model(
        set(resident),
        want=want,
        subagent_model=subagent,
        tool_summary_model=summary,
        default_model=default,
    )


def test_the_incident_substitutes_the_main_shaped_resident():
    """🔴 The reported case: two resident, one of them the subagent's."""
    assert pick([BIG, SUBAGENT], subagent=SUBAGENT) == BIG


def test_nothing_resident_is_the_ONLY_refusal():
    """Refuse now means exactly one thing."""
    assert pick([]) is None


def test_the_only_resident_being_the_subagent_model_is_still_substituted():
    """
    🔴 SENTINEL'S EXPLICIT RULING (0ccd6180): "if the only resident is the
    subagent model, substitute it too; refuse means nothing at all is loaded."
    A small model beats no answer.
    """
    assert pick([SUBAGENT], subagent=SUBAGENT) == SUBAGENT


def test_the_tool_summary_model_is_excluded_on_the_same_footing():
    """T640 gave the fold its own setting; it is a helper, not a seat."""
    assert pick([BIG, "summ-1b"], summary="summ-1b") == BIG


def test_both_helpers_excluded_leaves_the_main_one():
    assert pick([BIG, SUBAGENT, "summ-1b"], subagent=SUBAGENT, summary="summ-1b") == BIG


# ── the tie-break, in Sentinel's order ─────────────────────────────────────


def test_family_prefix_breaks_a_tie_before_alphabetical_order():
    """
    Step 2: the resident whose family matches `default_model`'s. Without it the
    fallback below would pick "alpha-7b" here purely because 'a' sorts first —
    a model from a different family than the one the user configured.
    """
    assert pick(["alpha-7b", "qwen3.5-4b"], default="qwen/qwen3.8-27b") == "qwen3.5-4b"


def test_family_is_read_from_the_LAST_path_segment():
    """`qwen/qwen3.8-27b` is family "qwen", not "qwen/qwen3"."""
    assert pick(["mistral-7b", "qwen-next"], default="qwen/qwen3.8-27b") == "qwen-next"


def test_first_by_name_when_no_family_matches():
    """Step 3, and it is deliberately boring: deterministic, not clever."""
    assert pick(["zeta-9b", "alpha-7b"], default="qwen/qwen3.8-27b") == "alpha-7b"


def test_the_pick_is_STABLE_for_the_same_resident_set():
    """
    A set has no order. Two runs against the same residency must choose the same
    model, or a restarted child silently answers as somebody else.
    """
    first = pick(["zeta-9b", "alpha-7b", "mid-8b"])
    for _ in range(5):
        assert pick(["mid-8b", "zeta-9b", "alpha-7b"]) == first


# ── the controls ───────────────────────────────────────────────────────────


def test_CONTROL_a_resident_selection_is_never_substituted():
    """
    The caller asks only when `want` is cold, but the guard is cheap and the
    failure it prevents — silently moving a turn off the model the user picked —
    is the worst one available here.
    """
    assert pick([BIG, SUBAGENT], want=BIG, subagent=SUBAGENT) == BIG


def test_CONTROL_helpers_are_excluded_by_VALUE_not_by_being_small():
    """
    Nothing here inspects a name for smallness. With NO helper configured and no
    family to prefer, the same two residents give the ALPHABETICAL answer — the
    small one — which is the proof that the exclusion, not a size heuristic, is
    what puts the big model forward in the incident arm above.

    ⚠️ MY FIRST VERSION OF THIS ARM WAS WRONG AND THE CODE WAS RIGHT. It used the
    incident's own `default_model` (qwen/...), so the FAMILY step fired and chose
    the qwen model for a reason this arm was not testing. `default=None` isolates
    the thing being claimed.
    """
    assert pick([BIG, SUBAGENT], default=None) == SUBAGENT  # "minicpm5-2b" < "qwen3.5-4b-..."


def test_blank_and_None_helper_settings_exclude_nothing():
    for helper in (None, "", "   "):
        assert pick([SUBAGENT], subagent=helper) == SUBAGENT
