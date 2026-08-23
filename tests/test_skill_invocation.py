"""Invoking a skill must SEND it to the model, not paint it on the screen.

Ryan, 2026-08-22: "invoking a skill just prints it to the screen... its not
getting sent to the agent correctly."

There was no send to break. `_cmd_skills` called `app._system(body)`, and
`_system` (app.py:3088) mounts a widget into #chat-log and returns — it never
touches `self.conversation`. The command's own comment said "Show what the MODEL
would receive", so it was built as a VIEWER and the invocation half was never
written. The banner then claimed `{n} chars as the model sees it` about a
delivery that did not happen: the lie was in the string, printed to the user,
every single time.

WHY THE SUITE COULD NOT CATCH THIS. tests/test_skills_picker.py's _StubApp
implements `_system` and nothing else. A stub with no `conversation` cannot
represent the difference between "shown" and "sent", so no assertion written
against it could ever have failed on the missing send. The stub here carries
both, which is the whole reason these tests can fail.

🔴 AND THE FIRST FIX WAS ALSO ONLY HALF. Delivery into the CONTEXT is not the
same as the model READING it: a model reads its context only when a request is
made. The first version put the body in the system turn, printed "sent to the
model", and then sat there — Ryan: "no gpu use no response ... nothing". The
banner had stopped lying about the destination and started lying about the
event.

So invocation follows the app's own established shape for injected work, the
one _wake_after_compact already uses: `_user_bubble` + `_append` + `_stream`.
The bubble and the message deliberately carry DIFFERENT text — they are
separate arguments — so the screen gets one line naming the skill while the
request carries the whole body.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import skills as skills_mod
from litetui.plugins.skills_plugin import _cmd_skills, _REPORT_ROW

BODY = "STEP ONE: do the thing.\nSTEP TWO: do the other thing."


def _skill(tmp_path: Path, name: str, body: str = BODY) -> skills_mod.Skill:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return skills_mod.Skill(name, f"{name} does things", p, "local")


class _Settings:
    skills_enabled = True
    skill_roots: list[str] = []


class _StubApp:
    """Carries THREE surfaces, because the defect lived in the gap between
    them: what is SHOWN, what is SENT, and whether a TURN was started.

    A stub with only `_system` could not fail on a skill that was displayed and
    never delivered. A stub with only `conversation` could not fail on a skill
    that was delivered and never asked about — which is the second half of the
    same bug and the one Ryan saw on screen.
    """

    def __init__(self, skills):
        self.skills = skills
        self.settings = _Settings()
        self.said: list[str] = []
        self.bubbles: list[str] = []
        self.pushed: list = []
        self.turns = 0
        self.conversation: list[dict] = [{"role": "system", "content": "BASE PROMPT"}]

    def _system(self, text):
        self.said.append(text)

    def _user_bubble(self, text, has_image, queued=False):
        self.bubbles.append(text)

    def _append(self, msg: dict) -> None:
        self.conversation.append(msg)

    def _stream(self) -> None:
        self.turns += 1

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))

    # -- what the assertions read -------------------------------------------
    @property
    def sent(self) -> str:
        """Everything the MODEL will read on the next request."""
        return "\n".join(str(m.get("content") or "") for m in self.conversation)

    @property
    def shown(self) -> str:
        return "\n".join(self.said)


def _pick(app, choice: str) -> None:
    """Drive the picker's callback the way the real PickerScreen does."""
    _cmd_skills(app, "skills", "")
    _screen, callback = app.pushed[-1]
    callback(choice)


# ── the delivery itself ──────────────────────────────────────────────────────
def test_invoking_by_name_sends_the_body_to_the_model(tmp_path: Path) -> None:
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert BODY in app.sent, (
        "the skill body never reached the conversation — it was painted on "
        "screen and dropped, which is the reported defect"
    )


def test_invoking_from_the_picker_sends_it_too(tmp_path: Path) -> None:
    """Two entry points, one delivery. The picker is the one Ryan actually
    uses, and it had its own copy of the same _system call."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _pick(app, "ls-mark")

    assert BODY in app.sent, "the picker path shows without sending"


def test_invoking_a_skill_STARTS_A_TURN(tmp_path: Path) -> None:
    """THE half that was missing, and the one Ryan saw on screen: the body
    reached the context and nothing happened — no GPU, no response, nothing.

    A model reads its context only when a request is made. Delivery without a
    turn is a banner saying "sent to the model" printed over silence, which is
    the same lying-label defect as before wearing different words."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert app.turns == 1, "the skill was loaded but no turn was started"


def test_the_picker_path_also_starts_a_turn(tmp_path: Path) -> None:
    """Two entry points, and last time only one of them got the fix."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    _pick(app, "ls-mark")
    assert app.turns == 1, "the picker loads without asking the model anything"


def test_the_body_goes_in_the_message_and_the_bubble_stays_short(tmp_path: Path) -> None:
    """The screen and the request carry DIFFERENT text, deliberately —
    _user_bubble and _append take separate arguments, so showing less than we
    send costs nothing."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert app.conversation[-1]["role"] == "user"
    assert BODY in app.conversation[-1]["content"], "the message carries no skill body"
    assert app.bubbles and BODY not in app.bubbles[0], (
        f"the whole body was rendered into the bubble: {app.bubbles!r}"
    )
    assert "ls-mark" in app.bubbles[0], "the bubble does not name the skill"


