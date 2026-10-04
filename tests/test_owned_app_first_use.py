"""Actual owned App/model chooser lifecycle; provider/network replaced, temp roots."""
from types import SimpleNamespace

import pytest

from litetui import app as app_module, paths, settings, convo_settings
from litetui.agent_launch_context import ordinary
from litetui.llm_backend import BackendError, ModelRow
from litetui.textfmt import memory_prompt, refresh_store_address


def test_storage_address_refresh_preserves_user_and_memory_snapshot(tmp_path):
    legacy = memory_prompt('old-cid', tmp_path / '.convos' / 'old-cid')
    snapshot = '\n## Your store, loaded once at the start of this conversation\nMEMORY SNAPSHOT USER TEXT'
    prompt = 'USER SYSTEM CUSTOM EDIT\n' + legacy + 'TRAILING UNHEADED USER TEXT' + snapshot
    result = refresh_store_address(prompt, 'new-cid', tmp_path / '.agents' / 'QuietHelm')
    assert '.convos/old-cid' not in result
    assert 'USER SYSTEM CUSTOM EDIT' in result
    assert snapshot in result
    assert 'TRAILING UNHEADED USER TEXT' in result
    assert '.agents/QuietHelm' in result


@pytest.mark.asyncio
@pytest.mark.parametrize('onboarding', [False, True])
async def test_unchosen_never_executes_and_actual_model_choice_persists(tmp_path, monkeypatch, onboarding):
    cfg = settings.Settings()
    cfg.default_model = None
    cfg.dialog_style = 'modal'
    cfg.user_name_asked = not onboarding
    cfg.user_name = 'Fixture'
    # This fixture is an ordinary human terminal, never our enclosing fleet seat.
    from litetui.seat_authority import AGENT_SHELL_MARKERS
    for name in (*AGENT_SHELL_MARKERS, 'LITESUITE_CANVAS_AGENT', 'LITEHARNESS_TIER',
                 'LITEHARNESS_AGENT_NAME', 'LITEHARNESS_SPAWNED_BY'):
        monkeypatch.delenv(name, raising=False)
    if onboarding:
        monkeypatch.delenv('LITETUI_TEST_USER_NAME_ASKED', raising=False)
        monkeypatch.setattr(app_module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    session = ordinary(tmp_path, cfg)
    app = app_module.LiteTUI(agent_session=session)
    messages = []
    app._system = messages.append
    app._convo_settings = convo_settings.born_from(cfg)
    with pytest.raises(BackendError, match='Choose a model'):
        await app._ensure_chat_ready()
    assert session.authority.model is None
    from litetui import launch_options
    from litetui.picker import PickerScreen
    async def prepared(_):
        return 'ok'
    async def discovered():
        return [ModelRow(key='real-fixture', path=None, loaded=True, source='fixture')]
    monkeypatch.setattr(launch_options, 'prepare', prepared)
    app._backend = SimpleNamespace(name='codex', label='Fixture', list_models=discovered,
                                   base_url=lambda: str(app.client.base_url), shutdown=lambda: None)
    monkeypatch.setattr(app_module.model_transport, 'bind_client', lambda *_: None)
    app._mcp_connect = lambda: None
    app.update_header = lambda: None
    app.fetch_context_window = lambda: None
    app.apply_context_length = lambda: None  # no real provider load in fixture
    app._rpc_emit_model_state = lambda: None
    app._convo_settings = convo_settings.born_from(cfg)
    async with app.run_test(size=(100, 30)) as pilot:
        if onboarding:
            from litetui.user_name_dialog import UserNameScreen
            for _ in range(15):
                await pilot.pause(0.1)
                if isinstance(app.screen, UserNameScreen):
                    break
            assert app._should_ask_user_name()
            assert isinstance(app.screen, UserNameScreen)
            # Discovery may finish while onboarding owns the modal; never stack.
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 2
            await pilot.press('enter')
        for _ in range(15):
            await pilot.pause(0.1)
            if isinstance(app.screen, PickerScreen):
                break
        assert isinstance(app.screen, PickerScreen), (messages, app.available_models, app._gui_connection_success)
        assert app.screen._title == 'Select a model'
        assert session.authority.model is None
        await pilot.press('escape')
        await pilot.pause(0.2)
        assert len(app.screen_stack) == 1
        with pytest.raises(BackendError, match='Choose a model'):
            await app._ensure_chat_ready()
        # A reconnect must not reoffer a cancelled first-use chooser.
        await app._connect().wait()
        await pilot.pause(0.2)
        assert len(app.screen_stack) == 1
        app._handle_command('/model')
        for _ in range(15):
            await pilot.pause(0.1)
            if isinstance(app.screen, PickerScreen):
                break
        assert isinstance(app.screen, PickerScreen)
        await pilot.press('enter')
        await pilot.pause()
        assert session.authority.model == 'real-fixture'
    # on_unmount releases the session; validate persisted authority by reopening.
    from litetui.agent_launch_context import acquire
    session = acquire(tmp_path, 'LiteTUI')
    app._agent_session = session
    assert session.authority.model == 'real-fixture'
    app.thinking_level = 'high'
    assert session.authority.thinking_level == 'high'
    app.store.release()
    session.release()


@pytest.mark.parametrize('state', ['released', 'stale', 'no-discovery', 'unmounted'])
def test_deferred_first_use_offer_does_not_revive_invalid_session(state):
    from litetui.agent_ownership import OwnershipError
    from litetui.agent_store import StoreError

    class Session:
        @property
        def authority(self):
            if state == 'released':
                raise OwnershipError('released')
            if state == 'stale':
                raise StoreError('changed')
            return SimpleNamespace(model=None)

    calls = []
    app = SimpleNamespace(
        _agent_session=Session(), _rpc=False,
        is_running=state != 'unmounted', available_models=[],
        screen_stack=[object()], _handle_command=calls.append,
    )
    app_module.LiteTUI._offer_unchosen_model(app)
    assert calls == []
    assert not getattr(app, '_unchosen_picker_offered', False)
