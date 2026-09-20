"""The /monitor plugin: command registration and argument dispatch.

The full sweep needs a live browser, so this test exercises the plumbing that
IS testable: _register binds the command, _config_path parses a trailing
--config, and _handle routes each subcommand (init / targets / demo / default).
The default (real-sweep) path is driven with a stubbed sweep and a synchronous
thread so the dispatch is verified without any Chrome.
"""

from __future__ import annotations

import json

from litetui.plugins import monitor_plugin as mp
plugin_mod = mp  # the module under test


class _FakeApp:
    def __init__(self):
        self.messages = []

    def call_from_thread(self, fn, *args):
        # Run inline so the test is deterministic and thread-free.
        fn(*args)

    def system_message(self, text):
        self.messages.append(text)


class _FakeCtx:
    def __init__(self):
        self.commands = []
        self.rows = []

    def command(self, tokens, handler, **kw):
        self.commands.append((tokens, handler, kw))

    def palette_row(self, title, help, run, **kw):
        self.rows.append((title, kw))


def test_register_binds_the_monitor_command():
    ctx = _FakeCtx()
    plugin_mod._register(ctx)
    assert any(tokens == ("/monitor",) for tokens, _, _ in ctx.commands)
    # The registered handler is the module's _handle.
    handler = next(h for t, h, _ in ctx.commands if t == ("/monitor",))
    assert handler is plugin_mod._handle
    # A palette row was also registered.
    assert any(title == "Browser monitor" for title, _ in ctx.rows)


def test_manifest_is_declared():
    assert plugin_mod.PLUGIN.id == "monitor"
    assert plugin_mod.PLUGIN.critical is False


def test_config_path_parsing():
    # OS-agnostic: Path normalises the separator, so compare by name.
    assert mp._config_path("").name == "monitor-targets.json"
    assert mp._config_path("run --config /tmp/x.json").name == "x.json"
    # A stray --config with no value falls back to the default.
    assert mp._config_path("--config").name == "monitor-targets.json"


def test_handle_init_writes_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = _FakeApp()
    cfg = tmp_path / "t.json"
    mp._handle(app, "monitor", f"init --config {cfg}")
    assert cfg.exists()
    assert json.loads(cfg.read_text())["targets"]
    assert any("wrote" in m for m in app.messages)


def test_handle_targets_missing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = _FakeApp()
    mp._handle(app, "monitor", f"targets --config {tmp_path / 'nope.json'}")
    assert any("no config" in m for m in app.messages)


def test_handle_demo_posts_a_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # demo's FileSink writes monitor-log.jsonl
    app = _FakeApp()
    mp._handle(app, "monitor", "demo")
    assert any("demo" in m for m in app.messages)
    assert (tmp_path / "monitor-log.jsonl").exists()


def test_handle_default_runs_the_real_sweep_via_thread(monkeypatch):
    # Stub the real sweep and make the thread run inline so the dispatch is
    # verified without a browser.
    called = {}

    def _stub_sweep(app, path):
        called["path"] = path
        mp._post(app, "SWEEP-DONE")

    monkeypatch.setattr(mp, "_run_sweep_real", _stub_sweep)

    class _SyncThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(mp.threading, "Thread", _SyncThread)

    app = _FakeApp()
    mp._handle(app, "monitor", "run")
    assert "SWEEP-DONE" in app.messages
    assert str(called["path"]).endswith("monitor-targets.json")


def test_handle_default_sweep_failure_is_reported(monkeypatch):
    # A sweep that raises must surface as a posted error, never a crash.
    def _boom(app, path):
        raise RuntimeError("nope")

    monkeypatch.setattr(mp, "_run_sweep_real", _boom)

    class _SyncThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(mp.threading, "Thread", _SyncThread)
    app = _FakeApp()
    mp._handle(app, "monitor", "run")
    assert any("sweep failed" in m and "nope" in m for m in app.messages)
