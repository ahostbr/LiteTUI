"""Owned-home execution publication, unchosen first-use, failure evidence."""
import json
import pytest
from litetui import settings, agent_ownership
from litetui.agent_launch_context import ordinary, acquire
from litetui.agent_store import AgentStore, StoreError


def test_explicit_unchosen_can_be_chosen_and_reopened(tmp_path):
    cfg = settings.Settings()
    cfg.default_model = None
    with ordinary(tmp_path, cfg) as session:
        name = session.authority.name
        assert session.authority.model is None
        raw = json.loads((session.memory_root / 'settings.json').read_text())
        assert raw['execution']['model_selection'] == 'unchosen'
        session.update_execution(backend='codex', model='real-fixture', thinking_level='high')
        assert session.authority.model == 'real-fixture'
    with acquire(tmp_path, name) as resumed:
        assert (resumed.authority.backend, resumed.authority.model, resumed.authority.thinking_level) == ('codex', 'real-fixture', 'high')


def test_prepublication_failure_preserves_old_authority_and_evidence(tmp_path, monkeypatch):
    cfg = settings.Settings()
    cfg.default_model = 'old-fixture'
    with ordinary(tmp_path, cfg) as session:
        before = (session.memory_root / 'settings.json').read_bytes()
        def failed(*args):
            raise OSError('fixture replace failure')
        monkeypatch.setattr(agent_ownership.os, 'replace', failed)
        with pytest.raises(OSError, match='replace failure'):
            session.update_execution(backend='codex', model='new-fixture', thinking_level='high')
        assert session.authority.model == 'old-fixture'
        assert (session.memory_root / 'settings.json').read_bytes() == before
        assert list(session.memory_root.glob('.settings.update.*.json'))


def test_home_success_is_reported_when_historical_snapshot_fails(tmp_path, monkeypatch):
    from litetui import settings_service
    from litetui.settings_service import SettingsService, SettingChange
    cfg = settings.Settings()
    cfg.default_model = 'old-fixture'
    with ordinary(tmp_path, cfg) as session:
        service = SettingsService(tmp_path, agent_session=session)
        cid = session.initial_conversation_id
        snap = service.snapshot(cid)
        original = settings_service._write
        def failed(path, raw):
            if 'conversations' in path.parts:
                raise OSError('fixture historical snapshot failure')
            return original(path, raw)
        monkeypatch.setattr(settings_service, '_write', failed)
        result = service.save_patch(cid, [SettingChange('default_model', 'new-fixture', 'conversation')], snap.revisions)
        assert result.persistence[0].saved and result.persistence[0].fields == ('default_model',)
        assert not result.persistence[1].saved
        assert 'default_model' not in result.persistence[1].fields
        assert session.authority.model == 'new-fixture'
        assert service.snapshot(cid).saved.default_model == 'new-fixture'


def test_missing_model_is_not_explicit_unchosen(tmp_path):
    cfg = settings.Settings()
    with ordinary(tmp_path, cfg) as session:
        path = session.memory_root / 'settings.json'
        raw = json.loads(path.read_text())
    del raw['execution']['model']
    path.write_text(json.dumps(raw))
    with pytest.raises(StoreError):
        AgentStore(tmp_path).list_agents()



def test_backend_publication_failure_keeps_old_backend_and_admission_open(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from litetui import app as app_module, convo_settings
    cfg = settings.Settings()
    cfg.default_model = 'old-fixture'
    with ordinary(tmp_path, cfg) as session:
        events = []
        old = SimpleNamespace(name=session.authority.backend,
                              _admission_session=SimpleNamespace(begin_close=lambda: events.append('close')))
        class Host:
            backend = app_module.LiteTUI.backend
            _remember_for_this_convo = app_module.LiteTUI._remember_for_this_convo
        app = Host()
        app._backend = old
        app._resume_backend_error = 'retain-error'
        app._agent_session = session
        app.settings = cfg
        app.convo_dir = session.conversation_directory(session.initial_conversation_id)
        app._convo_settings = convo_settings.born_from(cfg)
        app._system = events.append
        before = (session.memory_root / 'settings.json').read_bytes()
        def failed(*args):
            raise OSError('fixture backend publication refused')
        monkeypatch.setattr(agent_ownership.os, 'replace', failed)
        with pytest.raises(OSError, match='backend publication refused'):
            app.backend = SimpleNamespace(name='codex')
        assert app.backend is old
        assert app._resume_backend_error == 'retain-error'
        assert 'close' not in events
        assert session.authority.backend == old.name
        assert (session.memory_root / 'settings.json').read_bytes() == before
