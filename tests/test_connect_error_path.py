"""The error path must not need the thing whose absence caused the error (T858).

🔴 WHAT RYAN SAW. He opened LiteTUI with the NInfer engine down and got a
Textual `WorkerFailed` traceback dumped over his terminal — instead of the
sentence the code had already written for exactly this case:

    "no NInfer engine is registered — start it from LiteSuite's Model Hub
     (Settings → NInfer), or set ninfer_host (LITETUI_NINFER_HOST), or
     /engine start"

`ensure_running` raised that correctly. `connect`'s `except` caught it. Then
the HANDLER raised a second `BackendError`, out of `app.py`'s detail line,
because it built its log string with `self.backend.host()` — and `host()`
raises when no engine is registered.

    AN ERROR HANDLER RUNS ONLY WHEN SOMETHING IS ALREADY BROKEN, SO IT MUST NOT
    ASK FOR ANYTHING THAT THE BREAKAGE REMOVES. On this backend the most likely
    cause of the failure IS the thing that makes `host()` raise.

⬜ WHY NO EXISTING ARM SAW IT, AND THIS IS THE REUSABLE PART. Every arm that
exercises `connect` sets up a WORKING world — a backend with models, a host, a
server that answers. An error handler is by definition only reached once that
world is broken, so it is precisely the code least likely to be covered by a
suite whose fixtures all succeed. The assertion that would have caught this is
not about the message at all; it is "the handler finished".

⬜ THE FIX IS AT THE CALL SITE, NOT IN `host()`. `host()` raising is a real
contract: callers who need an address cannot proceed without one. The lesson
was already written in capitals on its neighbour — `base_url()`'s docstring
(ninfer_backend.py:335) says it MUST NEVER RAISE and names the boot crash it
caused. It was applied to one method and not to the other.
"""

from __future__ import annotations

import asyncio

import pytest

from litetui import app as app_mod
from litetui import ninfer_backend
from litetui.llm_backend import BackendError
from litetui.settings import Settings


@pytest.fixture
def unregistered(monkeypatch):
    """A REAL NInferBackend in the state Ryan's machine was in: nothing
    registered, no explicit host, so discovery answers None.

    Not a double. The whole defect is an interaction between two real methods
    of this class — `ensure_running` raising and `host()` raising for the SAME
    reason — and a stand-in with a hand-written `host()` would only prove that
    my idea of it is self-consistent.
    """
    monkeypatch.setattr(ninfer_backend, "discover_ninfer_host", lambda *a, **k: None)
    backend = ninfer_backend.NInferBackend(Settings(ninfer_host=""))
    # The precondition, asserted rather than assumed: both of these raise, and
    # that pairing is the defect.
    with pytest.raises(BackendError):
        backend.host()
    return backend


def _app(backend):
    a = app_mod.LiteTUI()
    a.settings = Settings(ninfer_host="")
    a.backend = backend
    a.said: list[str] = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    a._update_header = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


@pytest.mark.asyncio
async def test_a_missing_engine_reports_a_sentence_and_does_not_raise(unregistered):
    """🔴 THE ARM FOR THE TRACEBACK. Before T858 the handler raised a SECOND
    BackendError and Textual reported WorkerFailed."""
    a = _app(unregistered)
    async with a.run_test(size=(120, 35)) as pilot:
        for _ in range(60):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if a.said:
                break

    assert a.said, "connect produced no message at all"
    assert a.sub_title == "Disconnected", a.sub_title
    assert any("no NInfer engine is registered" in s for s in a.said), a.said
    # The words that tell him what to do survived; this is what he never saw.
    assert any("Model Hub" in s or "/engine start" in s for s in a.said), a.said


@pytest.mark.asyncio
async def test_the_handler_itself_completes(unregistered):
    """⬜ THE ASSERTION THAT WOULD HAVE CAUGHT IT, made explicit.

    The message arms above are about words. This one is about the handler
    REACHING ITS END: a second exception thrown anywhere inside it — from the
    log line, the sub_title, or the sentence — leaves the app with no
    `Disconnected` and the worker dead, which is exactly what happened.
    """
    a = _app(unregistered)
    async with a.run_test(size=(120, 35)) as pilot:
        for _ in range(60):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if a.sub_title == "Disconnected":
                break
    assert a.sub_title == "Disconnected"


def test_base_url_still_never_raises_in_the_same_state(unregistered):
    """⬜ THE NEIGHBOUR'S CONTRACT, pinned here because T858 is the second
    instance of the same defect and the first one was `base_url()`. If someone
    ever 'simplifies' it to call `host()`, this goes red in the file that
    explains why."""
    assert unregistered.base_url().endswith("/v1")
