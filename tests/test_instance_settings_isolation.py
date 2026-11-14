"""WS1 regression gates; all persisted state is temporary."""
import json

from _owned_seat import conversation_id, owned_session

from litetui import settings as st
from litetui import convo_settings as cs

# Conversation writes need an owned agent session (e52007fe); owned folders are UUIDs.
A, B = conversation_id('a'), conversation_id('b')


def _service(root, seat='LiteTUI'):
    from litetui.settings_service import SettingsService
    return SettingsService(root, agent_session=owned_session(root, seat))


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
    # An owned agent's backend is its authority, shared by its conversations (a2f8e823);
    # the other instance is another agent.
    svc, other = _service(tmp_path), _service(tmp_path, 'Other')
    a = svc.snapshot(A)
    b = other.snapshot(B)
    result = svc.save_patch(A, [SettingChange('backend', 'codex', 'conversation')], a.revisions)
    assert result.fully_saved
    assert svc.snapshot(A).saved.backend == 'codex'
    assert other.snapshot(B).saved.backend == b.saved.backend
    stale = svc.save_patch(A, [SettingChange('backend', 'ninfer', 'conversation')], a.revisions)
    assert not stale.fully_saved
    assert 'revision' in stale.persistence[0].error.lower()
    assert svc.snapshot(A).saved.backend == 'codex'


def test_service_rejects_wrong_destination_and_preserves_explicit_none(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    import pytest
    svc = _service(tmp_path)
    snap = svc.snapshot(A)
    with pytest.raises(ValueError):
        svc.save_patch(A, [SettingChange('backend', 'codex', 'device')], snap.revisions)
    result = svc.save_patch(A, [SettingChange('temperature', None, 'conversation')], snap.revisions)
    assert result.fully_saved
    assert svc.snapshot(A).saved.temperature is None


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


def _owned_process_patch(root, seat, conversation, key, value, scope, queue):
    from pathlib import Path
    from litetui.settings_service import SettingChange
    import _owned_seat
    try:
        svc = _service(Path(root), seat)
        revisions = svc.snapshot(conversation).revisions
        result = svc.save_patch(conversation, [SettingChange(key, value, scope)], revisions)
        queue.put(result.fully_saved)
    finally:
        _owned_seat.release_all()


def test_processes_keep_distinct_conversation_backends(tmp_path):
    import multiprocessing as mp
    # Each process is its own agent: a backend is an agent's authority (a2f8e823) and a
    # seat's lease belongs to one process. The parent reads each seat once its worker is done.
    # Both homes exist first: creating two agents at once is its own race (a half-made home
    # fails AgentStore.list_agents closed), not this test's subject.
    import _owned_seat
    for seat in ('SeatA', 'SeatB'):
        _service(tmp_path, seat)
    _owned_seat.release_all()
    context = mp.get_context('spawn')
    queue = context.Queue()
    workers = [context.Process(target=_owned_process_patch, args=(str(tmp_path), seat, name,
               'backend', backend, 'conversation', queue))
               for seat, name, backend in [('SeatA', A, 'codex'), ('SeatB', B, 'ninfer')]]
    for worker in workers:
        worker.start()
    try:
        assert all(queue.get(timeout=20) for _ in workers)
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert _service(tmp_path, 'SeatA').snapshot(A).saved.backend == 'codex'
        assert _service(tmp_path, 'SeatB').snapshot(B).saved.backend == 'ninfer'
        assert not st.settings_path(tmp_path).exists()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        queue.close()


def test_partial_persistence_is_explicit(tmp_path, monkeypatch):
    from litetui import settings_service as mod
    svc = _service(tmp_path)
    snap = svc.snapshot(A)
    real_write = mod._write
    def fail_global(path, raw):
        if path == st.settings_path(tmp_path):
            raise OSError('injected disk failure')
        return real_write(path, raw)
    monkeypatch.setattr(mod, '_write', fail_global)
    result = svc.save_patch(A, [mod.SettingChange('backend', 'codex', 'conversation'),
                            mod.SettingChange('theme_name', 'monokai', 'device')], snap.revisions)
    # Owned (e52007fe/a2f8e823): the backend lands in the agent's home, the conversation file
    # is written with it, and only the injected global (device) write fails.
    assert [(p.scope, p.saved) for p in result.persistence] == [
        ('conversation', True), ('conversation', True), ('device', False)]
    assert not result.fully_saved
    assert svc.snapshot(A).saved.backend == 'codex'
    assert svc.snapshot(A).saved.theme_name == st.Settings().theme_name


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
    svc = _service(tmp_path)
    first = svc.create_conversation(A)
    st.save(st.Settings(backend='codex', temperature=0.9), tmp_path)
    assert svc.snapshot(A).saved.backend == first.saved.backend


def test_service_cannot_persist_invalid_backend_name(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    import pytest
    svc = SettingsService(tmp_path)
    with pytest.raises(ValueError, match='backend'):
        svc.save_patch('a', [SettingChange('backend', 'invented', 'conversation')], svc.snapshot('a').revisions)


def test_partial_global_write_cannot_materialize_new_conversation_from_stale_defaults(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service(tmp_path)
    baseline = svc.snapshot(A)
    st.save(st.Settings(backend='codex'), tmp_path)
    result = svc.save_patch(A, [SettingChange('temperature', 0.2, 'conversation')], baseline.revisions)
    assert not result.fully_saved
    assert 'revision' in result.persistence[0].error.lower()
    assert not cs.path_for(svc.agent_session.conversation_directory(A)).exists()


def test_mixed_patch_creation_independent_of_change_order(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service(tmp_path)
    snap = svc.snapshot(A)
    result = svc.save_patch(A, [SettingChange('theme_name', 'monokai', 'device'),
                                 SettingChange('temperature', 0.2, 'conversation')], snap.revisions)
    assert result.fully_saved


def test_legacy_missing_execution_fields_materialize_on_scoped_write(tmp_path):
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service(tmp_path)
    d = svc.agent_session.conversation_directory(A)
    d.mkdir(parents=True)
    cs.save(d, cs.ConvoSettings(backend='codex'), agent_session=svc.agent_session)
    snap = svc.snapshot(A)
    assert svc.save_patch(A, [SettingChange('temperature', 0.2, 'conversation')], snap.revisions).fully_saved
    st.save(st.Settings(ninfer_host='http://changed:49260'), tmp_path)
    assert svc.snapshot(A).saved.ninfer_host == snap.saved.ninfer_host
