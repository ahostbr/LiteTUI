"""/settings — the settings screen's command surface.

The Settings dataclass, its persistence, and the apply-mapping
(_on_settings_saved with its deferred-list doctrine) stay app-owned:
single owner of a fact many plugins read. This is the door to the screen.
"""
from litetui import paths
from litetui.settings_screen import SettingsScreen

from litetui.plugins import PluginManifest


def mcp_server_names(app) -> list[str]:
    """Server names from mcp.json, for the per-server toggles.

    Returns [] rather than raising when MCP is absent or unreadable: a
    settings screen that cannot open because an optional config file is
    malformed is worse than one that shows no MCP section.

    🔴 THE FALLBACK PATH USES `paths.ROOT`, NOT `__file__` ARITHMETIC, AND THAT
    IS THE ONE CHANGE FROM THE APP.PY ORIGINAL. It read
    `Path(__file__).resolve().parent.parent.parent / "mcp.json"`, which is the
    repo root FROM app.py and `src/mcp.json` FROM HERE -- two directories up is
    not the same place from a file one directory deeper. Carried verbatim it
    would have read a path that does not exist, been swallowed by the `except`,
    and returned [] forever: the MCP section would simply be empty, with no
    error anywhere. `paths.ROOT` is location-independent, which is what a body
    that MOVES needs.
    """
    try:
        mgr = getattr(app, "mcp", None)
        if mgr is not None and getattr(mgr, "servers", None):
            return sorted(mgr.servers.keys())
        import json as _json
        cfg = paths.ROOT / "mcp.json"
        if cfg.exists():
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or data.get("servers") or {}
            if isinstance(servers, dict):
                return sorted(servers.keys())
    except Exception:
        pass
    return []


def _cmd_settings(app, name: str, arg: str) -> None:
    app.push_screen(
        SettingsScreen(
            app.settings,
            models=app.available_models,
            mcp_servers=mcp_server_names(app),
        ),
        app._on_settings_saved,
    )


def _register(ctx) -> None:
    ctx.command(
        ("/settings", "/config", "/set"), _cmd_settings,
        palette="Settings",
        help="Every knob, in one scrollable place.",
        group="app",
        order=10,
    )


PLUGIN = PluginManifest(id="settings-ui", register=_register)
