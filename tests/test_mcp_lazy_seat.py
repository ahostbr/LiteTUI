"""Seat selection and lazy inventory; no real project server or model is run."""
import json
import os

import pytest

from litetui.codex_mcp import config_overrides
from litetui.mcp_cache import ToolCache, cache_key
from litetui.mcp_client import MCPManager
from litetui.mcp_seat import server_selection
from litetui.plugins import PluginRegistry
from litetui.plugins.tool_search import _run

pytestmark = pytest.mark.real_mcp_load

TOOLS = [{"name": "inspect", "description": "Inspect things", "inputSchema": {"type": "object"}}]


class FakeServer:
    def __init__(self, name, starts):
        self.name, self.starts = name, starts
        self.tools = TOOLS

    def start(self):
        self.starts.append(self.name)

    def stop(self):
        pass

    def call(self, tool, args):
        return json.dumps([tool, args])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = {"command": "nonexistent-test-python", "args": ["server.py"]}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": cfg, "other": cfg}}))
    starts = []
    monkeypatch.setattr(MCPManager, "_build", lambda self, name, cfg: FakeServer(name, starts))
    return tmp_path, cfg, starts


@pytest.mark.parametrize("value,expected", [(None, None), ("all", None), ("none", frozenset()),
                                          ("srv, other", frozenset({"srv", "other"}))])
def test_selection(value, expected):
    assert server_selection(value) == expected


@pytest.mark.parametrize("value", ["", "srv,", "none,srv"])
def test_bad_selection(value):
    with pytest.raises(ValueError):
        server_selection(value)


def test_scope_both_paths_and_no_shared_mutation(setup):
    root, cfg, starts = setup
    original = (root / ".mcp.json").read_bytes()
    manager = MCPManager(root, seat_servers=frozenset({"srv"}))
    manager.load()
    assert starts == ["srv"]
    assert "excluded" in {r["state"] for r in manager.describe()}
    assert "excluded" in manager.connect("other")
    overrides = config_overrides(root, frozenset())
    assert "mcp_servers.srv.enabled=false" in overrides
    assert "mcp_servers.other.enabled=false" in overrides
    assert (root / ".mcp.json").read_bytes() == original
    manager.stop_all()


def test_cache_invalidates_launch_inputs_and_file_stamp(tmp_path):
    script = tmp_path / "server.py"
    script.write_text("one")
    cfg = {"command": "test-python", "args": ["server.py"]}
    cache = ToolCache(tmp_path / "cache")
    cache.write(tmp_path, "srv", cfg, TOOLS)
    assert cache.read(tmp_path, "srv", cfg) == TOOLS
    key = cache_key(tmp_path, cfg)
    stamp = script.stat().st_mtime_ns
    os.utime(script, ns=(stamp + 1_000_000, stamp + 1_000_000))
    assert cache_key(tmp_path, cfg) != key
    assert cache.read(tmp_path, "srv", cfg) is None
    assert cache_key(tmp_path, dict(cfg, args=["other.py"])) != key
    assert cache_key(tmp_path, dict(cfg, command="other-python")) != key


def registry(manager):
    reg = PluginRegistry()
    reg.defer_dynamic = True
    reg.add_dynamic("mcp", manager.tool_specs, manager.dispatch_for)
    return reg


def test_warm_cache_idle_until_schema_load(setup):
    root, cfg, starts = setup
    manager = MCPManager(root, seat_servers=frozenset({"srv"}), lazy=True)
    manager.cache.write(root, "srv", cfg, TOOLS)
    manager.load()
    reg = registry(manager)
    assert reg.deferred_specs()[0]["function"]["name"] == "mcp__srv__inspect"
    assert manager.dispatch_for("mcp__srv__inspect") is not None
    assert starts == []
    assert manager.describe()[0]["state"] == "not started (lazy)"
    assert "Loaded 1" in _run(reg, {"query": "select:mcp__srv__inspect"}, manager)
    assert starts == ["srv"]
    assert manager.describe()[0]["state"] == "connected"
    assert reg.dispatch_for("mcp__srv__inspect")({"a": 1}) == '["inspect", {"a": 1}]'
    manager.stop_all()


