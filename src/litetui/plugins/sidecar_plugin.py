"""Explicit, opt-in native *preview* command; working editors stay Textual."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from litetui import paths, settings_runtime
from litetui.plugins import PluginManifest
from litetui.sidecar_dispatch import SettingsPatchDispatcher
from litetui.sidecar_jobs import public_jobs
from litetui.sidecar_launch import SidecarWindow
from litetui.sidecar_patch import apply_patch
from litetui.sidecar_settings import public_snapshot

VIEWS = frozenset({"timeline", "calendar", "settings", "job"})


def _new_window(app) -> SidecarWindow:
    executable = Path(os.environ.get("LITETUI_SIDECAR_EXE") or
                      paths.data_root() / "bin" / ("litetui-sidecar.exe" if sys.platform == "win32" else "litetui-sidecar"))
    return SidecarWindow(executable)


def settings_snapshot(app) -> dict:
    """THIS instance's settings, as the TUI's /settings shows them on its backend.

    Always read from disk through the settings service, scoped to this app's own
    conversation, so a sidecar never sees or edits another instance's state.
    """
    from litetui.plugins.model_switch import backend_rows

    return public_snapshot(settings_runtime.service_for(app).snapshot(app.convo_dir.name),
                           backend=app.backend, backends=backend_rows(app),
                           models=list(app.available_models), model_id=app.model_id)


def _apply_and_refresh(app, payload: dict) -> dict:
    """The existing write contract (sidecar_patch), plus a fresh snapshot so the
    page's next edit carries current revisions instead of stale ones."""
    result = apply_patch(app, payload)
    result["snapshot"] = settings_snapshot(app)
    return result


def _open_background(app, owner: SidecarWindow, view: str) -> None:
    try:
        if view == "settings" and getattr(app, "convo_dir", None) is not None:
            snapshot = settings_snapshot(app)
            opened = owner.open_settings_snapshot(snapshot)
        elif view in {"calendar", "job", "timeline"} and hasattr(app, "jobs"):
            # The monitor may update in-memory jobs; capture its state on the UI thread.
            snapshot = app.call_from_thread(lambda: public_jobs(app.jobs))
            opened = owner.open_jobs_snapshot(view, snapshot)
        else:
            opened = owner.open(view)
        if opened:
            app.call_from_thread(app.system_message, (
                "[sidecar] Native settings opened; edits there save to this instance." if view == "settings"
                else f"[sidecar] Native {view} preview opened; its editor remains Textual."))
        else:
            app.call_from_thread(app.system_message, "[sidecar] Preview unavailable; use Textual /settings, /calendar or /job.")
    except Exception as exc:  # noqa: BLE001 - worker failure must be visible, never take down UI
        app.call_from_thread(app.system_message, f"[sidecar] Preview failed ({type(exc).__name__}); use Textual instead.")


def _handle(app, name: str, arg: str) -> None:
    view = arg.strip().lower() or "timeline"
    if view not in VIEWS:
        app.system_message("Usage: /sidecar [timeline|calendar|settings|job] (preview only)")
        return
    if not app.settings.sidecar_enabled:
        app.system_message("[sidecar] Preview disabled; enable it under Settings → Interface first.")
        return
    owner = getattr(app, "_sidecar_preview", None)
    if owner is None:
        owner = _new_window(app)
        owner.settings_write = True
        owner.on_event = SettingsPatchDispatcher(app, owner, apply=_apply_and_refresh)
        app._sidecar_preview = owner
    # A command never awaits the child handshake or a failed process termination.
    threading.Thread(target=lambda: _open_background(app, owner, view),
                     name="sidecar-preview", daemon=True).start()


def _register(ctx) -> None:
    ctx.command(("/sidecar",), _handle, palette="Native sidecar preview",
                help="Open the native sidecar; its settings view edits this instance's settings.",
                group="app", order=12)


PLUGIN = PluginManifest(id="sidecar-preview", register=_register)
