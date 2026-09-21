"""Tests for the /reload-plugins UI command (plugins.plugin_reload_ui).

Fake app + REAL core stages (stage_schema_refresh, commit_metadata_candidate,
produce_activity run for real); only the disk/edge reads are monkeypatched
(tool_schemas.load_fresh, tool_schemas.available, children_pending). Command
registration is captured through a real PluginContext. No backend starts.

Run only this file:
    python -m pytest tests/test_plugin_reload_ui.py
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

from litetui.plugins import PluginRegistry, PluginContext
from litetui.tool_policy import ToolPolicy, READ_ONLY
import litetui.plugins.plugin_reload_ui as ui


@pytest.fixture(autouse=True)
def _reset_cancellable():
    from litetui import ttyguard
    saved = ttyguard.CANCELLABLE.get("proc")
    ttyguard.CANCELLABLE["proc"] = None
    try:
        yield
    finally:
        ttyguard.CANCELLABLE["proc"] = saved


def _spec(name="bash", description="original desc", params=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": params or {"type": "object", "properties": {}, "required": []}}}


def _reg_with(name="bash", spec=None):
    reg = PluginRegistry()
    reg.add_tool("core-tools", spec or _spec(name), run=lambda a: "",
                 policy=ToolPolicy(frozenset({READ_ONLY}), "t"))
    return reg


def _app(reg=None, **over):
    msgs: list[str] = []
    base = dict(plugins=reg or _reg_with(), backend=NS(name="lmstudio"), convo_id="c1",
                workers=[], store=NS(pending=False, loading=False),
                screen_stack=[object()], system_message=msgs.append)
    base.update(over)
    app = NS(**base)
    app._msgs = msgs
    return app


@pytest.fixture
def patched(monkeypatch):
    """Default: schema 'bash' is available, load_fresh returns a NEW description,
    children probe idle. Individual tests override load_fresh/available."""
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    monkeypatch.setattr("litetui.tool_schemas.load_fresh",
                        lambda name, **f: _spec("bash", description="NEW desc"))
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    return monkeypatch


def _last(app) -> str:
    return app._msgs[-1] if app._msgs else ""


# ── command registration (captured) ──────────────────────────────────────────
def test_command_is_registered():
    reg = PluginRegistry()
    ui.PLUGIN.register(PluginContext(NS(), reg, "reload-plugins"))
    assert "/reload-plugins" in reg.commands


# ── usage / validation ───────────────────────────────────────────────────────
def test_no_arg_shows_honest_usage(patched):
    app = _app()
    ui._handle(app, "/reload-plugins", "")
    msg = _last(app)
    assert "NOT a full plugin reload" in msg and "bash" in msg


def test_unknown_name_rejected(patched):
    app = _app()
    ui._handle(app, "/reload-plugins", "nope")
    assert "unknown or ineligible" in _last(app)
    assert app.plugins is not None  # untouched


def test_powershell_excluded_from_eligibility(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"powershell"})
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app(reg=_reg_with("powershell"))
    ui._handle(app, "/reload-plugins", "")            # usage
    assert "(none)" in _last(app)
    ui._handle(app, "/reload-plugins", "powershell")  # direct
    assert "unknown or ineligible" in _last(app)


# ── outcomes ─────────────────────────────────────────────────────────────────
def test_identical_is_unchanged(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    monkeypatch.setattr("litetui.tool_schemas.load_fresh", lambda name, **f: _spec("bash"))
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app()
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "unchanged" in _last(app)
    assert app.plugins is original


def test_description_refresh_reloads_and_swaps(patched):
    app = _app()
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "reloaded" in _last(app)
    assert app.plugins is not original                      # pointer swapped
    desc = next(e.spec for e in app.plugins.tools if e.name == "bash")["function"]["description"]
    assert desc == "NEW desc"


def test_contract_change_is_restart_required(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    changed = _spec("bash", params={"type": "object", "properties": {"z": {"type": "string"}}, "required": []})
    monkeypatch.setattr("litetui.tool_schemas.load_fresh", lambda name, **f: changed)
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app()
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "restart required" in _last(app)
    assert app.plugins is original


def test_malformed_schema_is_failed_not_json_error(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    # valid JSON, but parameters.properties missing => _schema_problem, not a JSON exception
    bad = {"type": "function", "function": {"name": "bash", "description": "x",
                                            "parameters": {"type": "object"}}}
    monkeypatch.setattr("litetui.tool_schemas.load_fresh", lambda name, **f: bad)
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app()
    ui._handle(app, "/reload-plugins", "bash")
    msg = _last(app)
    assert "failed" in msg and "restart" not in msg


def test_disk_load_exception_is_failed_live_preserved(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    def boom(name, **f):
        raise ValueError("Expecting value: line 1 column 1")   # e.g. JSONDecodeError
    monkeypatch.setattr("litetui.tool_schemas.load_fresh", boom)
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app()
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "failed to load" in _last(app)
    assert app.plugins is original


def test_templated_placeholder_refused(monkeypatch):
    monkeypatch.setattr("litetui.tool_schemas.available", lambda: {"bash"})
    monkeypatch.setattr("litetui.tool_schemas.load_fresh",
                        lambda name, **f: _spec("bash", description="runs {exe} here"))
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda root, parent: False)
    app = _app()
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "templated" in _last(app) and "restart" in _last(app)
    assert app.plugins is original


def test_native_backend_restart_required_without_rpc(patched):
    class _NoRPC:
        def __getattr__(self, n):
            raise AssertionError(f"native app-server method invoked: {n}")
    app = _app(backend=NS(name="codex", app_server=_NoRPC()))
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    assert "restart required" in _last(app)
    assert app.plugins is original


def test_busy_activity_defers(patched):
    app = _app(workers=[NS(group="chat", state=WorkerState.RUNNING)])
    original = app.plugins
    ui._handle(app, "/reload-plugins", "bash")
    msg = _last(app)
    assert "deferred" in msg and "after the active work finishes" in msg
    assert app.plugins is original


def test_sibling_registry_independence(patched):
    app_a = _app()
    reg_b = _reg_with()
    app_b = _app(reg=reg_b)
    ui._handle(app_a, "/reload-plugins", "bash")
    assert "reloaded" in _last(app_a)
    # App B is untouched by App A's reload
    assert app_b.plugins is reg_b
    assert next(e.spec for e in app_b.plugins.tools if e.name == "bash")["function"]["description"] == "original desc"
