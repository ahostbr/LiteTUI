"""WS1 regression gates; all persisted state is temporary."""
import json
from litetui import settings as st
from litetui import convo_settings as cs


def test_unrelated_save_does_not_persist_environment_backend(tmp_path, monkeypatch):
    monkeypatch.delenv('LITETUI_BACKEND', raising=False)
    st.save(st.Settings(backend='codex'), tmp_path)
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    current = st.load(tmp_path)
    assert current.backend == 'ninfer'
    current.theme_name = 'monokai'
    st.save(current, tmp_path)
    disk = json.loads(st.settings_path(tmp_path).read_text(encoding='utf-8'))
    assert disk['backend'] == 'codex'
    assert disk['theme_name'] == 'monokai'


def test_new_conversation_snapshots_execution_configuration_deeply():
    defaults = st.Settings(backend='ninfer', ninfer_host='http://localhost:49260',
                           temperature=0.25, tools_disabled=['bash'])
    born = cs.born_from(defaults)
    defaults.ninfer_host = 'http://localhost:49261'
    defaults.temperature = 0.9
    defaults.tools_disabled.append('write')
    assert cs.resolved(born, defaults, 'ninfer_host') == 'http://localhost:49260'
    assert cs.resolved(born, defaults, 'temperature') == 0.25
    assert cs.resolved(born, defaults, 'tools_disabled') == ['bash']


