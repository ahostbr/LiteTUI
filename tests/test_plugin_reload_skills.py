"""Tests for the shared guarded skills refresh, exercised through BOTH surfaces:
/reload-plugins skills (plugin_reload_ui._handle_skills) and /skills refresh
(skills_plugin._cmd_refresh). Neither may bypass the gates.

Fake app + real registry + real guarded helper / provenance. The skills disk
work (app.refresh_skills) is a spy so we can assert it is NOT called when a gate
refuses. The children probe is monkeypatched (no real ~/.litetui-agents read).

Run only this file:
    python -m pytest tests/test_plugin_reload_skills.py
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

import litetui.app  # noqa: F401 — present at real startup; the skills provenance set baselines it
from litetui.plugins import PluginRegistry, PluginContext
import litetui.plugins.skills_plugin as sp
import litetui.plugins.plugin_reload_ui as ui
from litetui.plugin_reload_provenance import capture_skills_baseline


@pytest.fixture(autouse=True)
def _reset_cancellable():
    from litetui import ttyguard
    saved = ttyguard.CANCELLABLE.get("proc")
    ttyguard.CANCELLABLE["proc"] = None
    try:
        yield
    finally:
        ttyguard.CANCELLABLE["proc"] = saved


@pytest.fixture(autouse=True)
def _idle_children(monkeypatch):
    # Both surfaces build their activity with children_pending; keep it off-disk.
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda *a: False)
    monkeypatch.setattr("litetui.plugins.skills_plugin.children_pending", lambda *a: False)


def _app(*, skills_enabled=True, native=False, workers=None, refresh=None):
    msgs: list[str] = []
    calls: list[bool] = []

    def _default_refresh():
        calls.append(True)
        app.skills = [NS(name="newskill", description="d", source="local", path="x")]
        return (["newskill"], [])

    backend = NS(name="codex", app_server=object()) if native else NS(name="lmstudio")
    app = NS(
        settings=NS(skills_enabled=skills_enabled, skill_roots=[]),
        backend=backend, convo_id="c1", workers=workers or [],
        store=NS(pending=False, loading=False), screen_stack=[object()],
        skills=[], skills_cached_at=0.0, system_message=msgs.append,
    )
    app.refresh_skills = refresh or _default_refresh
    app._msgs = msgs
    app._refresh_calls = calls
    app.plugins = PluginRegistry()
    sp.PLUGIN.register(PluginContext(app, app.plugins, "skills"))
    capture_skills_baseline(app)
    return app


def _last(app):
    return app._msgs[-1] if app._msgs else ""


def _invoke(app, surface):
    if surface == "reload_ui":
        ui._handle_skills(app)
    else:
        sp._cmd_refresh(app)


BOTH = pytest.mark.parametrize("surface", ["reload_ui", "skills_cmd"])


# ── happy path: both surfaces refresh and it's visible to the live skill tool ──
@BOTH
def test_refresh_succeeds_and_is_live_visible(surface):
    app = _app()
    skill_tool = next(e for e in app.plugins.tools if e.name in ("skill",) or e.gate)
    # before: no skills => the skill tool is gated off
    gate_before = skill_tool.gate() if skill_tool.gate else None
    _invoke(app, surface)
    assert app._refresh_calls == [True]
    assert [s.name for s in app.skills] == ["newskill"]
    # live-read gate now sees the refreshed skills
    if skill_tool.gate:
        assert gate_before is False and skill_tool.gate() is True
    assert "refresh" in _last(app).lower()


# ── gates refuse BEFORE any discovery/cache write (spy never called) ──────────
@BOTH
def test_disabled_refuses(surface):
    app = _app(skills_enabled=False)
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "off" in _last(app).lower() or "disabled" in _last(app).lower()


@BOTH
def test_native_requires_restart(surface):
    app = _app(native=True)
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "restart" in _last(app).lower()


@BOTH
def test_busy_defers_without_touching_disk(surface):
    app = _app(workers=[NS(group="chat", state=WorkerState.RUNNING)])
    _invoke(app, surface)
    assert app._refresh_calls == []          # zero discover/write while busy
    assert "defer" in _last(app).lower()


@BOTH
def test_unknown_children_defers(surface, monkeypatch):
    # children evidence unavailable (None) => unknown => busy => defer, no write
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", lambda *a: None)
    monkeypatch.setattr("litetui.plugins.skills_plugin.children_pending", lambda *a: None)
    app = _app()
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "defer" in _last(app).lower()


@BOTH
def test_source_drift_requires_restart(surface):
    app = _app()
    # simulate a skills-source file drifting since startup
    baseline = app._skills_source_baseline
    a_path = next(p for p in baseline["files"] if p)
    baseline["files"][a_path] = "0" * 64
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "restart" in _last(app).lower()


@BOTH
def test_failure_leaves_skills_unchanged(surface):
    def _boom():
        raise RuntimeError("discover failed")
    app = _app(refresh=_boom)
    before = app.skills
    _invoke(app, surface)
    assert "fail" in _last(app).lower()
    assert app.skills is before          # atomic: original mapping preserved


# ── missing baseline fails closed ─────────────────────────────────────────────
@BOTH
def test_missing_skills_baseline_requires_restart(surface):
    app = _app()
    del app._skills_source_baseline      # simulate activate never captured it
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "restart" in _last(app).lower()


# ── REAL loader/refresh regression (temp roots; real discover_all/write_cache) ─
import types as _types
from litetui.app import LiteTUI
from litetui import skills as skills_mod
from litetui import paths as _paths


def _make_skill(data_root, name, description, body):
    d = data_root / skills_mod.SKILLS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")


def _real_app(tmp_path, monkeypatch):
    # Real LiteTUI.refresh_skills bound to a fake host, over a temp data root, so
    # discover_all/write_cache/cache_path all operate on the fixture, not ~/.claude.
    monkeypatch.setattr("litetui.paths.data_root", lambda: tmp_path)
    app = _app()
    app.refresh_skills = _types.MethodType(LiteTUI.refresh_skills, app)
    return app


@BOTH
def test_real_refresh_visible_to_loader_and_prompt(tmp_path, monkeypatch, surface):
    app = _real_app(tmp_path, monkeypatch)
    _make_skill(tmp_path, "myskill", "My test skill", "DO THE THING")
    _invoke(app, surface)
    # added discovered from the real fixture
    assert any(s.name == "myskill" for s in app.skills)
    # real skill loader (through the registered `skill` tool) returns the body
    tool = next(e for e in app.plugins.tools if e.gate)
    assert "DO THE THING" in tool.run({"name": "myskill"})
    # real prompt index (what the SKILLS_INDEX section renders) shows it
    assert "myskill" in skills_mod.index_block(app.skills)
    # cache was written to the temp root
    assert skills_mod.cache_path(tmp_path).exists()


def test_real_refresh_atomic_on_discover_error(tmp_path, monkeypatch):
    app = _real_app(tmp_path, monkeypatch)
    _make_skill(tmp_path, "myskill", "My test skill", "BODY")
    def _boom(root, extra=None):
        raise RuntimeError("discover exploded")
    monkeypatch.setattr("litetui.skills.discover_all", _boom)
    before = app.skills
    _invoke(app, "skills_cmd")
    assert "fail" in _last(app).lower()
    assert app.skills is before          # real refresh_skills swaps last => untouched on error
