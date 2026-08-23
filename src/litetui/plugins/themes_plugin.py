"""Themes — registration and the saved choice, applied at activate time.

Moved verbatim from on_mount. Runs inside the same synchronous on_mount pass
(the activate loop), so the choice still lands before first paint. The
_register_custom_themes method stays app-owned — the settings-save path
calls it too, and one owner per fact includes methods.
"""
from litetui import themes as themes_mod
from litetui.plugins import PluginManifest


def _activate(app) -> None:
    # LiteSuite's palette, available beside Textual's built-ins. Register
    # BEFORE applying the saved choice, or a saved LiteSuite theme would
    # not resolve on boot and fall back.
    for t in themes_mod.ALL_THEMES.values():
        app.register_theme(t)
    # Stripped by ruling (2026-08-21): no light themes in the picker.
    # Unregistered AFTER our set registers, BEFORE the saved choice
    # applies — a saved light name then falls into the except below.
    for name in themes_mod.LIGHT_BUILTINS:
        app.unregister_theme(name)
    app._register_custom_themes()
    try:
        app.theme = app.settings.theme_name
    except Exception:
        app.theme = "textual-dark"  # unknown name in settings: fall back


PLUGIN = PluginManifest(id="themes", activate=_activate)