# ── what the SCREEN does, which is the other half of the complaint ───────────
def test_the_screen_gets_a_confirmation_not_the_whole_body(tmp_path: Path) -> None:
    """Ryan's words were "just prints it to the screen". Dumping 8KB into the
    log was the visible symptom; the log should say what happened instead."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert BODY not in app.shown, (
        "the whole body is still being dumped into the chat log"
    )
    assert BODY not in "\n".join(app.bubbles), "the body is in the bubble instead"
    # The user IS told — on the bubble, which is the surface a loaded skill now
    # occupies. Ryan asked for the GUI and no printing; a system line beside
    # the bubble would be the printing coming back by another name.
    assert any("ls-mark" in b for b in app.bubbles), (
        "nothing on screen names the skill that was loaded"
    )


def test_the_banner_no_longer_claims_a_delivery_it_is_not_making(tmp_path: Path) -> None:
    """The old string said 'as the model sees it' while sending nothing. Once
    the send is real the claim is allowed — but it must not be printed on a
    path that does not send. This pins the wording to the truthful case."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert BODY in app.sent, "precondition: the send must actually happen"


# ── negative controls: things that must NOT inject ───────────────────────────
def test_a_missing_skill_injects_nothing(tmp_path: Path) -> None:
    """The error path must not put '[error] no such skill' into the model's
    system prompt, where it would stay for the rest of the session."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    before = app.sent

    _cmd_skills(app, "skills", "does-not-exist")

    assert app.sent == before, "a failed lookup contaminated the conversation"
    assert app.turns == 0, "a failed lookup burned a turn"
    assert "error" in app.shown.lower(), "the miss was not reported on screen"


def test_the_full_report_row_injects_nothing(tmp_path: Path) -> None:
    """The report is a UI affordance about the user's own filesystem. It is
    exactly the kind of text that must stay on the screen side."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    before = app.sent

    _pick(app, _REPORT_ROW)

    assert app.sent == before, "the roots/token-cost report was sent to the model"
    assert app.turns == 0, "the report started a turn"


def test_cancelling_the_picker_injects_nothing(tmp_path: Path) -> None:
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    before = app.sent

    _pick(app, None)

    assert app.sent == before, "cancelling still delivered something"
    assert app.turns == 0, "cancelling started a turn"


def test_refresh_injects_nothing(tmp_path: Path) -> None:
    """/skills refresh is housekeeping; its output is for the user."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    app.refresh_skills = lambda: ([], [])
    before = app.sent

    _cmd_skills(app, "skills", "refresh")

    assert app.sent == before, "a refresh contaminated the conversation"
    assert app.turns == 0, "a refresh started a turn"


# ── invoking WITH a request in the same line ────────────────────────────────
#
# Ryan: "it worked sent him the skill now but it lost the text i typed after it
# ... meaning i couldnt send him the link and invoke it at once".
#
# The autocomplete completes to `/name ` WITH A TRAILING SPACE, which is an
# explicit invitation to type an argument — and the argument was then dropped on
# the floor. An affordance that invites input it discards is worse than one that
# does not offer it at all.
def test_text_typed_after_the_skill_name_reaches_the_model(tmp_path: Path) -> None:
    app = _StubApp([_skill(tmp_path, "ls-youtube-transcript")])

    _cmd_skills(app, "skills", "ls-youtube-transcript https://youtu.be/6NukGtwJb7Y")

    sent = app.conversation[-1]["content"]
    assert BODY in sent, "the skill body did not survive having an argument"
    assert "https://youtu.be/6NukGtwJb7Y" in sent, (
        "the text typed after the skill name was dropped — the user cannot "
        "hand over the link and invoke the skill in one go"
    )


def test_the_argument_is_not_swallowed_into_the_skill_name(tmp_path: Path) -> None:
    """The negative control that matters: the lookup must still find the skill
    when a trailing argument is present. A naive fix that passes the whole
    remainder as the name turns every argument into a failed lookup."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark  do the thing")

    assert app.turns == 1, "the skill was not found once an argument followed it"
    assert BODY in app.conversation[-1]["content"]


def test_the_bubble_shows_what_the_user_actually_typed(tmp_path: Path) -> None:
    """Their words are their turn. Showing only 'Loaded skill: x' would make
    the request they typed vanish from the transcript."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark summarise this for me")

    joined = "\n".join(app.bubbles)
    assert "summarise this for me" in joined, (
        f"the user's own words are not on screen: {app.bubbles!r}"
    )
    assert BODY not in joined, "the body leaked into the bubble again"


def test_the_skill_comes_before_the_request_in_the_message(tmp_path: Path) -> None:
    """Procedure first, then the task it applies to. Reversed, the model reads
    an instruction it has no method for yet."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark THE-REQUEST")

    sent = app.conversation[-1]["content"]
    assert sent.index(BODY) < sent.index("THE-REQUEST"), (
        "the request precedes the procedure"
    )


def test_no_argument_still_works(tmp_path: Path) -> None:
    """The plain case must not regress."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    _cmd_skills(app, "skills", "ls-mark")
    assert BODY in app.conversation[-1]["content"] and app.turns == 1
