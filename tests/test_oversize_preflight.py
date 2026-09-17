"""A message too big for the window is refused BEFORE it is sent (T827).

🔴 COMPACTION CANNOT HELP, AND NOTHING SAID SO. Measured during T806 item 6:
one 85,384-char message put the context at 86.0% of 32,768. Autocompact was
due (`autocompact_due` returned 86) and fired — and then declined, correctly,
with "Nothing to compact — everything is already recent", because
`compact_keep_recent` is 4, the conversation body was exactly 4 messages, and
`_safe_tail` kept all of them so `head` was empty.

    COMPACTION SUMMARISES OLD HISTORY AND KEEPS THE RECENT TAIL. A WINDOW
    FILLED BY THE NEWEST MESSAGE IS A WINDOW IT IS REQUIRED TO PRESERVE.

So the sequence a user gets today is: a reassuring "Auto-compacting — context
at 86%…" line, then nothing changes, then the next request carries the same
oversized message and the engine answers 400 `context_length_exceeded` — which
before T824 rendered as "Something went wrong talking to the model server."
Three messages, none of which say **this one message does not fit**.

⚠️ AND NOTE WHAT IT COST A SEAT WITH FULL INSTRUMENTATION: I had the policy
function, the transcript, the context meter and py-spy, and still spent four
measurement runs before reading `compact_keep_recent`. A user has one chat line.
"""

from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui.settings import Settings


def _app(*, window=32768, loaded=True, tools=False,
         max_chat=8192, max_tools=12288):
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    a.settings = Settings(max_tokens_chat=max_chat, max_tokens_tools=max_tools)
    a.tools_enabled = tools
    a.ctx_max = window
    a.ctx_loaded = loaded
    a.said = []
    a._system = lambda m, *x, **k: a.said.append(str(m))
    a.emitted = []
    a._rpc_emit = a.emitted.append
    a.started = []
    a._stream = lambda *x, **k: a.started.append(True)
    return a


# ── the predicate ────────────────────────────────────────────────────────────


def test_an_ordinary_message_is_not_refused():
    """⬜ THE CONTROL, and the one that matters most: this check sits on the
    path every single message takes."""
    a = _app()
    assert a._oversize_refusal("what is a KV cache?") is None


def test_a_message_larger_than_the_usable_window_is_refused():
    """🔴 THE SHAPE FROM THE MEASUREMENT: 32,768 window, 8,192 reserved, so
    24,576 usable — and a ~21k-token message fits, while a ~30k one cannot."""
    a = _app()
    assert a._oversize_refusal("x" * (21_000 * 4)) is None
    said = a._oversize_refusal("x" * (30_000 * 4))
    assert said is not None
    for number in ("32,768", "8,192", "24,576"):
        assert number in said, (number, said)
    assert "cannot make room for this one" in said


def test_the_reserve_follows_which_one_the_request_will_send():
    """⬜ NO SECOND NOTION OF 'USABLE'. `TurnEngine.chat_request` picks
    max_tokens_tools when tools are on and max_tokens_chat otherwise; this must
    pick the same one or it refuses against a budget the request never uses."""
    text = "x" * (22_000 * 4)
    assert _app(tools=False)._oversize_refusal(text) is None      # 24,576 usable
    refused = _app(tools=True)._oversize_refusal(text)            # 20,480 usable
    assert refused is not None and "12,288" in refused


def test_an_unknown_window_refuses_nothing():
    """🔴 REFUSE ONLY ON NUMBERS IT HAS. Guessing a window would reject a
    message that might have been fine — and `ctx_max` is the model's CEILING
    until `ctx_loaded`, which is the 262,144-vs-8k trap the footer already
    carries a comment about."""
    huge = "x" * (60_000 * 4)
    assert _app(window=None)._oversize_refusal(huge) is None
    assert _app(loaded=False)._oversize_refusal(huge) is None


def test_the_image_parts_are_counted_too():
    """⬜ A list content is what an image message looks like; `_msg_chars`
    already folds it, so the check must not be text-only."""
    a = _app()
    parts = [{"type": "text", "text": "x" * (30_000 * 4)},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    assert a._oversize_refusal(parts) is not None


# ── the admission path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_oversized_submit_never_reaches_the_model_and_says_why():
    """🔴 BOTH CHANNELS. The chat line is for the human; `submit_refused` is
    for a host (Frontier Chat), which otherwise sees an `{"accepted": true}`
    followed by nothing."""
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text("x" * (30_000 * 4), alt_chord=False, source="rpc")

    assert a.started == [], "an unsendable message was sent anyway"
    refusals = [e for e in a.emitted if e.get("type") == "submit_refused"]
    assert [r["reason"] for r in refusals] == ["message_over_budget"]
    assert any("cannot make room" in s for s in a.said), a.said


@pytest.mark.asyncio
async def test_nothing_is_committed_when_the_message_is_refused():
    """🔴 THE REASON THE CHECK SITS WHERE IT DOES. After this point the message
    is queued or streamed, and a refusal would have to UNDO state — which is
    exactly the bug T824 fixed for a refused image, where the part stayed in
    history and poisoned every later turn."""
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        before = list(a.conversation)
        a._submit_text("x" * (30_000 * 4), alt_chord=False, source="rpc")
        assert a.conversation == before, "the refused message entered history"


@pytest.mark.asyncio
async def test_an_ordinary_message_still_goes_through():
    """⬜ THE CONTROL ON THE REAL PATH, not just the predicate."""
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text("what is a KV cache?", alt_chord=False, source="rpc")
    assert a.started == [True]
    assert [e for e in a.emitted if e.get("type") == "submit_refused"] == []
