"""D2/D11, the app.py half: ASK before opening a stream, so the plain words
land instead of a raw 400.

Anvil built the seam on the backend (`ensure_chat_ready`, tests/
test_llama_chat_ready.py) and correctly left it with NO CALLER, because every
call site is in app.py. This file is the caller, and its controls.

WHAT THE USER SAW — the captured b9360 body, reused here rather than invented,
so the red is the real string and not a plausible one:

    Error: Error code: 400 - {"error":{"code":400,"message":"model is not
    loaded","type":"invalid_request_error"}}

TWO SITES GUARDED, ONE DELIBERATELY NOT:

  3981  the user's turn      GUARDED — the reported defect
  4566  auto-compaction      GUARDED — nobody typed anything, so a raw 400 is
                             strictly more confusing there
  2104  tool-output summary  NOT GUARDED, ON PURPOSE. That site already has a
                             designed non-lossy fallback (`except -> summary =
                             '' -> render_mask`); the raw 400 never reaches the
                             user, the pointer survives and the tool result is
                             still correct. Guarding inside its try is a no-op;
                             guarding outside converts a working graceful
                             degradation into a hard failure. Test 5 pins that
                             as a decision rather than an oversight.

🔴 NOT A 400 HANDLER. Every guard below is a PRE-FLIGHT QUERY placed before
`create()`. No except clause is added or widened and no status code is
inspected anywhere, which is why this cannot swallow the D3 unload-400 that
tests/test_llama_chat_ready.py plants a guard for. That guard is imported into
this run (test 7) as the check on that reasoning.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.llm_backend import BackendError
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-chatready-"))

FIXTURES = Path(__file__).parent / "fixtures"
ROUTER_ERRORS = json.loads(
    (FIXTURES / "llama_router_chat_errors_b9360.json").read_text()
)

#: Exactly what app.py rendered before this fix, built from the captured body.
_CODE, _BODY = ROUTER_ERRORS["chat_unloaded"]
RAW_400 = f"Error code: {_CODE} - {_BODY}"

#: The plain words the seam raises in its place.
PLAIN = "'m1' is not loaded — /load m1 first, or /model to pick one that already is."


class _Raw400(Exception):
    """Stands in for openai.BadRequestError: what `create()` raises when the
    router refuses. Only its str() matters — that is all app.py renders."""

    def __str__(self) -> str:
        return RAW_400


# ── backends ───────────────────────────────────────────────────────────────

class _Backend:
    """The app-side view of Anvil's seam. `ready` decides whether the
    pre-flight query refuses, so a test can pick the situation directly
    instead of standing up a router to produce it."""

    name = "llamacpp"

    def __init__(self, *, ready: bool = True, hang: float = 0.0):
        self.ready = ready
        self.hang = hang
        self.asked: list = []

    def base_url(self) -> str:
        return "http://localhost:7470/v1"

    def host(self) -> str:
        return "localhost:7470"

    async def ensure_running(self) -> str:
        return "ok"

    async def list_models(self):
        return []

    def request_overrides(self, key):
        return {}

    async def ensure_chat_ready(self, key) -> None:
        self.asked.append(key)
        if self.hang:
            # A model still LOADING: the real seam rides this out for up to
            # LOAD_TIMEOUT_S (300s) inside a thread.
            await asyncio.sleep(self.hang)
        if not self.ready:
            raise BackendError(PLAIN)


class _Bare:
    """A backend WITHOUT ensure_chat_ready at all.

    LM Studio and llama.cpp both grew the method in Anvil's merge, but app.py
    is handed whatever `make_backend` returns, and plugins and test doubles
    predate it. A missing pre-flight query must mean "carry on", never an
    AttributeError mid-turn.
    """

    name = "llamacpp"

    def base_url(self) -> str:
        return "http://localhost:7470/v1"

    def host(self) -> str:
        return "localhost:7470"

    async def ensure_running(self) -> str:
        return "ok"

    async def list_models(self):
        return []

    def request_overrides(self, key):
        return {}


# ── app ────────────────────────────────────────────────────────────────────

def _app(backend, **overrides) -> app_mod.LiteTUI:
    a = app_mod.LiteTUI()
    base = dict(
        wake_after_compact=False,
        clear_screen_after_compact=False,
        compact_keep_recent=2,
        tools_enabled=False,
        autocompact_enabled=False,
    )
    base.update(overrides)
    a.settings = Settings(**base)
    a.tools_enabled = False
    a.backend = backend
    a.model_id = "m1"
    a.said: list[str] = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    a._connect = lambda: None      # boot must not race the test's backend
    a._fetch_ctx_window = lambda: None
    return a


def _seed(a) -> None:
    a.conversation = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do a big task"},
        {"role": "assistant", "content": "working on it"},
        {"role": "user", "content": "still going?"},
        {"role": "assistant", "content": "yes, nearly there"},
    ]


async def _settle(a, pilot, extra: int = 6, tick: float = 0.0) -> None:
    """`tick` buys REAL wall-clock per iteration.

    Without it this loop spins 200 near-instant pauses and returns while the
    worker is still running — which made the timeout test below pass for the
    wrong reason: it measured 0.0s elapsed and concluded the bound had worked.
    Any test asserting on DURATION must pass a tick.
    """
    for _ in range(200):
        await pilot.pause()
        if tick:
            await asyncio.sleep(tick)
        if not a._chat_running():
            break
    for _ in range(extra):
        await pilot.pause()


def _bubbles(a) -> str:
    """Everything the assistant bubbles are showing, as one string."""
    out = []
    for w in a.query(app_mod.AssistantMessage):
        try:
            out.append(str(w.body.content))
        except Exception:
            pass
    return "\n".join(out)


async def _raise_400(**kw):
    raise _Raw400()


# ── streams ────────────────────────────────────────────────────────────────

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
    def __init__(self, text: str):
        self._chunks = [_Chunk(text)]

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


def _ok_create(text: str):
    async def _create(**kw):
        return _Stream(text)
    return _create


# ── 1. THE REPORTED DEFECT — a turn against an unloaded model ─────────────

@pytest.mark.asyncio
async def test_a_turn_against_an_unloaded_model_says_plain_words_not_a_raw_400():
    a = _app(_Backend(ready=False))
    _seed(a)
    async with a.run_test() as pilot:
        # The router still refuses if anything reaches it — so a test that
        # passes can only be passing because the PRE-FLIGHT query fired.
        a.client.chat.completions.create = _raise_400
        a._append({"role": "user", "content": "hello"})
        a._stream()
        await _settle(a, pilot)
        shown = _bubbles(a)

    # The user-visible defect first — this is the line Ryan reported.
    assert "invalid_request_error" not in shown, (
        f"the raw router body reached the user: {shown!r}"
    )
    assert "not loaded" in shown and "/load m1" in shown, (
        f"the plain words never reached the user: {shown!r}"
    )
    assert a.backend.asked == ["m1"], "the pre-flight query was never asked"


# ── 2. CONTROL — a READY model still streams ──────────────────────────────

@pytest.mark.asyncio
async def test_a_ready_model_still_streams_normally():
    """The guard must not become a new blocker. Without this, refusing every
    turn would satisfy test 1."""
    a = _app(_Backend(ready=True))
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("a normal answer")
        a._append({"role": "user", "content": "hello"})
        a._stream()
        await _settle(a, pilot)
        shown = _bubbles(a)

    assert a.backend.asked == ["m1"], "the pre-flight query must still be asked"
    assert "a normal answer" in shown, f"a ready model stopped answering: {shown!r}"
    assert a.conversation[-1]["role"] == "assistant"


# ── 3. auto-compaction — the same words, and nobody typed anything ────────

@pytest.mark.asyncio
async def test_a_compaction_against_an_unloaded_model_says_plain_words():
    a = _app(_Backend(ready=False))
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _raise_400
        a._compact()
        await _settle(a, pilot)

    said = "\n".join(a.said)
    assert "invalid_request_error" not in said, (
        f"the raw router body reached the user: {said!r}"
    )
    assert "not loaded" in said and "/load m1" in said, (
        f"the plain words never reached the user: {said!r}"
    )
    assert a.backend.asked == ["m1"], "the pre-flight query was never asked"
    assert "Compact failed — conversation unchanged" in said, (
        "a refused compaction must still report itself as failed"
    )


# ── 4. CONTROL — a READY model still compacts ─────────────────────────────

@pytest.mark.asyncio
async def test_a_ready_model_still_compacts():
    a = _app(_Backend(ready=True))
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert a.backend.asked == ["m1"], "the pre-flight query must still be asked"
    assert any("the summary body" in str(m.get("content", ""))
               for m in a.conversation), "a ready model stopped compacting"


# ── 5. THE DECISION, PINNED: 2104 stays unguarded and degrades gracefully ─

@pytest.mark.asyncio
async def test_the_tool_summary_side_call_is_left_unguarded_and_falls_back():
    """Sentinel's brief said to guard all three non-user sites. This one was
    argued down and the ruling agreed, so the reasoning is pinned as a test
    rather than left in a message thread.

    The summarise side call already swallows its own failure and returns the
    MASK — non-lossy, pointer intact, tool result still correct. A pre-flight
    refusal here would be either a no-op (inside the try) or a regression
    (outside it). What must hold: an unready model still yields a usable tool
    result, and the raw 400 still never reaches the user.
    """
    from litetui import tool_context

    a = _app(
        _Backend(ready=False),
        tool_context_mode=tool_context.SUMM,     # force ROUTE_SUMMARISE
        tool_context_threshold_chars=2000,
    )
    _seed(a)
    a._materialise_convo()                        # the side call needs convo_dir
    raw = "x" * 50_000
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _raise_400
        out = await a._contextualise_tool_result("bash", raw)
        await pilot.pause()

    assert raw not in out, "the raw output must still be replaced by a pointer"
    assert "tool-raw" in out, f"the pointer to the parked raw is missing: {out!r}"
    assert "invalid_request_error" not in out, (
        f"the raw router body reached the tool result: {out[:400]!r}"
    )
    assert a.backend.asked == [], (
        "2104 must NOT ask the pre-flight query — guarding it would turn a "
        "working graceful degradation into a hard failure"
    )


# ── 6. the compaction's wait is BOUNDED — an explicit decision ────────────

@pytest.mark.asyncio
async def test_a_loading_model_does_not_stall_the_compaction_for_minutes(monkeypatch):
    """ensure_chat_ready WAITS OUT a model that is loading, up to
    LOAD_TIMEOUT_S = 300s, which is right for a turn the user is watching and
    wrong for an automatic maintenance action holding the chat group.

    The compaction bounds it and reports the wait instead of inheriting the
    300s ceiling. The turn at 3981 keeps the full wait deliberately.
    """
    monkey_bound = 0.5          # stand-in for the shipped 10s, so this is fast
    hang = 6.0                  # >> the bound: the seam would still be waiting
    a = _app(_Backend(ready=True, hang=hang))
    _seed(a)
    # monkeypatch, NOT a bare assign with a `finally` that restores a literal:
    # the first draft did that, and its finally CREATED the attribute on a
    # module where it did not exist — so the shipped-value test below passed
    # pre-fix purely from test-ordering pollution. raising=True makes a missing
    # constant an error here, and the restore is exact.
    monkeypatch.setattr(app_mod, "COMPACT_READY_TIMEOUT_S", monkey_bound,
                        raising=True)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("never reached")
        start = asyncio.get_running_loop().time()
        a._compact()
        await _settle(a, pilot, tick=0.02)
        elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < hang / 2, (
        f"the compaction sat through the whole load ({elapsed:.1f}s of a "
        f"{hang}s wait) — the bound never fired"
    )
    said = "\n".join(a.said)
    assert "still loading" in said, (
        f"a compaction that gave up waiting must SAY why: {said!r}"
    )
    assert "Compact failed — conversation unchanged" in said, (
        f"and must still report itself as failed: {said!r}"
    )
    assert a.conversation[-1]["content"] == "yes, nearly there", (
        "a compaction that gave up must leave the conversation untouched"
    )


def test_the_shipped_compaction_bound_is_far_below_the_backend_ceiling():
    """The value itself, not just the mechanism. If someone later raises this
    to the backend's ceiling the test above still passes — it patches the
    constant — so the shipped number needs its own assertion."""
    from litetui import llm_backend

    assert app_mod.COMPACT_READY_TIMEOUT_S <= 30, (
        "a compaction holds the exclusive chat group; the user's next message "
        "queues behind it for this long"
    )
    assert app_mod.COMPACT_READY_TIMEOUT_S < llm_backend.LOAD_TIMEOUT_S / 5, (
        "the whole point is that the compaction does NOT inherit the turn "
        f"path's {llm_backend.LOAD_TIMEOUT_S}s ceiling"
    )


# ── 7. A FAILING PROBE MUST NOT GATE THE TURN ────────────────────────────

@pytest.mark.asyncio
async def test_a_probe_that_itself_fails_does_not_block_the_turn():
    """The property that makes this fix additive rather than a new gate.

    `ensure_chat_ready` answers a question. When it cannot answer — server
    unreachable, a listing shape a newer llama build changed, a bug in the
    probe — we have NO information, and refusing a turn on no information
    turns a working setup into a broken one AND hides the real error behind a
    failure mode of our own making.

    Found by 12 existing tests, not by inspection: test_compaction_ui and
    test_wake_after_compact stub the CLIENT and leave a real backend pointed
    at nothing, and every one of them stopped compacting when the first draft
    let any exception through.
    """
    class _BrokenProbe(_Backend):
        async def ensure_chat_ready(self, key):
            self.asked.append(key)
            raise ConnectionError("no route to host")

    a = _app(_BrokenProbe())
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("answered anyway")
        a._append({"role": "user", "content": "hello"})
        a._stream()
        await _settle(a, pilot)
        shown = _bubbles(a)

    assert a.backend.asked == ["m1"], "the probe must still be attempted"
    assert "answered anyway" in shown, (
        f"a probe that could not answer blocked the turn: {shown!r}"
    )
    assert "no route to host" not in shown, (
        f"the probe's own failure was shown to the user: {shown!r}"
    )


@pytest.mark.asyncio
async def test_a_probe_that_itself_fails_does_not_block_a_compaction():
    """Same property on the bounded path — the wait_for wrapper must not
    reintroduce the gate the turn path just lost."""
    class _BrokenProbe(_Backend):
        async def ensure_chat_ready(self, key):
            self.asked.append(key)
            raise ConnectionError("no route to host")

    a = _app(_BrokenProbe())
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("the summary body")
        a._compact()
        await _settle(a, pilot)

    assert a.backend.asked == ["m1"], "the probe must still be attempted"
    assert any("the summary body" in str(m.get("content", ""))
               for m in a.conversation), (
        "a probe that could not answer blocked the compaction"
    )


@pytest.mark.asyncio
async def test_no_model_selected_still_sends_the_local_model_sentinel():
    """With nothing selected, _stream and _compact send
    `self.model_id or "local-model"` and let the server resolve it — how a
    single-model LM Studio serves a user who never picked anything.

    The seam answers "no model is selected" for that key, correctly for a
    caller with no fallback and wrongly for this one. Obeying it would refuse
    turns that work today, so the probe is skipped rather than asked about a
    model the request is not going to name.
    """
    class _RefusesEmpty(_Backend):
        async def ensure_chat_ready(self, key):
            self.asked.append(key)
            raise BackendError(
                "no model is selected — /model to pick one before sending.")

    a = _app(_RefusesEmpty())
    a.model_id = ""                      # nothing picked, the real-world case
    _seed(a)
    sent: list = []

    async def _capture(**kw):
        sent.append(kw.get("model"))
        return _Stream("answered")

    async with a.run_test() as pilot:
        a.client.chat.completions.create = _capture
        a._append({"role": "user", "content": "hello"})
        a._stream()
        await _settle(a, pilot)
        shown = _bubbles(a)

    assert a.backend.asked == [], (
        "the probe must not be asked about a model the request will not name"
    )
    # EVERY request, not just the first. The finished turn now also fires the
    # one-line card-summary side call (T:card-summary, 2026-09-16), which goes
    # through this same client - so this asserts the sentinel convention holds
    # for every call site rather than that only one exists. Strictly stronger
    # than the previous `sent == ["local-model"]`.
    assert sent, "no request was sent at all"
    # Pin the MAIN call by position, not just by membership: all() over an empty
    # or summary-only list would otherwise pass. sent[0] is _stream's request,
    # which is the one this file is about. Per-call-site cardinality for the
    # summary lives in test_card_summary.py, where it is not timing-dependent.
    assert sent[0] == "local-model", f"the stream did not send the sentinel: {sent!r}"
    assert all(m == "local-model" for m in sent), (
        f"the sentinel was not sent by every call site: {sent!r}"
    )
    assert "answered" in shown, f"an unselected model stopped working: {shown!r}"


# ── 8. a backend without the seam must not crash the turn ─────────────────

@pytest.mark.asyncio
async def test_a_backend_without_the_seam_still_streams():
    """app.py is handed whatever object `make_backend` returns, and test
    doubles and plugins predate this method. A missing pre-flight query must
    mean "carry on", never AttributeError mid-turn."""
    a = _app(_Bare())
    _seed(a)
    async with a.run_test() as pilot:
        a.client.chat.completions.create = _ok_create("still answers")
        a._append({"role": "user", "content": "hello"})
        a._stream()
        await _settle(a, pilot)
        shown = _bubbles(a)

    assert "still answers" in shown, (
        f"a backend without the seam broke the turn: {shown!r}"
    )
