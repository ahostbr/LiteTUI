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

import sqlite3
from pathlib import Path
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
        app.skills = [NS(name="newskill", description="d", source="local", path=Path("newskill"))]
        return (["newskill"], [])

    backend = NS(name="codex", app_server=object()) if native else NS(name="lmstudio")
    app = NS(
        settings=NS(skills_enabled=skills_enabled, skill_roots=[]),
        backend=backend, convo_id="c1", workers=workers or [],
        store=NS(pending=False, loading=False), screen_stack=[object()],
        skills=[], skills_cached_at=0.0, tools_enabled=True, system_message=msgs.append,
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
    # the actual REGISTERED prompt section (not a direct index_block call) shows it
    section = next(s for s in app.plugins.prompt_sections if s.owner == "skills")
    assert section.enabled() is True
    assert "myskill" in section.render()
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


@BOTH
def test_real_remove_delta_and_gone_from_loader_and_prompt(tmp_path, monkeypatch, surface):
    app = _real_app(tmp_path, monkeypatch)
    skdir = tmp_path / skills_mod.SKILLS_DIR_NAME / "myskill"
    _make_skill(tmp_path, "myskill", "My test skill", "DO THE THING")
    _invoke(app, surface)                                   # add
    assert any(s.name == "myskill" for s in app.skills)
    import shutil
    shutil.rmtree(skdir)                                    # remove the fixture
    app._msgs.clear()
    _invoke(app, surface)                                   # refresh again
    assert all(s.name != "myskill" for s in app.skills)     # removed from live mapping
    assert "myskill" in _last(app)                          # removed delta reported
    assert "myskill" not in skills_mod.index_block(app.skills)   # gone from prompt index
    tool = next(e for e in app.plugins.tools if e.gate)
    assert "error" in tool.run({"name": "myskill"}).lower()      # no longer loadable


def test_cache_write_raising_leaves_mapping_untouched(tmp_path, monkeypatch):
    # A cache write that RAISES (non-OSError) propagates before refresh_skills'
    # self.skills swap => live mapping untouched, guarded path reports failed.
    app = _real_app(tmp_path, monkeypatch)
    _make_skill(tmp_path, "myskill", "d", "BODY")
    def _raise(root, skills):
        raise RuntimeError("disk full")
    monkeypatch.setattr("litetui.skills.write_cache", _raise)
    before = app.skills
    _invoke(app, "skills_cmd")
    assert "fail" in _last(app).lower()
    assert app.skills is before


def test_cache_write_oserror_is_best_effort_live_updated_no_partial(tmp_path, monkeypatch):
    # The REAL writer catches OSError and returns None (best-effort: a cache that
    # cannot be written must not stop the app from having skills). refresh_skills
    # then swaps self.skills anyway — live mapping IS updated, NOT untouched — and
    # because the writer is atomic (mkstemp + os.replace) there is NO partial file.
    app = _real_app(tmp_path, monkeypatch)
    _make_skill(tmp_path, "myskill", "d", "BODY")
    monkeypatch.setattr("litetui.skills.write_cache", lambda root, skills: None)
    _invoke(app, "skills_cmd")
    assert any(s.name == "myskill" for s in app.skills)     # live updated (by design)
    assert "refresh" in _last(app).lower()
    assert not skills_mod.cache_path(tmp_path).exists()      # no partial cache left behind


# ── activity() exception defers (item 1) ─────────────────────────────────────
@BOTH
def test_activity_probe_exception_defers(surface, monkeypatch):
    app = _app()
    def _boom():
        raise RuntimeError("activity probe blew up")
    # make the surface's produce_activity raise
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.produce_activity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("litetui.plugins.skills_plugin.produce_activity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _invoke(app, surface)
    assert app._refresh_calls == []
    assert "defer" in _last(app).lower()


# ── real activation sequence + baseline completeness (item 3) ────────────────
def test_activation_sequence_captures_complete_baselines():
    from litetui.plugins import PluginRegistry, PluginContext
    from litetui.plugin_reload_provenance import _CORE_MODULES, _SKILLS_MODULES
    app = NS(plugins=PluginRegistry())
    ui.PLUGIN.register(PluginContext(app, app.plugins, "reload-plugins"))
    ui.PLUGIN.activate(app)
    core = app._plugin_source_baseline
    skills = app._skills_source_baseline
    assert all(n in core["core"] for n in _CORE_MODULES)
    assert all(n in skills["modules"] for n in _SKILLS_MODULES)
    # repeated activation must NOT recapture (cannot bless a later edit)
    ui.PLUGIN.activate(app)
    assert app._plugin_source_baseline is core
    assert app._skills_source_baseline is skills


def test_missing_reload_plugin_gives_explicit_diagnostic():
    from litetui.plugin_reload_provenance import skills_provenance_ok, provenance_ok
    app = NS(plugins=NS(tools=[]))     # reload plugin never activated => no baseline
    ok, reason = skills_provenance_ok(app)
    assert ok is False and "reload plugin" in reason.lower()
    ok2, reason2 = provenance_ok(app, "anything")
    assert ok2 is False and "reload plugin" in reason2.lower()


def test_malformed_baseline_restarts_not_crashes():
    from litetui.plugin_reload_provenance import skills_provenance_ok
    # a dict that is NOT a valid baseline (missing keys / empty module map) must
    # fail closed with a diagnostic, never KeyError.
    for bad in ({}, {"files": {}}, {"files": {}, "modules": {}}, {"files": 1, "modules": 2}):
        app = NS(plugins=NS(tools=[]))
        app._skills_source_baseline = bad
        ok, reason = skills_provenance_ok(app)
        assert ok is False and "restart" in reason.lower()


# ── real durable-child SQLite through the surface (item 5), fixture-home safe ──
from litetui.plugin_reload_children import children_pending as _real_children


def _use_real_children(monkeypatch, home):
    monkeypatch.setattr("litetui.plugins.plugin_reload_ui.children_pending", _real_children)
    monkeypatch.setattr("litetui.plugins.skills_plugin.children_pending", _real_children)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _agents_dir(home):
    d = home / ".litetui-agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_real_pending_child_defers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = _agents_dir(home)
    db = sqlite3.connect(d / "registry.sqlite")
    db.execute("CREATE TABLE agents(parent TEXT, state TEXT)")
    db.execute("INSERT INTO agents VALUES('c1', 'running')")   # != settled => pending
    db.commit()
    db.close()
    app = _app()
    _use_real_children(monkeypatch, home)
    _invoke(app, "skills_cmd")
    assert app._refresh_calls == []
    assert "defer" in _last(app).lower()


def test_real_unknown_child_evidence_defers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = _agents_dir(home)
    (d / "registry.sqlite").write_bytes(b"this is not a sqlite database")   # => sqlite error => None
    app = _app()
    _use_real_children(monkeypatch, home)
    _invoke(app, "skills_cmd")
    assert app._refresh_calls == []
    assert "defer" in _last(app).lower()


def test_real_idle_children_allows_refresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _agents_dir(home)                       # empty: no stores => children_pending False
    app = _app()
    _use_real_children(monkeypatch, home)
    _invoke(app, "skills_cmd")
    assert app._refresh_calls == [True]
    assert "refresh" in _last(app).lower()
