"""Scoped persistence proves ownership before mkdir/coordinator locks; temp only."""
import pytest

from litetui.agent_launch_context import create
from litetui.agent_store import StoreError
from litetui import convo_settings, settings
from litetui.settings_service import SettingsService, SettingChange

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'


def test_unowned_settings_snapshot_reads_but_mutations_leave_no_state(tmp_path):
    service = SettingsService(tmp_path)
    snapshot = service.snapshot(CID)
    with pytest.raises(StoreError, match='owned'):
        service.create_conversation(CID)
    result = service.save_patch(CID, [SettingChange('default_model', 'fixture', 'conversation')], snapshot.revisions)
    assert not result.persistence[0].saved
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(StoreError, match='owned'):
        convo_settings.save(tmp_path / '.convos' / CID, convo_settings.ConvoSettings())
    assert list(tmp_path.iterdir()) == []


def test_unowned_global_only_patch_is_still_possible(tmp_path):
    service = SettingsService(tmp_path)
    snap = service.snapshot(CID)
    result = service.save_patch(CID, [SettingChange('theme_name', 'textual-dark', 'device')], snap.revisions)
    assert result.persistence[0].saved
    assert not (tmp_path / '.convos').exists()


def test_owned_settings_destination_and_sibling_refusal(tmp_path):
    with create(tmp_path, 'QuietHelm', agent_id=AID, backend='codex', model='fixture', thinking_level='high') as session:
        cid = session.initial_conversation_id
        service = SettingsService(tmp_path, agent_session=session)
        service.create_conversation(cid)
        directory = session.conversation_directory(cid)
        assert (directory / 'settings.json').is_file()
        convo_settings.save(directory, convo_settings.born_from(settings.Settings()), agent_session=session)
        with pytest.raises(StoreError, match='outside'):
            convo_settings.save(tmp_path / '.convos' / cid, convo_settings.ConvoSettings(), agent_session=session)
        assert not (tmp_path / '.convos').exists()
