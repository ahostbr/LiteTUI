"""Owned runtime inspection projects bounded cached status without running MCP."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import native_inspection
from litetui.version import __version__

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio


def request(app):
    return {"type": native_inspection.OPERATION, "id": "runtime-status", "version": 1,
            **work.identity(app), "work_instance": work.bridge(app).instance}


async def test_registered_glassbox_status_uses_only_existing_local_state(armed, monkeypatch):
    app = armed
    from litetui.plugins import glassbox_plugin as glassbox

    def forbidden(*args, **kwargs):
        raise AssertionError("Status attempted a Glassbox side effect")

    assert app.plugins.commands["glassbox"].handler is glassbox._cmd_glassbox
    monkeypatch.setattr(glassbox.SERVER, "start", forbidden)
    monkeypatch.setattr(glassbox.SERVER, "stop", forbidden)
    monkeypatch.setattr(glassbox, "_ENABLED", True)
    monkeypatch.setattr(glassbox.SERVER, "_httpd",
                        SimpleNamespace(server_address=("127.0.0.1", 57491)))
    command = {**request(app), "type": native_inspection.WITH_GLASSBOX}
    result = await published_rpc(app, monkeypatch, command)
    assert result["diagnostics"]["glassbox"] == {"recording": True, "port": 57491}
    legacy = await published_rpc(app, monkeypatch, request(app))
    assert "glassbox" not in legacy["diagnostics"]
    monkeypatch.setattr(glassbox, "_ENABLED", False)
    monkeypatch.setattr(glassbox.SERVER, "_httpd", None)
    result = await published_rpc(app, monkeypatch, command)
    assert result["diagnostics"]["glassbox"] == {"recording": False, "port": None}


async def test_glassbox_observations_require_the_registered_builtin_and_loopback(armed, monkeypatch):
    app = armed
    from litetui.plugins import glassbox_plugin as glassbox
    command = {**request(app), "type": native_inspection.WITH_GLASSBOX}
    monkeypatch.setattr(glassbox.SERVER, "_httpd",
                        SimpleNamespace(server_address=("0.0.0.0", 7580)))
    with pytest.raises(ValueError, match="unexpected address"):
        native_inspection.status(app, command)
    monkeypatch.setitem(app.plugins.commands, "glassbox", SimpleNamespace(owner="third-party", handler=lambda *args: None))
    assert (await published_rpc(app, monkeypatch, command))["diagnostics"]["glassbox"] is None


async def test_real_rpc_projects_only_current_cached_metadata(armed, monkeypatch):
    app = armed
    manager = app.mcp
    secret = "private-credential-fixture"
    manager.configs = {"running": {"command": secret, "args": [secret], "env": {"token": secret}},
                       "lazy": {"command": secret}, "failed": {"url": secret},
                       "bad": secret, "excluded": {"command": secret}, "off": {"disabled": True},
                       "configured": {"url": secret}, "https://private-credential-fixture": {}}
    manager.servers = {"running": SimpleNamespace(tools=[secret], error=None),
                       "orphan": SimpleNamespace(tools=[secret, secret], error=None)}
    manager._lazy_pending = {"lazy"}
    manager.failures = {"failed": secret}
    manager.enabled = lambda name: name != "excluded"
    app._settings_persist_error = secret
    def forbidden(*_args, **_kwargs):
        pytest.fail("Inspection touched the manager lifecycle, raw inventory or backend diagnostics")
    for name in ("load", "reload_configs", "connect", "disconnect", "reconnect", "describe", "tool_specs", "lazy_names"):
        monkeypatch.setattr(manager, name, forbidden)
    monkeypatch.setattr(app, "_rpc_model_state", forbidden)
    before = app.convo_path.read_bytes()
    hello = await published_rpc(app, monkeypatch, {"type": "gui.hello", "id": "inspection-hello", "protocol_version": 1})
    assert native_inspection.OPERATION in hello["operations"]
    status = await published_rpc(app, monkeypatch, request(app))
    assert status.keys() == {"version", "kind", "session_id", "agent_id", "backend", "model", "work_instance",
                             "diagnostics", "mcp"}
    assert status["diagnostics"] == {"runtime_version": __version__, "protocol_version": 1,
        "conversation_persistence": "ok", "settings_persistence": "error"}
    assert secret not in str(status) and str(app.convo_dir) not in str(status)
    summary = status["mcp"]
    assert summary["available"] and not summary["complete"] and summary["remaining"] == 1
    assert all(row.keys() == {"name", "state", "transport", "tool_count"} for row in summary["servers"])
    rows = {row["name"]: (row["state"], row["transport"], row["tool_count"]) for row in summary["servers"]}
    assert rows == {"running": ("connected", "stdio", 1), "lazy": ("not started (lazy)", "stdio", 0),
                    "failed": ("failed", "http", 0), "bad": ("failed", None, 0), "excluded": ("excluded", "stdio", 0),
                    "off": ("disabled", None, 0), "configured": ("stopped", "http", 0), "orphan": ("orphan", None, 2)}
    assert app.convo_path.read_bytes() == before


async def test_large_caches_visit_only_the_prefix(armed):
    class GuardedDictionary(dict):
        def __iter__(self):
            for i, key in enumerate(super().__iter__()):
                if i >= 32:
                    pytest.fail("Inspection traversed an unbounded catalogue")
                yield key
    manager = armed.mcp
    manager.configs = GuardedDictionary({f"declared{i}": {} for i in range(1000)})
    manager.servers = GuardedDictionary({f"orphan{i}": SimpleNamespace(tools=[], error=None) for i in range(1000)})
    summary = native_inspection.status(armed, request(armed))["mcp"]
    assert not summary["complete"] and summary["remaining"] is None and len(summary["servers"]) == 32
    manager.servers.clear()
    summary = native_inspection.status(armed, request(armed))["mcp"]
    assert summary["remaining"] == 968 and len(summary["servers"]) == 32


async def test_bare_and_busy_managers_are_unavailable(armed, monkeypatch):
    manager = armed.mcp
    with manager._servers_lock:
        busy = native_inspection.status(armed, request(armed))["mcp"]
    monkeypatch.setattr(armed, "mcp", None)
    missing = native_inspection.status(armed, request(armed))["mcp"]
    assert busy == missing == {"enabled": armed.settings.mcp_enabled, "available": False, "complete": False,
                               "remaining": None, "servers": []}


async def test_exact_request_and_owned_epoch_required(armed):
    for fields in ({"session_id": "foreign"}, {"work_instance": "stale"}, {"version": True},
                   {"action": "reload"}, {"path": "injected"}, {"model": "other"}):
        with pytest.raises(ValueError):
            native_inspection.status(armed, {**request(armed), **fields})
    work.begin_close(armed)
    with pytest.raises(ValueError):
        native_inspection.status(armed, request(armed))
