"""Guarded owned output-budget review and save; never starts or loads engines."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, is_dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    codex_settings,
    settings_runtime,
)
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange, conversation_document

VERSION = 1
FIELDS = ("max_tokens_chat", "max_tokens_tools")
MAXIMUM = 2147483647
CLOSE_REQUIRED = "Settings publication is uncertain; close this runtime and reopen explicitly."


def _version(cmd):
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Native settings require version 1")


def _integer(value):
    if type(value) is not int or not 1 <= value <= MAXIMUM:
        raise ValueError(f"Output budget must be an integer from 1 through {MAXIMUM}")
    return value


def _bounded(value):
    backend_catalog._encoded(value, 64 * 1024)
    return value


def _identity(app):
    session = getattr(app, "_agent_session", None)
    return {"session_id": getattr(app, "convo_id", None),
            "agent_id": session.authority.agent_id if session is not None else None,
            "backend": getattr(getattr(app, "backend", None), "name", None),
            "model": getattr(app, "model_id", None)}


def _snapshot(app, cmd):
    _version(cmd)
    backend_transition._quiet(app, require_unowned_server=False)
    context = backend_catalog._context(app, cmd.get("session_id"))
    # Validate owned paths and byte limits before the canonical snapshot reader.
    before_read = backend_catalog.fingerprint(app, context, {"kind": "output_budget_preflight"})
    service = settings_runtime.service_for(app)
    saved = service.snapshot(context.session_id, read_only=True)
    controls = {key: codex_settings.control(app.backend, key) for key in FIELDS}
    request = {}
    if any(control is None or control.editable for control in controls.values()):
        request = app.backend.request_overrides(app.model_id)
        if not isinstance(request, dict):
            raise ValueError("Backend request controls are unavailable")
    fields = {}
    from litetui import settings

    for key in FIELDS:
        spec = SETTING_SPECS[key]
        if spec.scope.value != "conversation" or spec.apply_timing != "next_turn":
            raise ValueError("Output budget source scope or timing changed")
        control = controls[key]
        reason = control.help if control is not None and not control.editable else None
        if getattr(getattr(app, "_launch_options", None), "max_tokens", None) is not None:
            reason = "The --max-tokens launch option owns request output budgets"
        elif "max_tokens" in request:
            reason = "The selected model's request override owns its output budget"
        elif settings.source_of(key):
            reason = f"Environment {settings.source_of(key)} owns this output budget"
        fields[key] = {"scope": spec.scope.value, "apply_timing": spec.apply_timing,
                       "saved": _integer(getattr(saved.saved, key)),
                       "effective": _integer(getattr(app.settings, key)),
                       "editable": reason is None, "reason": reason,
                       "minimum": 1, "maximum": MAXIMUM}
    result = {"version": VERSION, "kind": "output_budget_snapshot", **_identity(app),
              "revisions": saved.revisions, "fields": fields}
    token = backend_catalog.fingerprint(app, context, result)
    if service.snapshot(context.session_id, read_only=True).revisions != saved.revisions:
        raise ValueError("Settings changed while reading; refresh before editing")
    if backend_catalog._context(app, context.session_id) != context or backend_catalog.fingerprint(app, context, result) != token:
        raise ValueError("Settings authority changed while reading; refresh before editing")
    if backend_catalog.fingerprint(app, context, {"kind": "output_budget_preflight"}) != before_read:
        raise ValueError("Settings changed while reading; refresh before editing")
    result["snapshot"] = token
    return _bounded(result), context, service, saved


def snapshot(app, cmd):
    return _snapshot(app, cmd)[0]


def _review(app, cmd):
    current, context, service, saved = _snapshot(app, cmd)
    if cmd.get("agent_id") != current["agent_id"]:
        raise ValueError("Settings review belongs to another agent")
    if cmd.get("snapshot") != current["snapshot"] or cmd.get("revisions") != current["revisions"]:
        raise ValueError("Settings review is stale; refresh before editing")
    key = cmd.get("field")
    if not isinstance(key, str) or key not in FIELDS:
        raise ValueError("Only conversation output budgets are editable here")
    field = current["fields"][key]
    if not field["editable"]:
        raise ValueError(field["reason"])
    value = _integer(cmd.get("value"))
    from litetui.gui_rpc import _validate

    # Reuse the same source type/positive/environment validation as Settings.
    _validate(app, {key: value})
    review = {"version": VERSION, "kind": "output_budget_review", **_identity(app),
              "snapshot": current["snapshot"], "revisions": current["revisions"],
              "field": key, "value": value, "scope": field["scope"],
              "apply_timing": field["apply_timing"], "saved_before": field["saved"],
              "effective_before": field["effective"]}
    review["review"] = hashlib.sha256(backend_catalog._encoded(review, 64 * 1024)).hexdigest()
    return _bounded(review), context, service, saved


def validate(app, cmd):
    """Pure review: no settings files, runtime values or launch overrides change."""
    return _review(app, cmd)[0]


def _protected(app, field):
    from litetui import store_identity
    from litetui.gui_rpc import _tool_policy

    values = asdict(app.settings)
    for key in (field,) if isinstance(field, str) else field:
        values.pop(key)
    launch = getattr(app, "_launch_options", None)
    return backend_catalog._encoded({
        "settings": values, "authority": asdict(app._agent_session.authority),
        "policy": _tool_policy(app), "history": app.conversation,
        "pending": getattr(app, "_pending_input", []),
        "hold": getattr(app, "_hold_restored_input", False),
        "identity": store_identity.capture(app),
        "launch": asdict(launch) if is_dataclass(launch) and not isinstance(launch, type) else None,
        "thinking": getattr(app, "_thinking_level", None),
        "cli_thinking": getattr(app, "_cli_effective_thinking", None),
    }, 8 * 1024 * 1024)


def _receipt(app, cmd, status, *, mutated=False, close_required=False, error=None,
             persistence=(), runtime=(), saved=None, effective=None, revisions=None):
    try:
        identity = _identity(app)
    except (ValueError, OSError):
        identity = {"session_id": getattr(app, "convo_id", None), "agent_id": cmd.get("agent_id"),
                    "backend": getattr(getattr(app, "backend", None), "name", None),
                    "model": getattr(app, "model_id", None)}
    return _bounded({"version": VERSION, "kind": "output_budget_save", **identity,
                     "snapshot": cmd.get("snapshot"), "review": cmd.get("review"),
                     "field": cmd.get("field"), "value": cmd.get("value"), "status": status,
                     "mutated": mutated, "close_required": close_required, "error": error,
                     "persistence": [asdict(item) for item in persistence],
                     "runtime": [asdict(item) for item in runtime], "saved": saved,
                     "effective": effective, "revisions": revisions})


def _documents(service, context):
    return owned_documents(service, context.session_id, context.session)


def owned_documents(service, session_id, session):
    """Read bounded canonical settings documents without migrations or writes."""
    from litetui.agent_store import _unlinked

    paths = service._paths(session_id)
    paths['agent'] = session.memory_root / 'settings.json'
    documents: dict[str, dict] = {}
    revisions: dict[str, str] = {}
    for name, target in paths.items():
        path = _unlinked(target)
        try:
            with path.open('rb') as stream:
                before = path.stat()
                raw = stream.read(1024 * 1024 + 1)
                after = path.stat()
        except FileNotFoundError:
            documents[name], revisions[name] = {}, 'absent'
            continue
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns)
        if len(raw) > 1024 * 1024 or identity(before) != identity(after):
            raise ValueError('Settings document changed or exceeded the read budget')
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise TypeError('Settings document must be an object')
        backend_catalog._encoded(document, 1024 * 1024)
        documents[name], revisions[name] = document, hashlib.sha256(raw).hexdigest()
    return paths, documents, revisions


def publish_reviewed(app, cmd, reader, receipt, checked, *, strict=False, guard=None):
    """One-field compatibility adapter over the same-destination publication core."""
    def read_many(owner, command):
        review, context, service, before = reader(owner, command)
        return ({**review, 'changes': {review['field']: review['value']},
                 'effective_before': {review['field']: review['effective_before']}},
                context, service, before)

    def single(owner, command, status, **detail):
        for name in ('saved', 'effective'):
            value = detail.get(name)
            if isinstance(value, dict):
                detail[name] = next(iter(value.values()))
        return receipt(owner, command, status, **detail)

    return publish_reviewed_many(app, cmd, read_many, single, checked,
                                 strict=strict, guard=guard)


def publish_reviewed_many(app, cmd, reader, receipt, checked, *, strict=True, guard=None):
    """Publish one reviewed patch to one canonical document, then verify every field."""
    try:
        review, context, service, before = reader(app, cmd)
        values, scope = review['changes'], review['scope']
        if not isinstance(values, dict) or not values or any(
                key not in SETTING_SPECS or SETTING_SPECS[key].scope.value != scope
                for key in values):
            raise ValueError('Settings patch must share one canonical scope')
        keys = tuple(values)
        if set(review['effective_before']) != set(keys):
            raise ValueError('Settings patch requires every effective preimage')
        destination = 'conversation' if scope == 'conversation' else 'global'
        changes = [SettingChange(key, value, scope) for key, value in values.items()]
        protected = _protected(app, keys)
        extra = guard(app) if guard is not None else None
        requested = copy.deepcopy(app.settings)
        for key, value in values.items():
            setattr(requested, key, copy.deepcopy(value))
        backend, settings, conversation = app.backend, app.settings, app.conversation
        documents, expected_documents, paths = {}, {}, {}
        if strict:
            paths, documents, revisions = _documents(service, context)
            if revisions != before.revisions or str(paths[destination]) != review['destination']:
                raise ValueError('Settings destination or revisions changed before publication')
            expected_documents = copy.deepcopy(documents)
            if destination == 'conversation':
                expected_documents[destination] = conversation_document(documents[destination], before.saved, changes)
            else:
                expected_documents[destination].update(copy.deepcopy(values))
            backend_catalog._encoded(expected_documents[destination], 1024 * 1024)
        if reader(app, cmd)[0] != review:
            raise ValueError('Settings changed before publication')
    except Exception as exc:  # noqa: BLE001 - malformed/stale requests are recoverable
        return receipt(app, cmd, 'refused', error=str(exc)[:2048])

    result = combined = None
    limits = {"max_bytes": 1024 * 1024} if strict else {}
    def read_values(settings):
        return {key: copy.deepcopy(getattr(settings, key)) for key in keys}

    try:
        result = service.save_patch(context.session_id, changes, before.revisions, **limits)
        after = service.snapshot(context.session_id, read_only=True, **limits)
        if (app.backend is not backend or app.settings is not settings or app.conversation is not conversation
                or _protected(app, keys) != protected
                or guard is not None and guard(app) != extra):
            raise ValueError('Settings authority changed during publication')
        observed = observed_revisions = None
        if strict:
            observed_paths, observed, observed_revisions = _documents(service, context)
            if observed_paths != paths or observed_revisions != after.revisions:
                raise ValueError('Settings raw destination disagrees with canonical readback')
        if not result.fully_saved:
            if (after.revisions != before.revisions
                    or not _same(read_values(app.settings), review['effective_before'])
                    or strict and not _same(observed, documents)):
                raise ValueError('Settings publication failed after an observable change')
            if strict:
                _persistence_many(result, changes, paths[destination], after.revisions[destination], saved=False)
            return receipt(app, cmd, 'refused', persistence=result.persistence,
                           saved=read_values(after.saved), effective=read_values(app.settings),
                           revisions=after.revisions, error='; '.join(item.error or 'Save refused' for item in result.persistence if not item.saved)[:2048])
        expected = {**asdict(before.saved), **copy.deepcopy(values)}
        if (asdict(after.saved) != expected or strict and not _same(asdict(after.saved), expected)
                or any(after.revisions[name] != before.revisions[name] for name in before.revisions if name != destination)
                or strict and not _same(observed, expected_documents)):
            raise ValueError('Saved settings failed independent durable readback')
        if strict:
            _persistence_many(result, changes, paths[destination], after.revisions[destination], saved=True)
        combined = settings_runtime.apply_saved_result(app, requested, result)
        if (app.backend is not backend or app.settings is not settings or app.conversation is not conversation
                or _protected(app, keys) != protected
                or guard is not None and guard(app) != extra):
            raise ValueError('Settings application changed protected execution state')
        effective = {key: checked(key, getattr(app.settings, key)) for key in keys}
        if tuple(row.field for row in combined.runtime) != keys or any(
                row.effective != effective[row.field] for row in combined.runtime):
            raise ValueError('Runtime settings outcome disagrees with readback')
        if strict:
            _persistence_many(combined, changes, paths[destination], after.revisions[destination], saved=True)
            if not _same([asdict(row) for row in combined.persistence], [asdict(row) for row in result.persistence]):
                raise ValueError('Runtime changed the verified persistence receipt')
            for runtime in combined.runtime:
                key, value = runtime.field, values[runtime.field]
                actual = effective[key]
                if (runtime.scope != scope or not _same(runtime.requested, value)
                        or not _same(runtime.effective, actual)
                        or runtime.status not in ('applied', 'pending', 'failed')
                        or runtime.status == 'applied' and (runtime.action != 'none' or runtime.reason is not None)
                        or runtime.status == 'failed' and (runtime.action != 'retry' or not isinstance(runtime.reason, str) or not runtime.reason)
                        or runtime.status == 'pending' and (runtime.action not in ('restart', 'reconnect', 'reload') or not isinstance(runtime.reason, str) or not runtime.reason)
                        or not any(_same(actual, allowed) for allowed in (review['effective_before'][key], value))
                        or runtime.status == 'applied' and not _same(actual, value)):
                    raise ValueError('Settings runtime outcome disagrees with its reviewed field')
            if (not _same(_documents(service, context)[1:], (expected_documents, after.revisions))
                    or service.snapshot(context.session_id, read_only=True, **limits).revisions != after.revisions):
                raise ValueError('Settings durable state changed during application')
        status = 'saved' if all(row.status == 'applied' and _same(effective[row.field], values[row.field])
                                for row in combined.runtime) else 'partial'
        return receipt(app, cmd, status, mutated=True, persistence=combined.persistence,
                       runtime=combined.runtime, saved=read_values(after.saved), effective=effective,
                       revisions=after.revisions)
    except BaseException as exc:
        app._backend_transition_error = CLOSE_REQUIRED
        app._owned_launch_error = CLOSE_REQUIRED
        app._cli_launch_error = CLOSE_REQUIRED
        if not isinstance(exc, Exception):
            raise
        return receipt(app, cmd, 'failed', mutated=True, close_required=True,
                       error=(str(exc) or type(exc).__name__)[:2048],
                       persistence=result.persistence if result else (),
                       runtime=combined.runtime if combined else ())


def _same(left, right):
    return backend_catalog._encoded(left, 8 * 1024 * 1024) == backend_catalog._encoded(right, 8 * 1024 * 1024)


def _persistence_many(result, changes, path, revision, *, saved):
    if len(result.persistence) != 1:
        raise ValueError('Settings publication must report exactly one destination')
    row = result.persistence[0]
    if (row.destination != str(path) or row.scope != changes[0].scope or row.fields != tuple(change.key for change in changes)
            or type(row.saved) is not bool or row.saved is not saved
            or saved and (row.revision != revision or row.error is not None)
            or not saved and (not isinstance(row.error, str) or not row.error)):
        raise ValueError('Settings persistence receipt disagrees with its destination')


def save(app, cmd):
    """One explicit publication attempt; preserve the output-budget v1 wire."""
    def reviewed(app, cmd):
        result = _review(app, cmd)
        if cmd.get('review') != result[0]['review']:
            raise ValueError('Save requires the exact validated review')
        return result

    return publish_reviewed(app, cmd, reviewed, _receipt, lambda _key, value: _integer(value))
