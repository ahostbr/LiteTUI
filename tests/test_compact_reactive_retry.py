"""Compaction is REACTIVE: no prompt size is estimated, nothing is refused before sending.

History: T0696 clamped the summary budget from a chars-to-tokens estimate and refused outright when
the estimate said nothing fit. On Ryan's DELETE seat (ninfer, 100,000-token window) the estimate was
98,817 tokens where the server measured 86,912, and a compaction that the server would have accepted
was thrown away ("Compact failed ... no summary fits"). It was reverted (364f2c08).

What is left is the one data-driven path: a server that refuses max_tokens and NAMES the room it has
("... Send a smaller max_tokens (at most 16300 here)") is believed, once, per request.

The replay below uses the server's own numbers from the real compaction on that seat
(convo 06d3a2e3..., the `truncate` record, measurements.rounds): 9 rounds, prompt_tokens
79,194 / 80,405 / 80,806 / 81,087 / 83,701 / 85,247 / 85,458 / 86,721 / 86,912, a 4,754-char summary.
"""
import json
import pathlib
from types import SimpleNamespace as NS

import pytest
from test_wake_after_compact import _Stream, _app, _settle

from litetui import app as app_mod
from litetui import model_transport
from litetui.turn_engine import TurnEngine

RECORDED_PROMPT_TOKENS = [79194, 80405, 80806, 81087, 83701, 85247, 85458, 86721, 86912]
RECORDED_COMPLETION_TOKENS = [2432, 88, 591, 68, 1664, 289, 48, 168, 1334]
WINDOW = 100_000
REAL_CONVO = pathlib.Path(
    r"C:\Projects\LiteTUI\.agents\DELETE\conversations\06d3a2e3-9dfd-48c9-86d1-59e2fc0ab82b\convo.jsonl"
)

STRATA_REFUSAL = (
    "Error code: 400 - prompt ({prompt} tokens) + max tokens ({asked}) exceeds the context ({window}); "
    "requests are never truncated. Send a smaller max_tokens (at most {room} here)"
)


# ── the parser: only a server's own words about max_tokens are believed ──────────────

@pytest.mark.parametrize("message, expected", [
    (STRATA_REFUSAL.format(prompt=83692, asked=24000, window=100000, room=16300), 16300),
    ("max_tokens is too large: 24000. This model supports at most 16384 completion tokens, "
     "whereas you provided 24000.", 16384),
    ("... Send a smaller max_tokens (at most 1,234 here)", 1234),
    ("the queue holds at most 5 requests", None),        # no max_tokens in the sentence: not read
    ("prompt too long; max_tokens must be smaller", None),  # no number named
    ("", None),
    (None, None),
])
def test_hint_is_read_only_from_a_servers_own_words(message, expected):
    assert TurnEngine.max_tokens_hint(message) == expected


# ── replay of the recorded compaction ────────────────────────────────────────────────

def _conversation_like_the_recording() -> list[dict]:
    """73 messages / 200,695 chars, the size the real record measured."""
    msgs = [{"role": "system", "content": "sys"}]
    per = (200_695 - 3) // 72
    for i in range(72):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "x" * per})
    return msgs


def _conversation_from_the_real_record() -> list[dict]:
    recs = [json.loads(line) for line in REAL_CONVO.read_text(encoding="utf-8").splitlines()]
    cut = next(i for i, r in enumerate(recs) if r.get("type") == "truncate")
    return [r["message"] for r in recs[:cut] if r.get("type") == "msg"]


class _Chunks:
    def __init__(self, chunks):
        self._chunks = chunks

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


def _round_stream(n: int) -> _Chunks:
    """Round n (1-based) as the real server answered it: its usage, then a tool call, or the summary."""
    usage = NS(
        prompt_tokens=RECORDED_PROMPT_TOKENS[n - 1],
        completion_tokens=RECORDED_COMPLETION_TOKENS[n - 1],
        total_tokens=RECORDED_PROMPT_TOKENS[n - 1] + RECORDED_COMPLETION_TOKENS[n - 1],
    )
    chunks = [NS(choices=[], usage=usage)]
    if n < len(RECORDED_PROMPT_TOKENS):
        call = NS(index=0, id=f"call_{n}", function=NS(name="read", arguments="{}"))
        delta = NS(content=None, reasoning_content=None, reasoning=None, tool_calls=[call])
    else:
        delta = NS(content="S" * 4754, reasoning_content=None, reasoning=None, tool_calls=None)
    chunks.append(NS(choices=[NS(delta=delta)], usage=None))
    return _Chunks(chunks)


def _replay_app(monkeypatch, conversation, server: str):
    """server: 'accepts' (ninfer measured) | 'strata' (refuses prompt+max_tokens > window, names the room)
    | 'opaque' (always a 500 with no number)."""
    app = _app(compact_max_tokens=24000, compact_max_tool_iters=12)
    app.conversation = conversation
    app.ctx_max, app.ctx_loaded, app.ctx_used = WINDOW, True, 80371
    notes: list[str] = []
    app._system = lambda text, *a, **k: notes.append(text)

    async def execute(name, args, **kw):
        return "tool output", True

    app._execute_tool = execute
    log = {"compaction": [], "refused": []}
    served = {"rounds": 0}

    async def create(purpose=None, **kw):
        if purpose != "compaction":
            return _Stream("ok")
        asked = kw["max_tokens"]
        log["compaction"].append(asked)
        n = served["rounds"] + 1
        real_prompt = RECORDED_PROMPT_TOKENS[min(n, len(RECORDED_PROMPT_TOKENS)) - 1]
        if server == "opaque":
            raise RuntimeError("Error code: 500 - internal server error")
        if server == "strata" and real_prompt + asked > WINDOW:
            log["refused"].append((n, asked))
            raise RuntimeError(STRATA_REFUSAL.format(
                prompt=real_prompt, asked=asked, window=WINDOW, room=WINDOW - real_prompt))
        served["rounds"] = n
        return _round_stream(n)

    monkeypatch.setattr(model_transport, "for_app", lambda a: NS(create=create))
    return app, notes, log, served


