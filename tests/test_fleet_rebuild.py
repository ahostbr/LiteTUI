"""Every host prompt rebuild retains the live registered seat's fleet guidance."""
from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui import appsvc
from litetui.conversation import ConversationRepository
from litetui.plugins import convo


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["tools", "plan-on", "plan-off", "new", "system"])
@pytest.mark.parametrize("registered", [True, False])
async def test_rebuild_carries_only_registered_fleet_text(make_app, action, registered):
    app = make_app()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app._apply_context_length = lambda: None
    app._materialise_convo()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        if action == "plan-off":
            assert app.set_plan_mode(True, announce=False)
        app.seat.registered = registered
        sentence = app._fleet_identity_sentence()
        capabilities = app_mod.load_prompt("harness-capabilities", app=app)
        assert capabilities.strip(), "the capability check must not match an empty prompt"
        if registered:
            # Registration can finish after message 0 and its store snapshot exist.
            app._request_messages()
            app._append_to_system(sentence + capabilities)
            assert app.conversation[0]["content"].count(sentence) == 1

        if action == "tools":
            app.action_toggle_tools()
        elif action.startswith("plan-"):
            assert app.set_plan_mode(action == "plan-on", announce=False)
        elif action == "new":
            convo._cmd_new(app, "/new", "")
        else:
            convo._cmd_system(app, "/system", "MY REPLACEMENT BODY")
        await pilot.pause()

        text = app.conversation[0]["content"]
        assert text.count(sentence) == int(registered)
        assert text.count(capabilities) == int(registered)
        assert text.count(appsvc.FLEET_OPENING) == int(registered)
        if action == "system":
            assert "MY REPLACEMENT BODY" in text
        requested = app._request_messages()[0]["content"]
        assert requested.count(sentence) == int(registered)
        assert requested.count(capabilities) == int(registered)
        if registered:
            assert requested.index(sentence) < requested.index(appsvc.STORE_HEADER)
            assert sentence + capabilities in appsvc.strip_store_block(requested)
        app._materialise_convo()  # /new remains staged until its first persisted turn.
        replay = ConversationRepository.read(app.convo_path)[1][0]["content"]
        assert replay == app.conversation[0]["content"]


@pytest.mark.parametrize("store_first", [True, False])
def test_late_registration_after_rebuild_is_idempotent(make_app, store_first):
    app = make_app()
    app._materialise_convo()
    assert not app.seat.registered
    app.conversation[0] = app._host_prompt_message()
    assert appsvc.FLEET_OPENING not in app.conversation[0]["content"]
    if store_first:
        app._request_messages()
    app.seat.registered = True
    fleet = app._fleet_identity_sentence() + app_mod.load_prompt("harness-capabilities", app=app)
    app._append_to_system(fleet)
    before = app.convo_path.read_bytes()
    app._append_to_system(fleet)
    assert app.convo_path.read_bytes() == before, "duplicate registration must not write an edit"
    text = app._request_messages()[0]["content"]
    assert text.count(fleet) == 1
    assert text.index(fleet) < text.index(appsvc.STORE_HEADER)
    assert fleet in appsvc.strip_store_block(text)


def test_native_claude_keeps_one_fleet_block_after_the_store(make_app, monkeypatch):
    from litetui.claude_turn import ledger_for, system_prompt_for

    app = make_app()
    app._materialise_convo()
    app.seat.registered = True
    monkeypatch.setattr(app, "_all_tools", lambda: [
        {"type": "function", "function": {"name": "harness"}},
    ])
    ledger = ledger_for(app)
    segment = ledger.select_segment("ws")
    text = system_prompt_for(app, segment)
    sentence = app._fleet_identity_sentence()
    capabilities = app_mod.load_prompt("harness-capabilities", app=app).strip()
    assert capabilities
    assert text.count(sentence) == 1
    assert text.count(capabilities) == 1
    assert text.index(appsvc.STORE_HEADER) < text.index(sentence)
    assert system_prompt_for(app, ledger.segment(segment["id"])) == text
