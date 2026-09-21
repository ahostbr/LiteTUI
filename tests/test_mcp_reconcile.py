"""Coordinator-level tests for MCPManager.reconcile + the claim/lock exclusion.

Fake transports only — NO real process/network. Exercises real behavior
(outcomes, preservation, busy rejection, ownership), not lock-existence asserts.

Run only this file:
    python -m pytest tests/test_mcp_reconcile.py
"""
from __future__ import annotations

import json

import pytest

import litetui.mcp_client as mc

# Opt out of conftest's autouse dial-out stub (_never_dial_out_from_a_constructor),
# which monkeypatches MCPManager.connect/reconnect/reload_configs. These tests
# drive the REAL public verbs (with a fake _build transport — no process/network).
pytestmark = pytest.mark.real_mcp_load


class Fake:
    """A stand-in transport: no subprocess, no network."""
    def __init__(self, name, *, fail=False):
        self.name = name
        self.tools = []
        self.fail = fail
        self.started = False
        self.stopped = False
        self.error = None

    def start(self):
        if self.fail:
            raise RuntimeError("start boom")
        self.started = True

    def stop(self):
        self.stopped = True


def _mgr(tmp_path, servers, *, fail=()):
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    m = mc.MCPManager(tmp_path)
    builds: list[Fake] = []

    def _build(name, sc):
        f = Fake(name, fail=name in fail)
        builds.append(f)
        return f

    m._build = _build
    m._builds = builds
    return m


