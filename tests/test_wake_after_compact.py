"""Wake after compaction — loop mode.

Ryan: "after compaction it always pings the model like the user would to wake
it up, essentially loop mode."

The gap this closes: a long agentic run hits the auto-compact threshold, the
context is replaced by a summary, and the model sits there — nothing asks for
anything once the context has been replaced, so the task parks silently until
the human happens to type. The wake ping buys the model one round-trip:
resume the in-flight task, or say standing by and stop.

Properties, each with a control:
  1. The wake ping is a USER message and starts a normal turn.
  2. The ping text offers a standing-by exit — without it, a "compact to free
     context" pays for the model to invent a task it has no business resuming.
  3. A wake that would land on a turn already running is DROPPED — a real
     user turn that won the chat group first is not queued behind a ping.
  4. A SUCCESSFUL compact schedules exactly one wake.
  5. A FAILED compact schedules none — waking on "nothing changed" is a ping
     with no answer.
  6. The setting OFF schedules none — it is opt-in per user.
  7. The shipped default is OFF — it changes what /compact does for every
     user and costs a round-trip when nothing is pending.
  8. The settings screen carries the switch — a knob with no control is a
     knob that silently cannot be changed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings

# Hermetic convo store, the same way test_context_length.py does it.
paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-wake-"))


# ── fakes ──────────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, content: str):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None


class _Chunk:
    def __init__(self, content=None):
        self.choices = [type("C", (), {"delta": _Delta(content)})()]
        self.usage = None


class _Stream:
    """Async-iterable fake: _compact streams now (glass-box compaction), so
    the fake must speak chunks. The old _Resp shape made the compact LOOK
    broken when only the fake was out of date — the same lesson as the
    docstring below, one protocol later."""

    def __init__(self, text: str):
        self._chunks = [_Chunk(text[:3]), _Chunk(text[3:])]

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


async def _ok_create(text: str, **kw):
    """An AWAITABLE fake completions.create returning a chunk STREAM.

    🔴 The first draft was `lambda **kw: _make_coro(text)` — a lambda that
    returned an async *function* rather than a coroutine. `_compact` does
    `await self.client.chat.completions.create(...)`, so awaiting a bare
    function object raised TypeError and killed the worker before it ever
    touched the conversation. The failure looked like the compact was broken;
    it was the fake. A fake that is not awaitable is a fake that lies.
    """
    assert kw.get("stream") is True, "glass-box compaction always streams"
    return _Stream(text)


async def _boom_create(**kw):
    raise RuntimeError("boom — the compact must not wake on a failure")


def _app(**overrides) -> app_mod.LiteTUI:
    a = app_mod.LiteTUI()
    base = dict(
        wake_after_compact=True,
        clear_screen_after_compact=False,  # keep the test away from the widget tree
        compact_keep_recent=2,
    )
    base.update(overrides)
    a.settings = Settings(**base)
    a._system = lambda *a, **k: None
    return a


def _seed(a: app_mod.LiteTUI) -> None:
    a.conversation = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do a big task"},
        {"role": "assistant", "content": "working on it"},
        {"role": "user", "content": "still going?"},
        {"role": "assistant", "content": "yes, nearly there"},
    ]


async def _settle(a, pilot, extra: int = 6) -> None:
    """Wait for the compact worker to finish, then let the refresh-scheduled
    wake fire (it is posted for the NEXT refresh, not the current one)."""
    for _ in range(80):
        await pilot.pause()
        if not a._chat_running():
            break
    for _ in range(extra):
        await pilot.pause()


# ── 1. the ping is a user message and starts a turn ───────────────────────

def test_wake_ping_is_a_user_message_and_starts_a_turn():
    a = _app()
    _seed(a)
    before = len(a.conversation)
    started = []
    a._user_bubble = lambda *x: None
    a._stream = lambda: started.append(True)

    a._wake_after_compact()

    assert a.conversation[before] == {
        "role": "user", "content": app_mod.WAKE_AFTER_COMPACT
    }, "the ping must be a user-role message with the wake text"
    assert started == [True], "the ping must start a normal turn"


# ── 2. the ping text offers a standing-by exit ────────────────────────────

def test_wake_text_offers_a_standing_by_exit():
    t = app_mod.WAKE_AFTER_COMPACT
    assert "standing by" in t, "the ping must give the model a cheap exit"
    assert "resume" in t, "the ping must ask for resumption of an in-flight task"


# ── 3. a running turn wins; the ping is dropped, not queued ───────────────

def test_wake_yields_to_a_turn_that_is_already_running():
    a = _app()
    _seed(a)
    before = len(a.conversation)
    started = []
    a._user_bubble = lambda *x: None
    a._stream = lambda: started.append(True)
    a._chat_running = lambda: True  # a real turn claimed the chat group

    a._wake_after_compact()

    assert started == [], "a running user turn must not be queued behind a ping"
    assert len(a.conversation) == before, "no ping message may be appended"


# ── 4. a successful compact wakes exactly once ────────────────────────────

@pytest.mark.asyncio
async def test_successful_compact_wakes_the_model():
    a = _app()
    _seed(a)
    started = []
    a._user_bubble = lambda *x: None
    a._stream = lambda: started.append(True)
    a.client.chat.completions.create = lambda **kw: _ok_create("the summary body", **kw)

    async with a.run_test() as pilot:
        a._compact()
        await _settle(a, pilot)

    # The compact itself landed: the old head was replaced by the summary pair.
    assert any("the summary body" in str(m.get("content", ""))
               for m in a.conversation), "the compact itself did not land"
    assert a.conversation[-1] == {
        "role": "user", "content": app_mod.WAKE_AFTER_COMPACT
    }, "the wake ping must be the last message"
    assert started == [True], "exactly one turn must be started by the wake"


# ── 5. a failed compact does not wake ─────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_compact_does_not_wake():
    a = _app()
    _seed(a)
    before = list(a.conversation)
    started = []
    a._user_bubble = lambda *x: None
    a._stream = lambda: started.append(True)
    a.client.chat.completions.create = _boom_create

    async with a.run_test() as pilot:
        a._compact()
        await _settle(a, pilot)

    assert a.conversation == before, "a failed compact must leave the conversation untouched"
    assert started == [], "waking on 'nothing changed' is a ping with no answer"


# ── 6. setting OFF schedules no wake ──────────────────────────────────────

@pytest.mark.asyncio
async def test_no_wake_when_the_setting_is_off():
    a = _app(wake_after_compact=False)
    _seed(a)
    started = []
    a._user_bubble = lambda *x: None
    a._stream = lambda: started.append(True)
    a.client.chat.completions.create = lambda **kw: _ok_create("summary two", **kw)

    async with a.run_test() as pilot:
        a._compact()
        await _settle(a, pilot)

    assert any("summary two" in str(m.get("content", ""))
               for m in a.conversation), "the compact itself must still land"
    assert started == [], "setting OFF means no ping"
    assert all(m != {"role": "user", "content": app_mod.WAKE_AFTER_COMPACT}
               for m in a.conversation)


# ── 7. shipped default ─────────────────────────────────────────────────────

def test_shipped_default_is_off():
    assert Settings().wake_after_compact is False, (
        "it changes what /compact does for every user and costs a round-trip "
        "when nothing is pending; ON lives in a user's settings.json"
    )


# ── 8. the settings screen carries the switch ─────────────────────────────

@pytest.mark.asyncio
async def test_settings_screen_has_the_wake_switch():
    from textual.app import App, ComposeResult
    from textual.widgets import Switch
    from litetui.settings_screen import SettingsScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SettingsScreen(Settings(), models=["m"], mcp_servers=[]),
                lambda r: None,
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        sw = pilot.app.screen.query_one("#f-wake_after_compact", Switch)
        assert sw.value is False, "the shipped default, on screen"
        sw.value = True


@pytest.mark.asyncio
async def test_compact_persists_measurements_and_keeps_tool_rounds(monkeypatch):
    from types import SimpleNamespace
    a = _app(wake_after_compact=False, compact_keep_recent=4)
    _seed(a)
    for i in range(4):
        a.conversation.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": str(i)}]},
            {"role": "tool", "tool_call_id": str(i), "content": "compile error"},
        ])
    expected_tail = a.conversation[-4:]
    records = []
    monkeypatch.setattr(a.store, "write_record", records.append)
    async def create(**kw):
        stream = _Stream("Task: fix the compiler errors.")
        usage = _Chunk()
        usage.choices = []
        usage.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110)
        stream._chunks.append(usage)
        return stream
    a.client.chat.completions.create = create
    async with a.run_test() as pilot:
        a.ctx_used = 900
        a.ctx_max = 1000
        a._compact_is_auto = True
        a._compact()
        await _settle(a, pilot)
        assert len(a.query(".compaction-round")) == 1
    record = next(r for r in records if r["type"] == "truncate")
    metrics = record["measurements"]
    assert metrics["auto"] is True
    assert metrics["ctx_used"] == 900
    assert metrics["ctx_percent"] == 90
    assert metrics["before_count"] == 13
    assert metrics["after_count"] == 7
    assert metrics["before_chars"] > 0 and metrics["after_chars"] > 0
    assert metrics["tokens_after"] is None
    assert metrics["tokens_after_estimate"] > 0
    assert metrics["duration_s"] >= metrics["rounds"][0]["model_s"] >= 0
    assert metrics["rounds"][0]["usage"]["prompt_tokens"] == 100
    assert metrics["rounds"][0]["first_chunk_s"] is not None
    assert metrics["store_files"] == []
    assert a.conversation[-4:] == expected_tail


def test_assistant_usage_is_row_metadata_and_never_enters_model_messages(monkeypatch):
    a = _app()
    records = []
    monkeypatch.setattr(a.store, "write_record", records.append)
    message = {"role": "assistant", "content": "done"}
    a._append(message, usage={"prompt_tokens": 20, "completion_tokens": 5})
    a._append({"role": "assistant", "content": "no usage reported"})
    assert records[0]["usage"] == {"prompt_tokens": 20, "completion_tokens": 5}
    assert records[0]["model"] == a.model_id
    assert "usage" not in records[0]["message"]
    assert "usage" not in records[1]
    assert "usage" not in a.conversation[-2]


@pytest.mark.parametrize("want", [1, 2, 3, 4, 7])
def test_safe_tail_retains_complete_parallel_tool_round(want):
    messages = [{"role": "user", "content": "task"}]
    for n in range(4):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": f"{n}a"}, {"id": f"{n}b"}]},
            {"role": "tool", "tool_call_id": f"{n}b"},
            {"role": "tool", "tool_call_id": f"{n}a"},
        ])
    tail = app_mod.LiteTUI._safe_tail(messages, want)
    assert len(tail) >= want
    assert tail[0]["role"] == "assistant"
    assert tail[-3:] == messages[-3:]
    assert app_mod.LiteTUI._safe_tail(messages, 0) == []
    assert app_mod.LiteTUI._safe_tail(messages[:-1], 1) == []
