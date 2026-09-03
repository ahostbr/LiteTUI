"""Per-server MCP lifecycle: connect / disconnect / reconnect / describe.

🔴 WHAT WAS MISSING AND WHY IT MATTERED. `MCPManager.load()` read the config
files ONCE at boot and started everything; after that the only record of a
server was `self.servers`, so a stopped server was represented by its own
ABSENCE. Absence cannot be listed, reconnected or removed — which is exactly
why LiteTUI had a working MCP client and no way to manage it. The two controls
that did exist (`mcp_enabled`, `mcp_disabled_servers`) are boot-time filters and
say so in the UI: "Applies on next /reconnect".

The split these tests pin is `configs` (what the FILES declare) against
`servers` (what is RUNNING). Every management verb needs both, and a test that
only looked at `servers` would pass while the UI had nothing to show.

⚠️ NO REAL SERVER IS SPAWNED. `_build` is monkeypatched to a stub, because the
property under test is the manager's bookkeeping, not a subprocess handshake —
and a test that shelled out would be slow, flaky, and would still not prove the
bookkeeping. `test_mcp_timeout_bounds.py` covers the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui.mcp_client import MCPManager


class StubServer:
    """Interface parity with MCPServer / HTTPMCPServer, and nothing else."""

    def __init__(self, name: str, cfg: dict, cwd: Path, log_handle, fail: bool = False):
        self.name = name
        self.cfg = cfg
        self.tools: list[dict] = []
        self.stopped = 0
        self._fail = fail

    def start(self) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.tools = [{"name": "alpha"}, {"name": "beta"}]

    def stop(self) -> None:
        self.stopped += 1


def _write_config(root: Path, servers: dict) -> None:
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A manager over a tmp root whose servers are stubs."""
    built: list[StubServer] = []

    def _build(self, name, sc):
        srv = StubServer(name, sc, self.root, None, fail=bool(sc.get("_fail")))
        built.append(srv)
        return srv

    monkeypatch.setattr(MCPManager, "_build", _build)
    m = MCPManager(tmp_path)
    m.built = built                      # test-visible construction log
    return m


# ── load() keeps its old contract ───────────────────────────────────────────
def test_load_starts_enabled_and_records_what_the_files_declare(mgr, tmp_path):
    """Boot behaviour is unchanged, and `configs` now remembers the disabled one.

    Before the split, `off` was indistinguishable from `not declared`: both were
    simply missing from `servers`. The second assertion is the whole point — the
    disabled server must still be NAMEABLE so a UI can offer to start it.
    """
    _write_config(tmp_path, {"a": {"command": "x"}, "b": {"command": "y", "disabled": True}})
    mgr.load()
    assert set(mgr.servers) == {"a"}
    assert set(mgr.configs) == {"a", "b"}


def test_load_survives_one_bad_server(mgr, tmp_path):
    """One server that cannot start must not cost the others, and must be recorded."""
    _write_config(tmp_path, {"good": {"command": "x"}, "bad": {"command": "y", "_fail": True}})
    mgr.load()
    assert set(mgr.servers) == {"good"}
    assert "bad" in mgr.failures
    assert "boom" in mgr.failures["bad"]


# ── connect ─────────────────────────────────────────────────────────────────
def test_connect_unknown_name_names_the_files_it_looked_in(mgr, tmp_path):
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.reload_configs()
    err = mgr.connect("nope")
    assert err is not None
    assert "nope" in err and "mcp.json" in err


def test_connect_is_idempotent(mgr, tmp_path):
    """A second connect must not build a second process for the same name.

    The UI has a Connect button that can be pressed twice, and the agent can
    call the verb; without this guard the first server object would be dropped
    on the floor still running — a leak with no handle left to stop it.
    """
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    assert len(mgr.built) == 1
    assert mgr.connect("a") is None
    assert len(mgr.built) == 1


def test_a_later_success_clears_the_recorded_failure(mgr, tmp_path):
    """A stale error is worse than none: it accuses a server that now works."""
    _write_config(tmp_path, {"a": {"command": "x", "_fail": True}})
    mgr.load()
    assert "a" in mgr.failures
    mgr.configs["a"] = {"command": "x"}          # the user fixed the config
    assert mgr.connect("a") is None
    assert "a" not in mgr.failures


