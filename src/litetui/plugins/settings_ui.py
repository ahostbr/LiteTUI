"""/settings — the settings screen's command surface.

The Settings dataclass, its persistence, and the apply-mapping
(_on_settings_saved with its deferred-list doctrine) stay app-owned:
single owner of a fact many plugins read. This is the door to the screen.
"""
from functools import partial

from litetui import paths
from litetui import model_residency
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.side_panel import present_dialog

from litetui.plugins import PluginManifest


def mcp_server_names(app) -> list[str]:
    """Server names from mcp.json / .mcp.json, for the per-server toggles.

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
        from litetui.mcp_client import config_files, read_server_configs
        # Same discovery as MCPManager.load(): mcp.json AND .mcp.json,
        # merged with the earlier file winning a collision. A toggle list
        # that missed one of the two files would leave its servers
        # un-toggleable from /settings.
        servers, _errors = read_server_configs(config_files(paths.ROOT))
        return sorted(servers.keys())
    except Exception:
        pass
    return []


def _cmd_settings(app, name: str, arg: str) -> None:
    # Both factories from ONE set of arguments — `picker.pick`'s reason: two
    # built at two places are two chances for the sidebar and the modal to show
    # different settings.
    models = app.available_models
    servers = mcp_server_names(app)
    # T640: the Agent-loop pickers mark which models are RESIDENT, which is the
    # only set a side call may use on a local backend without loading one.
    # Read here, with the model list, so both factories get one consistent
    # answer — the same reason `models` is read once above.
    loaded, remote = model_residency.resident_models(app)
    present_dialog(
        app,
        partial(SettingsBody, app.settings, models, servers, sorted(loaded), remote),
        partial(SettingsScreen, app.settings, models, servers, sorted(loaded), remote),
        app._on_settings_saved,
    )


def _cmd_hooks(app, name: str, arg: str) -> None:
    from litetui.hooks_screen import HooksBody, HooksScreen
    present_dialog(app, HooksBody, HooksScreen, lambda result: None)


def _register(ctx) -> None:
    ctx.command(("/hooks",), _cmd_hooks, palette="Hooks", help="Configure and test lifecycle hooks.", group="app", order=11)
    ctx.command(
        ("/settings", "/config", "/set"), _cmd_settings,
        palette="Settings",
        help="Every knob, in one scrollable place.",
        group="app",
        order=10,
    )


PLUGIN = PluginManifest(id="settings-ui", register=_register)
