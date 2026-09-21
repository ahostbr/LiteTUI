"""/monitor — run a browser-monitor sweep from inside the TUI.

This is the in-TUI door to the same engine the CLI drives. A sweep probes
several web targets through Chrome, which takes real seconds, so it must not
run on the UI thread — the handler spawns a daemon thread that does the sweep
and posts the report back with ``call_from_thread``. The UI stays responsive
and the result lands as a system message.

Subcommands (parsed from the command argument):
  /monitor            run a sweep over the default config
  /monitor demo       run with a fake probe (no browser) — the safe default
  /monitor targets    list the configured targets
  /monitor init       write a default config
A ``--config PATH`` may be passed to point at a specific target file.
"""

from __future__ import annotations

import threading
from pathlib import Path

from litetui import runtime_log
from litetui.monitor import targets as targets_mod
from litetui.monitor.engine import MonitorEngine
from litetui.monitor.metrics import FileSink, PushgatewaySink
from litetui.monitor.probe import ChromeProbe, FakeProbe
from litetui.monitor.regression import RegressionTracker
from litetui.monitor.report import format_report
from litetui.plugins import PluginManifest

DEFAULT_CONFIG = "monitor-targets.json"
DEFAULT_SHOTS = "monitor-shots"
DEFAULT_STATE = "monitor-baselines.json"
DEFAULT_LOG = "monitor-log.jsonl"


def _post(app, text: str) -> None:
    """Deliver a block of text to the user from any thread. Textual's
    call_from_thread hops onto the UI loop; system_message is the supported
    plugin-to-user channel (see its docstring) and is thread-agnostic here
    only because call_from_thread guarantees it runs on the UI thread."""
    app.call_from_thread(app.system_message, text)


def _config_path(arg: str) -> Path:
    # A trailing ``--config PATH`` in the command argument overrides the default.
    parts = arg.split()
    if "--config" in parts:
        i = parts.index("--config")
        if i + 1 < len(parts):
            return Path(parts[i + 1])
    return Path(DEFAULT_CONFIG)


def _load_targets(path: Path):
    if not path.exists():
        return None
    return targets_mod.load_targets(path)


def _run_sweep_real(app, path: Path) -> None:
    targets = _load_targets(path)
    if targets is None:
        _post(app, f"[monitor] no config at {path} — try /monitor init or /monitor demo")
        return
    if not targets:
        _post(app, "[monitor] config exists but has no targets")
        return
    probe = ChromeProbe(shot_dir=DEFAULT_SHOTS)
    sink = FileSink(DEFAULT_LOG)
    tracker = RegressionTracker(DEFAULT_STATE)
    engine = MonitorEngine(probe=probe, sink=sink, tracker=tracker)
    results = engine.run_sweep(targets)
    _post(app, format_report(results))


def _run_sweep_demo(app) -> None:
    from litetui.monitor.targets import Target

    probe = FakeProbe(fail=frozenset({"flaky"}))
    sink = FileSink(DEFAULT_LOG)
    engine = MonitorEngine(probe=probe, sink=sink)
    demo = [
        Target(name="example", url="https://example.com", expect_text="Example Domain"),
        Target(name="flaky", url="https://example.com/api", expect_text="Example Domain"),
    ]
    results = engine.run_sweep(demo)
    _post(app, format_report(results, title="monitor sweep (demo)"))


def _handle(app, name: str, arg: str) -> None:
    parts = arg.split()
    sub = parts[0].lower() if parts else "run"
    config_path = _config_path(arg)

    if sub == "init":
        p = config_path
        if p.exists():
            _post(app, f"[monitor] {p} already exists")
            return
        p.write_text(
            __import__("json").dumps(targets_mod.default_config(), indent=2),
            encoding="utf-8",
        )
        _post(app, f"[monitor] wrote {p}")
        return

    if sub == "targets":
        targets = _load_targets(config_path)
        if targets is None:
            _post(app, f"[monitor] no config at {config_path}")
            return
        rows = [f"  {t.name:<14} {t.url}" for t in targets] or ["  (none)"]
        _post(app, "[monitor] targets:\n" + "\n".join(rows))
        return

    if sub == "demo":
        # Demo is instant and side-effect-light; run it on the UI thread.
        _run_sweep_demo(app)
        return

    # Retain ownership before dispatch, including the not-yet-started window.
    # Reload eligibility must not mistake a raw thread for an idle App.
    threads = getattr(app, '_monitor_threads', None)
    if threads is None:
        threads = app._monitor_threads = set()
    # default: a real sweep — off the UI thread, because Chrome is slow.
    def go() -> None:
        try:
            _run_sweep_real(app, config_path)
        except Exception as exc:  # noqa: BLE001 - report, never crash the UI
            runtime_log.record(
                "monitor_sweep_failed",
                site="plugins.monitor.sweep",
                component="plugin",
                plugin="monitor",
                error_type=type(exc).__name__,
            )
            _post(app, f"[monitor] sweep failed: {type(exc).__name__}: {exc}")
        finally:
            threads.discard(thread)

    thread = threading.Thread(target=go, name="monitor-sweep", daemon=True)
    threads.add(thread)
    try:
        thread.start()
    except BaseException:
        threads.discard(thread)
        raise


def _register(ctx) -> None:
    ctx.command(
        ("/monitor",),
        _handle,
        palette="Browser monitor",
        help="Watch web targets and push metrics to Grafana.",
        group="automation",
        order=40,
    )
    ctx.palette_row(
        "Browser monitor",
        "Visit web targets, measure load/health, feed a Grafana dashboard.",
        lambda: None,
        group="automation",
        order=40,
        tag="/monitor demo",
    )


PLUGIN = PluginManifest(id="monitor", register=_register)
