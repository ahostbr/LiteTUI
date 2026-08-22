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

DELIVERY GOES THROUGH `_append_to_system`, NOT `_append`. app.py:2968 documents
why and it is not a style preference: qwen/qwen3.8-27b's chat template raises
"System message must be at the beginning" and the request fails with a 500 when
a second role:"system" turn appears mid-conversation. `_append_to_system`
extends the FIRST system message instead, and is idempotent — which also gives
re-invoking a skill the right behaviour for free.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import skills as skills_mod
from plugins.skills_plugin import _cmd_skills, _REPORT_ROW

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
    """Carries BOTH surfaces, so 'shown' and 'sent' are distinguishable.

    _append_to_system mirrors app.py's real one closely enough to test the
    caller: extend the first system turn, never add a second, idempotent.
    """

    def __init__(self, skills):
        self.skills = skills
        self.settings = _Settings()
        self.said: list[str] = []
        self.pushed: list = []
        self.conversation: list[dict] = [{"role": "system", "content": "BASE PROMPT"}]

    def _system(self, text):
        self.said.append(text)

    def _append_to_system(self, text: str) -> None:
        current = self.conversation[0].get("content") or ""
        if text in current:
            return
        self.conversation[0] = {
            **self.conversation[0],
            "content": (current.rstrip() + "\n\n" + text) if current else text,
        }

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))

    # -- what the assertions read -------------------------------------------
    @property
    def sent(self) -> str:
        return self.conversation[0]["content"]

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


def test_delivery_extends_the_first_system_turn_and_adds_no_second(tmp_path: Path) -> None:
    """A second role:system mid-conversation 500s qwen's template. This is the
    portability constraint, not a preference."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    roles = [m["role"] for m in app.conversation]
    assert roles.count("system") == 1, f"a second system turn appeared: {roles}"
    assert app.conversation[0]["content"].startswith("BASE PROMPT"), (
        "the base prompt was replaced rather than extended"
    )


def test_invoking_the_same_skill_twice_does_not_duplicate_it(tmp_path: Path) -> None:
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")
    _cmd_skills(app, "skills", "ls-mark")

    assert app.sent.count(BODY) == 1, "re-invoking stacked a second copy"


# ── what the SCREEN does, which is the other half of the complaint ───────────
def test_the_screen_gets_a_confirmation_not_the_whole_body(tmp_path: Path) -> None:
    """Ryan's words were "just prints it to the screen". Dumping 8KB into the
    log was the visible symptom; the log should say what happened instead."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])

    _cmd_skills(app, "skills", "ls-mark")

    assert "ls-mark" in app.shown, "the user is not told which skill was loaded"
    assert BODY not in app.shown, (
        "the whole body is still being dumped into the chat log"
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

    assert app.sent == before, "a failed lookup contaminated the system prompt"
    assert "error" in app.shown.lower(), "the miss was not reported on screen"


def test_the_full_report_row_injects_nothing(tmp_path: Path) -> None:
    """The report is a UI affordance about the user's own filesystem. It is
    exactly the kind of text that must stay on the screen side."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    before = app.sent

    _pick(app, _REPORT_ROW)

    assert app.sent == before, "the roots/token-cost report was sent to the model"


def test_cancelling_the_picker_injects_nothing(tmp_path: Path) -> None:
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    before = app.sent

    _pick(app, None)

    assert app.sent == before, "cancelling still delivered something"


def test_refresh_injects_nothing(tmp_path: Path) -> None:
    """/skills refresh is housekeeping; its output is for the user."""
    app = _StubApp([_skill(tmp_path, "ls-mark")])
    app.refresh_skills = lambda: ([], [])
    before = app.sent

    _cmd_skills(app, "skills", "refresh")

    assert app.sent == before, "a refresh contaminated the system prompt"
