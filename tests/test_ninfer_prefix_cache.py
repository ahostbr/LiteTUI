"""Prefix cache: compaction on every local backend whose server keeps one (T1306-C, P4 of the 2026-11-13 prefix audit).

NInfer first (Ryan 2026-11-08, from the serve log of seat 9663fc72 on DeepSeek-V4.1 Flash). Turns that reuse the server's cached
prefix take 1-6 s at 80k tokens (97-99.9% reused). Four requests re-read the prompt from zero instead: 104 s / 250 s / 281 s, the
compaction (405 s for 83k tokens) and the first turn after it (321 s for 20k):
  * `tool_search` loaded subagent, then studio/listen/pccontrol: the `tools` array changed, and it is rendered ahead of the messages;
  * the compaction request had a different system text (the live store appended), no tail and a different tools array;
  * after a compaction the store was re-snapshotted into message 0, so the system text and tools no longer matched any checkpoint.
The fixes, each with its control (the same call on a backend that is not in `prefix_cache_backends` keeps the old request):
  1. on ninfer ONLY, the built-in deferred tools are advertised from the first request (MCP tools stay deferred);
  2. a compaction sends the conversation exactly as the last chat turn did, the same tools array, and ONE final user message;
  3. a compaction leaves message 0 byte-identical and carries the refreshed store in the summary message;
  4. a compaction sends the chat turn's own reasoning effort (templates print it in the first lines of the prompt: a thinking-level
     change high -> low at 08:27:55 made the next request read 24,770 tokens again, 104 s).
Fixes 2-4 now apply to every backend in the `prefix_cache_backends` setting: llama.cpp, Strata, Xolotl and LM Studio keep a prefix
cache too (prefix-audit REPORT.md section 2). Fix 1 stays NInfer-only on purpose: tool advertisement is P5's.
"""
import copy
import json
from types import SimpleNamespace as NS

import pytest
from test_wake_after_compact import _Stream, _app, _seed, _settle

from litetui import model_transport
from litetui.settings import Settings

#: The backends the default `prefix_cache_backends` names. Spelled out here so a change to the default is a visible test edit.
PREFIX_CACHE_BACKENDS = ("ninfer", "llamacpp", "strata", "xolotl", "lmstudio")
#: Backends whose server is not ours to reason about: any OpenAI-compatible URL, a remote API, an OAuth or CLI provider.
NOT_LISTED = ("custom", "cline", "free", "codex", "anthropic", "claude")


@pytest.fixture(autouse=True)
def _authority_follows_the_renamed_backend(monkeypatch):
    """The fixture app's agent authority says "custom"; `_on` renames the backend after construction, which the owned-execution
    guard refuses (and `_compact` / `_stream` then return before any request). The guard is not under test here."""
    from litetui import app as app_mod
    monkeypatch.setattr(app_mod.LiteTUI, "_validate_owned_execution", lambda self, **kw: None)


def _on(app, backend: str):
    """The app as it runs on `backend`: the real predicate reads the backend's name, so no method is patched."""
    app.backend.name = backend
    return app


def _ninfer(app):
    return _on(app, "ninfer")


def _names(app) -> set[str]:
    return {s["function"]["name"] for s in app.plugins.tool_specs()}


def test_the_default_list_is_the_local_engines_with_a_prefix_cache():
    assert tuple(Settings().prefix_cache_backends) == PREFIX_CACHE_BACKENDS


@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
def test_every_listed_backend_is_a_prefix_cache_backend(backend):
    assert _on(_app(), backend)._prefix_cache_backend()


@pytest.mark.parametrize("backend", NOT_LISTED)
def test_a_backend_not_in_the_list_is_not(backend):
    assert not _on(_app(), backend)._prefix_cache_backend()


def test_the_list_is_the_setting_not_a_constant():
    a = _on(_app(), "custom")
    a.settings.prefix_cache_backends = ["custom"]
    assert a._prefix_cache_backend(), "a backend the user adds to the setting gets the prefix-cache requests"
    b = _ninfer(_app())
    b.settings.prefix_cache_backends = []
    assert not b._prefix_cache_backend(), "and an emptied list turns them off, NInfer included"


# ── Fix 1: resident built-ins. NInfer only, deliberately (P5 owns tool advertisement) ─────────────────────────────────────────

def test_ninfer_advertises_the_deferred_builtins_from_the_first_request():
    a = _ninfer(_app())
    assert {"subagent", "studio", "listen", "pccontrol"} <= _names(a), "the four built-ins must ride from turn one on ninfer"
    assert "tool_search" in _names(a)


