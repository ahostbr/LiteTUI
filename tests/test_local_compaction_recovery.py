"""T0245 local backend arm: paused tool loop resumes without loop-mode opt-in."""
from types import SimpleNamespace

import pytest
from test_wake_after_compact import _app, _ok_create, _seed, _settle


@pytest.mark.asyncio
async def test_local_success_resumes_interrupted_turn_once_with_wake_off():
    app = _app(wake_after_compact=False)
    _seed(app)
    started = []
    app._active_turn_started_at = 123.0
    continuation = (app.backend, app.convo_id, 123.0)
    app._interrupted_compact_resume = continuation
    app._user_bubble = lambda *args: None
    app._stream = lambda: started.append(True)
    app.client.chat.completions.create = lambda **kw: _ok_create("summary", **kw)
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)
    app._continue_interrupted_compact(continuation)
    assert started == [True]
    assert app._interrupted_compact_resume is None


@pytest.mark.parametrize("block", ["stop", "queued", "busy", "backend", "convo", "turn", "stop_requested"])
def test_local_continuation_yields_to_user_or_changed_owner(block):
    app = _app(wake_after_compact=False)
    _seed(app)
    app._active_turn_started_at = 123.0
    continuation = (app.backend, app.convo_id, 123.0)
    app._interrupted_compact_resume = continuation
    started = []
    app._stream = lambda: started.append(True)
    app._user_bubble = lambda *args: None
    if block == "stop":
        app._turn_abandoned = True
    elif block == "queued":
        app._pending_input = [{"content": "new instruction"}]
    elif block == "busy":
        app._chat_running = lambda: True
    elif block == "backend":
        app.backend = SimpleNamespace(name="other")
    elif block == "convo":
        app.convo_id = "other"
    elif block == "turn":
        app._active_turn_started_at = 456.0
    else:
        app._stop_requested = True
    app._continue_interrupted_compact(continuation)
    assert not started and app._interrupted_compact_resume is None


@pytest.mark.asyncio
async def test_local_failure_does_not_arm_later_manual_compaction():
    app = _app(wake_after_compact=False)
    _seed(app)
    app._active_turn_started_at = 123.0
    app._interrupted_compact_resume = (app.backend, app.convo_id, 123.0)
    started = []
    app._stream = lambda: started.append(True)

    async def fail(**kwargs):
        raise RuntimeError("summary failed")

    app.client.chat.completions.create = fail
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)
    assert not started and app._interrupted_compact_resume is None
