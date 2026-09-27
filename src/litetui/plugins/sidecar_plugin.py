"""Explicit, opt-in native *preview* command; working editors stay Textual."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from litetui import paths, runtime_log, settings_runtime
from litetui.plugins import PluginManifest
from litetui.sidecar_dispatch import SettingsPatchDispatcher
from litetui.sidecar_jobs import create_job, public_jobs
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
                           models=list(app.available_models), model_id=app.model_id,
                           launch=launch_overrides(app))


def launch_overrides(app) -> dict:
    """Fields this process was started with (--backend, --model, ...), and the
    value in effect: the TUI shows these, the settings service cannot see them."""
    return {key: getattr(app.settings, key) for key in getattr(app, "_invocation_saved_values", {})}


def _apply_and_refresh(app, payload: dict) -> dict:
    """The existing write contract (sidecar_patch), plus a fresh snapshot so the
    page's next edit carries current revisions instead of stale ones."""
    result = apply_patch(app, payload)
    result["snapshot"] = settings_snapshot(app)
    return result


def _requested_snapshot(app) -> dict:
    """The page's settings_request: current settings, read when asked, so a
    switch to Settings shows what the TUI holds now, not what it held at launch."""
    if getattr(app, "convo_dir", None) is None:
        return {"error": "No conversation yet; send a message first."}
    return {"snapshot": settings_snapshot(app)}


def _open_background(app, owner: SidecarWindow, view: str, fallback=None) -> None:
    """Launch or switch the native window off the UI thread. With `fallback`
    (/settings), a window that cannot open hands over to Textual."""
    try:
        if view == "settings" and getattr(app, "convo_dir", None) is not None:
            snapshot = settings_snapshot(app)
            opened = owner.open_settings_snapshot(snapshot)
        elif view in {"calendar", "job", "timeline"} and hasattr(app, "jobs"):
            # The monitor may update in-memory jobs; capture its state on the UI thread.
            snapshot = app.call_from_thread(lambda: public_jobs(app.jobs, app))
            opened = owner.open_jobs_snapshot(view, snapshot)
        else:
            opened = owner.open(view)
        if opened:
            app.call_from_thread(app.system_message, (
                "[sidecar] Native settings opened; edits there save to this instance." if view == "settings"
                else f"[sidecar] Native {view} preview opened; its editor remains Textual."))
        elif fallback is not None:
            app.call_from_thread(fallback)
        else:
            app.call_from_thread(app.system_message, "[sidecar] Preview unavailable; use Textual /settings, /calendar or /job.")
    except Exception as exc:  # noqa: BLE001 - worker failure must be visible, never take down UI
        runtime_log.record_error("sidecar_open_failed", exc=exc, site="sidecar_plugin._open_background",
                                 component="sidecar", view=view)
        app.call_from_thread(app.system_message, f"[sidecar] Preview failed ({type(exc).__name__}); use Textual instead.")
        if fallback is not None:
            app.call_from_thread(fallback)


def _warn(app, message: str) -> None:
    """Why the window did not open: in the transcript, and in the runtime log.
    Called on the launch thread only (SidecarWindow.open runs there)."""
    runtime_log.record_error("sidecar_warning", detail=message, site="sidecar_launch", component="sidecar")
    app.call_from_thread(app.system_message, f"[sidecar] {message}")


def _owner(app) -> SidecarWindow:
    owner = getattr(app, "_sidecar_preview", None)
    if owner is None:
        owner = _new_window(app)
        owner.settings_write = True
        owner.jobs_write = True  # T1082 R4: the page creates cron jobs through the parent
        owner.on_event = SettingsPatchDispatcher(app, owner, apply=_apply_and_refresh,
                                                 snapshot=_requested_snapshot, create_job=create_job)
        owner.warn = lambda message: _warn(app, message)
        app._sidecar_preview = owner
    return owner


def _launch(app, view: str, fallback=None) -> None:
    owner = _owner(app)
    # A command never awaits the child handshake or a failed process termination.
    threading.Thread(target=lambda: _open_background(app, owner, view, fallback),
                     name="sidecar-preview", daemon=True).start()


def open_preferred(app, view: str, textual) -> None:
    """/settings: the native window when Settings -> Interface
    "Prefer optional native sidecar" is on, else `textual()`. A window that
    cannot launch falls back to `textual()` after saying why."""
    if not app.settings.sidecar_enabled:
        textual()
        return
    _launch(app, view, fallback=textual)


def _handle(app, name: str, arg: str) -> None:
    view = arg.strip().lower() or "timeline"
    if view not in VIEWS:
        app.system_message("Usage: /sidecar [timeline|calendar|settings|job] (preview only)")
        return
    if not app.settings.sidecar_enabled:
        app.system_message("[sidecar] Preview disabled; enable it under Settings → Interface first.")
        return
    _launch(app, view)


def _register(ctx) -> None:
    ctx.command(("/sidecar",), _handle, palette="Native sidecar preview",
                help="Open the native sidecar; its settings view edits this instance's settings.",
                group="app", order=12)


PLUGIN = PluginManifest(id="sidecar-preview", register=_register)
