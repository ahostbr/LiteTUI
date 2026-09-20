"""Visual regression: fingerprinting and the baseline state machine.

The state machine is the interesting part: first capture sets a baseline, a
matching capture is stable, a different one is changed. The persistence test
proves the baseline survives a new tracker (a new process/run), which is what
makes "changed" meaningful across scheduled sweeps rather than within one.
"""

from __future__ import annotations

import json

from litetui.monitor.regression import (
    RegressionTracker,
    fingerprint_bytes,
    fingerprint_path,
)


def test_fingerprint_is_stable_and_distinct():
    a = fingerprint_bytes(b"hello")
    b = fingerprint_bytes(b"hello")
    c = fingerprint_bytes(b"world")
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex


def test_fingerprint_missing_path_is_none(tmp_path):
    assert fingerprint_path(tmp_path / "nope.png") is None


def test_fingerprint_reads_file(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"pixels")
    assert fingerprint_path(p) == fingerprint_bytes(b"pixels")


def test_baseline_then_stable_then_changed():
    t = RegressionTracker()
    assert t.observe("x", "h1").verdict == "baseline"
    assert t.observe("x", "h1").verdict == "stable"
    assert t.observe("x", "h2").verdict == "changed"


def test_none_verdict_when_no_screenshot():
    t = RegressionTracker()
    assert t.observe("x", None).verdict == "none"
    assert t.observe("x", None).as_metric() == 0.0


def test_changed_is_the_only_nonzero_metric_code():
    t = RegressionTracker()
    t.observe("x", "h1")          # baseline
    assert t.observe("x", "h1").as_metric() == 0.0   # stable
    assert t.observe("x", "h2").as_metric() == 1.0   # changed


def test_baseline_persists_across_trackers(tmp_path):
    state = tmp_path / "state.json"
    t1 = RegressionTracker(state)
    t1.observe("x", "h1")
    # A brand-new tracker (new run) loads the stored baseline.
    t2 = RegressionTracker(state)
    assert t2.observe("x", "h1").verdict == "stable"
    assert t2.observe("x", "h9").verdict == "changed"
    # And the file really holds the baseline, not a transient.
    assert json.loads(state.read_text())["x"] == "h1"


def test_targets_are_tracked_independently():
    t = RegressionTracker()
    t.observe("a", "h1")
    t.observe("b", "h1")
    # Changing a does not affect b's baseline.
    assert t.observe("b", "h1").verdict == "stable"
    assert t.observe("a", "h2").verdict == "changed"
