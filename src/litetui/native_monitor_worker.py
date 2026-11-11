"""Contained monitor engine invocation; never used by importing the frontend."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from litetui.monitor.engine import MonitorEngine
from litetui.monitor.metrics import FileSink, render_prometheus
from litetui.monitor.probe import ChromeProbe, FakeProbe
from litetui.monitor.regression import RegressionTracker
from litetui.monitor.targets import Target, default_config
from litetui.native_monitor_files import MAX_IMAGE, display_url, encoded


class RedactedSink(FileSink):
    def emit(self, rendered, metrics):
        metrics = [replace(metric, labels={**metric.labels, "url": display_url(metric.labels["url"])}) for metric in metrics]
        super().emit(render_prometheus(metrics), metrics)


def run(directory):
    parameters = json.loads((directory / "parameters.json").read_text(encoding="utf-8"))
    action = parameters["action"]
    targets = [Target(**row) for row in parameters["targets"]]
    if action == "init":
        (directory / "output-0").write_bytes((json.dumps(default_config(), indent=2) + "\n").encode())
        rows = []
    else:
        probe = FakeProbe(fail=frozenset({"flaky"})) if action == "demo" else ChromeProbe(shot_dir=directory / "shots")
        tracker = None if action == "demo" else RegressionTracker(directory / "output-1")
        engine = MonitorEngine(probe, RedactedSink(directory / "output-0"), tracker)
        results = engine.run_sweep(targets)
        rows = [{"name": result.target, "url": display_url(result.url), "ok": result.ok,
                 "status": result.status, "text_chars": result.text_chars,
                 "expect_met": result.expect_met, "regression": result.regression,
                 "error": "Probe failed; see the selected browser target." if result.error else ""}
                for result in results]
        if action == "sweep":
            index = 2
            for target in targets:
                if not target.screenshot:
                    continue
                screenshot = directory / "shots" / (target.name + ".png")
                if screenshot.exists():
                    with screenshot.open("rb") as stream:
                        data = stream.read(MAX_IMAGE + 1)
                    if len(data) > MAX_IMAGE:
                        raise ValueError("Monitor screenshot exceeds its artifact budget")
                    (directory / f"output-{index}").write_bytes(data)
                index += 1
    (directory / "result.json").write_bytes(encoded({"rows": rows}))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)
    run(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
