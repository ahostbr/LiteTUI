"""Drive ask_user_question through Textual's headless pilot — a real running app.

Covers both halves of the feature:
  * the BRIDGE: run() called from a worker thread against a live app must
    block, push the widget, return the serialized string, and leave the app
    clean — exactly what app.py's `asyncio.to_thread(fn, args)` dispatch does;
  * the WIDGET, per Ryan's locked spec (2026-08-20):
    - multi-select checkboxes (NOT radio),
    - jumpable step bar (Tab/Arrows, not strictly sequential),
    - toggleable answered-checkbox per step (untick clears the question),
    - a "Type something" note field on every question,
    - Submit commits everything; "Chat about this" returns the PARTIAL state;
      Esc cancels with no answers.
"""
import asyncio
import sys
import tempfile
import threading
from pathlib import Path

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _script_guard  # tests/ is sys.path[0] when a file is run as a script

# 🔴 `ask_user_question.py:623` branches on `app.settings.dialog_style`, so this
# file's screen assertions are only meaningful against a KNOWN value. Booting a
# real app reads the repo root's gitignored settings.json, and `conftest.py`'s
# autouse guard does not reach a script-style file. Unguarded, the wait_for at
# :130 timed out on Ryan's box (sidebar) while passing 48/48 at modal — the same
# tree, the same commit. The env pin keeps the sandboxed boot off the first-boot
# engine picker. See tests/_script_guard.py.
_script_guard.pin_first_boot_env()
_script_guard.redirect_live_settings()

# Deliberately a SECOND import block: the env pin and the settings redirect must
# run BETWEEN these imports, not before or after them.
from litetui import app as m
from litetui import ask_user_question as aq
from litetui import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-auq-"))
ok = []
#: The labels that FAILED. `ok` is bools, which is all an exit status needed; a
#: pytest arm has to be able to say WHICH check failed, and a bool cannot.
#: Recorded alongside rather than by changing `ok`, so `sum(ok)` / `all(ok)` /
#: `len(ok)` keep meaning exactly what they meant (T700).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


QUESTIONS = [
    {"label": "Liveness",
     "question": "Which check proves a live inbox watcher?",
     "options": [
         {"title": "Poll loop heartbeat", "description": "mtime advances"},
         {"title": "Roster re-read"},
         {"title": "Both, plus the janitor run"},
     ]},
    {"label": "Spot the bug",
     "question": "Which lines are wrong?",
     "options": [{"title": "line 10"}, {"title": "line 22"}, {"title": "line 31"}]},
    {"label": "Junctions",
     "question": "How to store the junction?",
     "options": [{"title": "One file"}, {"title": "One dir per seat"}]},
]

ARGS = {"questions": QUESTIONS}


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None          # never touch the network in a test
    a._fetch_ctx_window = lambda: None
    return a


async def wait_for(pilot, pred, timeout=5.0):
    import time
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("condition never became true")
        await pilot.pause()


def call_in_thread(app):
    box = []
    t = threading.Thread(target=lambda: box.append(aq.run(ARGS, app)), daemon=True)
    t.start()
    return box, t


