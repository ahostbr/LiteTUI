"""App-facing persistence adapter shared by commands, RPC and the dialog."""
from dataclasses import fields, asdict
from litetui import settings as st, convo_settings as cs
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsService, SettingChange
from litetui.settings_apply import SettingsSaveResult, PersistenceDestinationResult


def service_for(app):
    service = getattr(app, '_settings_service', None)
    directory = getattr(app, 'convo_dir', None)
    if service is None:
        from litetui.paths import data_root
        service = SettingsService(data_root(), directory.parent if directory else None)
        app._settings_service = service
    elif directory is not None and service.conversation_root.resolve() != directory.parent.resolve():
        # /resume accepts an external conversation path. Rebind only its
        # storage root; device/default preferences retain their original owner.
        service = SettingsService(service.root, directory.parent)
        app._settings_service = service
    return service


def capture_invocation(settings, overrides):
    """Apply explicit launch choices while retaining disk, not environment, values."""
    from copy import deepcopy
    disk = getattr(settings, '_saved_values', {})
    saved = {}
    for key, value in overrides.items():
        if key not in SETTING_SPECS:
            raise ValueError(f'Unknown invocation setting: {key}')
        saved[key] = deepcopy(disk.get(key, getattr(settings, key)))
        setattr(settings, key, deepcopy(value))
    return saved


def without_invocation(app, candidate):
    """Copy persistence values without inheriting unchanged CLI overrides.

    A value deliberately changed away from the effective invocation value is
    ordinary user intent. Never mutate the effective settings to save them.
    """
    from copy import deepcopy
    import os
    result = deepcopy(candidate)
    for key, saved in getattr(app.settings, '_saved_values', {}).items():
        env = st.ENV_OVERRIDES.get(key)
        if env and os.environ.get(env) and getattr(candidate, key) == getattr(app.settings, key):
            setattr(result, key, deepcopy(saved))
    for key, saved in getattr(app, '_invocation_saved_values', {}).items():
        if getattr(candidate, key) == getattr(app.settings, key):
            setattr(result, key, deepcopy(saved))
    return result


def snapshot_with_launch(app, conversation_id):
    """The service snapshot with this process's launch values (--backend ...)
    as effective: what the TUI form shows. Diffing a form against it makes an
    untouched launch value "no change" instead of a write."""
    from copy import deepcopy
    from dataclasses import replace
    snapshot = service_for(app).snapshot(conversation_id)
    effective = deepcopy(snapshot.effective)
    for key in getattr(app, '_invocation_saved_values', {}):
        setattr(effective, key, deepcopy(getattr(app.settings, key)))
    return replace(snapshot, effective=effective)


def retire_invocation(app, candidate, keys):
    saved = getattr(app, '_invocation_saved_values', {})
    for key in list(saved):
        if key in keys and getattr(candidate, key) != getattr(app.settings, key):
            del saved[key]
            if key == 'backend':
                app._cli_initial_backend = None


def persist_settings(app, candidate, *, baseline=None, expected_revisions=None):
    directory = getattr(app, 'convo_dir', None)
    if directory is None:
        persisted = without_invocation(app, candidate)
        path = st.save(persisted)
        from copy import deepcopy
        object.__setattr__(candidate, '_saved_values', deepcopy(asdict(persisted)))
        retire_invocation(app, candidate, {f.name for f in fields(st.Settings)})
        return SettingsSaveResult(persistence=(PersistenceDestinationResult(str(path), 'defaults', True),))
    service = service_for(app)
    snapshot = service.snapshot(directory.name)
    if baseline is None:
        baseline_values = getattr(candidate, '_baseline', asdict(snapshot.effective))
    else:
        baseline_values = asdict(baseline)
    # An unchanged invocation value is not a preference edit, even when the
    # candidate was cloned without its dynamic baseline metadata.
    for key in getattr(app, '_invocation_saved_values', {}):
        if getattr(candidate, key) == getattr(app.settings, key):
            baseline_values[key] = getattr(candidate, key)
    changes = [SettingChange(f.name, getattr(candidate, f.name), SETTING_SPECS[f.name].scope.value)
               for f in fields(st.Settings)
               if getattr(candidate, f.name) != baseline_values[f.name]]
    result = service.save_patch(directory.name, changes,
                               snapshot.revisions if expected_revisions is None else expected_revisions)
    if any(p.saved and p.scope == 'conversation' for p in result.persistence):
        app._convo_settings = cs.load(directory)
    from copy import deepcopy
    next_saved = deepcopy(asdict(snapshot.saved))
    next_baseline = dict(baseline_values)
    for outcome in result.persistence:
        if outcome.saved:
            retire_invocation(app, candidate, outcome.fields)
            for key in outcome.fields:
                next_baseline[key] = getattr(candidate, key)
                next_saved[key] = deepcopy(getattr(candidate, key))
    from copy import deepcopy
    object.__setattr__(candidate, '_baseline', deepcopy(next_baseline))
    object.__setattr__(candidate, '_saved_values', next_saved)
    app._settings_save_result = result
    return result


def persist_or_raise(app, candidate):
    result = persist_settings(app, candidate)
    failures = [p.error for p in result.persistence if not p.saved]
    if failures:
        raise OSError('; '.join(failures))
    return result


