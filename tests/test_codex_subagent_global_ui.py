"""The global child preference has a real, scoped settings control."""
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Select

from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.settings_service import SettingsService
from litetui.settings_ui_adapter import changes_between


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['codex', 'lmstudio'])
async def test_global_control_collects_without_changing_local_routing(backend):
    settings = Settings(backend=backend, subagent_model='local-child')

    class Host(App):
        model_id = 'parent'

        def on_mount(self):
            self.backend = SimpleNamespace(name=backend, reasoning_levels=lambda _: ['medium'],
                                           **({'app_server': object()} if backend == 'codex' else {}))
            self.push_screen(SettingsScreen(settings, ['parent', 'child'], [], [], True))

    host = Host()
    async with host.run_test() as pilot:
        await pilot.pause()
        body = host.screen.query_one(SettingsBody)
        widget = body.query_one('#f-codex_subagent_model', Select)
        assert widget.disabled == (backend != 'codex')
        local = body.query_one('#f-subagent_model', Select)
        assert local.disabled == (backend == 'codex')
        if backend == 'codex':
            widget.value = 'child'
        saved = body._collect()
        assert saved.subagent_model == 'local-child'
        assert saved.codex_subagent_model == ('child' if backend == 'codex' else None)


@pytest.mark.asyncio
async def test_dialog_shows_legacy_fallback_and_can_publish_first_null(tmp_path):
    import json

    from litetui.plugins.subagent_plugin import _resolve_model
    from litetui.settings_service import SettingChange

    service = SettingsService(tmp_path)
    snapshot = service.create_conversation('one')
    service.save_patch('one', [SettingChange('subagent_model', 'child', 'conversation')],
                       snapshot.revisions)
    snapshot = service.snapshot('one')
    assert snapshot.saved.codex_subagent_model == 'child'

    class Host(App):
        model_id = 'parent'

        def on_mount(self):
            self.backend = SimpleNamespace(name='codex', remote=True, app_server=object(),
                                           models={'parent': {}, 'child': {}},
                                           reasoning_levels=lambda _: ['medium'])
            self.push_screen(SettingsScreen(
                snapshot.effective, ['parent', 'child'], [], [], True,
                snapshot_provider=lambda: service.snapshot('one'),
                save_patch=lambda changes, revisions: service.save_patch('one', changes, revisions),
            ))

    host = Host()
    host.settings = snapshot.effective
    host._settings_service = service
    host.convo_dir = tmp_path / '.convos' / 'one'
    async with host.run_test() as pilot:
        await pilot.pause()
        body = host.screen.query_one(SettingsBody)
        widget = body.query_one('#f-codex_subagent_model', Select)
        assert widget.value == 'child'
        widget.value = ''
        body.action_save()
        await pilot.pause()
        raw = json.loads(tmp_path.joinpath('settings.json').read_text())
        assert raw['codex_subagent_model'] is None
        assert service.snapshot('one').saved.subagent_model == 'child'
        assert _resolve_model(host, '') == 'parent'


def test_settings_dialog_patch_routes_new_field_to_global(tmp_path):
    service = SettingsService(tmp_path)
    baseline = service.create_conversation('one')
    service.create_conversation('two')
    requested = Settings(**vars(baseline.saved))
    requested.codex_subagent_model = 'child'
    changes = changes_between(baseline.effective, requested)
    assert [(c.key, c.scope) for c in changes] == [('codex_subagent_model', 'device')]
    result = service.save_patch('one', changes, baseline.revisions)
    assert result.fully_saved
    assert service.snapshot('two').effective.codex_subagent_model == 'child'
