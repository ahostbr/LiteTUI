from types import SimpleNamespace
from litetui.settings_service import SettingsService, SettingChange
from litetui.settings_runtime import service_for


def test_resume_other_root_rebinds_conversation_storage_without_changing_device_root(tmp_path):
    device = tmp_path / 'device'
    first = SettingsService(device, tmp_path / 'first')
    second = SettingsService(device, tmp_path / 'second')
    first.create_conversation('same')
    second.create_conversation('same')
    app = SimpleNamespace(convo_dir=tmp_path / 'first' / 'same', _settings_service=first)
    assert service_for(app) is first
    original = (app.convo_dir / 'settings.json').read_bytes()
    app.convo_dir = tmp_path / 'second' / 'same'
    selected = service_for(app)
    assert selected.conversation_root == tmp_path / 'second'
    assert selected.root == device
    result = selected.save_patch('same', (SettingChange('subagent_model', 'child', 'conversation'),),
                                 selected.snapshot('same').revisions)
    assert result.fully_saved
    assert second.snapshot('same').saved.subagent_model == 'child'
    assert (tmp_path / 'first' / 'same' / 'settings.json').read_bytes() == original
    assert service_for(app) is selected
