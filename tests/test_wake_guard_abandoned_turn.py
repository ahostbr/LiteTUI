"""The post-compaction wake ping must not chase a turn the user ABANDONED.

⚠️ HONEST SCOPE, PRESERVED DELIBERATELY. This is NOT a fix for the compaction
bug Ryan originally reported, and it would NOT have fixed that symptom. It is a
separate, real edge case that was found while reading the same code. Do not cite
this file as evidence that the reported bug is closed.

THE CASE. `_stream` has an exit nobody thinks about: the user presses Escape,
`_stop_requested` goes True, and the turn returns at the "[stopped by you]"
branch — which, on its way out, schedules `_maybe_autocompact`. So a stopped
turn is one of the ways a compaction gets STARTED. When that compaction lands,
`_wake_after_compact` pings the model with "resume the in-flight task", and the
in-flight task is the one the user just deliberately killed.

The two guards already in `_wake_after_compact` do not catch it:
  - `_chat_running()` is False — the compact worker has ended.
  - `_pending_input` is empty — the Esc path queues nothing. (The *interrupt*
    path does queue a message, which is why that variant was already covered
    and this one was not.)

So the ping fires for a turn nobody is waiting on.

THE DISTINCTION THAT MATTERS: suppress on ABANDONED, not on FINISHED. A turn
that merely ended must still wake — that is the whole feature. Every test below
is paired with the finished-turn control that must stay green, because
"never ping" would satisfy the abandon tests on its own and would delete the
feature.

Properties, each with a control:
  1. Esc-stopped turn -> compact -> NO ping.
     Control: finished turn -> compact -> ping. (identical harness)
  2. Force-stop (second Esc, hard cancel) also marks the turn abandoned.
  3. The mark is not sticky: a real turn after the abandon clears it, and the
     next compaction pings again.
  4. Control: abandonment alone appends nothing — the suppression must be the
     ping's absence, not a conversation the guard mangled.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-wakeguard-"))


# ── fakes ──────────────────────────────────────────────────────────────────

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
    """Plain chunk stream — a turn that runs to completion."""

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


class _EscStream(_Stream):
    """A turn the user presses Escape on, mid-sentence.

    Trips the app's real `_stop_requested` flag between chunks, which is
    exactly what `action_stop_turn` -> `_on_stop_answer` does. The turn then
    leaves through `_stream`'s "[stopped by you]" branch — the real exit, not
    a simulated one.
    """

    def __init__(self, app, text: str):
        super().__init__(text)
        self._app = app

    async def __anext__(self):
        chunk = await super().__anext__()
        self._app._stop_requested = True
        return chunk


class _HangingStream:
    """A turn that is still in flight — it yields once and then never returns,
    so the force-stop has something real to kill.

    The first draft of the force-stop test used the instant `_Stream` and the
    turn had already ENDED by the time Escape was pressed; `action_stop_turn`
    is inert with nothing running, so the test failed on its precondition
    rather than on the behaviour. A fake that finishes cannot be interrupted.
    """

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Keeps TALKING rather than stalling inside one await. `_stream` only
        # notices `_stop_requested` BETWEEN chunks, so a fake that blocks
        # forever inside a single `await` can be neither soft-stopped nor
        # interrupted — only hard-cancelled. That cost the interrupt test a
        # first draft: the message queued and was never delivered, because
        # the turn holding it could not end.
        await asyncio.sleep(0.01)
        return _Chunk("still talking ")

    async def close(self):
        pass


async def _hanging_create(**kw):
    return _HangingStream()


def _app(**overrides) -> app_mod.LiteTUI:
    a = app_mod.LiteTUI()
    base = dict(
        wake_after_compact=True,
        clear_screen_after_compact=False,
        compact_keep_recent=2,
        tools_enabled=False,
        autocompact_enabled=False,   # compactions here are explicit, never timed
    )
    base.update(overrides)
    a.settings = Settings(**base)
    a.tools_enabled = False
    a.said: list[str] = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    return a


def _seed(a: app_mod.LiteTUI) -> None:
    a.conversation = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do a big task"},
        {"role": "assistant", "content": "working on it"},
        {"role": "user", "content": "still going?"},
        {"role": "assistant", "content": "yes, nearly there"},
    ]


async def _settle(a, pilot, extra: int = 8) -> None:
    for _ in range(120):
        await pilot.pause()
        if not a._chat_running():
            break
    for _ in range(extra):
        await pilot.pause()


def _watch_wake(a) -> list:
    """Stub the ping's two effects AFTER the turn phase, so the wake is
    observable without the abandon phase having to run against stubs."""
    started: list = []
    a._user_bubble = lambda *x, **k: None
    a._stream = lambda: started.append(True)
    return started


def _pinged(a) -> bool:
    return any(
        m.get("role") == "user" and m.get("content") == app_mod.WAKE_AFTER_COMPACT
        for m in a.conversation
    )


def _ok_create(text: str):
    async def _create(**kw):
        return _Stream(text)
    return _create


# ── 1a. NEGATIVE CONTROL — an Esc-stopped turn must not be woken ──────────

@pytest.mark.asyncio
async def test_a_turn_stopped_by_the_user_is_not_woken_after_compaction():
    a = _app()
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = lambda **kw: _stop_stream(a, **kw)
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        await _settle(a, pilot)

        # Precondition, asserted rather than assumed: the turn must actually
        # have left through the stop branch. Without this the test could pass
        # for the wrong reason (a turn that never ran cannot be woken either).
        assert any("stopped by you" in s for s in a.said), (
            f"the Esc-stop exit was never taken; said={a.said}"
        )

        started = _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert any("the summary body" in str(m.get("content", ""))
               for m in a.conversation), "the compact itself did not land"
    assert started == [], "a turn the user abandoned must not be resumed by a ping"
    assert not _pinged(a), "no wake message may be appended for an abandoned turn"


async def _stop_stream(a, **kw):
    return _EscStream(a, "partial reply")


# ── 1b. CONTROL — a turn that merely FINISHED must still be woken ─────────

@pytest.mark.asyncio
async def test_a_turn_that_merely_finished_is_still_woken():
    """The same harness, one difference: nobody pressed Escape.

    This is what stops the guard from being "never ping", which would satisfy
    1a while deleting the feature.
    """
    a = _app()
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("a finished answer")
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        await _settle(a, pilot)

        assert not any("stopped by you" in s for s in a.said), (
            f"this control must NOT go through the stop branch; said={a.said}"
        )

        started = _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert started == [True], "a finished turn must still be woken after compaction"
    assert _pinged(a), "the wake message must be appended for a finished turn"


# ── 2. the force-stop (second Esc) counts as abandonment too ──────────────

@pytest.mark.asyncio
async def test_force_stopping_a_turn_also_suppresses_the_ping():
    """`action_stop_turn`'s second press hard-cancels the chat group. That
    worker never reaches the "[stopped by you]" branch, so the flag has to be
    set at the kill site as well — otherwise the harder of the two stops is
    the one that gets ignored."""
    a = _app()
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _hanging_create
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        for _ in range(60):
            await pilot.pause()
            if a._chat_running():
                break
        assert a._chat_running(), "the turn never started — nothing to force-stop"

        a._stop_requested = True      # the first Esc already asked nicely
        a.action_stop_turn()          # the second Esc: force
        await _settle(a, pilot)

        assert any("force-stopped" in s for s in a.said), (
            f"the force-stop path was never taken; said={a.said}"
        )

        started = _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert started == [], "a force-stopped turn must not be resumed by a ping"
    assert not _pinged(a), "no wake message may be appended for a force-stopped turn"


# ── 3. the mark is not sticky — a real turn clears it ─────────────────────

@pytest.mark.asyncio
async def test_a_later_real_turn_clears_the_abandon_mark():
    """Abandon, then actually work, then compact. The ping must come back.

    A flag that latches would silently kill loop mode for the rest of the
    session — the failure mode of this fix, and the reason it has its own test.
    """
    a = _app()
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = lambda **kw: _stop_stream(a, **kw)
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        await _settle(a, pilot)
        assert any("stopped by you" in s for s in a.said), "the abandon never happened"

        # A real turn, run to completion, after the abandonment.
        a.client.chat.completions.create = _ok_create("a fresh finished answer")
        a._append({"role": "user", "content": "ok, different task"})
        a._stream()
        await _settle(a, pilot)

        started = _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert started == [True], (
        "the abandon mark latched — loop mode would stay dead for the session"
    )
    assert _pinged(a), "the wake message must return once a real turn has run"


# ── 4. INTERRUPT is not ABANDONMENT — the nearest neighbour path ──────────

@pytest.mark.asyncio
async def test_interrupting_a_turn_with_a_new_message_still_wakes_later():
    """`_submit_text`'s interrupt branch is the OTHER writer of
    `_stop_requested`, so it lands on the same "[stopped by you]" exit and
    picks up the abandon mark. But interrupting is not abandoning — the user
    replaced the instruction, they did not walk away, and the queued message
    runs as a real turn straight afterwards.

    Grepped `_stop_requested = True`: exactly two sites, this one and
    `_on_stop_answer`. This is the one that must NOT end up suppressed.
    """
    a = _app(enter_interrupts=True)
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _hanging_create
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        for _ in range(60):
            await pilot.pause()
            if a._chat_running():
                break
        assert a._chat_running(), "the turn never started — nothing to interrupt"

        # Enter, mid-turn, with enter_interrupts on: queue at the FRONT and
        # soft-stop. The real path, not a simulated one.
        a.client.chat.completions.create = _ok_create("answering the new one")
        a._submit_text("actually, do this instead", alt_chord=False)
        await _settle(a, pilot)
        # The queued message is flushed by on_worker_state_changed, which
        # starts a fresh turn; wait that one out too.
        await _settle(a, pilot)

        assert not a._pending_input, "the interrupting message was never delivered"

        started = _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert started == [True], (
        "an interrupted-and-replaced turn is live work — it must still wake"
    )
    assert _pinged(a)


# ── 5. control: suppression is the ping's absence, nothing else ───────────

@pytest.mark.asyncio
async def test_suppression_leaves_the_compacted_conversation_intact():
    """The guard must drop the ping and touch nothing else — the compaction's
    own result has to survive it."""
    a = _app()
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = lambda **kw: _stop_stream(a, **kw)
        a._append({"role": "user", "content": "start the long job"})
        a._stream()
        await _settle(a, pilot)

        _watch_wake(a)
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert a.conversation[0]["role"] == "system", "the system message was lost"
    assert any("the summary body" in str(m.get("content", ""))
               for m in a.conversation), "the summary must survive the suppression"
    assert a.conversation[-1].get("content") != app_mod.WAKE_AFTER_COMPACT
