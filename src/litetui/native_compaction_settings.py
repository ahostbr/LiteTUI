"""Reviewed compaction preferences; pure inspection and one canonical settings save."""
from __future__ import annotations

import copy
import json
import secrets
import uuid
from dataclasses import dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    codex_settings,
    compaction_preferences,
    native_settings,
    settings,
    settings_runtime,
)
from litetui.settings_scope import SETTING_SPECS
from litetui.turn_engine import side_call_reasoning_effort

VERSION = 1
MAX_WIRE = 65536
COMMON = ('version', 'session_id', 'agent_id', 'backend', 'model')
FIELDS = compaction_preferences.FIELDS
TOOL_REASON = 'Tools are shared turn authority; this editor does not change them.'
REASONING_SOURCE = 'canonical compaction request adapter'


@dataclass(frozen=True)
class _Review:
    wire: bytes
    draft: bytes


def _encoded(value):
    return backend_catalog._encoded(value, MAX_WIRE)


def _identity(app):
    return {'version': VERSION, **native_settings._identity(app)}


def _common(app, cmd):
    identifier = cmd.get('id')
    if not isinstance(identifier, str) or not identifier or len(identifier.encode('utf-8')) > 128:
        raise ValueError('Compaction settings request id must be 1 through 128 UTF-8 bytes')
    _encoded(cmd)
    if (type(cmd.get('version')) is not int or cmd['version'] != VERSION
            or any(cmd.get(key) != value for key, value in _identity(app).items())):
        raise ValueError('Compaction settings belong to another owned runtime or version')


def _keys(cmd, keys):
    if set(cmd) - {*COMMON, *keys, 'type', 'id'}:
        raise ValueError('Unsupported compaction settings request fields')


def _wire(cmd, value):
    _encoded({'type': 'response', 'id': cmd['id'], 'ok': True, 'result': value})
    return value


def _effort(app, value, levels):
    result = side_call_reasoning_effort(value, app.backend.name, app.model_id,
                                      app.settings.lmstudio_graded_thinking_models, levels)
    if result is not None and (not isinstance(result, str) or not result or len(result.encode('utf-8')) > 64):
        raise ValueError('Resolved compaction reasoning exceeds its vocabulary budget')
    return result


def _reasoning(app, fields):
    field = fields['compact_thinking_level']
    if not field['editable']:
        return {'available': False, 'reason': field['reason'], 'source': REASONING_SOURCE,
                'supported_levels': [], 'saved': None, 'effective': None}
    reader = getattr(app.backend, 'reasoning_levels', None)
    levels = reader(app.model_id) if callable(reader) else []
    if (not isinstance(levels, (tuple, list)) or len(levels) > 32
            or any(not isinstance(level, str) or not level or len(level.encode('utf-8')) > 64 for level in levels)
            or len(set(levels)) != len(levels)):
        raise ValueError('Compaction reasoning metadata is unavailable or malformed')
    return {'available': True, 'reason': None, 'source': REASONING_SOURCE,
            'supported_levels': list(levels), 'saved': _effort(app, field['saved'], levels),
            'effective': _effort(app, field['effective'], levels)}


def _guard(app):
    """Metadata and input facts may not change even when the selected value does."""
    from dataclasses import asdict

    controls = {key: codex_settings.control(app.backend, key) for key in FIELDS}
    thinking = controls['compact_thinking_level']
    available = thinking is None or thinking.editable
    levels = getattr(app.backend, 'reasoning_levels', None)
    metadata = levels(app.model_id) if available and callable(levels) else []
    return backend_catalog._encoded({
        'controls': {key: asdict(control) if control is not None else None for key, control in controls.items()},
        'environment': {key: settings.source_of(key) for key in FIELDS},
        'scope': {key: (SETTING_SPECS[key].scope.value, SETTING_SPECS[key].apply_timing) for key in FIELDS},
        'levels': metadata, 'pending_image': getattr(app, 'pending_image', None),
        'invocation_saved': getattr(app, '_invocation_saved_values', {}),
        'launch_overrides': getattr(app, '_launch_overrides', {}),
        'references': [id(app._agent_session), id(app.store), id(app.client)],
    }, 8 * 1024 * 1024)


def _raw_values(documents):
    global_values = documents['global']
    execution = documents['conversation'].get('execution', {})
    if not isinstance(execution, dict):
        raise TypeError('Conversation execution settings must be an object')
    for key in FIELDS:
        source = (execution if key != 'clear_screen_after_compact' and key in execution else global_values)
        if key in source:
            compaction_preferences.checked(key, source[key])