async def main():
    print("=== parsing: valid, lenient, and actionable errors ===")
    states = aq._parse_questions(ARGS)
    chk("three questions parsed", len(states) == 3)
    chk("labels carried through", states[0].label == "Liveness")
    chk("options carried through", states[0].options[0]["title"] == "Poll loop heartbeat")
    lenient = aq._parse_questions({"questions": [
        {"question": "Pick one", "options": ["plain string option", "another"]},
    ]})
    chk("string options accepted (local-model leniency)",
        lenient[0].options[0]["title"] == "plain string option")
    chk("missing label defaulted", lenient[0].label == "Question 1")
    e1 = aq.run({"questions": []}, None)
    chk("empty list rejected with a message", e1.startswith("[error]") and "non-empty" in e1)
    e2 = aq.run({"questions": [{"label": "x", "options": [{"title": "a"}]}]}, None)
    chk("missing question text rejected", "question" in e2)
    e3 = aq.run({"questions": [{"label": "x", "question": "q", "options": "nope"}]}, None)
    chk("non-list options rejected", "options" in e3)

    print("\n=== serialization: submit / chat / cancel ===")
    s = aq._serialize({
        "action": "submit",
        "questions": [
            {"label": "Liveness", "question": "q",
             "options": [{"title": "a", "description": ""}, {"title": "b"}],
             "selected": [0, 1], "note": "my note", "answered": True},
            {"label": "Junctions", "question": "q",
             "options": [{"title": "c"}], "selected": [], "note": "", "answered": False},
        ],
    })
    chk("submit header", s.startswith("[ask_user_question] SUBMITTED — 1 of 2 answered"))
    chk("selections marked", "[x] a" in s and "[x] b" in s and "[ ] c" in s)
    chk("note carried", "note: my note" in s)
    c = aq._serialize({
        "action": "chat",
        "questions": [{"label": "Liveness", "question": "q",
                       "options": [{"title": "a"}], "selected": [0], "note": "", "answered": True}],
    })
    chk("chat marked partial", "CHAT ABOUT THIS" in c and "PARTIAL" in c)
    x = aq._serialize({"action": "cancelled", "questions": []})
    chk("cancel says no answers", "CANCELLED" in x and "No answers" in x)

    print("\n=== BRIDGE + WIDGET: run() from a worker thread against a live app ===")
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        chk("spec registered by _all_tools",
            any(t["function"]["name"] == "ask_user_question" for t in a._all_tools()))
        chk("dispatch resolves ask_user_question", a._dispatch_for("ask_user_question") is not None)

        box, t = call_in_thread(a)
        await wait_for(pilot, lambda: isinstance(a.screen, aq.AskUserQuestionScreen))
        # THE STATE LIVES ON THE BODY NOW. T082 split this widget into a modal
        # screen plus a host-agnostic body so a sidebar can mount the same
        # content, and the cursor and per-question state moved with it.
        # Duplicating `_active` onto the screen would have kept this line
        # working and left two counters free to drift — worse than the edit.
        scr = a.screen.query_one(aq.AskUserQuestionBody)
        chk("AskUserQuestionScreen is on top while run() blocks", True)
        chk("three steps in the bar", len(list(scr.query(".auq-step-label"))) == 3)
        chk("active step is first", scr.query_one("#auq-lab-0").has_class("active"))
        rows = list(scr.query_one("#auq-qbody-0").query(".auq-row"))
        chk("q1 body: 3 option rows + note row", len(rows) == 4)

        # multi-select: tick two options on q1 (Enter toggles, does NOT dismiss)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        chk("multi-select: two options ticked", scr._states[0].selected == {0, 1})
        chk("untoggle works (toggle, not radio)", True)  # proven by the next line
        await pilot.press("up", "enter")
        await pilot.pause()
        chk("unticking clears that option", scr._states[0].selected == {1})
        await pilot.press("up", "enter")
        await pilot.pause()
        chk("re-ticking works", scr._states[0].selected == {0, 1})
        chk("step 1 checkbox now answered", scr.query_one("#auq-box-0").has_class("answered"))

        # jumpable: right -> q2, tab -> q3, shift+tab back to q2
        await pilot.press("right")
        await pilot.pause()
        chk("right jumped to q2", scr._active == 1)
        await pilot.press("tab")
        await pilot.pause()
        chk("tab jumped to q3", scr._active == 2)
        await pilot.press("shift+tab")
        await pilot.pause()
        chk("shift+tab back to q2", scr._active == 1)
        await pilot.press("enter")
        await pilot.pause()
        chk("q2 option ticked after jump", scr._states[1].selected == {0})

        # note field on q2: down to the note row (index 3, past 3 options),
        # enter focuses the input, type
        await pilot.press("down", "down", "down", "enter")
        await pilot.pause()
        chk("cursor reached the note row", scr._cursor == 3)
        chk("note input focused", a.focused is not None and a.focused.id == "auq-note-input")
        await pilot.press("k", "e", "y")
        await pilot.pause()
        chk("note stored on the ACTIVE question", scr._states[1].note == "key")
        chk("q2 counts as answered (note alone suffices)", scr._states[1].answered)

        # toggleable step checkbox: untick q2's answer box clears it
        await pilot.click(scr.query_one("#auq-box-1"))
        await pilot.pause()
        chk("step untick cleared q2 selections", scr._states[1].selected == set())
        chk("step untick cleared q2 note", scr._states[1].note == "")
        chk("q2 no longer answered", not scr._states[1].answered)

        # "Chat about this" = partial state early exit
        await pilot.click(scr.query_one("#auq-chat"))
        t.join(timeout=5)
        chk("run() returned after Chat about this", not t.is_alive() and len(box) == 1)
        res = box[0]
        chk("chat result marked partial", "CHAT ABOUT THIS" in res and "PARTIAL" in res)
        chk("q1 selections in the result", "[x] Poll loop heartbeat" in res and "[x] Roster re-read" in res)
        chk("q2 in the result as not answered", "Spot the bug — not answered" in res)
        chk("widget dismissed, app clean", a.screen is not scr and a.screen_stack)

    print("\n=== SUBMIT commits everything ===")
    b = make_app()
    async with b.run_test(size=(120, 30)) as pilot:
        box, t = call_in_thread(b)
        await wait_for(pilot, lambda: isinstance(b.screen, aq.AskUserQuestionScreen))
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")  # to the note row (index 3)
        await pilot.pause()
        await pilot.press("a", "n", "o")             # note on q1
        await pilot.pause()
        scr = b.screen.query_one(aq.AskUserQuestionBody)
        chk("q1 answered before submit", scr._states[0].answered)
        await pilot.click(scr.query_one("#auq-submit"))
        await pilot.pause(0.3)
        chk("Next advances without submitting", scr._active == 1 and t.is_alive() and not box)
        await pilot.click(scr.query_one("#auq-submit"))
        await pilot.pause(0.3)
        chk("Next reaches last question", scr._active == 2)
        await pilot.click(scr.query_one("#auq-submit"))
        await pilot.pause(0.3)
        chk("incomplete Submit returns to first unanswered question",
            scr._active == 1 and t.is_alive() and not box)
        await pilot.press("enter")
        await pilot.click(scr.query_one("#auq-submit"))
        await pilot.pause(0.3)
        scr.query_one("#auq-note-input").value = "Use the default"
        await pilot.pause()
        await pilot.click(scr.query_one("#auq-submit"))
        t.join(timeout=5)
        res = box[0]
        chk("submit result", res.startswith("[ask_user_question] SUBMITTED — 3 of 3 answered"))
        chk("selection + note both returned", "[x] Poll loop heartbeat" in res and "note: ano" in res)
        chk("note-only answer returned", "note: Use the default" in res)

    print("\n=== ESC cancels with no answers ===")
    c = make_app()
    async with c.run_test(size=(120, 30)) as pilot:
        box, t = call_in_thread(c)
        await wait_for(pilot, lambda: isinstance(c.screen, aq.AskUserQuestionScreen))
        scr = c.screen.query_one(aq.AskUserQuestionBody)
        await pilot.press("enter")
        await pilot.pause()
        chk("something was ticked before cancel", scr._states[0].selected == {0})
        await pilot.press("escape")
        t.join(timeout=5)
        res = box[0]
        chk("cancel result", "CANCELLED" in res and "No answers" in res)
        chk("cancel did not record the tick",
            "Poll loop heartbeat" not in res and "do not assume any option" in res)
        chk("widget dismissed", c.screen is not scr)



# ── the same checks, as a pytest arm (T700) ─────────────────────────────
#
# 🔴 THIS FILE IS NAMED `test_*` AND NOTHING HAS EVER RUN IT. A module-level
# exit raises SystemExit during collection, which pytest reports as
# INTERNALERROR and which abandons the WHOLE invocation — not just this file.
# Ten files in this directory were in that state (T699 fixed two, T700 the
# rest); each abort hid the others, which is why the class kept looking small.
#
# The tally and the exit left `main`; `main` itself is unchanged and still does
# every check.


@pytest.mark.asyncio
async def test_every_check_in_this_file_passed() -> None:
    await main()
    assert ok, "no check ran"
    assert failures == [], failures


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