def test_scoped_service_saves_and_detects_stale_revision(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    a = svc.snapshot('a')
    b = svc.snapshot('b')
    result = svc.save_patch('a', [SettingChange('backend', 'codex', 'conversation')], a.revisions)
    assert result.fully_saved
    assert svc.snapshot('a').saved.backend == 'codex'
    assert svc.snapshot('b').saved.backend == b.saved.backend
    stale = svc.save_patch('a', [SettingChange('backend', 'ninfer', 'conversation')], a.revisions)
    assert not stale.fully_saved
    assert 'revision' in stale.persistence[0].error.lower()
    assert svc.snapshot('a').saved.backend == 'codex'


def test_service_rejects_wrong_destination_and_preserves_explicit_none(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    import pytest
    svc = SettingsService(tmp_path)
    snap = svc.snapshot('a')
    with pytest.raises(ValueError):
        svc.save_patch('a', [SettingChange('backend', 'codex', 'device')], snap.revisions)
    result = svc.save_patch('a', [SettingChange('temperature', None, 'conversation')], snap.revisions)
    assert result.fully_saved
    assert svc.snapshot('a').saved.temperature is None


def test_service_effective_environment_never_becomes_saved(tmp_path, monkeypatch):
    from litetui.settings_service import SettingsService, SettingChange
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    svc = SettingsService(tmp_path)
    snap = svc.snapshot('a')
    assert snap.effective.backend == 'ninfer'
    assert snap.saved.backend == 'lmstudio'
    result = svc.save_patch('a', [SettingChange('theme_name', 'monokai', 'device')], snap.revisions)
    assert result.fully_saved
    assert svc.snapshot('a').saved.backend == 'lmstudio'


def _process_patch(root, conversation, key, value, scope, revisions, queue):
    from pathlib import Path
    from litetui.settings_service import SettingsService, SettingChange
    result = SettingsService(Path(root)).save_patch(conversation, [SettingChange(key, value, scope)], revisions)
    queue.put(result.fully_saved)


def test_processes_cannot_spend_same_global_revision(tmp_path):
    import multiprocessing as mp
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    revision = svc.snapshot('a').revisions
    context = mp.get_context('spawn')
    queue = context.Queue()
    workers = [context.Process(target=_process_patch, args=(str(tmp_path), name,
               'theme_name', theme, 'device', revision, queue))
               for name, theme in [('a', 'monokai'), ('b', 'textual-light')]]
    for worker in workers:
        worker.start()
    try:
        results = [queue.get(timeout=20) for _ in workers]
        assert sorted(results) == [False, True]
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert svc.snapshot('a').saved.theme_name in ('monokai', 'textual-light')
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        queue.close()


def test_processes_keep_distinct_conversation_backends(tmp_path):
    import multiprocessing as mp
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    context = mp.get_context('spawn')
    queue = context.Queue()
    workers = [context.Process(target=_process_patch, args=(str(tmp_path), name,
               'backend', backend, 'conversation', svc.snapshot(name).revisions, queue))
               for name, backend in [('a', 'codex'), ('b', 'ninfer')]]
    for worker in workers:
        worker.start()
    try:
        assert all(queue.get(timeout=20) for _ in workers)
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert svc.snapshot('a').saved.backend == 'codex'
        assert svc.snapshot('b').saved.backend == 'ninfer'
        assert not st.settings_path(tmp_path).exists()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        queue.close()


def test_partial_persistence_is_explicit(tmp_path, monkeypatch):
    from litetui import settings_service as mod
    svc = mod.SettingsService(tmp_path)
    snap = svc.snapshot('a')
    real_write = mod._write
    def fail_global(path, raw):
        if path == st.settings_path(tmp_path):
            raise OSError('injected disk failure')
        return real_write(path, raw)
    monkeypatch.setattr(mod, '_write', fail_global)
    result = svc.save_patch('a', [mod.SettingChange('backend', 'codex', 'conversation'),
                            mod.SettingChange('theme_name', 'monokai', 'device')], snap.revisions)
    assert [p.saved for p in result.persistence] == [True, False]
    assert not result.fully_saved
    assert svc.snapshot('a').saved.backend == 'codex'
    assert svc.snapshot('a').saved.theme_name == st.Settings().theme_name


def test_birth_does_not_persist_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv('LITETUI_BACKEND', raising=False)
    st.save(st.Settings(backend='codex'), tmp_path)
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    effective = st.load(tmp_path)
    born = cs.born_from(effective)
    assert born.backend == 'codex'
    assert born.execution['backend'] == 'codex'


def test_corrupt_execution_snapshot_is_diagnosed_but_conversation_opens(tmp_path):
    cs.path_for(tmp_path).write_text('{"execution": [], "backend": 42}', encoding='utf-8')
    loaded = cs.load(tmp_path)
    assert loaded.backend is None
    assert loaded.execution == {}
    assert loaded._diagnostics


def test_service_rejects_invalid_values_before_any_write(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    import pytest
    svc = SettingsService(tmp_path)
    snap = svc.snapshot('a')
    for key, value in [('backend', 42), ('tools_disabled', 'bash'),
                       ('ninfer_max_context', True), ('temperature', 'hot')]:
        with pytest.raises(ValueError):
            svc.save_patch('a', [SettingChange(key, value, 'conversation')], snap.revisions)
    assert not cs.path_for(tmp_path / '.convos' / 'a').exists()


def test_created_conversation_does_not_inherit_changed_defaults(tmp_path):
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    first = svc.create_conversation('a')
    st.save(st.Settings(backend='codex', temperature=0.9), tmp_path)
    assert svc.snapshot('a').saved.backend == first.saved.backend


def test_service_cannot_persist_invalid_backend_name(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    import pytest
    svc = SettingsService(tmp_path)
    with pytest.raises(ValueError, match='backend'):
        svc.save_patch('a', [SettingChange('backend', 'invented', 'conversation')], svc.snapshot('a').revisions)


def test_partial_global_write_cannot_materialize_new_conversation_from_stale_defaults(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    baseline = svc.snapshot('a')
    st.save(st.Settings(backend='codex'), tmp_path)
    result = svc.save_patch('a', [SettingChange('temperature', 0.2, 'conversation')], baseline.revisions)
    assert not result.fully_saved
    assert 'revision' in result.persistence[0].error.lower()
    assert not cs.path_for(tmp_path / '.convos' / 'a').exists()


def test_mixed_patch_creation_independent_of_change_order(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    snap = svc.snapshot('a')
    result = svc.save_patch('a', [SettingChange('theme_name', 'monokai', 'device'),
                                 SettingChange('temperature', 0.2, 'conversation')], snap.revisions)
    assert result.fully_saved


def test_legacy_missing_execution_fields_materialize_on_scoped_write(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    d = tmp_path / '.convos' / 'a'
    d.mkdir(parents=True)
    cs.save(d, cs.ConvoSettings(backend='codex'))
    snap = svc.snapshot('a')
    assert svc.save_patch('a', [SettingChange('temperature', 0.2, 'conversation')], snap.revisions).fully_saved
    st.save(st.Settings(ninfer_host='http://changed:49260'), tmp_path)
    assert svc.snapshot('a').saved.ninfer_host == snap.saved.ninfer_host
