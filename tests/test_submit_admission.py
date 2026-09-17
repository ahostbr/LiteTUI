"""What `_submit_text` sends, what it refuses, and that a refusal is AUDIBLE.

🔴 THE DEFECT THIS FILE EXISTS FOR (T822). `_looks_like_image_path` was an
UNANCHORED `re.search(r"\\.(png|jpe?g|gif|webp|bmp)\\b", text)` over the whole
message, and `_submit_text` printed a warning and RETURNED on it. So any
message that merely MENTIONED an image extension never reached the model:

    "why does my icon.png look blurry?"   -> never sent
    "convert foo.jpg to webp"             -> refused
    a pasted traceback naming a .png      -> refused

It was found by an 86,149-char prompt whose filler was `app.py` itself, so it
contained `.png` at offset 4778 — inside that file's own `IMAGE_EXTS` line.
FOUR measured driver runs stalled on it and reported nothing, because
`gui.prompt.submit` had already answered `{"accepted": true}` and the drop was
silent on the wire.

    A GREP MATCHES SYNTAX; THIS QUESTION IS ABOUT POSITION. And: AN ACCEPTANCE
    FOLLOWED BY A SILENT DROP IS WORSE THAN A REFUSAL — the host waits for a
    `turn_end` that is never coming.

⬜ WHY THE ARMS BELOW ARE SPLIT IN TWO. The predicate arms are pure and cheap,
so they can enumerate; the admission arms boot an app and assert the message
REACHES THE BACKEND, because the predicate returning False is not the same
claim as the turn being submitted — that gap is exactly where this lived.
"""

from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui.app import LiteTUI


# ── the predicate: a leading path ATTEMPT, never a mention ───────────────────

MENTIONS_BUT_IS_NOT_A_PATH = [
    "why does my icon.png look blurry?",
    "convert foo.jpg to webp",
    'the spec says IMAGE_EXTS = {".png", ".jpg"} which is fine',
    'here is the traceback:\n  File "x.py"\n  loading assets/logo.png failed',
    "should I use webp or a.gif for this",
]

IS_A_PATH_ATTEMPT = [
    "C:/x/shot.png",
    "shot.png what is this",
    "C:\\My Folder\\shot.png what is this",
    '"C:/My Folder/shot.png" what is this',
    "./rel/pic.jpeg",
    "~/Pictures/a.webp describe",
    "/tmp/x.gif",
]


@pytest.mark.parametrize("text", MENTIONS_BUT_IS_NOT_A_PATH)
def test_a_mention_is_an_ordinary_message(text):
    """🔴 EACH OF THESE WAS SILENTLY REFUSED BEFORE T822."""
    assert LiteTUI._looks_like_image_path(text) is False


@pytest.mark.parametrize("text", IS_A_PATH_ATTEMPT)
def test_a_leading_path_is_still_an_attempt(text):
    """⬜ THE CONTROL, and it is the half that must not regress: the warning
    exists because a bare path sent as prose made the model write an OCR tool
    to work around its own blindness."""
    assert LiteTUI._looks_like_image_path(text) is True


def test_a_huge_payload_containing_the_extension_list_is_ordinary():
    """🔴 THE ORIGINAL REPRODUCTION, kept at size.

    Not because size matters — it never did — but because this exact payload
    is what four runs stalled on, and an arm that only used the short sentences
    would leave a reader believing the bug was about length.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "litetui" / "app.py"
    filler = (src.read_text(encoding="utf-8", errors="replace") * 6)[:86_000]
    payload = "Reply with the single word OK. Context follows.\n\n" + filler
    assert ".png" in payload, "the payload must contain the substring to be a test"
    assert LiteTUI._looks_like_image_path(payload) is False


# ── admission: the message actually reaches the backend ──────────────────────


def _app():
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    a.said = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    a.emitted = []
    a._rpc_emit = a.emitted.append
    a.started = []
    a._stream = lambda *x, **k: a.started.append(True)
    return a


@pytest.mark.asyncio
@pytest.mark.parametrize("text", MENTIONS_BUT_IS_NOT_A_PATH)
async def test_a_mention_is_admitted_as_a_turn(text):
    """🔴 THE END-TO-END HALF. The predicate being False is not the same claim
    as the turn being submitted — `_submit_text` has several other early
    returns, and the defect lived between them."""
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text(text, alt_chord=False, source="rpc")
    assert a.started == [True], f"never reached the model: {a.said}"
    assert [e for e in a.emitted if e.get("type") == "submit_refused"] == []


@pytest.mark.asyncio
async def test_a_huge_payload_is_admitted_as_a_turn():
    """🔴 THE MEASUREMENT THAT FOUR RUNS COULD NOT TAKE."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "litetui" / "app.py"
    filler = (src.read_text(encoding="utf-8", errors="replace") * 6)[:100_000]
    payload = "Reply with the single word OK. Context follows.\n\n" + filler
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text(payload, alt_chord=False, source="rpc")
    assert a.started == [True], f"never reached the model: {a.said}"


@pytest.mark.asyncio
async def test_a_bad_path_still_refuses_AND_SAYS_SO_ON_THE_WIRE(tmp_path):
    """🔴 THE REFUSAL MUST BE AUDIBLE. A silent return is what made four runs
    indistinguishable from a slow turn: the host had an `{"accepted": true}`
    and then nothing forever.

    ⬜ The chat line is still there too — this arm asserts BOTH channels,
    because moving the message to the wire and dropping it from chat would be
    the same defect facing the other way.
    """
    missing = tmp_path / "does-not-exist.png"
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text(str(missing), alt_chord=False, source="rpc")

    assert a.started == [], "a broken path was sent to the model as prose"
    refusals = [e for e in a.emitted if e.get("type") == "submit_refused"]
    assert len(refusals) == 1, a.emitted
    assert refusals[0]["reason"] == "image_path_unopenable"
    assert any("could not open it" in line for line in a.said), a.said


@pytest.mark.asyncio
async def test_the_refusal_does_not_echo_a_whole_document_back():
    """⬜ The old warning printed the ENTIRE message. On the payload that found
    this bug that was 86 KB of source in the chat log, which buried the one
    sentence saying what to do."""
    a = _app()
    # NOT a leading "/" — `_submit_text` routes those to `_handle_command`
    # first, so a POSIX-absolute path typed here is read as a slash command
    # ("Unknown: /nope/... — try /help"). Pre-existing behaviour, noted and
    # deliberately not changed on this card; it just means the arm must use a
    # Windows-style path to reach the branch under test.
    long_path = "C:/nope/" + ("x" * 4000) + ".png"
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text(long_path + "\nand a second line", alt_chord=False, source="rpc")
    warning = "".join(line for line in a.said if "could not open" in line)
    assert warning, a.said
    assert len(warning) < 600, f"echoed {len(warning)} chars back"
    assert "second line" not in warning


@pytest.mark.asyncio
async def test_an_empty_submit_is_refused_audibly_too():
    """⬜ The same rule at the other early return: nothing to send is still an
    answer a host needs."""
    a = _app()
    async with a.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        a._submit_text("   ", alt_chord=False, source="rpc")
    assert a.started == []
    assert [e["reason"] for e in a.emitted if e.get("type") == "submit_refused"] == ["empty"]
