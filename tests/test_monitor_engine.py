"""The engine: orchestration over a target list.

Uses the real engine with a fake probe, a real FileSink and a real
RegressionTracker. The probe here deliberately writes a screenshot file so the
visual-regression branch runs for real (not just the "no screenshot" path).
The key behaviours pinned: one bad target never aborts the sweep, metrics are
actually emitted through the sink, and a changed screenshot is surfaced.
"""

from __future__ import annotations

import json
from pathlib import Path

from litetui.monitor.engine import MonitorEngine
from litetui.monitor.metrics import FileSink
from litetui.monitor.probe import ProbeResult
from litetui.monitor.regression import RegressionTracker
from litetui.monitor.targets import Target


class _ScreamingProbe:
    """A probe that crashes on a named target — proves the sweep survives it."""

    def __init__(self, crash_on):
        self.crash_on = frozenset(crash_on)
        self.shots = []

    def probe(self, target: Target) -> ProbeResult:
        if target.name in self.crash_on:
            raise RuntimeError("boom: target exploded")
        # Write a real (tiny) screenshot file so the regression branch runs.
        shot = self.shots_dir / f"{target.name}.png" if hasattr(self, "shots_dir") else None
        pr = ProbeResult(url=target.url, ok=True, status="ok",
                         load_ms=50.0, text_chars=10,
                         expect_met=(target.expect_text is None))
        return pr


def _mkprobe(shots_dir, pixels_by_target):
    shots_dir = shots_dir if hasattr(shots_dir, "mkdir") else Path(shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)

    class P:
        def probe(self, target: Target) -> ProbeResult:
            shot = shots_dir / f"{target.name}.png"
            shot.write_bytes(pixels_by_target.get(target.name, b"base"))
            expect_met = True
            if target.expect_text is not None:
                expect_met = target.expect_text in "the-page-text"
            return ProbeResult(
                url=target.url, ok=True, status="ok", load_ms=50.0,
                text_chars=12, expect_met=expect_met,
                screenshot_path=str(shot), screenshot_bytes=shot.stat().st_size,
            )

    return P()


def test_sweep_runs_every_target_despite_one_error(tmp_path):
    probe = _ScreamingProbe(crash_on={"bad"})
    sink = FileSink(tmp_path / "log.jsonl")
    engine = MonitorEngine(probe=probe, sink=sink)
    targets = [
        Target(name="ok1", url="https://a.test"),
        Target(name="bad", url="https://b.test"),
        Target(name="ok2", url="https://c.test"),
    ]
    results = engine.run_sweep(targets)
    # The crash did not stop the sweep: all three are reported.
    assert len(results) == 3
    by_name = {r.target: r for r in results}
    assert by_name["ok1"].ok is True
    assert by_name["bad"].ok is False
    assert by_name["bad"].status == "error"
    assert "boom" in by_name["bad"].error
    assert by_name["ok2"].ok is True


def test_metrics_are_emitted_through_sink(tmp_path):
    probe = _mkprobe(tmp_path / "shots", {"a": b"px"})
    sink = FileSink(tmp_path / "log.jsonl")
    engine = MonitorEngine(probe=probe, sink=sink)
    targets = [Target(name="a", url="https://a.test", expect_text="the-page-text")]
    results = engine.run_sweep(targets)
    assert results[0].ok is True
    assert results[0].expect_met is True

    records = [json.loads(l) for l in
               (tmp_path / "log.jsonl").read_text().strip().splitlines()]
    assert len(records) == 1
    names = {m["name"] for m in records[0]["metrics"]}
    assert {"browser_monitor_up", "browser_monitor_load_seconds",
            "browser_monitor_text_chars", "browser_monitor_expect_met",
            "browser_monitor_changed"} <= names
    # The rendered prometheus text is present and well-formed.
    prom = records[0]["prometheus"]
    assert "browser_monitor_up{name=" in prom
    assert 'name="a"' in prom


def test_visual_regression_detected(tmp_path):
    shots = tmp_path / "shots"
    shots.mkdir()
    tracker = RegressionTracker(tmp_path / "state.json")
    probe = _mkprobe(shots, {"a": b"first"})
    sink = FileSink(tmp_path / "log.jsonl")
    engine = MonitorEngine(probe=probe, sink=sink, tracker=tracker)
    targets = [Target(name="a", url="https://a.test")]

    r1 = engine.run_sweep(targets)
    assert r1[0].regression == "baseline"

    # Same pixels again: stable.
    r2 = engine.run_sweep(targets)
    assert r2[0].regression == "stable"

    # New pixels: changed.
    probe = _mkprobe(shots, {"a": b"different-pixels"})
    engine = MonitorEngine(probe=probe, sink=sink, tracker=tracker)
    r3 = engine.run_sweep(targets)
    assert r3[0].regression == "changed"
    # The changed verdict is carried into the emitted metric as 1.0.
    records = [json.loads(l) for l in
               (tmp_path / "log.jsonl").read_text().strip().splitlines()]
    changed_metrics = [m for m in records[-1]["metrics"]
                       if m["name"] == "browser_monitor_changed"]
    assert changed_metrics and changed_metrics[0]["value"] == 1.0


def test_no_tracker_means_no_regression(tmp_path):
    probe = _mkprobe(tmp_path / "shots", {"a": b"px"})
    sink = FileSink(tmp_path / "log.jsonl")
    engine = MonitorEngine(probe=probe, sink=sink, tracker=None)
    results = engine.run_sweep([Target(name="a", url="https://a.test")])
    assert results[0].regression == "none"