def test_cold_cache_discovers_only_explicit_server(setup):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.load()
    reg = registry(manager)
    assert "no deferred" in _run(reg, {"query": "inspect"}, manager)
    assert starts == []
    assert "Loaded 1" in _run(reg, {"query": "srv"}, manager)
    assert starts == ["srv"]
    assert manager.cache.read(root, "srv", cfg) == TOOLS
    manager.stop_all()


def test_first_call_starts_and_disconnect_revokes_lazy_access(setup):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.load()
    call = manager.dispatch_for("mcp__srv__inspect")
    assert starts == []
    assert call({}) == '["inspect", {}]'
    assert starts == ["srv"]
    manager.disconnect("other")
    assert manager.dispatch_for("mcp__other__inspect") is None
    manager.stop_all()


@pytest.mark.parametrize("selection,expected", [("none", frozenset()), ("srv", frozenset({"srv"})),
                                             ("all", None)])
def test_cli_passes_invocation_only_selection(tmp_path, monkeypatch, selection, expected):
    import sys
    from types import SimpleNamespace
    from litetui import cli, shared_state
    seen = {}
    class App:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def run(self, **kwargs):
            pass
    monkeypatch.setitem(sys.modules, "litetui.app", SimpleNamespace(
        LiteTUI=App, wants_ansi_fallback=lambda: False))
    monkeypatch.setitem(sys.modules, "litetui.image_viewer", SimpleNamespace(init_image_backend=lambda: None))
    monkeypatch.setattr(shared_state, "check_data_version", lambda root: None)
    monkeypatch.setattr(sys, "argv", ["litetui", "--mcp-servers", selection, "--mcp-start", "eager"])
    cli.main()
    assert seen["mcp_servers"] == expected
    assert seen["mcp_lazy"] is False


def test_failed_lazy_load_not_reported_as_loaded(setup, monkeypatch):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.cache.write(root, "srv", cfg, TOOLS)
    manager.load()
    monkeypatch.setattr(manager, "connect", lambda name: "test startup failed")
    reg = registry(manager)
    result = _run(reg, {"query": "select:mcp__srv__inspect"}, manager)
    assert result.startswith("[error]") and "test startup failed" in result
    assert reg.activated == set()
    assert starts == []


def test_disabled_after_inventory_read_cannot_start(setup):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.cache.write(root, "srv", cfg, TOOLS)
    manager.load()
    call = manager.dispatch_for("mcp__srv__inspect")
    manager.enabled = lambda name: False
    assert manager.tool_specs() == []
    assert "excluded" in call({})
    assert starts == []


def test_none_disables_native_http_without_registering_stdio(setup):
    root, cfg, starts = setup
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"http": {"url": "http://invalid/mcp"}}}))
    assert "mcp_servers.http.enabled=false" in config_overrides(root, frozenset())
    assert "url" in config_overrides(root, frozenset({"http"}))[-1]


def test_disconnect_stays_stopped_across_inventory_reload(setup):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.load()
    call = manager.dispatch_for("mcp__srv__inspect")
    manager.disconnect("srv")
    manager.reload_configs()  # /mcp list/dialog performs this read
    assert "srv" not in manager.lazy_names()
    assert manager.describe()[0]["state"] == "stopped"
    assert "disconnected" in call({})
    assert starts == []
    assert manager.connect("srv") is None
    assert starts == ["srv"]
    manager.stop_all()


@pytest.mark.parametrize("change", ["remove", "disable"])
def test_reconcile_revokes_pending_lazy_inventory(setup, change):
    root, cfg, starts = setup
    manager = MCPManager(root, lazy=True)
    manager.cache.write(root, "srv", cfg, TOOLS)
    manager.load()
    servers = {"other": cfg}
    if change == "disable":
        servers["srv"] = dict(cfg, disabled=True)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    manager.reconcile()
    assert "srv" not in manager.lazy_names()
    assert manager.dispatch_for("mcp__srv__inspect") is None
    assert manager.tool_specs() == []
    assert starts == []
    manager.stop_all()


def test_cache_stamps_args_in_configured_server_cwd(tmp_path):
    cwd = tmp_path / "server"
    cwd.mkdir()
    script = cwd / "entry.py"
    script.write_text("one")
    cfg = {"command": "test-python", "args": ["entry.py"], "cwd": str(cwd)}
    key = cache_key(tmp_path, cfg)
    script.write_text("changed-size")
    assert cache_key(tmp_path, cfg) != key
