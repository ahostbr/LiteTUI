from types import SimpleNamespace
import pytest
from litetui.agent_receipts import ParentReceipts
from litetui.agent_parent_wake import wake_parent


@pytest.fixture
def state(tmp_path):
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    receipts.accept_for_conversation('parent', 'chat', {'completion_id': 'completion', 'result': {
        'child_id': 'child', 'conversation_id': 'child-chat', 'status': 'completed',
        'summary': 'done', 'cleanup': {}, 'evidence': []}})
    receipts.mark_applied('parent', 'completion')
    messages = [{'role': 'user', 'content': 'already committed result'}]
    app = SimpleNamespace(_chat_running=lambda: False, backend=object(), convo_id='chat',
        conversation=messages, settings=SimpleNamespace(tool_policy_profile='autonomous'),
        store=SimpleNamespace(convo_id='chat', owned=True, pending=False, loading=False,
                              convo_path=tmp_path / 'history', read=lambda p: ({}, messages[:])))
    return app, receipts


@pytest.mark.asyncio
async def test_wake_does_not_append_and_finishes_only_after_worker(state):
    app, receipts = state
    calls = []
    class Worker:
        async def wait(self):
            assert receipts.uncertain_wakes('parent', 'chat') == ['completion']
            calls.append('wait')
    app._stream = lambda: Worker()
    before = app.conversation[:]
    assert await wake_parent(app, parent='parent', receipts=receipts) == ['completion']
    assert calls == ['wait']
    assert app.conversation == before
    assert app._hook_source == 'child-result'
    assert not receipts.uncertain_wakes('parent', 'chat')
    assert await wake_parent(app, parent='parent', receipts=receipts) == []


@pytest.mark.asyncio
async def test_cancelled_worker_retains_uncertain_claim(state):
    import asyncio
    app, receipts = state
    class Worker:
        async def wait(self): raise asyncio.CancelledError()
    app._stream = lambda: Worker()
    with pytest.raises(asyncio.CancelledError):
        await wake_parent(app, parent='parent', receipts=receipts)
    assert receipts.uncertain_wakes('parent', 'chat') == ['completion']
    assert await wake_parent(app, parent='parent', receipts=receipts) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['busy', 'quit', 'queued', 'stopped', 'native', 'hooks'])
async def test_wake_defers_without_claiming(state, reason, monkeypatch):
    app, receipts = state
    if reason == 'busy': app._chat_running = lambda: True
    if reason == 'quit': app._gui_quitting = True
    if reason == 'queued': app._pending_input = [{}]
    if reason == 'stopped': app._stop_requested = True
    if reason == 'native': app.backend = SimpleNamespace(app_server=object())
    if reason == 'hooks':
        from litetui import hook_host
        monkeypatch.setattr(hook_host, 'snapshot', lambda a: SimpleNamespace(hooks=[object()], error=None))
    assert await wake_parent(app, parent='parent', receipts=receipts) == []
    assert receipts.claim_wake('parent', 'chat') == ['completion']


@pytest.mark.asyncio
async def test_real_textual_worker_wakes_once_without_reappending(state):
    from textual.app import App
    from textual import work
    from litetui.app import LiteTUI
    fixture, receipts = state
    class Host(App):
        _schedule_child_wake = LiteTUI._schedule_child_wake
        def _chat_running(self): return False
        def _system(self, text): self.notices.append(text)
        @work(group='chat', exclusive=True)
        async def _stream(self): self.turns += 1
    app = Host()
    for name in ('backend', 'convo_id', 'conversation', 'settings', 'store'):
        setattr(app, name, getattr(fixture, name))
    app.notices, app.turns = [], 0
    async with app.run_test() as pilot:
        app._schedule_child_wake(parent='parent', receipts=receipts)
        await pilot.pause(0.15)
        assert app.turns == 1
        assert not receipts.uncertain_wakes('parent', 'chat')
        app._schedule_child_wake(parent='parent', receipts=receipts)
        await pilot.pause(0.15)
        assert app.turns == 1
        assert len(app.conversation) == 1
        assert not app.notices