def _write_cfg(tmp_path, servers):
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── reconcile outcomes ───────────────────────────────────────────────────────
def test_reconcile_connects_new(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    assert m.reconcile() == {"a": "connected"}
    assert list(m.servers) == ["a"]


def test_reconcile_disconnects_removed(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    old = m.servers["a"]
    _write_cfg(tmp_path, {})                      # valid empty = intentional remove-all
    assert m.reconcile() == {"a": "disconnected"}
    assert list(m.servers) == [] and old.stopped is True


def test_reconcile_reconnects_changed(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    old = m.servers["a"]
    _write_cfg(tmp_path, {"a": {"command": "y"}})   # config changed
    assert m.reconcile() == {"a": "reconnected"}
    assert old.stopped is True and m.servers["a"] is not old


def test_orphan_never_declared_is_untouched(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    orphan = Fake("orphan")
    m.servers["orphan"] = orphan                 # running, never in configs
    m.reconcile()
    assert "orphan" in m.servers and orphan.stopped is False


# ── failure / non-atomic ─────────────────────────────────────────────────────
def test_partial_connect_failure_is_non_atomic(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}, "b": {"command": "y"}}, fail={"b"})
    out = m.reconcile()
    assert out["a"] == "connected" and out["b"].startswith("failed:")
    assert "a" in m.servers and "b" not in m.servers   # partial applied, not rolled back


def test_changed_server_start_failure_reports_outage(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    _write_cfg(tmp_path, {"a": {"command": "y"}})
    m._build = lambda name, sc: Fake(name, fail=True)   # the restart fails
    out = m.reconcile()
    assert out["a"].startswith("failed (outage):")
    assert "a" not in m.servers                          # stopped, new start failed, no rollback


# ── config safety ────────────────────────────────────────────────────────────
def test_malformed_config_preserves_no_bulk_disconnect(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    (tmp_path / "mcp.json").write_text("NOT JSON", encoding="utf-8")
    out = m.reconcile()
    assert list(out) == [""] and "config invalid" in out[""]
    assert list(m.servers) == ["a"]                     # preserved, not bulk-disconnected


def test_empty_valid_config_is_remove_all(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}, "b": {"command": "y"}})
    m.reconcile()
    _write_cfg(tmp_path, {})                             # valid empty, no parse error
    out = m.reconcile()
    assert out == {"a": "disconnected", "b": "disconnected"}
    assert list(m.servers) == []


# ── exclusion: busy, never block/cancel ──────────────────────────────────────
def test_concurrent_reconcile_reports_busy(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m._maint_active = True                               # a lifecycle op holds the claim
    out = m.reconcile()
    assert list(out) == [""] and out[""].startswith("busy:")
    m._maint_active = False


def test_public_verbs_raise_busy_when_claimed(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m._maint_active = True
    for call in (lambda: m.connect("a"), lambda: m.disconnect("a"), lambda: m.reconnect("a")):
        with pytest.raises(mc.MCPBusy):
            call()
    m._maint_active = False


def test_reconcile_internals_do_not_self_reject(tmp_path):
    # reconcile holds the claim and calls the *_locked internals (connect+
    # disconnect) — those must NOT raise MCPBusy against reconcile's own claim.
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    _write_cfg(tmp_path, {"a": {"command": "z"}})       # forces internal disconnect+connect
    assert m.reconcile() == {"a": "reconnected"}


# ── shutdown never busy-refuses / never leaks ────────────────────────────────
def test_stop_all_stops_and_clears_even_if_claim_flag_set(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}, "b": {"command": "y"}})
    m.reconcile()
    srvs = list(m.servers.values())
    m.stop_all()
    assert list(m.servers) == [] and all(s.stopped for s in srvs)


def test_connect_after_stop_all_is_refused_no_leak(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    m.stop_all()
    result = m.connect("a")                       # public connect, claim free, but closing
    assert "closing" in result and "a" not in m.servers


def test_reconcile_after_closing_starts_nothing(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.stop_all()                                  # sets closing
    _write_cfg(tmp_path, {"a": {"command": "x"}})
    out = m.reconcile()
    assert out.get("a", "").startswith("failed:") and "closing" in out["a"]
    assert list(m.servers) == []


def test_add_remove_reload_raise_busy_when_claimed(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m._maint_active = True                         # a maintenance op holds the claim
    with pytest.raises(mc.MCPBusy):
        m.reload_configs()
    with pytest.raises(mc.MCPBusy):
        m.add("newsrv", {"command": "echo"})
    with pytest.raises(mc.MCPBusy):
        m.remove("a")
    m._maint_active = False


# ── batch 1: coordinator-state correctness (review remediation) ───────────────
def test_reconcile_disconnects_newly_disabled_server(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    _write_cfg(tmp_path, {"a": {"command": "x", "disabled": True}})
    out = m.reconcile()
    assert out == {"a": "disconnected"} and "a" not in m.servers


def test_non_object_config_preserved_as_invalid(tmp_path):
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    (tmp_path / "mcp.json").write_text("[]", encoding="utf-8")   # valid JSON, wrong type
    out = m.reconcile()
    assert list(out) == [""] and "config invalid" in out[""]
    assert "a" in m.servers                                       # preserved, no bulk disconnect


def test_invalid_entry_reported_and_describe_does_not_crash(tmp_path):
    m = _mgr(tmp_path, {})
    _write_cfg(tmp_path, {"a": []})                               # entry is not an object
    out = m.reconcile()
    assert out["a"].startswith("failed:")
    assert m.describe()[0]["state"] == "failed"                  # describe survives it


def test_stop_failure_retains_ownership_and_quarantines(tmp_path):
    class BadStop(Fake):
        def stop(self):
            raise RuntimeError("stop boom")
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m._build = lambda n, sc: BadStop(n)
    m.reconcile()
    _write_cfg(tmp_path, {})
    out = m.reconcile()
    assert out["a"].startswith("failed:") and "a" in m.servers    # not "disconnected", retained
    # quarantined: a reconnect must NOT start a second process over it
    assert "stop unresolved" in (m._connect_locked("a") or "")


def test_describe_surfaces_runtime_transport_error(tmp_path):
    m = _mgr(tmp_path, {})
    f = Fake("a"); f.tools = [{"name": "x"}]; f.error = "timed out"
    m.configs = {"a": {"command": "x"}}
    m.servers["a"] = f
    row = m.describe()[0]
    assert row["state"] == "failed" and row["error"] == "timed out"


def test_remove_lower_precedence_keeps_effective_server(tmp_path):
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "high"}}}), encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "low"}}}), encoding="utf-8")
    m = mc.MCPManager(tmp_path)
    m._build = lambda n, sc: Fake(n)
    m.reconcile()
    assert m.remove("a") is None
    assert "a" in m.configs and "a" in m.servers                  # effective server untouched


def test_invalid_entry_retains_running_healthy_server(tmp_path):
    # A malformed edit to a RUNNING server's entry must not disconnect it
    # (continuity): retain the server + the last-known-good config, report failed.
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.reconcile()
    srv = m.servers["a"]
    _write_cfg(tmp_path, {"a": []})                 # malformed entry, not a removal
    out = m.reconcile()
    assert out["a"].startswith("failed:") and "retained" in out["a"]
    assert "a" in m.servers and m.servers["a"] is srv and srv.stopped is False
    assert m.configs["a"] == {"command": "x"}        # last-known-good config kept


def test_failed_start_whose_cleanup_also_fails_retains_handle(tmp_path):
    class BadStartStop(Fake):
        def start(self):
            raise RuntimeError("start boom")
        def stop(self):
            raise RuntimeError("stop boom too")
    m = _mgr(tmp_path, {"a": {"command": "x"}})
    m.configs = {"a": {"command": "x"}}               # _connect_locked reads configs
    m._build = lambda n, sc: BadStartStop(n)
    err = m._connect_locked("a")
    assert err and "start boom" in err               # start failure returned, not raised
    assert "a" in m.servers and "a" in m._stop_failed  # handle retained + quarantined
