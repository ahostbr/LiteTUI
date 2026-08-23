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
from litetui.harness import agent_id_for_convo, new_agent_id

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

    # 🔴 THIS ASSERTION USED TO READ `is False`, with the inline comment "the
    # next heartbeat re-registers it". Both were wrong, and together they made
    # this test the DEFENDER of the defect: heartbeat() returns immediately
    # unless `registered`, so the seat stayed dark after every /new and
    # /resume, and any correct fix would have failed here and looked like the
    # regression. Retiring the old row is only half a transition — the seat has
    # to come back under the new id, in the same breath.
    assert seat.registered is True
    assert seat.registrations == 1, "the new id was never announced"


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
    """A roster that keeps a stale row beats a resume that dies.

    Strengthened with the rebind: surviving is no longer enough. If a failed
    retirement aborted the transition it would trade a ghost row for a seat
    that never comes back — the strictly worse failure — so the seat must
    still end up armed under the new id.
    """
    seat = FakeSeat(agent_id_for_convo(CONVO), registered=True)
    seat.deregister = lambda: (_ for _ in ()).throw(OSError("liteharness gone"))
    app = app_with(seat, CONVO[:-1] + "3")
    sync(app)
    assert seat.agent_id == agent_id_for_convo(CONVO[:-1] + "3")
    assert seat.registered is True, "a failed retirement must not leave the seat dark"
    assert app.said == [], "nothing failed for the user — do not report one"


def test_no_convo_id_yet_leaves_the_seat_alone():
    seat = FakeSeat("keep-me", registered=True)
    sync(app_with(seat, ""))
    assert seat.agent_id == "keep-me"
    assert seat.deregistered == 0
