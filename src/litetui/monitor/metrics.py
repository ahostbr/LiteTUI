"""Turn probe results into Prometheus metrics and ship them somewhere.

Two concerns live here and they are kept apart:

* **rendering** — :func:`render_prometheus` turns a list of :class:`Metric`
  into the Prometheus text exposition format. Pure, no I/O, fully testable.
* **sinking** — a ``MetricsSink`` takes the rendered text and puts it somewhere.
  ``PushgatewaySink`` POSTs it to a Prometheus Pushgateway; ``FileSink`` appends
  it to a JSONL file. FileSink is the zero-dependency default, which is what
  makes the whole monitor runnable on a machine with no Prometheus (and with
  no network at all).

Only stdlib is used. The Pushgateway POST goes through :mod:`urllib.request`
rather than httpx so a sink can be constructed and unit-tested with no client
library, and so a push failure is a contained error, not an import-time one.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


# Prometheus metric and label names: [a-zA-Z_][a-zA-Z0-9_]*
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

DEFAULT_JOB = "browser-monitor"


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    labels: dict[str, str]
    type: str = "gauge"
    help: str = ""

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid metric name: {self.name!r}")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sample_line(m: Metric) -> str:
    if m.labels:
        pairs = ",".join(
            f'{k}="{_escape(str(m.labels[k]))}"' for k in sorted(m.labels)
        )
        return f"{m.name}{{{pairs}}} {_format_num(m.value)}"
    return f"{m.name} {_format_num(m.value)}"


def _format_num(value: float) -> str:
    if value != value:  # NaN
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "Inf" if value > 0 else "-Inf"
    return repr(value)


def render_prometheus(metrics: Sequence[Metric], job: str = DEFAULT_JOB) -> str:
    """Render metrics to the text exposition format.

    Metrics that share a name+label-set (same series) are collapsed to the
    most recent value — that is how a gauge should behave across repeated
    sweeps, and it is what keeps re-runs from stacking duplicate series.
    """
    # Collapse to latest per (name, frozen-labels). Order-preserving by first
    # appearance so the output is stable for a stable input.
    latest: dict[tuple[str, tuple[tuple[str, str], ...]], Metric] = {}
    order: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for m in metrics:
        key = (m.name, tuple(sorted(m.labels.items())))
        if key not in latest:
            order.append(key)
        latest[key] = m

    lines: list[str] = []
    emitted_help: set[str] = set()
    for key in order:
        m = latest[key]
        if m.name not in emitted_help:
            if m.help:
                lines.append(f"# HELP {m.name} {m.help}")
            lines.append(f"# TYPE {m.name} {m.type}")
            emitted_help.add(m.name)
        lines.append(_sample_line(m))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


class MetricsSink(Protocol):
    def emit(self, rendered: str, metrics: Sequence[Metric]) -> None: ...


class FileSink:
    """Append one JSON line per sweep to a file.

    A JSONL sink (rather than raw prometheus text) because the point of the
    zero-dependency path is to have a durable, human- and machine-readable
    record you can ``grep``, ``jq`` or load into pandas without a Prometheus
    server. The prometheus rendering is still emitted alongside for the
    Grafana path.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, rendered: str, metrics: Sequence[Metric]) -> None:
        record = {
            "metrics": [
                {"name": m.name, "value": m.value, "labels": m.labels}
                for m in metrics
            ],
            "prometheus": rendered,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class PushgatewaySink:
    """POST rendered prometheus text to a Pushgateway.

    The pushgateway groups metrics so that a job+group pair is a single
    overwrite-able batch: re-running the monitor replaces the previous reading
    for that group instead of appending, which is the correct semantic for a
    "current state" gauge. The label ``instance`` is the conventional
    pushgateway way to name the pushing host, so it is set here.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9091",
        job: str = DEFAULT_JOB,
        group: str = "monitor",
        instance: str = "litetui",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.job = job
        self.group = group
        self.instance = instance
        self.timeout = timeout
        # Injected for tests; when set, emit() calls this instead of urllib.
        self._post = None

    def url(self) -> str:
        return f"{self.base_url}/metrics/job/{self.job}/group/{self.group}"

    def emit(self, rendered: str, metrics: Sequence[Metric]) -> None:
        if self._post is not None:
            self._post(self.url(), rendered)
            return
        body = rendered.encode("utf-8")
        req = urllib.request.Request(
            self.url(), data=body, method="POST",
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"pushgateway unreachable at {self.url()}: {exc}"
            ) from exc
