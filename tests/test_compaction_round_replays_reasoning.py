"""A compaction round resends its own reply with the reasoning, the way a chat turn does.

Ryan, 2026-11-21, Inkling 2 Mini on NInfer with --preserve-thinking, a conversation at 123k tokens: "on every single
write during compact its needs to prefill fully again". His engine log: compaction rounds 43, 44, 45 and 47 each
read all ~123,600 prompt tokens again (cache 0, 66 s to the first token); round 46 reused 123,707 of 123,714, and
the round before it was 64 tokens long. The engine resumes a sequence only when the next prompt replays what the
sequence produced, reasoning included; the compaction loop appended its own tool-call rounds without it.
"""
from types import SimpleNamespace

import pytest
from test_card_summary import _Chunk, _Stream, _probe
from test_pause import _agentic_app

from litetui import app as app_mod


@pytest.mark.asyncio
async def test_a_compaction_round_resends_its_reply_with_the_reasoning(monkeypatch, tmp_path):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: None)
    app.settings.autocompact_enabled = True
    app.settings.wake_after_compact = False
    app.settings.compact_keep_recent = 0
    app.settings.prefix_cache_backends = [app.backend.name]  # the request that keeps the conversation as the chat turn sent it
    app._kick_card_summary = lambda *args: None
    app._fetch_ctx_window = lambda: None
    app._autocompact_due = lambda: 80 if len(calls) == 1 else None
    calls = []

    async def ready(**kwargs):
        pass

    app._ensure_chat_ready = ready

    async def create(**kwargs):
        calls.append((kwargs.get("purpose", "turn"), [dict(m) for m in kwargs["messages"]]))
        n = len(calls)
        if n == 1:   # the chat turn: a tool call, then the pause at the threshold
            return _Stream([_Chunk(content="working")] + _probe(1))
        if n == 2:   # compaction round 1 thinks, then calls a tool
            return _Stream([_Chunk(reasoning_content="Save the notes "), _Chunk(reasoning_content="before summarising.")]
                           + _probe(2))
        if n == 3:   # compaction round 2 calls a tool and says nothing else
            return _Stream(_probe(3))
        if n == 4:
            return _Stream([_Chunk(content="Summary: continue the task after the completed probe.")])
        return _Stream([_Chunk(content="resumed and finished")])

    monkeypatch.setattr(app_mod.model_transport, "for_app", lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 200_000, 100_000, True
        app._append({"role": "user", "content": "do the task"})
        app._stream()
        for _ in range(200):
            await pilot.pause(.05)
            if len(calls) >= 5 and not app._chat_running():
                break
    assert [purpose for purpose, _ in calls] == ["turn", "compaction", "compaction", "compaction", "turn"]
    round1, round2, round3 = calls[1][1], calls[2][1], calls[3][1]
    # Round 2 is round 1's request, then round 1's reply with its reasoning, then the tool result.
    assert round2[:len(round1)] == round1
    reply1 = round2[len(round1)]
    assert reply1["role"] == "assistant" and reply1["tool_calls"][0]["id"] == "c2"
    assert reply1.get("reasoning_content") == "Save the notes before summarising."
    # A round that did not reason resends no reasoning field at all (an empty one is not what the engine produced).
    reply2 = round3[len(round2)]
    assert reply2["role"] == "assistant" and reply2["tool_calls"][0]["id"] == "c3"
    assert "reasoning_content" not in reply2
