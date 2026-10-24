"""Compaction never asks for more summary tokens than the window has left.

Ryan's DELETE seat on Strata (2026-10-24): a 100,000-token window, an 83,692-token compaction
prompt and compact_max_tokens=24000 gave a 400 "prompt + max tokens exceeds the context;
requests are never truncated ... at most 16300 here".
"""
import pytest
from test_wake_after_compact import _Stream, _app, _settle

from litetui import llm_backend
from litetui.turn_engine import TurnEngine


def _budget(**kw):
    base = dict(configured=24000, ctx_max=100000, ctx_loaded=True, prompt_tokens=50000)
    base.update(kw)
    return TurnEngine.compact_budget(**base)


def test_budget_passes_through_when_it_fits():
    assert _budget(prompt_tokens=50000) == 24000


def test_budget_is_cut_to_the_room_left():
    got = _budget(prompt_tokens=83692)
    assert got is not None and got < 16308  # the server's own room, minus a margin
    assert got > 12000


def test_budget_is_none_when_no_summary_fits():
    assert _budget(prompt_tokens=98000) is None


def test_budget_untouched_without_a_loaded_window():
    # ctx_max may be the model's ceiling, not the window: never clamp against it.
    assert _budget(ctx_loaded=False, prompt_tokens=99999) == 24000
    assert _budget(ctx_max=None, prompt_tokens=99999) == 24000


def test_hint_is_read_from_the_server_words():
    msg = "prompt (83692 tokens) + max tokens (24000) exceeds the context (100000). Send a smaller max_tokens (at most 16300 here)"
    assert TurnEngine.max_tokens_hint(msg) == 16300
    assert TurnEngine.max_tokens_hint("some other 400") is None


def _window_app(used=80000, **settings):
    app = _app(compact_max_tokens=24000, **settings)
    big = "x" * 53000  # six of these = 318,000 chars ~ 80,000 tokens
    app.conversation = [{"role": "system", "content": "sys"}]
    for n in range(3):
        app.conversation.append({"role": "user", "content": big})
        app.conversation.append({"role": "assistant", "content": big})
    app.conversation += [  # the kept-verbatim tail is tiny, so nearly all the history is in the prompt
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": "ok"},
    ]
    app.ctx_max, app.ctx_loaded, app.ctx_used = 100000, True, used
    app._compact_notes = []
    app._system = lambda text, *a, **k: app._compact_notes.append(text)
    return app


@pytest.mark.asyncio
async def test_compaction_sends_a_budget_that_fits_the_window():
    app = _window_app()
    sent = []

    async def create(**kw):
        sent.append(kw["max_tokens"])
        return _Stream("summary")

    app.client.chat.completions.create = create
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)
    assert sent and sent[0] < 24000
    assert 1024 <= sent[0] <= 100000 - 79000  # inside the room the prompt leaves
    assert any("budget cut" in n for n in app._compact_notes)
    assert not any("Compact failed" in n for n in app._compact_notes)


@pytest.mark.asyncio
async def test_compaction_retries_once_with_the_ceiling_the_server_names():
    app = _window_app(used=1000)  # estimate says plenty of room; the server disagrees
    app.ctx_max = None  # no window known: nothing to pre-clamp with
    sent = []

    async def create(**kw):
        sent.append(kw["max_tokens"])
        if len(sent) == 1:
            raise RuntimeError(
                "Error code: 400 - prompt (83692 tokens) + max tokens (24000) exceeds the context "
                "(100000); requests are never truncated. Send a smaller max_tokens (at most 16300 here)"
            )
        return _Stream("summary")

    app.client.chat.completions.create = create
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)
    assert sent[:2] == [24000, 16300]  # later calls are the wake turn and card summary
    assert not any("Compact failed" in n for n in app._compact_notes)


@pytest.mark.asyncio
async def test_compaction_with_no_room_says_so_and_sends_nothing():
    app = _window_app(used=99000)
    sent = []

    async def create(**kw):
        sent.append(kw)
        return _Stream("summary")

    app.client.chat.completions.create = create
    async with app.run_test() as pilot:
        app._compact()
        await _settle(app, pilot)
    assert not sent
    failed = [n for n in app._compact_notes if "Compact failed" in n]
    assert failed and "no summary fits" in failed[0] and "/truncate" in failed[0]
    assert len(app.conversation) == 9  # unchanged
