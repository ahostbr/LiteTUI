"""Read-only projection of authoritative settings for the trusted native preview."""
from __future__ import annotations

import re
from copy import deepcopy

from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsSnapshot

_SENSITIVE_NAME = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)


def is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name) or SETTING_SPECS.get(name) and SETTING_SPECS[name].sensitive)


def _control(backend, key):
    """What the TUI settings screen does with this field on `backend`.

    The SAME call (codex_settings.control) SettingsScreen._backend_control makes:
    its help replaces the field's help text, and a non-editable one disables the
    row. Ryan 2026-09-24: "the sidecar is a gui representation of the settings
    menu" — so it carries the menu's per-backend meaning, not a copy of it.
    """
    from litetui.codex_settings import control

    found = control(backend, key)
    if found is None:
        return None
    return {"owner": found.owner, "help": found.help, "editable": found.editable}


def public_snapshot(snapshot: SettingsSnapshot, *, backend=None, backends=(), models=(),
                    model_id: str | None = None, launch: dict | None = None) -> dict:
    """Return a detached wire payload; no write capability or secret-shaped fields.

    With `backend` (the app's live backend), each field also carries the TUI's
    per-backend control, and a `choices` block lists what the TUI's pickers
    offer: the engines with their /backend readiness marks (`backends`, built by
    model_switch.backend_rows), the models the app already holds (`models`,
    never a fresh network list), and the backend's own thinking levels.

    `launch` holds what this process was STARTED with (`--backend`, `--model`,
    ...: the app's invocation overrides, key -> the value in effect). The
    settings service only knows environment overrides, so without this a
    `--backend cline` instance showed its saved engine. Those fields are
    effective here with source "override", as the TUI shows them.
    """
    launch = launch or {}
    fields = {}
    for key, spec in SETTING_SPECS.items():
        if is_sensitive(key):
            continue
        saved = deepcopy(getattr(snapshot.saved, key))
        effective = deepcopy(launch[key] if key in launch else getattr(snapshot.effective, key))
        fields[key] = {"scope": spec.scope.value, "apply_timing": spec.apply_timing,
                       "saved": saved, "effective": effective,
                       "source": "override" if key in launch or saved != effective else "saved"}
        if fields[key]["source"] == "override":
            fields[key]["override_by"] = "launch" if key in launch else "environment"
        if backend is not None:
            from litetui.settings_screen import NOT_A_SETTINGS_CONTROL

            fields[key]["control"] = _control(backend, key)
            # /settings shows no control for these; neither does the sidecar.
            fields[key]["settings_control"] = key not in NOT_A_SETTINGS_CONTROL
    result = {"revisions": dict(snapshot.revisions), "fields": fields}
    if backend is not None:
        from litetui.settings_screen import thinking_choices

        rows = thinking_choices(backend, model_id, fields["thinking_level"]["effective"])
        result["backend"] = getattr(backend, "name", None)
        result["choices"] = {
            "backend": [{"value": value, "label": label} for value, label in backends],
            "default_model": list(models),
            "thinking_level": [{"value": value, "label": label} for label, value in rows],
        }
    return result
