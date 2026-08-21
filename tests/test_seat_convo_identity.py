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

from harness import agent_id_for_convo, new_agent_id

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

from app import LiteTUI

sync = LiteTUI._sync_seat_identity


class FakeSeat:
    def __init__(self, agent_id, registered=False):
        self.agent_id = agent_id
        self.registered = registered
        self.deregistered = 0

    def deregister(self):
        self.deregistered += 1


def app_with(seat, convo_id):
    return SimpleNamespace(seat=seat, convo_id=convo_id)


def test_startup_adopts_the_derived_id_without_deregistering():
    """At boot the seat holds a random id and is not yet registered — there is
    nothing to retire, so nothing may be retired."""
    seat = FakeSeat(new_agent_id(), registered=False)
    sync(app_with(seat, CONVO))
    assert seat.agent_id == agent_id_for_convo(CONVO)
    assert seat.deregistered == 0


def test_switching_conversation_retires_the_stale_row():
    """THE GHOST FIX. The stale row carries this process's pid, so every
    liveness check would read it as alive and keep offering it as a delivery
    target. It has to be retired explicitly."""
    seat = FakeSeat(agent_id_for_convo(CONVO), registered=True)
    other = CONVO[:-1] + "2"
    sync(app_with(seat, other))
    assert seat.deregistered == 1
    assert seat.agent_id == agent_id_for_convo(other)
    assert seat.registered is False   # the next heartbeat re-registers it


def test_resuming_the_same_conversation_is_a_no_op():
    """NEGATIVE ARM. Without this, a version that deregistered unconditionally
    would pass the test above and churn the roster on every resume — throwing
    away the identity this whole change exists to keep."""
    seat = FakeSeat(agent_id_for_convo(CONVO), registered=True)
    sync(app_with(seat, CONVO))
    assert seat.deregistered == 0
    assert seat.registered is True
    assert seat.agent_id == agent_id_for_convo(CONVO)


def test_a_failing_deregister_never_blocks_the_resume():
    """A roster that keeps a stale row beats a resume that dies."""
    seat = FakeSeat(agent_id_for_convo(CONVO), registered=True)
    seat.deregister = lambda: (_ for _ in ()).throw(OSError("liteharness gone"))
    sync(app_with(seat, CONVO[:-1] + "3"))
    assert seat.agent_id == agent_id_for_convo(CONVO[:-1] + "3")


def test_no_convo_id_yet_leaves_the_seat_alone():
    seat = FakeSeat("keep-me", registered=True)
    sync(app_with(seat, ""))
    assert seat.agent_id == "keep-me"
    assert seat.deregistered == 0
