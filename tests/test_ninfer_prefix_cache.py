"""NInfer prefix cache (Ryan 2026-11-08, from the serve log of seat 9663fc72 on DeepSeek-V4.1 Flash).

Turns that reuse the server's cached prefix take 1-6 s at 80k tokens (97-99.9% reused). Four requests re-read the prompt from zero
instead: 104 s / 250 s / 281 s, the compaction (405 s for 83k tokens) and the first turn after it (321 s for 20k):
  * `tool_search` loaded subagent, then studio/listen/pccontrol: the `tools` array changed, and it is rendered ahead of the messages;
  * the compaction request had a different system text (the live store appended), no tail and a different tools array;
  * after a compaction the store was re-snapshotted into message 0, so the system text and tools no longer matched any checkpoint.
The fixes, each with its control (the same call on a backend that has no prefix cache keeps the old request):
  1. on ninfer the built-in deferred tools are advertised from the first request (MCP tools stay deferred);
  2. on ninfer a compaction sends the conversation exactly as the last chat turn did, the same tools array, and ONE final user message;
  3. on ninfer a compaction leaves message 0 byte-identical and carries the refreshed store in the summary message.
"""
from types import SimpleNamespace as NS

import pytest
from test_wake_after_compact import _Stream, _app, _seed, _settle

from litetui import model_transport


def _ninfer(app):
    app._prefix_cache_backend = lambda: True
    return app


def _names(app) -> set[str]:
    return {s["function"]["name"] for s in app.plugins.tool_specs()}


def test_ninfer_advertises_the_deferred_builtins_from_the_first_request():
    a = _ninfer(_app())
    assert {"subagent", "studio", "listen", "pccontrol"} <= _names(a), "the four built-ins must ride from turn one on ninfer"
    assert "tool_search" in _names(a)


def test_a_backend_without_a_prefix_cache_keeps_them_deferred():
    a = _app()
    assert not ({"subagent", "studio", "listen", "pccontrol"} & _names(a))


def test_loading_a_builtin_on_ninfer_does_not_change_the_tools_array():
    a = _ninfer(_app())
    before = [s["function"]["name"] for s in a.plugins.tool_specs()]
    a.plugins.search_tools("select:subagent")
    assert [s["function"]["name"] for s in a.plugins.tool_specs()] == before


def _capture(monkeypatch):
    seen = []

    async def create(purpose=None, **kw):
        seen.append((purpose, kw))
        return _Stream("the summary body")

    monkeypatch.setattr(model_transport, "for_app", lambda app: NS(create=create))
    return seen


async def _compact_with(app):
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)


CHAT_TOOLS = [{"type": "function", "function": {"name": "only_this_one", "parameters": {"type": "object", "properties": {}}}}]


@pytest.mark.asyncio
async def test_ninfer_compaction_sends_the_conversation_and_tools_of_the_last_turn(monkeypatch):
    a = _ninfer(_app(compact_keep_recent=2))
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    sent_as_chat = list(a._request_messages())
    seen = _capture(monkeypatch)

    await _compact_with(a)

    (purpose, kw), = [c for c in seen if c[0] == "compaction"][:1]
    assert kw["messages"][:len(sent_as_chat)] == sent_as_chat, "the whole conversation, system text untouched, is the cached prefix"
    assert kw["tools"] is CHAT_TOOLS, "the same tools array as the last turn, or the prefix is lost"
    final = kw["messages"][len(sent_as_chat):]
    assert len(final) == 1 and final[0]["role"] == "user", "everything compaction adds is ONE final user message"
    assert "stay verbatim after your summary" in final[0]["content"]


@pytest.mark.asyncio
async def test_ninfer_compaction_leaves_message_zero_byte_identical(monkeypatch):
    a = _ninfer(_app(compact_keep_recent=2))
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    system_before = a.conversation[0]["content"]
    _capture(monkeypatch)

    await _compact_with(a)

    assert a.conversation[0]["role"] == "system" and a.conversation[0]["content"] == system_before, \
        "message 0 must not change, or the server's cached system + tools prefix no longer matches"
    assert any("the summary body" in str(m.get("content", "")) for m in a.conversation), "the compaction itself must land"


@pytest.mark.asyncio
async def test_other_backends_keep_the_old_compaction_request(monkeypatch):
    a = _app(compact_keep_recent=2)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    seen = _capture(monkeypatch)

    await _compact_with(a)

    (purpose, kw), = [c for c in seen if c[0] == "compaction"][:1]
    assert kw["tools"] is not CHAT_TOOLS
    assert "stay verbatim after your summary" not in str(kw["messages"][-1]["content"])
