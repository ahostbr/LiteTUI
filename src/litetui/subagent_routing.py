"""Subagent route validation, legacy migration and live preference resolution.

A global null follows the parent. Conversation null inherits; {} explicitly
follows the parent. A concrete route always names both backend and model.
"""
from __future__ import annotations

from copy import deepcopy

LOCAL_BACKENDS = frozenset({'lmstudio', 'llamacpp', 'ninfer', 'strata', 'custom'})


def validate_route(value, *, override=False):
    from litetui.llm_backend import BACKEND_NAMES

    if value is None or override and value == {}:
        return
    if (not isinstance(value, dict) or set(value) != {'backend', 'model'}
            or not isinstance(value.get('backend'), str)
            or value.get('backend') not in BACKEND_NAMES
            or not isinstance(value.get('model'), str) or not value['model'].strip()):
        raise ValueError('Invalid subagent route: expected {backend, model} or null')


def migrate_global_file(path):
    """Atomic, idempotent migration; new route wins if both keys exist.

    Import storage helpers lazily to avoid a settings/service import cycle.
    Errors remain visible to callers, never rewritten as a silent reset.
    """
    from litetui.settings_service import _read, _write
    from litetui.shared_state import coordinated_write

    raw, _ = _read(path)
    if 'codex_subagent_model' not in raw:
        return
    with coordinated_write(path):
        raw, _ = _read(path)
        if 'codex_subagent_model' not in raw:
            return
        old = raw.pop('codex_subagent_model')
        if 'subagent_route' not in raw:
            if old is not None and (not isinstance(old, str) or not old.strip()):
                raise ValueError('Invalid legacy Codex subagent model during route migration')
            raw['subagent_route'] = {'backend': 'codex', 'model': old} if old is not None else None
        validate_route(raw['subagent_route'])
        _write(path, raw)


def resolve_route(app, explicit_model=None):
    """Refresh authoritative preferences for each child, without model loading."""
    from litetui.settings_runtime import service_for

    backend = getattr(getattr(app, 'backend', None), 'name', 'lmstudio')
    parent = {'backend': backend, 'model': app.model_id}
    if explicit_model:
        return {'backend': backend, 'model': explicit_model}
    service = service_for(app)
    directory = getattr(app, 'convo_dir', None)
    if directory:
        saved = service.snapshot(directory.name).saved
        override = saved.subagent_route_override
        validate_route(override, override=True)
        if override is not None:
            return deepcopy(override or parent)
        if saved.subagent_model:
            return {'backend': backend, 'model': saved.subagent_model}
    else:
        # Tool hosts before conversation materialization still carry legacy
        # preferences in memory; authoritative conversation reads win once born.
        override = getattr(app.settings, 'subagent_route_override', None)
        if override is not None:
            validate_route(override, override=True)
            return deepcopy(override or parent)
        legacy = getattr(app.settings, 'subagent_model', None)
        if legacy:
            return {'backend': backend, 'model': legacy}
    _, route = service.global_value('subagent_route')
    validate_route(route)
    return deepcopy(route or parent)