def _snapshot(app, cmd):
    _common(app, cmd)
    backend_transition._quiet(app, require_unowned_server=False)
    context = backend_catalog._context(app, cmd.get('session_id'))
    before = backend_catalog.fingerprint(app, context, {'kind': 'compaction_settings_preflight'})
    service = settings_runtime.service_for(app)
    _paths, documents, raw_revisions = native_settings._documents(service, context)
    _raw_values(documents)
    saved = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    if saved.revisions != raw_revisions:
        raise ValueError('Settings documents changed before canonical snapshot')
    paths = service._paths(context.session_id)
    fields = {}
    for key in FIELDS:
        spec = SETTING_SPECS[key]
        scope = 'device' if key == 'clear_screen_after_compact' else 'conversation'
        timing = 'immediate' if scope == 'device' else 'next_turn'
        if spec.scope.value != scope or spec.apply_timing != timing:
            raise ValueError('Compaction preference source scope or timing changed')
        control = codex_settings.control(app.backend, key)
        reason = control.help if control is not None and not control.editable else None
        if settings.source_of(key):
            reason = f'Environment {settings.source_of(key)} owns this compaction preference'
        bounds = compaction_preferences.BOUNDS.get(key)
        kind = 'integer' if bounds else 'choice' if key == 'compact_thinking_level' else 'boolean'
        fields[key] = {'type': kind, 'scope': scope, 'apply_timing': timing,
                       'destination': 'global' if scope == 'device' else 'conversation',
                       'minimum': bounds[0] if bounds else None, 'maximum': bounds[1] if bounds else None,
                       'choices': list(compaction_preferences.CHOICES) if kind == 'choice' else [],
                       'saved': compaction_preferences.checked(key, getattr(saved.saved, key)),
                       'effective': compaction_preferences.checked(key, getattr(app.settings, key)),
                       'editable': reason is None, 'reason': reason}
    if type(app.tools_enabled) is not bool:
        raise ValueError('Compaction tools authority must be boolean')
    result = {'kind': 'compaction_settings_snapshot', **_identity(app), 'revisions': saved.revisions,
              'destinations': {name: str(path) for name, path in paths.items()}, 'fields': fields,
              'tools': {'effective': app.tools_enabled, 'editable': False, 'reason': TOOL_REASON},
              'reasoning': _reasoning(app, fields)}
    nonce = getattr(app, '_native_compaction_settings_nonce', None)
    if nonce is None:
        nonce = app._native_compaction_settings_nonce = secrets.token_hex(32)
    binding = {'value': result, 'nonce': nonce,
               'objects': [id(context.session), id(context.store), id(context.backend), id(context.settings)]}
    token = backend_catalog.fingerprint(app, context, binding)
    if (service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024).revisions != saved.revisions
            or backend_catalog._context(app, context.session_id) != context
            or backend_catalog.fingerprint(app, context, binding) != token
            or backend_catalog.fingerprint(app, context, {'kind': 'compaction_settings_preflight'}) != before
            or _reasoning(app, fields) != result['reasoning']):
        raise ValueError('Compaction settings changed while reading; refresh before editing')
    result['snapshot'] = token
    return _wire(cmd, result), context, service, saved


def snapshot(app, cmd):
    _keys(cmd, ())
    return _snapshot(app, cmd)[0]


def _draft(app, cmd):
    _keys(cmd, ('snapshot', 'revisions', 'field', 'value'))
    current, context, service, saved = _snapshot(app, cmd)
    if cmd.get('snapshot') != current['snapshot'] or _encoded(cmd.get('revisions')) != _encoded(current['revisions']):
        raise ValueError('Compaction settings review is stale; refresh before editing')
    key = cmd.get('field')
    if not isinstance(key, str) or key not in FIELDS:
        raise ValueError('Only the eight compaction preferences are editable here')
    field = current['fields'][key]
    if not field['editable']:
        raise ValueError(field['reason'])
    value = compaction_preferences.checked(key, cmd.get('value'))
    from litetui.gui_rpc import _validate

    _validate(app, {key: value})
    resolved = _effort(app, value, current['reasoning']['supported_levels']) if key == 'compact_thinking_level' else None
    review = {'kind': 'compaction_settings_review', **_identity(app), 'snapshot': current['snapshot'],
              'revisions': current['revisions'], 'field': key, 'value': value,
              'scope': field['scope'], 'apply_timing': field['apply_timing'],
              'destination': current['destinations'][field['destination']],
              'saved_before': field['saved'], 'effective_before': field['effective'], 'resolved_value': resolved}
    return review, context, service, saved


def validate(app, cmd):
    # Even a failed new draft supersedes prior intent; no hidden older save remains.
    app._native_compaction_settings_review = None
    review, *_ = _draft(app, cmd)
    review.update(review=secrets.token_hex(32), action_id=str(uuid.uuid4()))
    _wire(cmd, review)
    app._native_compaction_settings_review = _Review(_encoded(review), _encoded(cmd))
    return copy.deepcopy(review)


def _receipt(app, cmd, status, **outcome):
    # Reuse the shared receipt's explicit durable/runtime outcome vocabulary.
    value = native_settings._receipt(app, cmd, status, **outcome)
    value.update(kind='compaction_settings_save', action_id=cmd.get('action_id'),
                 scope=cmd.get('scope'), apply_timing=cmd.get('apply_timing'), destination=cmd.get('destination'))
    return _wire(cmd, value)


def save(app, cmd):
    stored = getattr(app, '_native_compaction_settings_review', None)
    app._native_compaction_settings_review = None

    def reviewed(app, cmd):
        _common(app, cmd)
        if not isinstance(stored, _Review):
            raise TypeError('Compaction settings save requires a fresh unconsumed review')
        review = json.loads(stored.wire)
        expected = {key: value for key, value in review.items() if key != 'kind'}
        actual = {key: value for key, value in cmd.items() if key not in ('type', 'id')}
        if _encoded(actual) != _encoded(expected):
            raise ValueError('Save must exactly echo the validated compaction preference')
        fresh, context, service, before = _draft(app, json.loads(stored.draft))
        if _encoded(fresh) != _encoded({key: value for key, value in review.items() if key not in ('review', 'action_id')}):
            raise ValueError('Compaction preference changed after review')
        return review, context, service, before

    return native_settings.publish_reviewed(app, cmd, reviewed, _receipt, compaction_preferences.checked, strict=True, guard=_guard)
