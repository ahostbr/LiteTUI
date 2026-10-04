"""Actual owned App/model chooser lifecycle; provider/network replaced, temp roots."""
import asyncio
from types import SimpleNamespace

import pytest

from litetui import app as app_module, paths, settings, convo_settings
from litetui.agent_launch_context import ordinary
from litetui.llm_backend import BackendError
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
async def test_unchosen_never_executes_and_actual_model_choice_persists(tmp_path, monkeypatch):
    cfg = settings.Settings()
    cfg.default_model = None
    cfg.dialog_style = 'modal'
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    session = ordinary(tmp_path, cfg)
    app = app_module.LiteTUI(agent_session=session)
    app._system = lambda text: None
    app._convo_settings = convo_settings.born_from(cfg)
    with pytest.raises(BackendError, match='Choose a model'):
        await app._ensure_chat_ready()
    assert session.authority.model is None
    from litetui import launch_options
    from litetui.plugins import model_switch
    from litetui.picker import PickerScreen
    async def prepared(_):
        return 'ok'
    async def discovered():
        return [SimpleNamespace(key='real-fixture', loaded=True, source='fixture')]
    monkeypatch.setattr(launch_options, 'prepare', prepared)
    app._backend = SimpleNamespace(name='codex', label='Fixture', list_models=discovered,
                                   base_url=lambda: str(app.client.base_url), shutdown=lambda: None)
    monkeypatch.setattr(app_module.model_transport, 'bind_client', lambda *_: None)
    app._connect = lambda: None
    app._mcp_connect = lambda: None
    app.update_header = lambda: None
    app.fetch_context_window = lambda: None
    app.apply_context_length = lambda: None  # no real provider load in fixture
    app._rpc_emit_model_state = lambda: None
    app._convo_settings = convo_settings.born_from(cfg)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.connect().wait()
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        assert app.screen._title == 'Select a model'
        assert session.authority.model is None
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
