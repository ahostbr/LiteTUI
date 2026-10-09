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

import asyncio
import json
import re
import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-ident-"))

OLD = "c5ea9cf2-641e-4a17-b080-0efae35ac889"
NEW = "ed8ee93e-e749-4841-8b19-0cdfe0eb0ac1"

PROMPT = (
    "You are a helpful assistant.\n\n"
    f"You are registered in the LiteHarness fleet as LiteTUI (id {OLD}, tier worker). "
    "Other agents can message you and their mail arrives as a user turn.\n\n"
    "OWNER'S OWN EDIT: never touch the F drive."
)


class _Seat:
    def __init__(self, agent_id=NEW, name="LiteTUI", tier="worker"):
        self.agent_id, self.name, self.tier = agent_id, name, tier

    def registry_name(self):
        return None


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
    assert "OWNER'S OWN EDIT: never touch the F drive." in body


def test_CONTROL_an_already_correct_prompt_is_left_alone():
    # Without this, "make it match" is satisfied by rewriting on every call,
    # which would mark the conversation dirty and rewrite the store forever.
    a = _app()
    a.conversation[0]["content"] = a._fleet_identity_sentence() + "Keep my own edit."
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
    assert "CyanWedge" in body
    assert NEW in body and "tier leader" in body
    assert OLD not in body


def test_no_system_message_is_not_a_crash():
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.conversation = []
    a.seat = _Seat()
    assert a._sync_fleet_identity() is False


def test_the_resume_path_corrects_the_previous_process_identity(tmp_path, monkeypatch):
    """Exercise the resume contract rather than parsing its source.

    `_resume_cli_conversation` now precedes `_resume`; splitting on the text
    ``def _resume`` therefore inspected the helper instead of the resume method
    and reported this already-wired behavior as missing.
    """
    monkeypatch.setenv("LITEHARNESS_HOME", str(tmp_path / "harness-home"))
    folder = tmp_path / "saved"
    folder.mkdir()
    path = folder / "convo.jsonl"
    path.write_text(
        '{"type":"meta","v":3,"id":"saved"}\n'
        + json.dumps({
            "type": "snapshot",
            "messages": [{"role": "system", "content": PROMPT}],
        })
        + "\n",
        encoding="utf-8",
    )
    app = app_mod.LiteTUI()
    app.seat = _Seat()
    app._render_resumed = lambda path: None

    assert app._resume(path, startup=True)
    body = app.conversation[0]["content"]
    assert NEW in body
    assert OLD not in body
    edits = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if json.loads(line).get("type") == "edit" and json.loads(line).get("index") == 0]
    assert edits, "resume rewrote only memory; on-disk index 0 still names the dead seat"
    saved = edits[-1]["message"]["content"]
    assert saved.count("You are registered in the LiteHarness fleet") == 1
    assert NEW in saved and OLD not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [PROMPT, PROMPT.replace(OLD, NEW),
                                    "You are a helpful assistant."])
async def test_registered_monitor_keeps_polling_and_heartbeat_after_identity_sync(
        prompt, monkeypatch):
    """Changed, already-correct, and absent sentences all reach the poll loop."""
    from litetui import harness

    monkeypatch.setattr(app_mod, "_INBOX_SETTLE_S", 0)
    monkeypatch.setattr(harness, "POLL_SECONDS", 0)
    monkeypatch.setattr(harness, "HEARTBEAT_EVERY", 1)
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    seen = []

    class FakeSeat:
        name, agent_id, tier, registered = "LiteTUI", NEW, "worker", False
        error = None
        model = None
        thinking_level = None

        def register(self):
            self.registered = True
            return True

        def poll(self):
            seen.append("poll")
            return []

        def refresh_name(self):
            return False

        def heartbeat(self):
            seen.append("heartbeat")
            return True

    app.seat = FakeSeat()
    app._resumed_seat_name = None
    app._update_header = lambda: None
    app._system = lambda text: None
    app._append_to_system = lambda text: app.conversation.__setitem__(
        0, {**app.conversation[0], "content": app.conversation[0]["content"] + text})
    app.conversation = [{"role": "system", "content": prompt}]

    task = asyncio.create_task(app_mod.LiteTUI._inbox_monitor.__wrapped__(app))
    try:
        await asyncio.wait_for(_wait_for_heartbeat(seen), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    body = app.conversation[0]["content"]
    assert seen[:2] == ["poll", "heartbeat"]
    assert body.count("You are registered in the LiteHarness fleet") == 1
    assert app._fleet_identity_sentence() in body
    assert OLD not in body


async def _wait_for_heartbeat(seen):
    while "heartbeat" not in seen:
        await asyncio.sleep(0.01)


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


def test_fleet_sentence_labels_the_registered_id_as_inbox_sender():
    sentence = _app()._fleet_identity_sentence()
    assert NEW in sentence
    assert "your inbox/sender id (use it for any from=/--from)" in sentence


def test_harness_guidance_distinguishes_automatic_and_external_senders():
    guidance = app_mod.load_prompt("harness-capabilities")
    assert "sender is set automatically" in guidance
    assert "external" in guidance
    assert "registered agent id" in guidance
    assert "conversation id" in guidance


def test_resume_migrates_legacy_label_even_when_agent_id_is_current():
    a = _app(prompt=PROMPT.replace(OLD, NEW))
    assert a._sync_fleet_identity() is True
    assert a._fleet_identity_sentence() in a.conversation[0]["content"]
    assert "your inbox/sender id" in a.conversation[0]["content"]


def test_resume_updates_new_sender_label_without_leaving_the_old_id():
    a = _app(seat=_Seat(agent_id=OLD))
    a.conversation[0]["content"] = a._fleet_identity_sentence() + "Keep my own edit."
    a.seat = _Seat()
    assert a._sync_fleet_identity() is True
    assert OLD not in a.conversation[0]["content"]
    assert a._fleet_identity_sentence() in a.conversation[0]["content"]
    assert a.conversation[0]["content"].endswith("Keep my own edit.")
