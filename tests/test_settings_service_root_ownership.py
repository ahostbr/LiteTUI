from types import SimpleNamespace

import pytest

from litetui.agent_launch_context import ordinary
from litetui.agent_store import StoreError
from litetui import settings
from litetui.settings_service import SettingsService, SettingChange
from litetui.settings_runtime import service_for

CID = '33333333-3333-4333-8333-333333333333'


def test_resume_other_root_rebinds_conversation_storage_without_changing_device_root(tmp_path):
    """Owned capability forbids arbitrary rebind; archive snapshots stay read-only."""
    cfg = settings.Settings(default_model='fixture-model')
    with ordinary(tmp_path, cfg) as session:
        first = SettingsService(tmp_path, agent_session=session)
        cid = session.initial_conversation_id
        first.create_conversation(cid)
        directory = session.conversation_directory(cid)
        app = SimpleNamespace(convo_dir=directory, _settings_service=first, _agent_session=session)
        assert service_for(app) is first
        original = (directory / 'settings.json').read_bytes()
        authority = session.authority
        foreign = tmp_path / 'second' / CID
        app.convo_dir = foreign
        with pytest.raises(StoreError):
            service_for(app)
        assert app._settings_service is first
        assert session.authority == authority
        assert (directory / 'settings.json').read_bytes() == original
        assert not foreign.parent.exists()

    # Retained archive consumers may rebind their readonly snapshot root without
    # claiming a capability or permitting a mutable create/save at either root.
    device = tmp_path / 'device'
    archive = SettingsService(device, tmp_path / 'first')
    app = SimpleNamespace(convo_dir=tmp_path / 'first' / CID, _settings_service=archive)
    assert service_for(app) is archive
    app.convo_dir = tmp_path / 'second' / CID
    selected = service_for(app)
    assert selected.conversation_root == tmp_path / 'second'
    assert selected.root == device
    assert selected.snapshot(CID).saved.subagent_model is None
    with pytest.raises(StoreError):
        selected.create_conversation(CID)
    result = selected.save_patch(CID, (SettingChange('subagent_model', 'child', 'conversation'),),
                                 selected.snapshot(CID).revisions)
    assert not result.fully_saved
    assert len(result.persistence) == 1 and not result.persistence[0].saved
    assert 'owned agent session' in result.persistence[0].error
    assert not (tmp_path / 'first').exists()
    assert not (tmp_path / 'second').exists()
