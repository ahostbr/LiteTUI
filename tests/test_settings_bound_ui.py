"""Actual Textual settings surface bound to scoped temporary storage."""
from types import SimpleNamespace
import pytest
from textual.app import App
from litetui.settings_service import SettingsService
from litetui.settings_runtime import apply_saved_result
from litetui.settings_screen import SettingsScreen, SettingsBody


class BoundHost(App):
    backend = SimpleNamespace(name='lmstudio')
    model_id = 'test-model'

    def __init__(self, root):
        super().__init__()
        self.service = SettingsService(root)
        self.settings = self.service.create_conversation('a').effective
        self.convo_dir = root / '.convos' / 'a'

    def on_mount(self):
        self.push_screen(SettingsScreen(self.settings,
            snapshot_provider=lambda: self.service.snapshot('a'),
            save_patch=lambda changes, rev: self.service.save_patch('a', changes, rev),
            runtime_apply=lambda target, result: apply_saved_result(self, target, result)))


@pytest.mark.asyncio
async def test_bound_save_retains_restart_pending_and_persists_only_own_conversation(tmp_path):
    app = BoundHost(tmp_path)
    other = app.service.create_conversation('b')
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        body.query_one('#f-ninfer_max_context').value = '16384'
        body.action_save()
        await pilot.pause()
        assert app.service.snapshot('a').saved.ninfer_max_context == 16384
        assert app.service.snapshot('b').saved.ninfer_max_context == other.saved.ninfer_max_context
        assert app.settings.ninfer_max_context == 32768
        assert app.screen.query_one(SettingsBody)
        assert app._settings_save_result.has_pending_runtime


@pytest.mark.asyncio
async def test_a_saved_restart_pending_change_is_not_called_unsaved_on_exit(tmp_path):
    """The save stays open to report what applies later, but what is on screen IS saved: Esc/Cancel
    must not offer to save it again (Ryan 2026-10-24: Strata context length, 'pending changes and wont
    let me close')."""
    import uuid
    from litetui import settings as settings_mod
    from litetui.agent_launch_context import ordinary
    cfg = settings_mod.Settings(default_model='fixture-model')
    with ordinary(tmp_path, cfg) as session:
        service = SettingsService(tmp_path, agent_session=session)
        cid = str(uuid.uuid4())
        effective = service.create_conversation(cid).effective

        class Host(App):
            def __init__(self):
                super().__init__()
                self.settings = effective

            def on_mount(self):
                self.push_screen(SettingsScreen(
                    self.settings,
                    snapshot_provider=lambda: service.snapshot(cid),
                    save_patch=lambda changes, rev: service.save_patch(cid, changes, rev),
                    runtime_apply=lambda target, result: apply_saved_result(self, target, result)))

        app = Host()
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            body = app.screen.query_one(SettingsBody)
            body.query_one('#f-ninfer_max_context').value = '16384'
            await pilot.pause()
            assert body._has_unsaved_changes()          # control: an unsaved edit is still reported
            body.action_save()
            await pilot.pause()
            assert app._settings_save_result.has_pending_runtime
            assert not body._has_unsaved_changes()
