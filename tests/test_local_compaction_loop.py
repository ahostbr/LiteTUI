"""Drive the real tool loop through its existing compact_due exit."""
from types import SimpleNamespace

import pytest
from test_card_summary import _Chunk, _Stream
from test_pause import _agentic_app

from litetui import app as app_mod
from litetui.app import WAKE_AFTER_COMPACT


@pytest.mark.asyncio
async def test_tool_round_threshold_compacts_then_resumes_without_optional_wake(monkeypatch, tmp_path):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: None)
    app.settings.autocompact_enabled = True
    app.settings.wake_after_compact = False
    app.settings.compact_keep_recent = 0
    app._kick_card_summary = lambda *args: None
    app._fetch_ctx_window = lambda: None
    app._autocompact_due = lambda: 80 if len(calls) == 1 else None
    calls, snapshots = [], []

    async def ready(**kwargs):
        pass

    app._ensure_chat_ready = ready

    async def create(**kwargs):
        calls.append(kwargs.get("purpose", "turn"))
        snapshots.append([dict(m) for m in app.conversation])
        if len(calls) == 1:
            # One real tool result exists before the budget pause.
            from test_card_summary import _probe
            return _Stream([_Chunk(content="working")] + _probe(1))
        if len(calls) == 2:
            return _Stream([_Chunk(content="Summary: continue the task after the completed probe.")])
        return _Stream([_Chunk(content="resumed and finished")])

    monkeypatch.setattr(app_mod.model_transport, "for_app", lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 200_000, 160_000, True
        app._append({"role": "user", "content": "do the task"})
        app._stream()
        for _ in range(120):
            await pilot.pause(.05)
            if len(calls) >= 3 and not app._chat_running():
                break
    assert calls == ["turn", "compaction", "turn"]
    assert any(m["role"] == "tool" for m in snapshots[1])
    assert any(m.get("content") == WAKE_AFTER_COMPACT for m in snapshots[2])
    assert app._interrupted_compact_resume is None