@pytest.mark.parametrize("backend", ("custom",) + tuple(b for b in PREFIX_CACHE_BACKENDS if b != "ninfer"))
def test_other_backends_keep_the_builtins_deferred(backend):
    """Widening compaction does NOT widen residency: that would add ~2,600 tokens of window to every request on llama.cpp,
    Strata, Xolotl and LM Studio (plugins/tool_search.py:4-7). It is P5's decision, not a side effect of P4."""
    a = _on(_app(), backend)
    assert not ({"subagent", "studio", "listen", "pccontrol"} & _names(a))


def test_loading_a_builtin_on_ninfer_does_not_change_the_tools_array():
    a = _ninfer(_app())
    before = [s["function"]["name"] for s in a.plugins.tool_specs()]
    a.plugins.search_tools("select:subagent")
    assert [s["function"]["name"] for s in a.plugins.tool_specs()] == before


# ── Fixes 2-4 with a recorded last chat turn ──────────────────────────────────────────────────────────────────────────────

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
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_compaction_sends_the_conversation_and_tools_of_the_last_turn(monkeypatch, backend):
    a = _on(_app(compact_keep_recent=2), backend)
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
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_compaction_leaves_message_zero_byte_identical(monkeypatch, backend):
    a = _on(_app(compact_keep_recent=2), backend)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    system_before = a.conversation[0]["content"]
    _capture(monkeypatch)

    await _compact_with(a)

    assert a.conversation[0]["role"] == "system" and a.conversation[0]["content"] == system_before, \
        "message 0 must not change, or the server's cached system + tools prefix no longer matches"
    assert any("the summary body" in str(m.get("content", "")) for m in a.conversation), "the compaction itself must land"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_compaction_sends_the_chat_turns_reasoning_effort(monkeypatch, backend):
    a = _on(_app(compact_keep_recent=2, compact_thinking_level="low"), backend)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    a._last_chat_effort = "xhigh"
    seen = _capture(monkeypatch)

    await _compact_with(a)

    (purpose, kw), = [c for c in seen if c[0] == "compaction"][:1]
    assert kw["extra_body"]["reasoning_effort"] == "xhigh", "the prompt header carries the effort; a different one re-reads everything"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("custom", "cline"))
