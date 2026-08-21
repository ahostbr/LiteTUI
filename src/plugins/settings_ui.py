"""/settings — the settings screen's command surface.

The Settings dataclass, its persistence, and the apply-mapping
(_on_settings_saved with its deferred-list doctrine) stay app-owned:
single owner of a fact many plugins read. This is the door to the screen.
"""
from settings_screen import SettingsScreen

from plugins import PluginManifest


def _cmd_settings(app, name: str, arg: str) -> None:
    app.push_screen(
        SettingsScreen(
            app.settings,
            models=app.available_models,
            mcp_servers=app._mcp_server_names(),
        ),
        app._on_settings_saved,
    )


def _register(ctx) -> None:
    ctx.command(
        ("/settings", "/config", "/set"), _cmd_settings,
        palette="Settings",
        help="Every knob, scrollable (/settings)",
    )


PLUGIN = PluginManifest(id="settings-ui", register=_register)
