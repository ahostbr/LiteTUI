"""App-facing persistence adapter shared by commands, RPC and the dialog."""
from dataclasses import fields, asdict
from litetui import settings as st, convo_settings as cs
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsService, SettingChange
from litetui.settings_apply import SettingsSaveResult, PersistenceDestinationResult


def service_for(app):
    service = getattr(app, '_settings_service', None)
    if service is None:
        from litetui.paths import data_root
        directory = getattr(app, 'convo_dir', None)
        service = SettingsService(data_root(), directory.parent if directory else None)
        app._settings_service = service
    return service


def persist_settings(app, candidate, *, baseline=None, expected_revisions=None):
    directory = getattr(app, 'convo_dir', None)
    if directory is None:
        path = st.save(candidate)
        return SettingsSaveResult(persistence=(PersistenceDestinationResult(str(path), 'defaults', True),))
    service = service_for(app)
    snapshot = service.snapshot(directory.name)
    if baseline is None:
        baseline_values = getattr(candidate, '_baseline', asdict(snapshot.effective))
    else:
        baseline_values = asdict(baseline)
    changes = [SettingChange(f.name, getattr(candidate, f.name), SETTING_SPECS[f.name].scope.value)
               for f in fields(st.Settings)
               if getattr(candidate, f.name) != baseline_values[f.name]]
    result = service.save_patch(directory.name, changes,
                               snapshot.revisions if expected_revisions is None else expected_revisions)
    if any(p.saved and p.scope == 'conversation' for p in result.persistence):
        app._convo_settings = cs.load(directory)
    next_baseline = dict(baseline_values)
    for outcome in result.persistence:
        if outcome.saved:
            for key in outcome.fields:
                next_baseline[key] = getattr(candidate, key)
    from copy import deepcopy
    object.__setattr__(candidate, '_baseline', deepcopy(next_baseline))
    app._settings_save_result = result
    return result


def persist_or_raise(app, candidate):
    result = persist_settings(app, candidate)
    failures = [p.error for p in result.persistence if not p.saved]
    if failures:
        raise OSError('; '.join(failures))
    return result


def apply_saved_result(app, requested, result):
    """Apply only saved fields; engine-affecting changes remain explicitly pending.

    No implicit model load/restart is performed by saving preferences.
    """
    from copy import deepcopy
    from litetui.settings_apply import RuntimeSettingStatus
    statuses = []
    effective = app.settings
    for outcome in result.persistence:
        if not outcome.saved:
            continue
        for key in outcome.fields:
            spec = SETTING_SPECS[key]
            want, before = deepcopy(getattr(requested, key)), deepcopy(getattr(effective, key))
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