async def test_other_backends_keep_the_old_compaction_request(monkeypatch, backend):
    a = _on(_app(compact_keep_recent=2), backend)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    seen = _capture(monkeypatch)

    await _compact_with(a)

    (purpose, kw), = [c for c in seen if c[0] == "compaction"][:1]
    assert kw["tools"] is not CHAT_TOOLS
    assert "stay verbatim after your summary" not in str(kw["messages"][-1]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_compaction_falls_back_to_the_head_when_the_full_prefix_does_not_fit(monkeypatch, backend):
    """Seen 2026-11-08 11:05 on a fresh seat: one turn of large reads took the window from under the threshold to 100%, and the
    full-prefix compaction request was refused as longer than the engine's context ('Compact failed - conversation unchanged').
    The engine's own context_length_exceeded is believed (nothing estimated) and the head-only request is sent instead."""
    a = _on(_app(compact_keep_recent=2), backend)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    sent_as_chat = list(a._request_messages())
    seen = []

    class _Full(Exception):
        body = {"error": {"code": "context_length_exceeded", "message": "prompt too long"}}

    async def create(purpose=None, **kw):
        seen.append((purpose, kw))
        if len([c for c in seen if c[0] == "compaction"]) == 1:
            raise _Full("prompt too long")
        return _Stream("the summary body")

    monkeypatch.setattr(model_transport, "for_app", lambda app: NS(create=create))

    await _compact_with(a)

    calls = [kw for purpose, kw in seen if purpose == "compaction"]
    assert len(calls) == 2, "one refusal, one resend"
    assert calls[0]["messages"][:len(sent_as_chat)] == sent_as_chat
    assert len(calls[1]["messages"]) < len(calls[0]["messages"]), "the resend drops the kept tail"
    kept = len(sent_as_chat) - 2  # compact_keep_recent=2
    assert calls[1]["messages"][:kept] == sent_as_chat[:kept], "message 0 and the head are untouched, so the server can restore its stored prefix"
    assert len(calls[1]["messages"]) == kept + 1 and calls[1]["messages"][-1]["role"] == "user", "the compaction rides in ONE final user message"
    assert calls[1]["tools"] is CHAT_TOOLS, "the same tools array as the chat turns, or the stored prefix is lost (request 13 re-read 125,834 tokens)"
    assert any("the summary body" in str(m.get("content", "")) for m in a.conversation), "the compaction must land"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_an_unrelated_refusal_is_still_raised_not_retried(monkeypatch, backend):
    a = _on(_app(compact_keep_recent=2), backend)
    _seed(a)
    a._last_chat_tools = CHAT_TOOLS
    seen = []

    class _Other(Exception):
        body = {"error": {"code": "server_overloaded"}}

    async def create(purpose=None, **kw):
        seen.append(purpose)
        raise _Other("busy")

    monkeypatch.setattr(model_transport, "for_app", lambda app: NS(create=create))

    await _compact_with(a)

    assert seen.count("compaction") == 1, "only a context overflow earns the head-only resend"


# ── The wire: a real chat turn, the compaction, the next chat turn, serialised as they are posted ─────────────────────────────

#: Keys handed to the transport that are not part of the posted body (the client's own arguments).
_CLIENT_ONLY = ("retry_notice", "retry_budget")


def _wire(value) -> str:
    """JSON the way the OpenAI client posts it (httpx: compact separators, UTF-8 verbatim)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _chat_then_compact_then_chat(monkeypatch, backend: str):
    """Drive the app's own request builders: one chat turn, a compaction, one more chat turn. Returns their bodies in order.

    The store gains a line between the chat turn and the compaction (STEP 1 of the compaction prompt writes memory.md), which is
    what made the old rebuild rewrite message 0. The chat turn runs at "high" and compaction at "low" so the effort arm can tell
    the two apart."""
    a = _on(_app(compact_keep_recent=2, compact_thinking_level="low", wake_after_compact=False), backend)
    _seed(a)
    store ={"memory.md": "- the user prefers tabs"}
    a._read_store_file = lambda name, cap: store.get(name, "")
    a._store_injected = False
    a.thinking_level = "high"
    a._kick_card_summary = lambda *x, **k: None

    async def ready(**kw):
        pass

    a._ensure_chat_ready = ready
    bodies = []

    async def create(purpose="chat", **kw):
        body = copy.deepcopy({k: v for k, v in kw.items() if k not in _CLIENT_ONLY})
        bodies.append((purpose, body))
        return _Stream("the summary body" if purpose == "compaction" else "a reply")

    monkeypatch.setattr(model_transport, "for_app", lambda app: NS(create=create))

    async def wait(pilot):
        for _ in range(120):
            await pilot.pause(.02)
            if not a._chat_running():
                break

    async with a.run_test() as pilot:
        a._append({"role": "user", "content": "and one more thing"})
        a._stream()
        await wait(pilot)
        store["memory.md"] += "\n- written during compaction"
        a._compact()
        await _settle(a, pilot)
        a._append({"role": "user", "content": "carry on"})
        a._stream()
        await wait(pilot)
    purposes = [p for p, _ in bodies]
    assert purposes == ["chat", "compaction", "chat"], purposes
    return [b for _, b in bodies]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_the_compaction_body_extends_the_chat_body_byte_for_byte(monkeypatch, backend):
    chat, compaction, _ = await _chat_then_compact_then_chat(monkeypatch, backend)

    chat_messages, compact_messages = _wire(chat["messages"]), _wire(compaction["messages"])
    assert compact_messages.startswith(chat_messages[:-1] + ","), \
        "the posted messages array must carry the chat turn's whole array as a prefix: only the final message is new"
    new = compaction["messages"][len(chat["messages"]):]
    assert [m["role"] for m in new] == ["assistant", "user"] and new[0]["content"] == "a reply", \
        "after the chat turn's prompt come only its own reply (already in the server's state) and ONE final user message"
    assert _wire(compaction["tools"]) == _wire(chat["tools"]), "the tools array renders before the history"
    assert (compaction.get("extra_body") or {}).get("reasoning_effort") == (chat.get("extra_body") or {}).get("reasoning_effort"), \
        "the effort renders at token ~0 on DeepSeek, Strata, Xolotl and Qwen3.8"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PREFIX_CACHE_BACKENDS)
async def test_after_the_rebuild_the_next_chat_keeps_message_zero(monkeypatch, backend):
    chat, _, after = await _chat_then_compact_then_chat(monkeypatch, backend)

    assert _wire(after["messages"][0]) == _wire(chat["messages"][0]), \
        "message 0 after the compaction must be the bytes the server already holds"
    assert _wire(after["tools"]) == _wire(chat["tools"])
    assert "written during compaction" in _wire(after["messages"][1]), "the fresh store rides in the summary message instead"


@pytest.mark.asyncio
async def test_control_a_backend_not_listed_still_breaks_the_prefix_on_the_wire(monkeypatch):
    """CONTROL for the two wire tests: on a backend outside the list the old requests go out, and the same assertions fail.
    If this ever passes the prefix checks, the wire tests above cannot tell old behaviour from new."""
    chat, compaction, after = await _chat_then_compact_then_chat(monkeypatch, "custom")

    assert not _wire(compaction["messages"]).startswith(_wire(chat["messages"])[:-1] + ","), "old request: live store in message 0, head only"
    assert _wire(after["messages"][0]) != _wire(chat["messages"][0]), "old rebuild: message 0 re-snapshots the store"
