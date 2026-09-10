"""T558-A step 1 — THE REPRODUCTION, before any fix.

I claimed from READING that `ask_user_question` deadlocks under `--rpc`. A claim
about a hang is exactly the kind that must be executed before it is designed on:
a hang and a slow-but-finite wait look identical until someone puts a clock on
them, and the whole shape of the fix depends on which one it is.

WHAT IS ASSERTED: with the app running HEADLESS — the state `--rpc` puts it in
(`cli.py` calls `app.run(headless=True)`) — `ask_user_question.run` does not
return, because it pushes a Textual screen and then blocks its worker thread on a
`threading.Event` that only a human pressing keys in a visible TUI can set. There
is no rpc branch in that module.

⚠️ WHAT "DOES NOT RETURN" MEANS HERE, precisely: it did not return within the
timeout below. That is the honest form of a hang assertion — you cannot prove a
negative by waiting, only bound it. The bound is generous relative to the work
(pushing a screen is milliseconds) and the failure mode it is distinguishing
against is "returns promptly with an error", which would be well inside it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import ask_user_question as auq  # noqa: E402

#: Generous: the behaviour being separated from a hang is an IMMEDIATE return.
BLOCKED_FOR_SECONDS = 3.0

#: Longer than ONE poll interval of the release loop (5s, ask_user_question.py:671).
RELEASED_AFTER_TEARDOWN_SECONDS = 8.0

#: A payload the tool ACCEPTS. Options are keyed `title`, not `label`
#: (ask_user_question._parse_questions:143) — my first draft used `label`, the call
#: returned "[error] ... no usable options" in milliseconds, and the reproduction
#: "proved" there was no hang. A REPRODUCTION THAT FAILS VALIDATION LOOKS EXACTLY
#: LIKE A REPRODUCTION THAT FOUND NOTHING TO REPRODUCE.
ONE_QUESTION = {
    "questions": [
        {
            "label": "Approach",
            "question": "Which approach?",
            "options": [
                {"title": "A", "description": "the first"},
                {"title": "B", "description": "the second"},
            ],
        }
    ]
}


@pytest.mark.asyncio
async def test_ask_user_question_does_not_return_while_headless():
    """The reproduction. This is the defect, not a regression guard."""
    a = m.LiteTUI()
    returned: list[str] = []

    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # The real dispatch path: app.py runs the tool with asyncio.to_thread, so
        # a worker thread is where this blocks in production too.
        worker = threading.Thread(
            target=lambda: returned.append(auq.run(ONE_QUESTION, a)),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=BLOCKED_FOR_SECONDS)

        blocked = worker.is_alive()
        # Let the app tear down; the module's own docblock says teardown is the
        # ONLY thing that releases the thread when nobody answers.
        assert blocked, (
            f"ask_user_question returned within {BLOCKED_FOR_SECONDS}s while headless "
            f"— it returned {returned!r}. If this now passes an answer back, the "
            "deadlock is fixed and THIS TEST should be replaced by the arms for "
            "the fix, not deleted."
        )

    # After teardown the thread IS released, which is what makes the hang last
    # exactly as long as the session rather than forever.
    #
    # ⚠️ THE NUMBER IS NOT ARBITRARY AND 3s WAS WRONG. The release comes from a
    # poll — `while not done.wait(timeout=5)` then an `is_running` check
    # (ask_user_question.py:671-672) — so a worker can sit up to one FULL poll
    # interval past teardown. My first version joined for 3s, saw a live thread,
    # and read a correct 5-second poll as a leak. A release check must outlast
    # one interval of whatever does the releasing.
    worker.join(timeout=RELEASED_AFTER_TEARDOWN_SECONDS)
    assert not worker.is_alive(), (
        "the worker outlived app teardown by more than one poll interval — the "
        "is_running escape hatch is the only thing that ends this thread, and it "
        "is not firing"
    )


def test_the_module_has_no_rpc_path_at_all():
    """The mechanism behind the hang, asserted structurally.

    The timing arm above says IT BLOCKS. This one says WHY: there is nothing in
    the module that could answer a question from off-screen. If someone adds an
    rpc branch, this goes red and the timing arm above should be revisited in the
    same change — which is the point of pinning both.
    """
    source = (
        Path(__file__).resolve().parent.parent / "src" / "litetui" / "ask_user_question.py"
    ).read_text(encoding="utf-8")
    assert "rpc" not in source.lower(), (
        "ask_user_question now mentions rpc — if the wire path landed, this "
        "reproduction is stale and the fix's own arms replace it"
    )