def save_selection_defaults(**choices):
    """Explicit picker choices also become startup defaults, immediately.

    Start from disk and force only these keys into the atomic merge. Never
    copy a conversation's unrelated overrides or launch environment to defaults.
    """
    allowed = {'backend', 'backend_chosen', 'default_model', 'ninfer_artifact'}
    if not choices.keys() <= allowed:
        raise ValueError('Not a backend/model selection')
    defaults = st.load()
    for key, value in choices.items():
        setattr(defaults, key, value)
        defaults._baseline.pop(key, None)
    return st.save(defaults)


def apply_saved_result(app, requested, result):
    """Apply only saved fields; engine-affecting changes remain explicitly pending.

    No implicit model load/restart is performed by saving preferences.
    """
    from copy import deepcopy
    from litetui.settings_apply import RuntimeSettingStatus
    statuses = []
    effective = app.settings
    # A saved change to a field set at launch (--backend ...) retires the launch
    # value, as persist_settings does; otherwise prepare_reconnect re-applies it
    # and the saved edit silently never takes effect. Before the loop: it
    # compares against the value still in effect.
    retire_invocation(app, requested, {key for outcome in result.persistence if outcome.saved
                                       for key in outcome.fields})
    for outcome in result.persistence:
        if not outcome.saved:
            continue
        for key in outcome.fields:
            spec = SETTING_SPECS[key]
            want, before = deepcopy(getattr(requested, key)), deepcopy(getattr(effective, key))
            env = st.source_of(key)
            if env:
                import os
                actual = st._coerce(key, os.environ[env], before)
                setattr(effective, key, actual)
                statuses.append(RuntimeSettingStatus(key, spec.scope.value,
                                'pending' if actual != want else 'applied',
                                'restart' if actual != want else 'none', want, actual,
                                f'Environment {env} overrides saved preference' if actual != want else None))
                continue
            action = spec.apply_timing
            if action in ('restart', 'reconnect', 'reload') and want != before:
                statuses.append(RuntimeSettingStatus(key, spec.scope.value, 'pending', action,
                                want, before, f'Saved; {action} required'))
                continue
            try:
                setattr(effective, key, want)
                if key == 'theme_name' and hasattr(app, 'theme'):
                    app.theme = want
                elif key == 'tools_enabled':
                    app.tools_enabled = want
                elif key == 'thinking_level':
                    app._thinking_level = None if want in (None, 'default') else want
                elif key == 'tool_policy_profile':
                    app._active_tool_profile = want
                elif key == 'tts_enabled' and not want:
                    from litetui import voice_backend
                    voice_backend.stop()
                if key in ('tts_enabled', 'autoscroll'):
                    refresh = getattr(app, '_refresh_prompt_controls', None)
                    if refresh is not None:
                        refresh()
                    if key == 'autoscroll' and hasattr(app, '_next_follow_generation'):
                        app._next_follow_generation()
                        app._scroll_down()
                statuses.append(RuntimeSettingStatus(key, spec.scope.value, 'applied',
                                requested=want, effective=deepcopy(getattr(effective, key))))
            except Exception as exc:
                setattr(effective, key, before)
                statuses.append(RuntimeSettingStatus(key, spec.scope.value, 'failed', 'retry',
                                want, before, str(exc)))
    object.__setattr__(effective, '_baseline', asdict(effective))
    if getattr(app, 'convo_dir', None):
        app._convo_settings = cs.load(app.convo_dir)
    combined = SettingsSaveResult(result.persistence, tuple(statuses))
    app._settings_save_result = combined
    return combined


def prepare_reconnect(app):
    """Adopt saved reconnect-time configuration, not engine-restart settings."""
    if getattr(app, 'convo_dir', None) is None:
        return
    from copy import deepcopy
    snapshot = service_for(app).snapshot(app.convo_dir.name)
    target = deepcopy(app.settings)
    for key, spec in SETTING_SPECS.items():
        if spec.apply_timing == 'reconnect':
            setattr(target, key, deepcopy(getattr(snapshot.effective, key)))
    # Reconnect must not quietly return a CLI-selected server to the saved URL.
    # Deliberate preference edits retire their key from this map above.
    for key in getattr(app, '_invocation_saved_values', {}):
        setattr(target, key, deepcopy(getattr(app.settings, key)))
    backend = getattr(app, 'backend', None)
    if backend is not None and (getattr(backend, 'name', None) != target.backend
                               or app.settings.codex_native_engine != target.codex_native_engine):
        from litetui.llm_backend import make_backend
        replacement = make_backend(target)
        if getattr(backend, 'owns_native_turns', False):
            from litetui.claude_backend import close_native
            close_native(app, backend)
        elif hasattr(backend, 'app_server'):
            backend.shutdown()
        app.backend = replacement
        app.available_models = []
        app._model_id = ''
    elif backend is not None and hasattr(backend, 'set_settings'):
        from litetui.claude_backend import close_native
        close_native(app, backend)
        backend.set_settings(target)
    app.settings = target
    object.__setattr__(target, '_baseline', asdict(target))
