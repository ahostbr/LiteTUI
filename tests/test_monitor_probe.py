"""The probe contract and the ProbeResult invariants.

The real ChromeProbe is not exercised here (it needs a live extension); instead
this pins the contract the engine relies on: FakeProbe's determinism, and the
two derived invariants on ProbeResult — ok/status agree, and a failed visit can
never claim to have met its expectation.
"""

from __future__ import annotations

from litetui.monitor.probe import FakeProbe, ProbeResult
from litetui.monitor.targets import Target


def test_probe_result_ok_derived_from_status():
    ok = ProbeResult(url="u", ok=False, status="ok")
    assert ok.ok is True
    err = ProbeResult(url="u", ok=True, status="error", error="x")
    assert err.ok is False


def test_failed_visit_cannot_claim_expect_met():
    r = ProbeResult(url="u", ok=True, status="error", error="x", expect_met=True)
    # A failed visit never read the page, so expect_met is forced False.
    assert r.expect_met is False


def test_fake_probe_is_deterministic_and_records_calls():
    probe = FakeProbe(fail=frozenset({"down"}))
    good = Target(name="up", url="https://up.test", expect_text="absent-zzz")
    bad = Target(name="down", url="https://down.test")

    r_up = probe.probe(good)
    r_down = probe.probe(bad)

    assert r_up.ok is True
    assert r_up.expect_met is False  # "absent-zzz" not in the fake text
    assert r_down.ok is False
    assert r_down.status == "error"
    assert probe.calls == ["up", "down"]


def test_fake_probe_expect_met_when_substring_present():
    probe = FakeProbe(text="hello Example Domain world")
    t = Target(name="x", url="https://x.test", expect_text="Example Domain")
    assert probe.probe(t).expect_met is True


def test_fake_probe_missing_expect_is_not_a_crash():
    probe = FakeProbe()
    t = Target(name="x", url="https://x.test", expect_text="NOT-ON-PAGE")
    r = probe.probe(t)
    assert r.ok is True          # the page still loaded
    assert r.expect_met is False  # but the expectation was not met
