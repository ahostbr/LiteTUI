"""Tests for the WS7 handler-reload eligibility + lifecycle-seam contract.

Fake-only: pure data + policy, no module import/reload, no activate(), no
resource teardown. This is the safe precursor to the later handler-generation
swap slice; these tests pin the default-to-restart-required policy and the
four-seam completeness gate.
"""
from litetui.plugin_reload_eligibility import (
    LifecycleSeams,
    ReloadEligibility,
    assess_reload_eligibility,
)

_SEAMS = LifecycleSeams(prepare=lambda: None, validate=lambda: None,
                        activate=lambda: None, deactivate=lambda: None)


def _fn(tag: str):
    def _x():
        return tag
    _x.__name__ = f"seam_{tag}"
    return _x


def test_reviewed_stateless_seam_complete_is_eligible():
    r = assess_reload_eligibility("p", reviewed=True, stateless=True, owns=(), seams=_SEAMS)
    assert r.eligible
    assert r.reasons == ()


def test_unreviewed_defaults_to_restart_required():
    r = assess_reload_eligibility("p", reviewed=False, seams=_SEAMS)
    assert not r.eligible
    assert any("not in reviewed" in x for x in r.reasons)


def test_stateful_disqualifies_even_if_reviewed():
    r = assess_reload_eligibility("p", reviewed=True, stateless=False, seams=_SEAMS)
    assert not r.eligible
    assert any("stateful" in x for x in r.reasons)


def test_owned_resources_disqualify():
    r = assess_reload_eligibility("p", reviewed=True, owns=("timers", "mcp"), seams=_SEAMS)
    assert not r.eligible
    assert any("owns side-effect resources" in x for x in r.reasons)
    assert r.owns == ("timers", "mcp")


def test_incomplete_seams_disqualify_and_name_the_gap():
    seams = LifecycleSeams(prepare=_fn("prepare"), validate=_fn("validate"),
                           activate=_fn("activate"))  # deactivate missing
    assert not seams.complete
    assert seams.missing() == ("deactivate",)
    r = assess_reload_eligibility("p", reviewed=True, seams=seams)
    assert not r.eligible
    assert any("lifecycle seams incomplete" in x and "deactivate" in x for x in r.reasons)


def test_multiple_failures_are_all_reported():
    r = assess_reload_eligibility("p", reviewed=False, stateless=False, owns=("processes",))
    # no seams supplied -> also incomplete
    assert not r.eligible
    joined = " | ".join(r.reasons)
    assert "not in reviewed" in joined
    assert "stateful" in joined
    assert "owns side-effect resources" in joined
    assert "lifecycle seams incomplete" in joined


def test_lifecycle_seams_complete_true_only_when_all_four_present():
    assert LifecycleSeams().complete is False
    assert LifecycleSeams().missing() == ("prepare", "validate", "activate", "deactivate")
    assert _SEAMS.complete is True
    assert _SEAMS.missing() == ()


def test_result_carries_owner_owns_and_seams():
    r = assess_reload_eligibility("own-plugin", reviewed=True, owns=("children",), seams=_SEAMS)
    assert isinstance(r, ReloadEligibility)
    assert r.owner == "own-plugin"
    assert r.owns == ("children",)
    assert r.seams is _SEAMS
    assert not r.eligible  # owns children -> disqualifies
