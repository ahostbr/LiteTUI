"""Command-line interface for the browser monitor.

Four verbs:

  monitor init     write a default config file
  monitor targets  list the configured targets
  monitor run      do a sweep: probe each target, emit metrics, print a report
  monitor demo     do a sweep with a fake probe — no browser, no network.
                   Useful to see the whole pipeline (metrics, report, file
                   sink) produce output before wiring up a real Chrome.

``run`` needs a reachable chrome-bridge extension; ``demo`` needs nothing,
which is why it is the thing to try first on a fresh machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from litetui.monitor import targets as targets_mod
from litetui.monitor.engine import MonitorEngine
from litetui.monitor.metrics import FileSink, PushgatewaySink
from litetui.monitor.probe import ChromeProbe, FakeProbe
from litetui.monitor.regression import RegressionTracker
from litetui.monitor.report import format_report, to_json

DEFAULT_CONFIG = "monitor-targets.json"
DEFAULT_SHOTS = "monitor-shots"
DEFAULT_STATE = "monitor-baselines.json"
DEFAULT_LOG = "monitor-log.jsonl"


def _load_config(path: str) -> list:
    p = Path(path)
    if not p.exists():
        sys.exit(
            f"config not found: {path}\n"
            f"create one with:  monitor init --config {path}"
        )
    return targets_mod.load_targets(p)


def _cmd_init(args) -> int:
    p = Path(args.config)
    if p.exists() and not args.force:
        sys.exit(f"{p} already exists; pass --force to overwrite")
    p.write_text(json.dumps(targets_mod.default_config(), indent=2), encoding="utf-8")
    print(f"wrote {p}")
    return 0


def _cmd_targets(args) -> int:
    for t in _load_config(args.config):
        expect = f"  expects: {t.expect_text!r}" if t.expect_text else ""
        print(f"{t.name:<16} {t.url}{expect}")
    return 0


def _cmd_run(args) -> int:
    targets = _load_config(args.config)
    if not targets:
        print("no targets configured", file=sys.stderr)
        return 1

    probe = ChromeProbe(shot_dir=args.shots)
    tracker = RegressionTracker(args.state)

    if args.sink == "pushgateway":
        sink = PushgatewaySink(base_url=args.pushgateway)
    else:
        sink = FileSink(args.log)

    engine = MonitorEngine(probe=probe, sink=sink, tracker=tracker)
    results = engine.run_sweep(targets)

    print(format_report(results))
    if args.json:
        print(to_json(results))
    return 0


def _cmd_demo(args) -> int:
    """Prove the pipeline end-to-end without a browser. The probe is a fake,
    but the metrics rendering, the file sink and the report are all real, so a
    green demo means everything below the browser works."""
    from litetui.monitor.targets import Target

    demo_targets = [
        Target(name="example", url="https://example.com", expect_text="Example Domain"),
        Target(name="flaky", url="https://example.com/api", expect_text="Example Domain"),
    ]
    probe = FakeProbe(fail=frozenset({"flaky"}))
    sink = FileSink(args.log)
    engine = MonitorEngine(probe=probe, sink=sink)
    results = engine.run_sweep(demo_targets)
    print(format_report(results, title="monitor sweep (demo, fake probe)"))
    if args.json:
        print(to_json(results))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="monitor", description="Browser monitor for Grafana.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="write a default config file")
    pi.add_argument("--config", default=DEFAULT_CONFIG)
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=_cmd_init)

    pt = sub.add_parser("targets", help="list configured targets")
    pt.add_argument("--config", default=DEFAULT_CONFIG)
    pt.set_defaults(func=_cmd_targets)

    pr = sub.add_parser("run", help="probe every target and emit metrics")
    pr.add_argument("--config", default=DEFAULT_CONFIG)
    pr.add_argument("--sink", choices=("file", "pushgateway"), default="file")
    pr.add_argument("--pushgateway", default="http://localhost:9091")
    pr.add_argument("--shots", default=DEFAULT_SHOTS)
    pr.add_argument("--state", default=DEFAULT_STATE)
    pr.add_argument("--log", default=DEFAULT_LOG)
    pr.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    pr.set_defaults(func=_cmd_run)

    pd = sub.add_parser("demo", help="run the pipeline with a fake probe (no browser)")
    pd.add_argument("--log", default=DEFAULT_LOG)
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=_cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
