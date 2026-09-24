from types import SimpleNamespace
import pytest


def test_poll_transfers_busy_results_but_does_not_apply():
    from litetui.agent_parent_delivery import poll_receipts
    calls = []
    class Receipts:
        def replay_from_inbox(self, parent, **kwargs): calls.append(('transfer', parent))
    app = SimpleNamespace(_chat_running=lambda: True, _gui_quitting=False)
    assert poll_receipts(app, parent='parent', receipts=Receipts(), inbox=object(), registry=object()) == []
    assert calls == [('transfer', 'parent')]


def test_quit_does_not_take_new_receipt_responsibility():
    from litetui.agent_parent_delivery import poll_receipts
    app = SimpleNamespace(_gui_quitting=True)
    assert poll_receipts(app, parent='parent', receipts=None, inbox=None, registry=None) == []


def test_native_provider_is_not_silently_mutated():
    from litetui.agent_parent_delivery import poll_receipts
    calls = []
    class Receipts:
        def replay_from_inbox(self, parent, **kwargs): calls.append(parent)
    app = SimpleNamespace(_gui_quitting=False, _chat_running=lambda: False,
                          backend=SimpleNamespace(app_server=object()))
    assert poll_receipts(app, parent='parent', receipts=Receipts(), inbox=None, registry=None) == []
    assert calls == ['parent']


@pytest.mark.asyncio
async def test_real_textual_timer_delivers_and_replaces_previous_timer():
    from textual.app import App
    from litetui.app import LiteTUI
    calls = []
    class Receipts:
        def replay_from_inbox(self, parent, **kwargs): calls.append('transfer')
    class Host(App):
        _start_child_delivery = LiteTUI._start_child_delivery
        def _chat_running(self): return False
        def _apply_child_receipts(self, **kwargs): calls.append('apply'); return []
        def _system(self, text): calls.append(text)
    app = Host()
    async with app.run_test() as pilot:
        first = app._start_child_delivery(parent='parent', receipts=Receipts(), inbox=None, registry=None)
        second = app._start_child_delivery(parent='parent', receipts=Receipts(), inbox=None, registry=None)
        assert first is not second
        await pilot.pause(1.15)
        assert calls == ['transfer', 'apply']
        app._gui_quitting = True
        await pilot.pause(1.05)
        assert calls == ['transfer', 'apply']