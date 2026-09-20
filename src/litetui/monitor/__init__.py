"""Browser monitor: watch web targets from the outside, feed Grafana.

A small, dependency-light engine that visits a list of URLs through a
:class:`BrowserProbe` (the real one wraps the chrome-bridge; a fake stands in
for tests), measures load time / text / screenshots, detects visual
regressions, and ships the numbers to a :class:`MetricsSink` (Prometheus
pushgateway or a local JSONL file).

Public surface is re-exported here so callers import from the package, not the
modules: ``from litetui.monitor import MonitorEngine, FileSink, ...``.
"""

from __future__ import annotations

from litetui.monitor.engine import CheckResult, MonitorEngine
from litetui.monitor.metrics import (
    DEFAULT_JOB,
    FileSink,
    Metric,
    MetricsSink,
    PushgatewaySink,
    render_prometheus,
)
from litetui.monitor.probe import BrowserProbe, ChromeProbe, FakeProbe, ProbeResult
from litetui.monitor.regression import (
    RegressionResult,
    RegressionTracker,
    fingerprint_bytes,
    fingerprint_path,
)
from litetui.monitor.report import format_report, summary, to_json
from litetui.monitor.targets import Target, load_targets

__all__ = [
    "BrowserProbe",
    "CheckResult",
    "ChromeProbe",
    "DEFAULT_JOB",
    "FakeProbe",
    "FileSink",
    "Metric",
    "MetricsSink",
    "MonitorEngine",
    "ProbeResult",
    "PushgatewaySink",
    "RegressionResult",
    "RegressionTracker",
    "Target",
    "fingerprint_bytes",
    "fingerprint_path",
    "format_report",
    "load_targets",
    "render_prometheus",
    "summary",
    "to_json",
]