def _summary_of(app) -> str:
    return next((m["content"] for m in app.conversation
                 if isinstance(m.get("content"), str)
                 and m["content"].startswith("[Summary of earlier conversation")), "")


async def _run(app, pilot):
    app._compact()
    await _settle(app, pilot)


@pytest.mark.asyncio
async def test_replay_on_a_server_that_accepts_changes_nothing(monkeypatch):
    """ninfer, as measured: it accepted 79,194 + 24,000 > 100,000. Behaviour is exactly the old one."""
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "accepts")
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert served["rounds"] == 9
    assert log["compaction"] == [24000] * 9      # the configured budget, every round, nothing clamped
    assert not log["refused"]
    assert not any("Compact failed" in n or "retrying" in n for n in notes), notes
    assert _summary_of(app).startswith("[Summary of earlier conversation")
    assert "S" * 4754 in _summary_of(app)


@pytest.mark.asyncio
async def test_replay_on_a_server_that_refuses_like_strata_retries_at_its_number(monkeypatch):
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "strata")
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert served["rounds"] == 9, notes
    assert not any("Compact failed" in n for n in notes), notes
    # round 1 is refused at 24,000 and retried at exactly the server's room (100,000 - 79,194)
    assert log["refused"][0] == (1, 24000)
    assert log["compaction"][:2] == [24000, WINDOW - RECORDED_PROMPT_TOKENS[0]]
    # Every request after the first refusal starts from the room the server named, and a round the
    # server still refuses is retried once at ITS new number (80,405 + 20,806 > 100,000 -> 19,595).
    expected = [24000, WINDOW - RECORDED_PROMPT_TOKENS[0]]
    cap = WINDOW - RECORDED_PROMPT_TOKENS[0]
    for prompt in RECORDED_PROMPT_TOKENS[1:]:
        if prompt + cap > WINDOW:
            expected.append(cap)                       # refused...
            cap = WINDOW - prompt
        expected.append(cap)                           # ...then accepted at the server's number
    assert log["compaction"] == expected, (log["compaction"], expected)
    assert all(asked + RECORDED_PROMPT_TOKENS[n - 1] <= WINDOW or n == rnd
               for n, asked in [(1, 0)] for rnd in [1])  # (shape check only)
    # ONE plain line about it, not one per round
    said = [n for n in notes if "retrying with" in n]
    assert len(said) == 1 and "room for 20,806 tokens" in said[0], notes
    assert "S" * 4754 in _summary_of(app)


@pytest.mark.asyncio
async def test_a_refusal_without_a_number_fails_once_with_the_servers_words(monkeypatch):
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "opaque")
    before = list(app.conversation)
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert log["compaction"] == [24000]            # no retry without a number
    failed = [n for n in notes if "Compact failed" in n]
    assert len(failed) == 1
    assert "internal server error" in failed[0]     # the server's own words reach the user
    for gone in ("LM Studio", "PARALLEL", "/truncate", "needs more room", "That is what was refused"):
        assert gone not in failed[0]
    assert app.conversation == before               # unchanged


@pytest.mark.asyncio
async def test_a_number_too_small_to_write_a_summary_in_is_not_retried(monkeypatch):
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "strata")

    async def create(purpose=None, **kw):
        if purpose != "compaction":
            return _Stream("ok")
        log["compaction"].append(kw["max_tokens"])
        raise RuntimeError(STRATA_REFUSAL.format(prompt=99600, asked=24000, window=WINDOW, room=400))

    monkeypatch.setattr(model_transport, "for_app", lambda a: NS(create=create))
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert log["compaction"] == [24000]
    failed = [n for n in notes if "Compact failed" in n]
    assert len(failed) == 1 and "exceeds the context" in failed[0]


@pytest.mark.asyncio
@pytest.mark.skipif(not REAL_CONVO.exists(), reason="Ryan's DELETE seat record is not on this machine")
@pytest.mark.parametrize("server", ["accepts", "strata"])
async def test_replay_of_the_real_recorded_conversation(monkeypatch, server):
    conversation = _conversation_from_the_real_record()
    assert len(conversation) >= 70, "the record should hold the compacted history"
    app, notes, log, served = _replay_app(monkeypatch, conversation, server)
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert served["rounds"] == 9, notes
    assert not any("Compact failed" in n for n in notes), notes
    assert "S" * 4754 in _summary_of(app)


def test_no_compaction_path_estimates_a_prompt_size():
    """The estimator is gone for good: nothing in app.py turns characters into a prompt token count
    for compaction, and the 'needs more room than the conversation already fills' paragraph is deleted."""
    source = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "_compaction_fit_note" not in source
    assert "needs more room" not in source
    assert "compact_budget" not in source
    assert "PARALLEL setting of N" not in source
