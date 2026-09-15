from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

from litetui.app import LiteTUI
from litetui.model_transport import ProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unconfirmed", "failed", "accepted", "switched", "before_start"])
async def test_recovery_worker_does_not_retrigger_itself(monkeypatch, outcome):
    jobs, notices, scheduled = [], [], []
    app = NS(convo_id="original", conversation=[], backend=NS(app_server=object()),
             _pending_input=[{"_codex_entry": {"state": "sending"}}],
             _chat_running=lambda: False, _system=notices.append,
             _flush_pending_input=lambda: None, call_after_refresh=scheduled.append)
    app.run_worker = lambda coro, **kwargs: jobs.append((coro, kwargs))

    async def recover(host, server):
        assert outcome != "before_start"
        if outcome == "failed":
            raise ProviderError("PRIVATE synthetic persistence failure")
        if outcome == "switched":
            host.conversation = []
        return outcome == "accepted"

    monkeypatch.setattr("litetui.codex_steering.recover_queue_head", recover)
    LiteTUI._flush_pending_input(app)
    LiteTUI._flush_pending_input(app)
    assert len(jobs) == 1
    coroutine, options = jobs[0]
    assert options["group"] == "codex-recovery"
    if outcome == "before_start":
        app.conversation = []
    await coroutine
    assert app._codex_recovery_pending is False
    before = len(scheduled)
    LiteTUI.on_worker_state_changed(app, NS(worker=NS(group=options["group"]), state=WorkerState.SUCCESS))
    assert len(scheduled) == before == (1 if outcome == "accepted" else 0)
    assert len(notices) == (1 if outcome in ("failed", "unconfirmed") else 0)
    assert "PRIVATE" not in str(notices)
    assert app._pending_input  # The fixture never consumes or resends input.
