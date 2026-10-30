"""Owned device-theme review and save, with independent durable/runtime facts."""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, dataclass, is_dataclass

from textual.theme import BUILTIN_THEMES

from litetui import (
    backend_catalog,
    backend_transition,
    native_engine,
    settings_runtime,
    theme_palette,
    themes,
)
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange

VERSION = 1
MAX_RESPONSE = 1024 * 1024
MAX_ROWS = 256
COMMON = ("version", "session_id", "agent_id", "backend", "model")
FIELDS = ("custom_themes", "theme_name")


@dataclass(frozen=True)
class _Review:
    wire: bytes
    command: bytes


def _error(error):
    return (str(error) or type(error).__name__).encode("utf-8", "replace")[:4096].decode("utf-8", "ignore")


def _encoded(value):
    return theme_palette.encoded(value, MAX_RESPONSE)


def _bounded(value):
    return json.loads(_encoded(value))


def _wire(cmd, result):
    response = {"type": "response", "ok": True}
    if cmd.get("id") is not None:
        response["id"] = cmd["id"]
    response["result"] = result
    if len(json.dumps(response, default=str, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Theme response exceeds the full 1 MiB wire budget")
    return result


def _identity(app):
    session = getattr(app, "_agent_session", None)
    return {"version": VERSION, "session_id": app.convo_id,
            "agent_id": session.authority.agent_id if session is not None else None,
            "backend": app.backend.name, "model": app.model_id}


def _common(app, cmd):
    if len(json.dumps(cmd.get("id"), default=str, allow_nan=False).encode("utf-8")) > 4096:
        raise ValueError("Theme operation id exceeds its 4096-byte metadata budget")
    expected = _identity(app)
    if type(cmd.get("version")) is not int or any(cmd.get(key) != value for key, value in expected.items()):
        raise ValueError("Theme operation belongs to another owned runtime or protocol version")
    return expected


def _name(value, *, allow_empty=False):
    if not isinstance(value, str) or not allow_empty and not value or len(value.encode("utf-8")) > theme_palette.MAX_NAME:
        raise ValueError("Theme name is invalid or exceeds its byte budget")
    return value


def _global(app):
    """Read only bounded owned device settings; never migrate legacy preferences."""
    from litetui.agent_store import _unlinked

    path = _unlinked(app._agent_session.store.data_root / "settings.json")
    if not path.exists():
        return {}
    before = path.stat()
    with path.open("rb") as stream:
        data = stream.read(MAX_RESPONSE + 1)
    after = path.stat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
    if len(data) > MAX_RESPONSE or identity(before) != identity(after):
        raise ValueError("Theme device settings changed or exceeded the read budget")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise TypeError("Theme device settings must be an object")
    _encoded(value)
    return value


def _customs(customs):
    if not isinstance(customs, dict) or len(customs) > MAX_ROWS:
        raise ValueError("Custom theme registry exceeds its entry budget")
    compiled, invalid = {}, []
    for name, tokens in customs.items():
        _name(name)
        try:
            theme = themes.theme_from_tokens(name, tokens)
            digest = theme_palette.definition_digest(theme)
            raw = {key: str(value).strip() for key, value in tokens.items() if key in themes.THEME_FORM_TOKENS}
            theme_palette.token_rows(theme, raw)
            compiled[theme.name] = (digest, raw)
        except Exception as error:  # noqa: BLE001 - report bounded source/runtime refusal
            invalid.append({"name": name, "reason": _error(error)})
    return compiled, invalid


def _registry(app):
    registered = app.available_themes
    if not isinstance(registered, dict) or len(registered) > MAX_ROWS:
        raise ValueError("Registered theme catalog exceeds its entry budget")
    customs, invalid = _customs(app.settings.custom_themes)
    rows, identities = [], []
    for name, theme in registered.items():
        _name(name)
        digest = theme_palette.definition_digest(theme)
        identities.append((name, id(theme), digest))
        if not theme.dark:
            continue
        try:
            if theme.name != name:
                raise ValueError("Registered theme key disagrees with its definition")
            raw = None
            source = "registered"
            if name in customs and customs[name][0] == digest:
                source, raw = "custom", customs[name][1]
            elif name in themes.ALL_THEMES and theme_palette.definition_digest(themes.ALL_THEMES[name]) == digest:
                source = "litetui"
            elif name in BUILTIN_THEMES and theme_palette.definition_digest(BUILTIN_THEMES[name]) == digest:
                source = "builtin"
            selectable = app.get_theme(name) is theme
            rows.append({"row_id": hashlib.sha256(_encoded([name, id(theme), digest])).hexdigest(),
                         "name": name, "definition": digest, "source": source,
                         "selectable": selectable,
                         "reason": None if selectable else "Name does not resolve to this exact registered definition",
                         "palette": theme_palette.resolved_colors(theme),
                         "tokens": theme_palette.token_rows(theme, raw)})
        except Exception as error:  # noqa: BLE001 - report bounded source/runtime refusal
            invalid.append({"name": name, "reason": _error(error)})
    return rows, invalid, identities


def _saved_definition(saved):
    name = saved.saved.theme_name
    definitions = {name: theme for name, theme in {**BUILTIN_THEMES, **themes.ALL_THEMES}.items()
                   if name not in themes.LIGHT_BUILTINS}
    for key, tokens in saved.saved.custom_themes.items():
        try:
            candidate = themes.theme_from_tokens(key, tokens)
            definitions[candidate.name] = candidate
        except (ValueError, TypeError, AttributeError):
            continue  # boot retains the public definition when a custom is invalid
    selected = definitions.get(name)
    return theme_palette.definition_digest(selected) if selected is not None else None


def _selection(app, saved, rows):
    effective = app.current_theme
    digest = theme_palette.definition_digest(effective)
    row = next((item for item in rows if item["name"] == effective.name and item["definition"] == digest), None)
    return {"saved_name": _name(saved.saved.theme_name, allow_empty=True), "saved_definition": _saved_definition(saved),
            "requested_name": _name(app.settings.theme_name, allow_empty=True), "runtime_name": _name(app.theme, allow_empty=True),
            "effective_name": _name(effective.name), "effective_row_id": row["row_id"] if row is not None else None,
            "effective_definition": digest,
            "effective_reason": None if row is not None else "Active source theme has no supported opaque native RGB palette"}


def _eligibility(app):
    reason = getattr(app, "_native_theme_error", None)
    if reason:
        return reason
    try:
        backend_transition._quiet(app, require_unowned_server=False)
    except Exception as error:  # noqa: BLE001 - report bounded source/runtime refusal
        return _error(error)
    return None


def _snapshot(app, cmd):
    identity = _common(app, cmd)
    # This exemption grants synchronous metadata inspection only. Mutations
    # independently recheck ordinary execution/lifecycle eligibility below.
    with native_engine.inspection(app):
        context = backend_catalog._context(app, cmd.get("session_id"))
        initial = backend_catalog.fingerprint(app, context, {"kind": "theme_preflight"})
        raw = _global(app)
        service = settings_runtime.service_for(app)
        saved = service.snapshot(context.session_id, read_only=True)
        registry = _registry(app)
        rows, invalid, objects = registry
        # Expose invalid persisted entries even if they have not reached this
        # runtime's cached settings yet; all raw entries still bind the snapshot.
        _, saved_invalid = _customs(saved.saved.custom_themes)
        invalid = [json.loads(item) for item in dict.fromkeys(_encoded(row) for row in [*invalid, *saved_invalid])]
        if len(invalid) > MAX_ROWS:
            raise ValueError("Theme diagnostics exceed the catalog entry budget")
        selection = _selection(app, saved, rows)
        for field in FIELDS:
            spec = SETTING_SPECS[field]
            if spec.scope.value != "device" or spec.apply_timing != "immediate":
                raise ValueError("Theme source scope or timing changed")
        reason = _eligibility(app)
        # _quiet runs inside the metadata exemption; preserve the independently
        # observed engine admission restriction on mutation eligibility.
        engine_reason = native_engine.submission_refusal(app)
        if engine_reason:
            reason = engine_reason
        result = {"kind": "theme_snapshot", **identity, "revisions": saved.revisions,
                  "scope": "device", "apply_timing": "immediate", "selection": selection,
                  "eligible": reason is None, "reason": reason, "rows": rows, "invalid": invalid,
                  "registered_definitions": {name: digest for name, _, digest in objects}}
        token = backend_catalog.fingerprint(app, context, {"result": result, "registry": objects, "global": raw})
        if (service.snapshot(context.session_id, read_only=True).revisions != saved.revisions
                or _registry(app) != registry
                or _global(app) != raw
                or backend_catalog._context(app, cmd.get("session_id")) != context
                or backend_catalog.fingerprint(app, context, {"kind": "theme_preflight"}) != initial):
            raise ValueError("Theme source changed while reading; refresh before editing")
        result["snapshot"] = token
        return _bounded(result), context, service, saved, raw


def snapshot(app, cmd):
    return _wire(cmd, _snapshot(app, cmd)[0])


def _draft(app, cmd):
    current, context, service, saved, raw = _snapshot(app, cmd)
    if not current["eligible"]:
        raise ValueError(current["reason"])
    if cmd.get("snapshot") != current["snapshot"] or cmd.get("revisions") != current["revisions"]:
        raise ValueError("Theme review is stale; refresh before editing")
    action = cmd.get("action")
    target = None
    custom_map = None
    overwrite: dict[str, object] = {"custom": False, "registered": False, "previous_definition": None}
    allowed = {*COMMON, "type", "id", "snapshot", "revisions", "action"}
    if action == "select":
        allowed.add("row_id")
        row = next((row for row in current["rows"] if row["row_id"] == cmd.get("row_id")), None)
        if row is None or not row["selectable"]:
            raise ValueError("Selected theme row is unavailable or cannot resolve exactly")
        target = {key: copy.deepcopy(row[key]) for key in ("row_id", "name", "definition", "palette", "tokens")}
        if row["source"] != "registered":
            selected_saved = copy.deepcopy(saved)
            selected_saved.saved.theme_name = row["name"]
            if _saved_definition(selected_saved) != row["definition"]:
                raise ValueError("Saved theme differs from this runtime; review an explicit custom overwrite")
        fields = ["theme_name"]
    elif action == "custom":
        allowed.update(("name", "tokens"))
        candidate, tokens = theme_palette.custom(cmd.get("name"), cmd.get("tokens"))
        custom_map = copy.deepcopy(saved.saved.custom_themes)
        overwrite["custom"] = candidate.name in custom_map
        previous = app.available_themes.get(candidate.name)
        overwrite["registered"] = previous is not None
        overwrite["previous_definition"] = theme_palette.definition_digest(previous) if previous is not None else None
        custom_map[candidate.name] = tokens
        merged_saved = copy.deepcopy(saved)
        merged_saved.saved.custom_themes = custom_map
        merged_saved.saved.theme_name = candidate.name
        if _saved_definition(merged_saved) != theme_palette.definition_digest(candidate):
            raise ValueError("A later trimmed-name alias overrides this theme; resolve the saved name collision before editing")
        if len(custom_map) > MAX_ROWS:
            raise ValueError("Custom theme registry exceeds its entry budget")
        target = {"row_id": None, "name": candidate.name,
                  "definition": theme_palette.definition_digest(candidate),
                  "palette": theme_palette.resolved_colors(candidate),
                  "tokens": theme_palette.token_rows(candidate, tokens)}
        fields = list(FIELDS)
    else:
        raise ValueError("Theme action must be select or custom")
    if set(cmd) - allowed:
        raise ValueError("Theme draft contains unsupported fields")
    review = {"version": VERSION, "kind": "theme_review", **_identity(app),
              "snapshot": current["snapshot"], "revisions": current["revisions"], "action": action,
              "target": target, "overwrite": overwrite, "selection_before": current["selection"],
              "scope": "device", "apply_timing": "immediate", "fields": fields}
    return _bounded(review), context, service, saved, raw, custom_map


def validate(app, cmd):
    review, *_ = _draft(app, cmd)
    review.update(review=secrets.token_hex(32), action_id=str(uuid.uuid4()))
    _wire(cmd, _bounded(review))
    # Bytes, not caller-owned nested objects, own the reviewed intent.
    app._native_theme_review = _Review(_encoded(review), _encoded(cmd))
    return copy.deepcopy(review)


def _protected(app):
    from litetui import store_identity
    from litetui.gui_rpc import _tool_policy

    settings = asdict(app.settings)
    for field in FIELDS:
        settings.pop(field)
    launch = getattr(app, "_launch_options", None)
    return backend_catalog._encoded({"identity": _identity(app), "settings": settings,
            "authority": asdict(app._agent_session.authority), "policy": _tool_policy(app),
            "history": app.conversation, "pending": getattr(app, "_pending_input", []),
            "hold": getattr(app, "_hold_restored_input", False), "store": store_identity.capture(app),
            "thinking": getattr(app, "_thinking_level", None),
            "launch": asdict(launch) if is_dataclass(launch) and not isinstance(launch, type) else None,
            "cli_thinking": getattr(app, "_cli_effective_thinking", None)}, 8 * 1024 * 1024)


def _same_references(app, references):
    return all(before is after for before, after in zip(references, (app.backend, app.settings, app.conversation)))


def _unchanged():
    return {"status": "unchanged", "registered": None, "selected": None,
            "refresh_requested": False, "error": None}


def _receipt(app, cmd, review, status, *, mutated=False, error=None, persistence=(),
             runtime=None, selection=None, revisions=None, close_required=False):
    target = review.get("target", {})
    return _wire(cmd, _bounded({"kind": "theme_save", **_identity(app),
            "snapshot": cmd.get("snapshot"), "review": cmd.get("review"), "action_id": cmd.get("action_id"),
            "action": review.get("action"), "target_name": target.get("name"),
            "target_definition": target.get("definition"), "status": status, "mutated": mutated,
            "close_required": close_required, "error": error,
            "persistence": [asdict(row) for row in persistence],
            "runtime": runtime if runtime is not None else _unchanged(),
            "selection": selection, "revisions": revisions}))


def save(app, cmd):
    """Consume one immutable review; no automatic retry or guessed rollback."""
    stored = getattr(app, "_native_theme_review", None)
    app._native_theme_review = None
    review = json.loads(stored.wire) if isinstance(stored, _Review) else {}
    try:
        _common(app, cmd)
        if not isinstance(stored, _Review):
            raise TypeError("Theme save requires a fresh validated review")
        expected = {key: review[key] for key in (*COMMON, "snapshot", "revisions", "review", "action_id")}
        if {key: value for key, value in cmd.items() if key not in ("type", "id")} != expected:
            raise ValueError("Theme save must echo the exact validated review")
        draft = json.loads(stored.command)
        fresh, context, service, before, raw_before, custom_map = _draft(app, draft)
        if fresh != {key: value for key, value in review.items() if key not in ("review", "action_id")}:
            raise ValueError("Theme definition or settings changed after review")
        protected = _protected(app)
        references = app.backend, app.settings, app.conversation
        requested = copy.deepcopy(app.settings)
        requested.theme_name = review["target"]["name"]
        if custom_map is not None:
            requested.custom_themes = custom_map
        changes = [SettingChange(key, copy.deepcopy(getattr(requested, key)), "device") for key in review["fields"]]
        expected_raw = copy.deepcopy(raw_before)
        expected_raw.update({change.key: change.value for change in changes})
        _encoded(expected_raw)
        # The synchronous write follows this final source check without an await.
        if _draft(app, draft)[0] != fresh:
            raise ValueError("Theme source changed immediately before publication")
    except Exception as error:  # noqa: BLE001 - report bounded source/runtime refusal
        return _receipt(app, cmd, review, "refused", error=_error(error))

    result = None
    runtime = _unchanged()
    try:
        result = service.save_patch(context.session_id, changes, before.revisions)
        after = service.snapshot(context.session_id, read_only=True)
        raw_after = _global(app)
        if (not _same_references(app, references) or _protected(app) != protected
                or after.revisions["conversation"] != before.revisions["conversation"]
                or after.revisions["agent"] != before.revisions["agent"]):
            raise ValueError("Theme publication changed protected execution state")
        if not result.fully_saved:
            if raw_after == raw_before and after.revisions == before.revisions:
                return _receipt(app, cmd, review, "refused", persistence=result.persistence,
                                error="; ".join(row.error or "Device save refused" for row in result.persistence)[:4096],
                                selection=_selection(app, after, _registry(app)[0]), revisions=after.revisions)
            if raw_after != expected_raw:
                raise ValueError("Device write failed with an unverified durable result")
            # A replace completed before its error: disk is known, runtime remains
            # unchanged. Report partial evidence rather than retrying the write.
            return _receipt(app, cmd, review, "partial", mutated=True, persistence=result.persistence,
                            error="Device value changed although persistence reported failure",
                            selection=_selection(app, after, _registry(app)[0]), revisions=after.revisions)
        if raw_after != expected_raw:
            raise ValueError("Theme save failed independent durable readback")
        app._theme_runtime_result = None
        settings_runtime.apply_saved_result(app, requested, result)
        runtime = copy.deepcopy(app._theme_runtime_result)
        if not isinstance(runtime, dict):
            raise TypeError("Theme runtime application did not report its outcome")
        if not _same_references(app, references) or _protected(app) != protected:
            raise ValueError("Theme application changed protected execution state")
        observed = _snapshot(app, _identity(app))[0]
        selection = observed["selection"]
        target = review["target"]
        exact = (selection["saved_name"] == selection["requested_name"] == selection["runtime_name"]
                 == selection["effective_name"] == target["name"]
                 and selection["effective_definition"] == target["definition"])
        status = "saved" if runtime["status"] == "applied" and exact else "partial"
        if runtime["status"] == "applied" and not exact:
            raise ValueError("Applied theme disagrees with its reviewed definition")
        return _receipt(app, cmd, review, status, mutated=True, persistence=result.persistence,
                        runtime=runtime, selection=selection, revisions=observed["revisions"],
                        error=runtime["error"])
    except Exception as error:  # noqa: BLE001 - report bounded source/runtime refusal
        # A presentation-only failure does not invent an execution fault. Block
        # further theme saves until a fresh process when publication is uncertain.
        app._native_theme_error = "Theme publication is uncertain; reopen before another theme save"
        try:
            unsafe = not _same_references(app, references) or _protected(app) != protected
        except Exception:  # noqa: BLE001 - unreadable authority cannot be trusted
            unsafe = True
        if unsafe:
            app._backend_transition_error = "Theme operation changed execution authority; close this runtime"
        return _receipt(app, cmd, review, "failed", mutated=True, error=_error(error),
                        persistence=result.persistence if result is not None else (),
                        runtime={**runtime, "status": "unknown"}, close_required=unsafe)
