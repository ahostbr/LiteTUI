"""A resumed conversation must not tell the model it is a dead seat.

REPORTED FROM INSIDE THE APP by the LiteTUI agent (ed8ee93e), measured:

    live seat (whoami in-process): ed8ee93e-...
    system prompt says:            c5ea9cf2-...   <- no registry row

WHY. The fleet-identity sentence is written into conversation[0] after
registration, and the agent id is minted PER PROCESS. `_resume` deliberately
does not rebuild the system message — rebuilding would discard the agent's own
/system edits — so it replays the sentence the PREVIOUS process wrote.

🔴 NOT COSMETIC. That same prompt instructs the model to answer mail with the
`harness` tool. Believing a dead id means replies addressed from a mailbox
nothing can deliver to. Same family as the pi extension aiming at a dead
sibling's mailbox.

⚠️ AND THE REPORTER'S OWN SEARCH SAID THE ID WAS ABSENT. `findstr` over
convo.jsonl returned 0 hits for c5ea9cf2; a byte scan found 90, across 18 `msg`
records. findstr silently skips lines past its length limit and that file is
5.7 MB with megabyte-long lines. The id WAS persisted — the instrument could not
see it, and "absent" is the answer that stops you looking.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import app as app_mod
import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-ident-"))

OLD = "c5ea9cf2-641e-4a17-b080-0efae35ac889"
NEW = "ed8ee93e-e749-4841-8b19-0cdfe0eb0ac1"

PROMPT = (
    "You are a helpful assistant.\n\n"
    f"You are registered in the LiteHarness fleet as LiteTUI (id {OLD}, tier worker). "
    "Other agents can message you and their mail arrives as a user turn.\n\n"
    "RYAN'S OWN EDIT: never touch the F drive."
)


class _Seat:
    def __init__(self, agent_id=NEW, name="LiteTUI", tier="worker"):
        self.agent_id, self.name, self.tier = agent_id, name, tier


def _app(prompt=PROMPT, seat=None):
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)  # no Textual mount needed
    a.conversation = [{"role": "system", "content": prompt}]
    a.seat = seat or _Seat()
    return a


def test_a_resumed_prompt_adopts_the_LIVE_seat_id():
    a = _app()
    assert a._sync_fleet_identity() is True
    body = a.conversation[0]["content"]
    assert NEW in body
    assert OLD not in body, "the dead seat id survived into the running prompt"


def test_it_rewrites_rather_than_appending_a_second_line():
    # Two identity sentences is worse than one wrong one: the model has no way
    # to tell which is current.
    a = _app()
    a._sync_fleet_identity()
    body = a.conversation[0]["content"]
    assert len(re.findall(r"You are registered in the LiteHarness fleet", body)) == 1


def test_it_preserves_everything_else_in_the_prompt():
    """The no-rebuild rule in _resume exists to protect the agent's own /system
    edits. This fix must not become the rebuild that rule forbids."""
    a = _app()
    a._sync_fleet_identity()
    body = a.conversation[0]["content"]
    assert "You are a helpful assistant." in body
    assert "RYAN'S OWN EDIT: never touch the F drive." in body


def test_CONTROL_an_already_correct_prompt_is_left_alone():
    # Without this, "make it match" is satisfied by rewriting on every call,
    # which would mark the conversation dirty and rewrite the store forever.
    a = _app(prompt=PROMPT.replace(OLD, NEW))
    assert a._sync_fleet_identity() is False


def test_CONTROL_a_prompt_with_no_fleet_line_is_not_invented():
    # Registration appends the line. If this synthesised one, an unregistered
    # seat would advertise a mailbox that does not exist — the same defect
    # pointed the other way.
    a = _app(prompt="You are a helpful assistant.")
    assert a._sync_fleet_identity() is False
    assert "LiteHarness fleet" not in a.conversation[0]["content"]


def test_it_survives_a_different_name_and_tier():
    # The registry can GRANT a different name than the one requested, so the
    # sentence must be matched by shape, not by the old name.
    a = _app(seat=_Seat(agent_id=NEW, name="CyanWedge", tier="leader"))
    assert a._sync_fleet_identity() is True
    body = a.conversation[0]["content"]
    assert f"CyanWedge (id {NEW}, tier leader)" in body
    assert OLD not in body


def test_no_system_message_is_not_a_crash():
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.conversation = []
    a.seat = _Seat()
    assert a._sync_fleet_identity() is False


def test_the_resume_path_actually_CALLS_the_sync():
    """The seven tests above prove the FUNCTION is right. They say nothing about
    whether anything invokes it — and an unwired repair is the defect, not the
    fix. `_resume` is the path that replays a system message written by another
    process, so it is the one that must call this.
    """
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    resume = src.split("def _resume", 1)[1].split("\n    def ", 1)[0]
    assert "_sync_fleet_identity()" in resume, (
        "resume replays the previous process's fleet id and never corrects it"
    )


def test_registration_replaces_rather_than_appending_a_second_line():
    """The registration path appends the identity sentence. On a RESUMED
    conversation one is already present, so appending would leave two — and the
    model cannot tell which names the live seat."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    # 2000, was 1200: the radius is a CACHE of "how far into this branch
    # the call sits", and an unrelated comment inserted above the call
    # overflowed it while the behaviour stood. The number is not the
    # contract; the call being in the registration branch is.
    reg = src.split("harness seat online", 1)[1][:2000]
    assert "_sync_fleet_identity()" in reg, (
        "registration can still append a second identity line onto a resumed prompt"
    )