def test_uncertain_restart_displays_one_recovery_notice_without_retry(state):
    from litetui.app import LiteTUI
    app, receipts = state
    receipts.claim_wake('parent', 'chat')
    notices = []
    app._system = notices.append
    def worker(coro, **kwargs):
        coro.close()
        return SimpleNamespace(is_finished=True)
    app.run_worker = worker
    LiteTUI._schedule_child_wake(app, parent='parent', receipts=receipts)
    LiteTUI._schedule_child_wake(app, parent='parent', receipts=receipts)
    assert len(notices) == 1
    assert 'completion' in notices[0]
    assert 'No automatic retry' in notices[0]
    assert receipts.uncertain_wakes('parent', 'chat') == ['completion']

# ── batch 3: MCP-maintenance gate outcomes on the CLAIMED receipt ──────────────
# The gate races: wake_parent's pre-claim guard already defers a wake that STARTS
# during maintenance, but maintenance can begin AFTER that guard and BEFORE the
# turn generates. Then the receipt is already claimed and the gate defers. A
# deferral there is provably side-effect free, so the claim returns to pending
# (re-fires); any other failure keeps it claimed (uncertain) — never re-run.
@pytest.mark.asyncio
async def test_gate_deferral_releases_claim_to_pending(state):
    from litetui.turn_deferral import STREAM_DEFERRED
    app, receipts = state

    class Worker:                       # _stream deferred at the gate → sentinel
        async def wait(self):
            return STREAM_DEFERRED

    app._stream = lambda: Worker()
    assert await wake_parent(app, parent='parent', receipts=receipts) == []
    assert not receipts.uncertain_wakes('parent', 'chat')          # not left claimed
    assert receipts.claim_wake('parent', 'chat') == ['completion']  # re-fires cleanly


@pytest.mark.asyncio
async def test_non_deferral_failure_keeps_claim_uncertain(state):
    from textual.worker import WorkerFailed
    app, receipts = state

    class Worker:
        async def wait(self):
            raise WorkerFailed(RuntimeError("boom"))

    app._stream = lambda: Worker()
    with pytest.raises(WorkerFailed):
        await wake_parent(app, parent='parent', receipts=receipts)
    assert receipts.uncertain_wakes('parent', 'chat') == ['completion']  # NOT released


def test_release_wake_refuses_non_claimed(state):
    app, receipts = state
    with pytest.raises(ValueError):                       # still pending, never claimed
        receipts.release_wake('parent', 'chat', ['completion'])
    receipts.claim_wake('parent', 'chat')
    receipts.finish_wake('parent', 'chat', ['completion'])
    with pytest.raises(ValueError):                       # finished, not claimed
        receipts.release_wake('parent', 'chat', ['completion'])


@pytest.mark.asyncio
async def test_mounted_stream_blocks_during_maintenance_then_resumes():
    # The gate inside a REAL Textual @work worker: it suspends the turn on the
    # completion Event and runs it exactly once after maintenance settles.
    import asyncio
    from textual.app import App
    from textual import work
    from litetui.app import LiteTUI

    class Host(App):
        _await_mcp_maintenance = LiteTUI._await_mcp_maintenance

        @work(group='chat', exclusive=True)
        async def _stream(self):
            await self._await_mcp_maintenance()
            self.turns += 1

    app = Host()
    app.backend, app.convo_id, app._stop_requested = object(), 'chat', False
    app.turns, app._mcp_maintenance = 0, True
    async with app.run_test() as pilot:
        app._mcp_maintenance_done = asyncio.Event()       # created on the loop
        app._stream()
        await pilot.pause(0.05)
        assert app.turns == 0                             # blocked in the gate
        app._mcp_maintenance = False                      # settle (flag then event)
        app._mcp_maintenance_done.set()
        await pilot.pause(0.05)
        assert app.turns == 1                             # resumed, ran once
