"""Scoped settings persistence with explicit destinations and optimistic revisions.

Storage never applies runtime changes. Callers receive independent persistence
outcomes and must report runtime outcomes separately through SettingsSaveResult.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import tempfile

from litetui import settings as st, convo_settings as cs
from litetui.settings_scope import SETTING_SPECS, SettingScope, validate_registry
from litetui.settings_apply import SettingsSaveResult, PersistenceDestinationResult
from litetui.shared_state import coordinated_write


@dataclass(frozen=True)
class SettingChange:
    key: str
    value: object
    scope: str


@dataclass(frozen=True)
class SettingsSnapshot:
    saved: st.Settings
    effective: st.Settings
    revisions: dict[str, str]


def _read(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}, 'absent'
    raw = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError(f'Settings must be an object: {path}')
    return raw, hashlib.sha256(data).hexdigest()


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.settings-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return _read(path)[1]


class SettingsService:
    def __init__(self, root: Path, conversation_root: Path | None = None):
        validate_registry()
        self.root = Path(root)
        self.conversation_root = Path(conversation_root) if conversation_root else self.root / '.convos'

    def _paths(self, conversation_id):
        # IDs are directory names, never caller-supplied paths escaping the store.
        if not conversation_id or conversation_id in ('.', '..') or any(c in conversation_id for c in '/\\:'):
            raise ValueError('Invalid conversation id')
        return {'global': st.settings_path(self.root),
                'conversation': self.conversation_root / conversation_id / cs.SETTINGS_NAME}

    def create_conversation(self, conversation_id):
        """Materialize inherited execution once, never replace an existing record."""
        paths = self._paths(conversation_id)
        with coordinated_write(paths['conversation']):
            if not paths['conversation'].exists():
                inherited = self.snapshot(conversation_id).saved
                _write(paths['conversation'], asdict(cs.born_from(inherited)))
        return self.snapshot(conversation_id)

    def snapshot(self, conversation_id, overrides=None):
        paths = self._paths(conversation_id)
        global_raw, global_rev = _read(paths['global'])
        convo, convo_rev = _read(paths['conversation'])
        saved = st.Settings()
        for key, value in global_raw.items():
            if key in SETTING_SPECS:
                setattr(saved, key, st._coerce(key, value, getattr(saved, key)))
        execution = convo.get('execution', {})
        if not isinstance(execution, dict):
            raise ValueError('Invalid conversation execution snapshot')
        for key, value in execution.items():
            if key in SETTING_SPECS and SETTING_SPECS[key].scope == SettingScope.CONVERSATION:
                setattr(saved, key, st._coerce(key, value, getattr(saved, key)))
        for own, key in cs.BORN_FROM.items():
            if convo.get(own) is not None:
                setattr(saved, key, st._coerce(key, convo[own], getattr(saved, key)))
        effective = deepcopy(saved)
        for key, env in st.ENV_OVERRIDES.items():
            if os.environ.get(env):
                setattr(effective, key, st._coerce(key, os.environ[env], getattr(effective, key)))
        for key, value in (overrides or {}).items():
            if key not in SETTING_SPECS:
                raise ValueError(f'Unknown invocation setting: {key}')
            setattr(effective, key, deepcopy(value))
        object.__setattr__(effective, '_baseline', asdict(effective))
        return SettingsSnapshot(saved, effective, {'global': global_rev, 'conversation': convo_rev})

    def save_patch(self, conversation_id, changes, expected_revisions):
        paths = self._paths(conversation_id)
        groups = {}
        for change in changes:
            spec = SETTING_SPECS.get(change.key)
            if spec is None or change.scope != spec.scope.value:
                raise ValueError(f'Invalid persistence scope for {change.key}: {change.scope}')
            declared = str(next(f.type for f in fields(st.Settings) if f.name == change.key))
            value = change.value
            if value is None:
                valid = 'None' in declared
            elif 'bool' in declared:
                valid = type(value) is bool
            elif 'list' in declared:
                valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
            elif 'dict' in declared:
                valid = isinstance(value, dict)
            elif 'int' in declared:
                valid = type(value) is int
            elif 'float' in declared:
                valid = type(value) in (int, float)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError(f'Invalid value for {change.key}: expected {declared}')
            if change.key == 'backend' and value not in ('lmstudio', 'llamacpp', 'ninfer', 'codex'):
                raise ValueError('Unknown backend')
            # Reject unserializable nested payloads before any destination writes.
            json.dumps(value, allow_nan=False)
            destination = 'conversation' if spec.scope == SettingScope.CONVERSATION else 'global'
            groups.setdefault(destination, []).append(change)
        results = []
        # Materialize conversation before our own global patch changes its birth revision.
        for destination in sorted(groups, key=lambda key: key != 'conversation'):
            patch = groups[destination]
            path = paths[destination]
            keys = tuple(c.key for c in patch)
            scopes = {c.scope for c in patch}
            scope = next(iter(scopes)) if len(scopes) == 1 else 'mixed'
            try:
                with coordinated_write(path):
                    raw, revision = _read(path)
                    if expected_revisions.get(destination) != revision:
                        raise ValueError(f'Stale {destination} revision; reload before retry')
                    if destination == 'conversation':
                        # Materialize all inherited defaults on first scoped write.
                        if not raw:
                            with coordinated_write(paths['global']):
                                inherited = self.snapshot(conversation_id)
                                if inherited.revisions['global'] != expected_revisions.get('global'):
                                    raise ValueError('Stale global revision during conversation creation')
                                raw = asdict(cs.born_from(inherited.saved))
                        execution = raw.setdefault('execution', {})
                        if not isinstance(execution, dict):
                            raise ValueError('Invalid conversation execution snapshot')
                        execution_keys = {k for k, spec in SETTING_SPECS.items()
                                          if spec.scope == SettingScope.CONVERSATION}
                        if execution_keys - execution.keys():
                            with coordinated_write(paths['global']):
                                inherited = self.snapshot(conversation_id)
                                if inherited.revisions['global'] != expected_revisions.get('global'):
                                    raise ValueError('Stale global revision during legacy materialization')
                                for key in execution_keys - execution.keys():
                                    execution[key] = deepcopy(getattr(inherited.saved, key))
                        aliases = {v: k for k, v in cs.BORN_FROM.items()}
                        for change in patch:
                            execution[change.key] = deepcopy(change.value)
                            if change.key in aliases:
                                raw[aliases[change.key]] = deepcopy(change.value)
                        raw['schema_version'] = 2
                    else:
                        raw.update({c.key: deepcopy(c.value) for c in patch})
                    revision = _write(path, raw)
                results.append(PersistenceDestinationResult(str(path), scope, True,
                               revision=revision, fields=keys))
            except (OSError, ValueError, TypeError) as exc:
                results.append(PersistenceDestinationResult(str(path), scope, False,
                               error=str(exc), fields=keys))
        return SettingsSaveResult(persistence=tuple(results))
