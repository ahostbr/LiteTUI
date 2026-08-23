"""Live thinking-trace E2E: streaming, click-to-collapse, and a follow-up turn.

MOVED HERE FROM tests/test_thinking.py. It was in the default suite and failed
with `AssertionError: no ThinkingBlock appeared!` on any machine without a
thinking model resident — which is every machine that has not started one. The
project documents a no-service test promise, and this test broke it: a red here
means "no model is running", not "the code is wrong", and a gate that cannot
run in a clean environment trains people to skip the whole bar.

It genuinely needs a live model. It sends a real prompt, waits up to 180s for a
real stream, and asserts the assistant carried `reasoning_content` — none of
which can be faked without testing the fake instead of the product.

Gated twice: e2e/ is outside `testpaths`, and e2e/conftest.py skips everything
here unless LITETUI_E2E=1. See that file for why one gate was not enough.

Run it deliberately:
    set LITETUI_E2E=1 && uv run --locked pytest e2e/test_thinking_live.py -s
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path as _P

import pytest

import sys

# e2e/ is one level below the repo root, same as tests/ was.
sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "src"))

from litetui import paths  # noqa: E402
from litetui.app import LiteTUI, AssistantMessage, ThinkingBlock, ThinkingHeader  # noqa: E402

# Booting LiteTUI creates a real .convos/<uuid>/ before anything is typed, so a
# test that instantiates it leaves an empty conversation in the user's list.
# `from app import LiteTUI` does not bind CONVO_DIR here, but _new_convo reads
# the MODULE global at call time, so patching the module is what takes effect.
paths.CONVO_DIR = _P(tempfile.mkdtemp(prefix="convos-thinking-"))

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def get_text(w) -> str:
    r = w.content
    return r.plain if hasattr(r, "plain") else str(r)


async def wait_stream_done(app, timeout_s: float = 180.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        running = any(
            getattr(w, "group", None) == "chat" and getattr(w, "is_running", False)
            for w in app.workers
        )
        if not running:
            return True
        await asyncio.sleep(0.5)
    return False


async def test_thinking_trace_streams_collapses_and_survives_a_second_turn() -> None:
    app = LiteTUI()
    async with app.run_test(size=(120, 32)) as pilot:
        # Wait for connection
        for _ in range(60):
            await pilot.pause(0.25)
            if app.sub_title not in ("Connecting...",):
                break
        print(f"[1] connected, model = {app.sub_title}")

        # ── Turn 1: expect a thinking trace ─────────────────────────
        inp = app.query_one("#message-input")
        inp.value = "What is 2+2? Answer in one word."
        await pilot.press("enter")

        ok = await wait_stream_done(app)
        print(f"[2] stream done: {ok}")

        blocks = list(app.query(ThinkingBlock))
        print(f"[3] thinking blocks: {len(blocks)}")
        assert blocks, "no ThinkingBlock appeared!"
        block = blocks[0]
        trace = get_text(block.text)
        print(f"[4] thinking expanded={block.expanded}, trace length={len(trace)}")
        print(f"    trace head: {trace[:80]!r}")
        assert len(trace) > 10, "thinking trace looks empty"

        body = app.query_one(AssistantMessage).body
        print(f"    answer head: {get_text(body)[:80]!r}")

        # ── Click header: should collapse ───────────────────────────
        header = block.query_one(ThinkingHeader)
        block.scroll_visible()
        await pilot.pause()
        ok = await pilot.click(header)
        await pilot.pause()
        print(f"[5] click landed={ok} expanded={block.expanded} (expect False)")
        assert block.expanded is False

        # ── Click again: should expand ──────────────────────────────
        block.scroll_visible()
        await pilot.pause()
        ok = await pilot.click(header)
        await pilot.pause()
        print(f"[6] click landed={ok} expanded={block.expanded} (expect True)")
        assert block.expanded is True

        # ── Turn 2: verify echoed reasoning_content doesn't break API ─
        assert any(
            isinstance(m.get("content"), str) and m.get("reasoning_content")
            for m in app.conversation
            if m["role"] == "assistant"
        ), "assistant message should carry reasoning_content"
        inp.value = "Now what is 3+3? One word."
        await pilot.press("enter")
        ok2 = await wait_stream_done(app)
        print(f"[7] turn 2 stream done: {ok2}")
        msgs = list(app.query(AssistantMessage))
        last_body = msgs[-1].body
        text = get_text(last_body)
        print(f"    turn 2 answer: {text[:80]!r}")
        assert "Error" not in text, "turn 2 errored"
        assert ok2
