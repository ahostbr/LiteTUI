"""Global routing has real controls for every parent backend."""
import json
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Input, Select, Switch

from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.settings_service import SettingChange, SettingsService
from litetui.subagent_routing import resolve_route


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['codex', 'lmstudio', 'claude'])
async def test_global_route_controls_save_and_reset_without_touching_overrides(tmp_path, backend):
    service = SettingsService(tmp_path)
    snap = service.create_conversation('one')
    service.save_patch('one', [SettingChange('subagent_route_override', {'backend': 'codex', 'model': 'child'}, 'conversation')], snap.revisions)
    snapshot = service.snapshot('one')

    class Host(App):
        model_id = 'parent'

        def on_mount(self):
            self.backend = SimpleNamespace(name=backend, remote=True,
                                           owns_native_turns=backend == 'claude',
                                           models={'parent': {}, 'child': {}},
                                           reasoning_levels=lambda _: ['medium'],
                                           **({'app_server': object()} if backend == 'codex' else {}))
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
        selector = body.query_one('#f-subagent_route', Select)
        assert not selector.disabled
        assert not body.query_one('#f-allow_local_subagents', Switch).value
        assert json.loads(body.query_one('#f-subagent_route_override', Input).value) == {'backend': 'codex', 'model': 'child'}
        selector.value = 'free'
        body.query_one('#subagent-route-model', Input).value = 'free-model'
        body.action_save()
        await pilot.pause()
        assert service.snapshot('two').saved.subagent_route == {'backend': 'free', 'model': 'free-model'}
        assert resolve_route(host) == {'backend': 'codex', 'model': 'child'}

    # An explicit reset is a change to an established route, including a null.
    result = service.save_patch('one', [SettingChange('subagent_route', None, 'device')], service.snapshot('one').revisions)
    assert result.fully_saved
    assert service.snapshot('two').saved.subagent_route is None
    assert resolve_route(host) == {'backend': 'codex', 'model': 'child'}
