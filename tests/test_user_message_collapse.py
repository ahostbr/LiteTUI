"""User cards share assistant folding and cancel only still-unsent queue items."""
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.containers import VerticalScroll

from litetui.app import LiteTUI
from litetui.widgets import AssistantMessage, UserMessage


class CardApp(App):
    """Real layout/event loop with the shipping card CSS and queue methods."""
    CSS = LiteTUI.CSS
    _user_bubble = LiteTUI._user_bubble
    _scroll_down = LiteTUI._scroll_down
    _autocollapse_offscreen = LiteTUI._autocollapse_offscreen
    _flush_pending_input = LiteTUI._flush_pending_input

    def __init__(self):
        super().__init__()
        self.settings = SimpleNamespace(autoscroll=True)
        self._follow_generation = 0
        self._scroll_queued = None
        self._pending_input = []
        self.backend = SimpleNamespace(name="test")

    def compose(self):
        yield VerticalScroll(id="chat-log")

    def get_css_variables(self):
        from litetui import themes
        variables = super().get_css_variables()
        for key, value in themes.extra_defaults(
            primary=variables["primary"], bone=variables["foreground"]
        ).items():
            variables.setdefault(key, value)
        return variables

    def on_user_message_cancel_queued(self, event):
        LiteTUI.on_user_message_cancel_queued(self, event)

    def _chat_running(self):
        return False


@pytest.mark.asyncio
async def test_app_user_header_keeps_fold_marker_and_click_toggles():
    app = CardApp()
    async with app.run_test(size=(100, 40)) as pilot:
        card = app._user_bubble("long user prompt\nsecond line", False)
        await pilot.pause()
        assert card.border_title.startswith("▾")
        await pilot.click(card, offset=(4, 0))
        assert card.collapsed
        await pilot.click(card, offset=(4, 0))
        assert not card.collapsed


@pytest.mark.asyncio
async def test_offscreen_user_folds_in_same_follow_pass_and_stays_reopened():
    app = CardApp()
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat-log")
        user = app._user_bubble("prompt\n" * 4, False)
        assistant = AssistantMessage()
        await log.mount(assistant)
        assistant.set_answer("reply\n\n" * 60)
        await pilot.pause()
        app._scroll_down()
        await pilot.pause()
        assert user.collapsed
        assert not assistant.collapsed
        user.set_collapsed(False)
        app._scroll_down()
        await pilot.pause()
        assert not user.collapsed


@pytest.mark.asyncio
async def test_queued_x_removes_only_clicked_duplicate_and_cannot_send_it(monkeypatch):
    from litetui import hook_host

    app = CardApp()
    async with app.run_test(size=(100, 40)) as pilot:
        first = app._user_bubble("same prompt", False, queued=True)
        second = app._user_bubble("same prompt", False, queued=True)
        items = [{"content": "same prompt", "bubble": first},
                 {"content": "same prompt", "bubble": second}]
        app._pending_input[:] = items
        await pilot.pause()
        assert "X" in first.border_title
        await pilot.click(first, offset=(5, 0))
        await pilot.pause()
        assert app._pending_input == [items[1]]
        assert not first.is_attached
        assert not second.collapsed
        sent = []
        monkeypatch.setattr(hook_host, "start_prompt", lambda app, item: sent.append(item))
        app._flush_pending_input()
        assert sent == [items[1]]
        assert not app._pending_input


def test_queued_card_does_not_autofold_and_delivery_removes_x():
    card = UserMessage("prompt", queued=True)
    assert card.autocollapse() is False
    card.set_collapsed(True)
    assert "X" in card.border_title
    card.mark_delivered()
    assert "X" not in card.border_title
    card.set_collapsed(False)
    assert card.autocollapse() is True


@pytest.mark.asyncio
async def test_unlocked_reader_keeps_offscreen_user_expanded():
    app = CardApp()
    app.settings.autoscroll = False
    async with app.run_test(size=(100, 40)) as pilot:
        user = app._user_bubble("prompt", False)
        reply = AssistantMessage()
        await app.query_one("#chat-log").mount(reply)
        reply.set_answer("reply\n\n" * 60)
        await pilot.pause()
        app._scroll_down()
        await pilot.pause()
        assert not user.collapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_cancelled_native_queue_does_not_restore(tmp_path, monkeypatch, provider):
    from litetui import claude_turn, codex_steering
    from litetui.claude_persistence import ClaudeLedger

    app = CardApp()
    app.convo_id = "test-conversation"
    if provider == "claude":
        ledger = ClaudeLedger(tmp_path)
        segment = ledger.select_segment(str(tmp_path))
        entry = ledger.prepare(segment["id"], "cancel me", "scheduled", "typed")
        metadata = {"_claude_entry": entry, "_claude_conversation": app.convo_id}
        monkeypatch.setattr(claude_turn, "ledger_for", lambda app: ledger)
    else:
        metadata = {"steering": []}
        ledger = codex_steering.SteeringLedger(metadata["steering"], lambda: None)
        entry = ledger.enqueue({"content": "cancel me"}, "thread", "turn")
        app.conversation = [{"provider_metadata": metadata}]
        metadata = {"_codex_entry": entry, "_codex_ledger": ledger}
    async with app.run_test(size=(100, 40)) as pilot:
        card = app._user_bubble("cancel me", False, queued=True)
        app._pending_input.append({"content": "cancel me", "bubble": card, **metadata})
        await pilot.pause()
        await pilot.click(card, offset=(5, 0))
        await pilot.pause()
        assert not app._pending_input
        if provider == "claude":
            assert not ClaudeLedger(tmp_path).pending(segment["id"])
        else:
            codex_steering.restore_queue(app)
            assert not app._pending_input


@pytest.mark.asyncio
async def test_inflight_native_input_is_not_falsely_cancelled():
    app = CardApp()
    async with app.run_test(size=(100, 40)) as pilot:
        card = app._user_bubble("already sending", False, queued=True)
        item = {"content": "already sending", "bubble": card,
                "_codex_entry": {"state": "sending"}}
        app._pending_input.append(item)
        await pilot.pause()
        await pilot.click(card, offset=(5, 0))
        await pilot.pause()
        assert app._pending_input == [item]
        assert card.is_attached


@pytest.mark.asyncio
async def test_folded_queue_keeps_clickable_x():
    app = CardApp()
    async with app.run_test(size=(100, 40)) as pilot:
        card = app._user_bubble("cancel while folded", False, queued=True)
        card.set_collapsed(True)
        app._pending_input.append({"content": "cancel while folded", "bubble": card})
        await pilot.pause()
        await pilot.click(card, offset=(5, 0))
        await pilot.pause()
        assert not app._pending_input
        assert not card.is_attached
