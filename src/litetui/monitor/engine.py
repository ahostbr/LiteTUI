"""The engine: one sweep over a target list, probes, measures, records.

The engine owns the orchestration and nothing else. It takes a ``BrowserProbe``
and a ``MetricsSink`` (both protocols, so tests substitute fakes), optionally a
``RegressionTracker``, and walks the target list. For each target it:

  1. probes it into a :class:`ProbeResult`,
  2. turns that into a set of :class:`Metric`,
  3. fingerprints the screenshot and asks the tracker for a verdict,
  4. emits the metrics through the sink,
  5. records a :class:`CheckResult` for the report.

Every target is probed independently and a failure on one never stops the
sweep — monitoring that aborts on its first red check has failed at monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from litetui.monitor.metrics import (
    DEFAULT_JOB,
    Metric,
    MetricsSink,
    render_prometheus,
)
from litetui.monitor.probe import BrowserProbe, ProbeResult
from litetui.monitor.regression import RegressionTracker, fingerprint_path
from litetui.monitor.targets import Target

HELP_UP = "1 when the last visit succeeded, 0 when it errored."
HELP_LOAD = "Page load time in seconds, measured wall-clock around navigation."
HELP_TEXT = "Number of characters of visible text captured."
HELP_EXPECT = "1 when an expected substring was found on the page."
HELP_REGRESSION = "1 when the screenshot differs from the stored baseline."


@dataclass
class CheckResult:
    target: str
    url: str
    ok: bool
    status: str
    load_ms: float
    text_chars: int
    expect_met: bool
    error: str = ""
    screenshot_path: str = ""
    regression: str = "none"
    metrics: list[Metric] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "target": self.target,
            "url": self.url,
            "ok": self.ok,
            "status": self.status,
            "load_ms": round(self.load_ms, 1),
            "text_chars": self.text_chars,
            "expect_met": self.expect_met,
            "error": self.error,
            "screenshot_path": self.screenshot_path,
            "regression": self.regression,
        }


class MonitorEngine:
    def __init__(
        self,
        probe: BrowserProbe,
        sink: MetricsSink,
        tracker: RegressionTracker | None = None,
        job: str = DEFAULT_JOB,
    ) -> None:
        self.probe = probe
        self.sink = sink
        self.tracker = tracker
        self.job = job

    def _metrics(self, target: Target, pr: ProbeResult, regression: float) -> list[Metric]:
        labels = target.as_labels()
        return [
            Metric("browser_monitor_up", 1.0 if pr.ok else 0.0, labels, help=HELP_UP),
            Metric("browser_monitor_load_seconds", pr.load_ms / 1000.0, labels, help=HELP_LOAD),
            Metric("browser_monitor_text_chars", float(pr.text_chars), labels, help=HELP_TEXT),
            Metric("browser_monitor_expect_met", 1.0 if pr.expect_met else 0.0, labels, help=HELP_EXPECT),
            Metric("browser_monitor_changed", regression, labels, help=HELP_REGRESSION),
        ]

    def check_one(self, target: Target) -> CheckResult:
        pr: ProbeResult = self.probe.probe(target)

        regression_code = 0.0
        regression_verdict = "none"
        if pr.screenshot_path and self.tracker is not None:
            fp = fingerprint_path(pr.screenshot_path)
            rr = self.tracker.observe(target.name, fp)
            regression_verdict = rr.verdict
            regression_code = rr.as_metric()

        metrics = self._metrics(target, pr, regression_code)
        rendered = render_prometheus(metrics, job=self.job)
        self.sink.emit(rendered, metrics)

        return CheckResult(
            target=target.name,
            url=target.url,
            ok=pr.ok,
            status=pr.status,
            load_ms=pr.load_ms,
            text_chars=pr.text_chars,
            expect_met=pr.expect_met,
            error=pr.error,
            screenshot_path=pr.screenshot_path,
            regression=regression_verdict,
            metrics=metrics,
        )

    def run_sweep(self, targets: Sequence[Target]) -> list[CheckResult]:
        results: list[CheckResult] = []
        for target in targets:
            # A misconfigured target (bad URL) must not abort the sweep.
            try:
                results.append(self.check_one(target))
            except Exception as exc:  # noqa: BLE001 - monitoring never aborts on one target
                results.append(
                    CheckResult(
                        target=target.name,
                        url=target.url,
                        ok=False,
                        status="error",
                        load_ms=0.0,
                        text_chars=0,
                        expect_met=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results
