"""Read-only projection of authoritative settings for the trusted native preview."""
from __future__ import annotations

import re
from copy import deepcopy

from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsSnapshot

_SENSITIVE_NAME = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)


#: The free-tier keys (settings.py). The sidecar may SET or CLEAR them but is
#: only ever told whether each is set, never the value (the user 2026-09-24,
#: liteask a-29b8bd60: "a key field per source in /settings + sidecar").
SECRET_FIELDS = frozenset({"groq_api_key", "cerebras_api_key", "nvidia_api_key", "mistral_api_key",
                           "github_models_token", "openrouter_api_key", "gemini_api_key", "ollama_api_key",
                           "zai_api_key", "cloudflare_api_key", "longcat_api_key", "sealion_api_key"})


def is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name) or SETTING_SPECS.get(name) and SETTING_SPECS[name].sensitive)


def _control(backend, key):
    """What the TUI settings screen does with this field on `backend`.

    The SAME call (codex_settings.control) SettingsScreen._backend_control makes:
    its help replaces the field's help text, and a non-editable one disables the
    row. the user 2026-09-24: "the sidecar is a gui representation of the settings
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
        if key in SECRET_FIELDS:
            # Whether it is set, never the value.
            fields[key] = {"scope": spec.scope.value, "apply_timing": spec.apply_timing, "secret": True,
                           "set": bool(getattr(snapshot.effective, key)), "source": "saved"}
        elif is_sensitive(key):
            continue
        else:
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
    # UI metadata is a projection of the same model Textual searches, never a
    # second keyword/label registry maintained by the sidecar.
    from litetui.settings_ui_model import SETTINGS_SECTIONS, SETTINGS_TABS

    ui = {
        "tabs": [{"id": tab.tab_id, "label": tab.label,
                  "sections": list(tab.section_ids)} for tab in SETTINGS_TABS],
        "sections": [{"id": section.section_id, "tab": section.tab_id,
                      "title": section.title, "summary": section.summary,
                      "keywords": list(section.keywords),
                      "fields": [field.name for field in section.fields if field.name in fields]}
                     for section in SETTINGS_SECTIONS],
        "fields": {field.name: {"label": field.label,
                                "description": field.description,
                                "keywords": list(field.keywords), "key": field.name}
                   for section in SETTINGS_SECTIONS for field in section.fields
                   if field.name in fields},
    }
    # A theme picker must use the very same choices and palette as /settings;
    # a static color list in the sidecar would drift from user-created themes.
    from litetui import themes as themes_mod
    from litetui.settings_screen import _theme_choices

    def palette(name):
        theme = themes_mod.ALL_THEMES.get(name)
        if theme is None and name in snapshot.effective.custom_themes:
            theme = themes_mod.theme_from_tokens(name, snapshot.effective.custom_themes[name])
        if theme is None:
            from textual.theme import BUILTIN_THEMES
            theme = BUILTIN_THEMES.get(name)
        if theme is None:
            return {"accent": "#c9a24d", "void": "#0a0a0b", "panel": "#131314", "bone": "#e8e4dc",
                    "tokens": {}}
        def color(value):
            # Textual's defaults may be None or ANSI names. Resolve colors
            # through its own color system before handing them to HTML inputs.
            value = value.hex if hasattr(value, "hex") else value
            return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None

        resolved = theme.to_color_system().generate()
        fallback = themes_mod.ALL_THEMES["oscura-midnight"].to_color_system().generate()
        tokens = {key: color(getattr(theme, key)) or color(resolved.get(key)) or color(fallback[key])
                  for key in themes_mod.THEME_TOKENS}
        extras = theme.variables or {}
        for key in themes_mod.THEME_EXTRA_TOKENS + themes_mod.THEME_FOOTER_TOKENS:
            value = color(extras.get(key)) or color(resolved.get(key))
            if value is not None:
                tokens[key] = value
        return {"accent": tokens["primary"], "void": tokens["background"],
                "panel": tokens["surface"], "bone": tokens["foreground"], "tokens": tokens}

    ui["themes"] = [{"id": name, "label": label.replace("-", " ").title(), **palette(name)}
                    for label, name in _theme_choices(snapshot.effective.custom_themes)]
    # Named tokens are the TUI creator's own schema. The sidecar uses these to
    # render color controls, not to invent a second palette representation.
    ui["theme_tokens"] = list(themes_mod.THEME_FORM_TOKENS)
    ui["custom_themes"] = deepcopy(snapshot.effective.custom_themes)
    result = {"revisions": dict(snapshot.revisions), "fields": fields, "ui": ui}
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
