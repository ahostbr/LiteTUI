"""Tests for litetui.plugin_reload_provenance.

Real byte hashing. Target handler sources are tmp files (created/edited/deleted
for real) backed by fake modules registered in sys.modules under _provtest_*
names; a fixture removes those. Core-drift is exercised by tampering the stored
baseline digest of a real core file (equivalent to the disk differing from the
baseline) — the real core files are never edited.

Run only this file:
    python -m pytest tests/test_plugin_reload_provenance.py
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

import pytest

import litetui.plugin_reload_provenance as prov


@pytest.fixture(autouse=True)
def _clean_fake_modules():
    yield
    for name in [n for n in sys.modules if n.startswith("_provtest_")]:
        del sys.modules[name]


def _tool(name: str, path) -> NS:
    modname = f"_provtest_{name}"
    mod = types.ModuleType(modname)
    mod.__file__ = str(path)
    def run(a):  # noqa: ANN001
        return ""
    run.__module__ = modname
    sys.modules[modname] = mod
    return NS(name=name, run=run)


def _app(tools) -> NS:
    return NS(plugins=NS(tools=tools))


def _write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


# ── baseline ─────────────────────────────────────────────────────────────────
def test_baseline_captures_core_and_targets(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    baseline = prov.capture_baseline(app)
    assert baseline["core"]                                   # real core files discovered
    assert baseline["targets"]["bash"] == str(src.resolve())
    assert baseline["files"][str(src.resolve())] is not None


def test_unchanged_target_is_ok(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    prov.capture_baseline(app)
    assert prov.provenance_ok(app, "bash") == (True, "")


# ── target drift ─────────────────────────────────────────────────────────────
def test_target_source_changed_requires_restart(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    prov.capture_baseline(app)
    _write(src, "v2 — edited after startup")
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "changed after startup" in reason


def test_target_unreadable_now(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    prov.capture_baseline(app)
    src.unlink()
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "unreadable now" in reason


def test_target_unreadable_at_startup(tmp_path):
    missing = tmp_path / "gone.py"       # never created
    app = _app([_tool("bash", missing)])
    prov.capture_baseline(app)
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "unreadable at startup" in reason


def test_handler_rebind_to_other_source_is_drift(tmp_path):
    a = _write(tmp_path / "a.py", "aaa")
    b = _write(tmp_path / "b.py", "bbb")
    tool = _tool("bash", a)
    app = _app([tool])
    prov.capture_baseline(app)
    # rebind the handler to a different (unchanged) source
    modname = f"_provtest_rebind"
    mod = types.ModuleType(modname); mod.__file__ = str(b)
    sys.modules[modname] = mod
    tool.run.__module__ = modname
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "different source" in reason
    del sys.modules[modname]


# ── core drift gates all targets ─────────────────────────────────────────────
def test_core_drift_blocks_every_target(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    baseline = prov.capture_baseline(app)
    core_path = baseline["core"][0]
    baseline["files"][core_path] = "0" * 64          # simulate disk != baseline
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "reload machinery source" in reason and "changed" in reason


# ── missing baseline / capture-once ──────────────────────────────────────────
def test_missing_baseline_requires_restart(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])       # capture never called
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "no source provenance baseline" in reason


def test_capture_is_once_only_cannot_bless_edit(tmp_path):
    src = _write(tmp_path / "t.py", "v1")
    app = _app([_tool("bash", src)])
    first = prov.capture_baseline(app)
    _write(src, "v2 — edited after startup")
    second = prov.capture_baseline(app)     # must NOT recapture
    assert second is first
    ok, reason = prov.provenance_ok(app, "bash")
    assert ok is False and "changed after startup" in reason
