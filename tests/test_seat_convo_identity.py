"""The seat id must be a function of the CONVERSATION, not of the process.

The bug: the seat registered with a fresh uuid4 every launch, so resuming one
conversation joined the fleet as a stranger each time and left the old id
behind, still heartbeating. One conversation produced three ids in an evening
(ed8ee93e -> 8113984f -> e8a69016) with two ghosts on the roster pointing at
nothing. A dispatch sent to the id you last saw lands in a dead mailbox — and
`liteharness.cli send` exits 0, so the misdelivery is silent. That happened:
a task dispatched to the previous id was never seen by the seat.
"""
import uuid

from litetui import harness as harness_mod
from litetui.harness import process_agent_id, agent_id_for_convo, new_agent_id

CONVO = "5f8e1a90-2c3b-4d7e-9a10-0badc0ffee11"


def test_same_conversation_gives_the_same_seat_id():
    """THE POINT OF THE FEATURE. Resume must not mint a new identity."""
    assert agent_id_for_convo(CONVO) == agent_id_for_convo(CONVO)


def test_different_conversations_give_different_seat_ids():
    """NEGATIVE ARM. Without it, a function returning one constant would
    satisfy the test above — and every seat in the fleet would collide."""
    assert agent_id_for_convo(CONVO) != agent_id_for_convo(CONVO[:-1] + "2")


def test_the_seat_id_is_not_the_conversation_id():
    """They are different namespaces. Returning the convo id verbatim would
    pass both tests above while making the roster and the convo store collide
    on one identifier."""
    assert agent_id_for_convo(CONVO) != CONVO


def test_the_seat_id_is_a_valid_uuid():
    """liteharness takes it as --agent-id; a non-uuid would surface far away
    from here, if at all."""
    uuid.UUID(agent_id_for_convo(CONVO))  # raises if malformed


def test_no_conversation_falls_back_to_a_random_id():
    """Before a conversation exists there is nothing to derive from. This must
    stay RANDOM: resolving to a shared constant would make every convo-less
    seat in the fleet claim one id."""
    a, b = agent_id_for_convo(""), agent_id_for_convo("   ")
    assert a != b
    uuid.UUID(a)
    uuid.UUID(b)


def test_whitespace_around_a_convo_id_is_the_same_conversation():
    assert agent_id_for_convo(f"  {CONVO}  ") == agent_id_for_convo(CONVO)


def test_new_agent_id_is_still_random():
    """The old entry point is untouched — this change adds a path, it does not
    redefine the existing one."""
    assert new_agent_id() != new_agent_id()


# --- _sync_seat_identity: the wiring, not just the derivation ---------------
from types import SimpleNamespace

from litetui.app import LiteTUI

sync = LiteTUI._sync_seat_identity


class FakeSeat:
    """The REAL transition over a fake transport.

    🔴 `rebind` IS BOUND FROM THE REAL Seat ON PURPOSE. A hand-written
    stand-in lets this file agree with a broken seam, and that is not
    hypothetical — the switching test below used to assert `registered is
    False` and cite "the next heartbeat re-registers it", a heartbeat that
    could never fire. The fake defended the defect for the life of the bug.
    Only the two subprocess calls are faked; the logic under test is shipped
    code.
    """

    rebind = harness_mod.Seat.rebind

    def __init__(self, agent_id, registered=False, register_ok=True):
        self.agent_id = agent_id
        self.registered = registered
        self.register_ok = register_ok
        self.error = None
        self.deregistered = 0
        self.registrations = 0

    def deregister(self):
        self.deregistered += 1

    def register(self):
        self.registrations += 1
        self.registered = self.register_ok
        if not self.registered:
            self.error = "refused"
        return self.registered


def app_with(seat, convo_id):
    """`_system` is captured, not discarded: the seam reports a failed rebind
    through it, and a fake that swallowed the report could not tell a loud
    failure from the silent one this whole change exists to remove."""
    said: list[str] = []
    return SimpleNamespace(seat=seat, convo_id=convo_id, _system=said.append, said=said)


# 🔴 THREE ARMS STOOD HERE AND THE CONTRACT THEY TESTED IS GONE (T579).
# They were: _startup_adopts_the_derived_id_without_deregistering,
# _switching_conversation_retires_the_stale_row, and
# _a_failing_deregister_never_blocks_the_resume. All three drove
# `_sync_seat_identity` and asserted that a conversation change REBOUND the
# seat to agent_id_for_convo(convo) and retired the old row.
#
# f64442b (T507-T5, 2026-09-08) reversed that deliberately: the id is
# `process_agent_id()` = uuid5(hostname:pid), stable for the life of the
# process, and `_sync_seat_identity` is now a no-op. Rebinding per
# conversation is what MADE the ghosts those arms existed to retire —
# measured in that commit: "LiteTUI/BurntPath/BrightDuct = 3 ghosts of pid
# 133252". With no rebinding there is no stale row, so there is nothing to
# retire and no failing retirement to survive.
#
# ⚠️ THEY ARE NOT REPLACED ONE FOR ONE, because three ways of checking that
# a no-op does nothing is not three times the coverage. The single arm below
# pins the case that used to rebind — a DIFFERENT conversation — and
# `test_resuming_the_same_conversation_is_a_no_op` already pins the same one.
# The file keeps its shape as the place this contract is asserted, which is
# why this is a note and not a deletion: the next person to wonder why the
# seat id survives /new should find the answer here rather than an absence.


def test_a_DIFFERENT_conversation_does_not_rebind_or_retire_anything():
    """The case that used to rebind, now pinned as a no-op (T507-T5).

    This is the arm that goes red if anyone reintroduces per-conversation
    identity — which would bring the ghost rows back with it.
    """
    seat = FakeSeat(process_agent_id(), registered=True)
    before = seat.agent_id
    sync(app_with(seat, CONVO[:-1] + "4"))
    assert seat.agent_id == before, "a conversation change rebound the seat id"
    assert seat.deregistered == 0, "a conversation change retired a row"
    assert seat.registrations == 0, "a conversation change announced a new id"
    assert seat.registered is True


def test_resuming_the_same_conversation_is_a_no_op():
    """NEGATIVE ARM. Without this, a version that deregistered unconditionally
    would pass the test above and churn the roster on every resume — throwing
    away the identity this whole change exists to keep."""
    seat = FakeSeat(agent_id_for_convo(CONVO), registered=True)
    sync(app_with(seat, CONVO))
    assert seat.deregistered == 0
    assert seat.registered is True
    assert seat.agent_id == agent_id_for_convo(CONVO)


def test_no_convo_id_yet_leaves_the_seat_alone():
    seat = FakeSeat("keep-me", registered=True)
    sync(app_with(seat, ""))
    assert seat.agent_id == "keep-me"
    assert seat.deregistered == 0
