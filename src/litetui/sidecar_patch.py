"""Validate native settings edits, then reuse the Textual settings save adapter."""
from __future__ import annotations

from copy import copy

from litetui import settings_runtime
from litetui.settings_apply import RuntimeSettingStatus, SettingsSaveResult
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_ui_adapter import SettingsUiAdapter
from litetui.sidecar_settings import is_sensitive

MAX_CHANGES = 32
_NO_CHANGE = object()


def apply_patch(app, payload: dict) -> dict:
    """Run on the Textual thread; the sidecar never writes settings itself."""
    if not isinstance(payload, dict) or not (
            {"changes", "expected_revisions"} <= set(payload) <= {"changes", "expected_revisions", "confirm_cache"}):
        raise ValueError("Invalid settings patch")
    changes, expected = payload["changes"], payload["expected_revisions"]
    confirmed = payload.get("confirm_cache", False)
    if type(confirmed) is not bool:
        raise ValueError("Invalid cache confirmation")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ValueError("Invalid settings patch count")
    if not isinstance(expected, dict) or set(expected) != {"global", "conversation"} or not all(
        isinstance(value, str) for value in expected.values()
    ):
        raise ValueError("Invalid settings revisions")
    directory = getattr(app, "convo_dir", None)
    if directory is None:
        raise ValueError("Settings patch requires a conversation")
    service = settings_runtime.service_for(app)
    snapshot = service.snapshot(directory.name)
    launch = getattr(app, "_invocation_saved_values", {})
    keys = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value", "scope"}:
            raise ValueError("Invalid settings change")
        key = change["key"]
        from litetui.settings_screen import NOT_A_SETTINGS_CONTROL

        if (not isinstance(key, str) or key not in SETTING_SPECS or is_sensitive(key)
                or key in NOT_A_SETTINGS_CONTROL):
            raise ValueError("Setting not editable from sidecar")
        if key in keys:
            raise ValueError("Duplicate settings field")
        keys.add(key)
        if change["scope"] != SETTING_SPECS[key].scope.value:
            raise ValueError("Invalid settings scope")
        # A field set at launch (--backend ...) is edited against the value in
        # effect, as the TUI's /settings shows it, not against the saved one.
        current = getattr(app.settings, key) if key in launch else getattr(snapshot.saved, key)
        if current == change["value"]:
            raise ValueError("No change to saved setting")
        if key not in launch and getattr(snapshot.effective, key) != getattr(snapshot.saved, key):
            raise ValueError("Overridden setting not editable from sidecar")
    if snapshot.revisions != expected:
        return {"saved": False, "conflict": True, "revisions": snapshot.revisions,
                "persistence": [], "runtime": []}
    # An effort change on a live Claude session costs a full uncached re-read.
    # The TUI asks before the next send; the sidecar asks BEFORE saving, with
    # the same words (claude_cache), and Cancel means nothing was written.
    from litetui import claude_cache, claude_turn
    effort = next((c["value"] for c in changes if c["key"] == "thinking_level"), _NO_CHANGE)
    warning = None if effort is _NO_CHANGE else claude_turn.effort_change_warning(app, effort)
    if warning is not None and not confirmed:
        return {"saved": False, "conflict": False, "revisions": snapshot.revisions,
                "cache_warning": {"kind": warning[0], "text": warning[1] + claude_cache.WARNING_TAIL},
                "persistence": [], "runtime": []}
    adapter = SettingsUiAdapter(
        app.settings,
        snapshot_provider=lambda: snapshot,
        save_patch=lambda requested, revisions: service.save_patch(directory.name, requested, revisions),
        runtime_apply=lambda requested, result: settings_runtime.apply_saved_result(app, requested, result),
    )
    # Choosing the SAVED value for a launch-set field writes nothing: it only
    # releases the launch value, so the saved preference governs from here on.
    released = [c for c in changes if c["key"] in launch and c["value"] == getattr(snapshot.saved, c["key"])]
    writes = [c for c in changes if c not in released]
    target = adapter.effective
    for change in writes:
        setattr(target, change["key"], change["value"])
    result = adapter.save(target) if writes else SettingsSaveResult((), ())
    if result is None:
        raise RuntimeError("Settings service unavailable")
    result = SettingsSaveResult(result.persistence, result.runtime + _release(app, released))
    if warning is not None and result.fully_saved:
        # Confirmed here, so the next send does not ask again for this change.
        app._claude_cache_preapproved = ("effort", claude_turn.effort_for(app) or "default")
    # Do not echo requested/effective values: fields and outcomes are sufficient.
    persistence = [{"destination": "conversation" if item.scope == "conversation" else "global",
                    "scope": item.scope, "saved": item.saved, "error": item.error,
                    "revision": item.revision, "fields": list(item.fields)}
                   for item in result.persistence]
    runtime = [{"field": item.field, "status": item.status, "action": item.action,
                "reason": item.reason} for item in result.runtime]
    return {"saved": result.fully_saved if writes else True, "conflict": any(
        "Stale" in (item.error or "") for item in result.persistence),
        "revisions": service.snapshot(directory.name).revisions,
        "persistence": persistence, "runtime": runtime}


def _release(app, released) -> tuple:
    """Drop the launch value for fields set back to their saved preference,
    exactly as a saved edit does (settings_runtime.retire_invocation)."""
    statuses = []
    for change in released:
        key, spec = change["key"], SETTING_SPECS[change["key"]]
        candidate = copy(app.settings)
        setattr(candidate, key, change["value"])
        settings_runtime.retire_invocation(app, candidate, {key})
        if spec.apply_timing in ("restart", "reconnect", "reload"):
            statuses.append(RuntimeSettingStatus(key, spec.scope.value, "pending", spec.apply_timing,
                                                 change["value"], getattr(app.settings, key),
                                                 f"Launch value released; saved preference applies on {spec.apply_timing}"))
        else:
            setattr(app.settings, key, change["value"])
            statuses.append(RuntimeSettingStatus(key, spec.scope.value, "applied",
                                                 requested=change["value"], effective=change["value"]))
    return tuple(statuses)
