"""Narrow theme runtime application: no model, voice or execution side effects."""
from __future__ import annotations

from copy import deepcopy

from textual.app import App

from litetui import themes
from litetui.theme_palette import definition_digest

FIELDS = ("custom_themes", "theme_name")


def register_custom_themes(app):
    """Return failed registrations; callers retain their ordinary notification UX."""
    failures = []
    for name, tokens in (app.settings.custom_themes or {}).items():
        try:
            app.register_theme(themes.theme_from_tokens(name, tokens))
        except Exception as error:  # noqa: BLE001 - retain canonical per-entry boot isolation
            failures.append({"name": name, "reason": str(error)})
    return failures


def apply_selection(app, *, previous_definition=None):
    """Select only the exact registered definition and request same-name repaint."""
    name = app.settings.theme_name
    registered = app.available_themes.get(name)
    if registered is None or app.get_theme(name) is not registered:
        raise ValueError("Theme selection does not resolve to its exact registered definition")
    before_name = app.theme
    changed = before_name != name or previous_definition != definition_digest(registered)
    app.theme = name
    if before_name == name and changed:
        app.mutate_reactive(App.theme)
    if app.current_theme is not registered:
        raise ValueError("Theme application resolved a different registered definition")
    return changed


def apply_saved_result(app, requested, result):
    """Apply only successfully saved theme fields, without another persistence call.

    Preserve partial state on failure: runtime readback describes what exists,
    never a guessed rollback. Theme-specific integration may reuse these helpers.
    """
    saved = {key for outcome in result.persistence if outcome.saved for key in outcome.fields}
    if not saved or saved - set(FIELDS):
        raise ValueError("Theme runtime application requires only saved theme fields")
    name = requested.theme_name
    desired = app.available_themes.get(name)
    for key, tokens in requested.custom_themes.items():
        try:
            candidate = themes.theme_from_tokens(key, tokens)
            if candidate.name == name:
                desired = candidate
        except (ValueError, TypeError, AttributeError):
            continue
    expected_definition = definition_digest(desired) if desired is not None else None
    outcome = {"status": "applied", "registered": False, "selected": False,
               "refresh_requested": False, "error": None}
    try:
        previous = definition_digest(app.current_theme)
        # Set the cached choice before the reactive property: watch_theme then
        # sees an already-saved value and never issues a second device write.
        for key in FIELDS:
            if key in saved:
                setattr(app.settings, key, deepcopy(getattr(requested, key)))
        if "custom_themes" in saved:
            failures = register_custom_themes(app)
            failure = next((row for row in failures if str(row["name"]).strip() == name), None)
            if failure:
                raise ValueError(failure["reason"])
        registered = app.available_themes.get(name)
        outcome["registered"] = registered is not None and definition_digest(registered) == expected_definition
        outcome["refresh_requested"] = apply_selection(app, previous_definition=previous)
        outcome["selected"] = True
    except Exception as error:  # noqa: BLE001 - preserve partial runtime facts
        outcome["status"] = "failed"
        outcome["error"] = str(error).encode("utf-8", "replace")[:4096].decode("utf-8", "ignore")
        try:
            registered = app.available_themes.get(name)
            outcome["registered"] = registered is not None and definition_digest(registered) == expected_definition
            outcome["selected"] = outcome["registered"] and app.current_theme is registered
            outcome["refresh_requested"] = None
        except Exception:  # noqa: BLE001 - mark unreadable runtime facts unknown
            outcome["registered"] = outcome["selected"] = None
    return outcome
