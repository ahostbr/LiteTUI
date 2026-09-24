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
        servers, _errors = read_server_configs(config_files(paths.data_root()))
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
    bindings = {}
    if getattr(app, 'convo_dir', None) is not None:
        from litetui import settings_runtime
        service = settings_runtime.service_for(app)
        directory = app.convo_dir
        conversation_id = directory.name
        from litetui.settings_apply import (SettingsSaveResult,
            PersistenceDestinationResult, RuntimeSettingStatus)

        def still_current():
            return getattr(app, 'convo_dir', None) == directory

        def save_patch(changes, revisions):
            if not still_current():
                return SettingsSaveResult((PersistenceDestinationResult(
                    str(directory), 'conversation', False,
                    error='Conversation changed; close and reopen /settings.',
                    fields=tuple(change.key for change in changes)),))
            return service.save_patch(conversation_id, changes, revisions)

        def runtime_apply(requested, result):
            if not still_current():
                return SettingsSaveResult(result.persistence, tuple(
                    RuntimeSettingStatus(key, outcome.scope, 'failed', 'retry',
                        requested=getattr(requested, key),
                        reason='Conversation changed; close and reopen /settings.')
                    for outcome in result.persistence if outcome.saved
                    for key in outcome.fields))
            return settings_runtime.apply_saved_result(app, requested, result)

        bindings = {
            'snapshot_provider': lambda: settings_runtime.snapshot_with_launch(app, conversation_id),
            'save_patch': save_patch,
            'runtime_apply': runtime_apply,
        }
    present_dialog(
        app,
        partial(SettingsBody, app.settings, models, servers, sorted(loaded), remote, **bindings),
        partial(SettingsScreen, app.settings, models, servers, sorted(loaded), remote, **bindings),
        (lambda result: None) if bindings else app._on_settings_saved,
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
