"""Claude submission uses attachment state owned by each individual message."""
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui.claude_persistence import ClaudeLedger


@pytest.mark.parametrize("maintenance", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_fresh_claude_submit_and_followup(tmp_path, monkeypatch, attached, maintenance):
    submitted, bubbles, spills = [], [], []
    image_path = str(tmp_path / "image.png")

    def spill(data):
        spills.append(data)
        return image_path

    app = SimpleNamespace(
        pending_image="AAAA" if attached else None,
        backend=SimpleNamespace(name="claude", owns_native_turns=True, session=None),
        convo_dir=tmp_path, convo_id="fresh", chosen_tool_profile="autonomous",
        tools_enabled=True, _pending_input=[], _mcp_maintenance=maintenance,
        notify=lambda *args, **kwargs: None,
        _materialise_convo=lambda: None, _chat_running=lambda: False,
        _split_image_path=lambda text: (None, text),
        _looks_like_image_path=lambda text: False,
        _oversize_refusal=lambda content: None,
        _spill_image_for_reclick=spill,
        _system=lambda text: None,
        _user_bubble=lambda text, has_image, **kwargs: bubbles.append((text, has_image, kwargs)),
        _scroll_down=lambda **kwargs: None,
    )
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda app, item: submitted.append(item))

    app_mod.LiteTUI._submit_text(app, "hey buddy", alt_chord=False)
    app_mod.LiteTUI._submit_text(app, "followup", alt_chord=False)

    if maintenance:
        assert submitted == []
        submitted = app._pending_input
    assert len(submitted) == 2
    assert submitted[1]["content"] == "followup"
    assert bubbles[1][2]["image_path"] is None
    assert app.pending_image is None
    if attached:
        assert image_path in submitted[0]["content"]
        assert bubbles[0][2]["image_path"] == image_path
        assert spills == ["AAAA"]
    else:
        assert submitted[0]["content"] == "hey buddy"
        assert bubbles[0][2]["image_path"] is None
        assert spills == []
    ledger = ClaudeLedger(tmp_path)
    assert [entry["content"] for entry in ledger.pending(ledger.selected["id"])] == [
        item["content"] for item in submitted
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["default", "haiku", "claude-haiku-5-5"])
async def test_owned_first_hello_reaches_native_turn_after_model_choice(tmp_path, monkeypatch, model):
    """Real submit/admission/chat worker; only native provider execution is replaced."""
    from litetui import claude_turn, paths, settings
    from litetui.agent_launch_context import ordinary
    from litetui.claude_backend import ClaudeBackend
    from litetui.llm_backend import ModelRow
    from litetui.plugins.model_switch import switch_model

    cfg = settings.Settings(backend="claude", default_model="default",
                            user_name="Fixture", user_name_asked=True, mcp_enabled=False)
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    # Mount UI without starting fleet/MCP/provider background integrations.
    monkeypatch.setattr(app_mod.plugins_mod, "activate_plugins", lambda *args: None)
    monkeypatch.setenv("LITETUI_DISABLE_UPDATE_CHECK", "1")
    dispatched, notices = [], []

    async def native(app):
        dispatched.append((app.model_id, app.conversation[-1]["content"]))

    monkeypatch.setattr(claude_turn, "stream_turn", native)
    with ordinary(tmp_path, cfg) as owned:
        app = app_mod.LiteTUI(agent_session=owned)
        app.backend = ClaudeBackend(cfg)
        app.backend.models = {key: {"value": key} for key in ("default", "haiku", "claude-haiku-5-5")}
        app.available_models = list(app.backend.models)
        app.model_rows = {key: ModelRow(key=key, path=None, source="Claude Agent", loaded=True)
                          for key in app.available_models}
        app._connect = lambda: None
        app.connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app.fetch_context_window = lambda: None
        app.apply_context_length = lambda: None
        app._rpc_emit_model_state = lambda: None
        app._system = notices.append
        app.jobs[:] = []
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                assert switch_model(app, model)
                app._submit_text("hello", alt_chord=False)
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert dispatched == [(model, "hello")]
                assert owned.authority.backend == "claude"
                assert owned.authority.model == model
                assert not any("legacy model requests" in notice for notice in notices)
                assert not any("NOT loaded" in notice for notice in notices)
        finally:
            app.store.release()