# ── disconnect ──────────────────────────────────────────────────────────────
def test_disconnect_stops_it_but_leaves_it_declared(mgr, tmp_path):
    """🔴 THE CORE OF THE FEATURE. Stopped must remain LISTABLE.

    `servers` loses it and `configs` keeps it, so describe() can still offer a
    row with a Connect action. Asserting the state string as well as the dicts
    is deliberate: the dicts are the mechanism, the row is what the user sees.
    """
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    srv = mgr.servers["a"]
    assert mgr.disconnect("a") is True
    assert srv.stopped == 1
    assert "a" not in mgr.servers
    assert "a" in mgr.configs
    assert [r["state"] for r in mgr.describe()] == ["stopped"]


def test_disconnect_does_not_rewrite_the_config(mgr, tmp_path):
    """A runtime stop must not persist. Persistence is mcp_disabled_servers' job.

    If disconnect quietly wrote `disabled: true`, a user who stopped a server to
    try something would find it still off after a restart, with no memory of
    having asked for that.
    """
    _write_config(tmp_path, {"a": {"command": "x"}})
    before = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    mgr.load()
    mgr.disconnect("a")
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == before


def test_disconnect_of_something_not_running_is_false_not_an_error(mgr, tmp_path):
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.reload_configs()
    assert mgr.disconnect("a") is False


# ── reconnect ───────────────────────────────────────────────────────────────
def test_reconnect_builds_a_fresh_object(mgr, tmp_path):
    """🔴 NEVER start() THE STOPPED OBJECT AGAIN.

    Both transports carry per-connection state that stop() does not reset — the
    stdio one holds a dead Popen, a finished reader thread, a frame queue and
    the `_pending` map. Restarting in place hands the new process the old one's
    leftovers, and the failure would look like a protocol bug rather than a
    lifecycle one. Identity is the only assertion that can see this.
    """
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    first = mgr.servers["a"]
    assert mgr.reconnect("a") is None
    second = mgr.servers["a"]
    assert second is not first
    assert first.stopped == 1


def test_reconnect_a_stopped_server_just_starts_it(mgr, tmp_path):
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    mgr.disconnect("a")
    assert mgr.reconnect("a") is None
    assert "a" in mgr.servers


# ── describe ────────────────────────────────────────────────────────────────
def test_describe_reports_transport_by_shape_not_by_declared_type(mgr, tmp_path):
    """`url` without `command` is HTTP — the same rule _build dispatches on.

    Keyed off shape rather than the optional `"type": "http"` field, because
    that field is a Claude Code convention and entries in the wild omit it.
    """
    _write_config(tmp_path, {
        "web": {"url": "http://localhost:7423/mcp"},
        "proc": {"command": "python", "args": ["-m", "srv"]},
    })
    mgr.load()
    rows = {r["name"]: r for r in mgr.describe()}
    assert rows["web"]["transport"] == "http"
    assert rows["web"]["target"] == "http://localhost:7423/mcp"
    assert rows["proc"]["transport"] == "stdio"
    assert rows["proc"]["target"] == "python"


def test_describe_counts_tools_and_carries_the_error(mgr, tmp_path):
    _write_config(tmp_path, {"ok": {"command": "x"}, "bad": {"command": "y", "_fail": True}})
    mgr.load()
    rows = {r["name"]: r for r in mgr.describe()}
    assert rows["ok"]["state"] == "connected" and rows["ok"]["tools"] == 2
    assert rows["bad"]["state"] == "failed" and "boom" in rows["bad"]["error"]


def test_describe_still_shows_a_server_deleted_from_the_file_while_running(mgr, tmp_path):
    """⚠️ THE UNION, NOT JUST THE CONFIGS.

    Edit the file to drop a server and re-read: the process is still up. If
    describe() listed only `configs`, the UI would show nothing while the thing
    was still in the user's process list and still serving tools to the model —
    visible everywhere except the screen that exists to manage it.
    """
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    _write_config(tmp_path, {})
    mgr.reload_configs()
    rows = {r["name"]: r for r in mgr.describe()}
    assert rows["a"]["state"] == "orphan"
    assert mgr.disconnect("a") is True


def test_reload_configs_does_not_touch_running_servers(mgr, tmp_path):
    """Reading is not reconciling. `/mcp list` must not restart anything."""
    _write_config(tmp_path, {"a": {"command": "x"}})
    mgr.load()
    srv = mgr.servers["a"]
    _write_config(tmp_path, {"a": {"command": "DIFFERENT"}})
    mgr.reload_configs()
    assert mgr.servers["a"] is srv
    assert srv.stopped == 0
