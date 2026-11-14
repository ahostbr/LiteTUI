"""Actual Textual settings surface bound to scoped temporary storage."""
from types import SimpleNamespace
import pytest
from _owned_seat import conversation_id, owned_session
from textual.app import App
from litetui.settings_service import SettingsService
from litetui.settings_runtime import apply_saved_result
from litetui.settings_screen import SettingsScreen, SettingsBody


A, B = conversation_id('a'), conversation_id('b')


class BoundHost(App):
    backend = SimpleNamespace(name='lmstudio')
    model_id = 'test-model'

    def __init__(self, root):
        super().__init__()
        # Conversation writes need an owned agent session (e52007fe).
        self._agent_session = owned_session(root)
        self.service = SettingsService(root, agent_session=self._agent_session)
        self.settings = self.service.create_conversation(A).effective
        self.convo_dir = self._agent_session.conversation_directory(A)

    def on_mount(self):
        self.push_screen(SettingsScreen(self.settings,
            snapshot_provider=lambda: self.service.snapshot(A),
            save_patch=lambda changes, rev: self.service.save_patch(A, changes, rev),
            runtime_apply=lambda target, result: apply_saved_result(self, target, result)))


@pytest.mark.asyncio
async def test_bound_save_closes_on_restart_pending_and_persists_only_own_conversation(tmp_path):
    """Save writes only this conversation and closes even though the change applies on the next
    engine start. It used to stay open on "Runtime changes are pending"; 50ce7625 changed that on
    Ryan's word (2026-10-24): "i click save it doesnt close ... i press esc go back and the setting
    didnt change". The pending runtime outcome is still reported to the app.

    The conversation half uses tool_iterations: ninfer_max_context is device-wide since 5f5ef63f
    (2026-10-24, Ryan: "I set the context in settings to 100K for Strata ... It's still sitting at
    150K"), so every conversation sees it by design."""
    app = BoundHost(tmp_path)
    other = app.service.create_conversation(B)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        body.query_one('#f-ninfer_max_context').value = '16384'
        body.query_one('#f-tool_iterations').value = '7'
        body.action_save()
        await pilot.pause()
        assert app.service.snapshot(A).saved.ninfer_max_context == 16384
        assert app.service.snapshot(A).saved.tool_iterations == 7
        assert app.service.snapshot(B).saved.tool_iterations == other.saved.tool_iterations
        assert app.settings.ninfer_max_context == 32768
        assert not app.screen.query(SettingsBody)
        assert app._settings_save_result.has_pending_runtime


@pytest.mark.asyncio
async def test_saving_a_change_that_applies_later_closes_the_dialog(tmp_path):
    """A saved engine setting (context length) takes effect on the next engine start. The app says so in
    the chat when the dialog closes, so Save closes it; before, Save kept it open on "Runtime changes are
    pending" and Esc offered to save again (Ryan 2026-10-24, Strata context length, three screenshots).
    Control: an environment override of the saved value is still reported, not closed over."""
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
            assert body._has_unsaved_changes()
            body.action_save()
            await pilot.pause()
            assert app._settings_save_result.has_pending_runtime       # still reported to the app...
            assert not app.screen.query(SettingsBody)                  # ...and the dialog is gone


def test_only_a_deferred_save_is_non_blocking():
    from litetui.settings_apply import RuntimeSettingStatus, SettingsSaveResult
    from litetui.settings_screen import _blocking_pending_runtime
    deferred = RuntimeSettingStatus('strata_max_context', 'device', 'pending', 'restart', 1, 2, 'Saved; restart required')
    env = RuntimeSettingStatus('strata_max_context', 'device', 'pending', 'restart', 1, 2, 'Environment X overrides saved preference')
    assert not _blocking_pending_runtime(SettingsSaveResult(runtime=(deferred,)))
    assert _blocking_pending_runtime(SettingsSaveResult(runtime=(env,)))
    assert _blocking_pending_runtime(SettingsSaveResult(runtime=(deferred, env)))


@pytest.mark.asyncio
async def test_the_closing_note_says_how_a_restart_setting_applies(monkeypatch, tmp_path):
    """The chat note after Settings closes named only NInfer's spawn-time knobs; a Strata or Xolotl
    context length read as inert (the running engine keeps its flags until restarted)."""
    from copy import deepcopy
    from _owned_seat import owned_app
    from litetui import settings_runtime
    from litetui.settings_apply import PersistenceDestinationResult, SettingsSaveResult
    app = owned_app()
    said = []
    app._system = lambda msg, *a, **k: said.append(str(msg))
    monkeypatch.setattr(settings_runtime, 'persist_settings', lambda *a, **k: SettingsSaveResult(
        persistence=(PersistenceDestinationResult(str(tmp_path / 'settings.json'), 'device', True,
                                                  fields=('strata_max_context',)),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        new = deepcopy(app.settings)
        new.strata_max_context = 100000
        app._on_settings_saved(new)
    note = next(line for line in said if line.startswith('Settings saved'))
    assert 'strata_max_context' in note
    assert 'apply on the next start' in note and '/engine stop' in note
