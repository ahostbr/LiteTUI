"""The seat must survive /new and /resume — END TO END, not in halves.

🔴 THE BUG THIS PINS. `_sync_seat_identity()` deregistered the old row, assigned
the new agent id, and set `registered = False`, on the theory — stated in its own
docstring, three lines above the assignment that defeated it — that "the next
tick registers the new id by itself". `Seat.heartbeat()` opens with
`if not self.registered ... return False`, and `register()` is reached from
exactly ONE place (app.py:1835, at startup, via `asyncio.to_thread`). Nothing
re-armed the seat. After a conversation switch the app was absent from
`discover`, its id named no registry row, and the footer read "unregistered".

Probed 2026-08-23 (BoldChip, confirmed by Sentinel in source):

    registered_after_switch: False
    heartbeat_return:        False
    transport_calls:         0

⚠️ WHY THIS FILE IS END-TO-END AND NOT TWO UNIT TESTS. Identity derivation and
heartbeat behaviour were each covered and each PASSED while this shipped —
testing the halves separately is precisely what let it through. The assertions
below are about the state a conversation switch LEAVES BEHIND, which is the only
level at which the defect is visible.

🔴 AND ONE ASSERTION HAD TO BE STRENGTHENED TO BE ABLE TO FAIL AT ALL.
The obvious end-to-end check — "after the switch a send still reaches transport"
— is VACUOUS here: `Seat.send()` has no registration gate, so it shells out
whether or not the seat is registered, and that assertion passes with the bug
fully present. What actually breaks is WHO the send claims to be from: an id
that no `register` call ever announced. So the test asserts the sending id is
one the transport was told about, not merely that a send happened.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import harness as harness_mod

CLI_VERB = 3  # [python, "-m", "liteharness.cli", <verb>, ...]


class _Transport:
    """Stands in for every subprocess `Seat` shells out to.

    Faked at `ttyguard.run`, the single choke point both register() and
    deregister() go through, so no test can reach the LIVE fleet registry —
    the suite has evicted Ryan's running app from the roster before (see
    conftest) and this file must not be the fourth instance.
    """

    def __init__(self, register_rc: int = 0):
        self.calls: list[list[str]] = []
        self.register_rc = register_rc

    def run(self, cmd, *, timeout=None, **kw):
        self.calls.append([str(c) for c in cmd])
        rc = self.register_rc if self._verb(cmd) == "register" else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="refused")

    @staticmethod
    def _verb(cmd) -> str:
        return str(cmd[CLI_VERB]) if len(cmd) > CLI_VERB else ""

    def _ids_for(self, verb: str) -> list[str]:
        out = []
        for c in self.calls:
            if self._verb(c) == verb and "--agent-id" in c:
                out.append(c[c.index("--agent-id") + 1])
        return out

    @property
    def registered_ids(self) -> list[str]:
        return self._ids_for("register")

    @property
    def deregistered_ids(self) -> list[str]:
        return self._ids_for("deregister")

    @property
    def sent_from_ids(self) -> list[str]:
        return [
            c[c.index("--from") + 1]
            for c in self.calls
            if self._verb(c) == "send" and "--from" in c
        ]


@pytest.fixture
def transport(monkeypatch):
    """A live-looking harness with every subprocess intercepted."""
    t = _Transport()
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    monkeypatch.setattr(harness_mod.ttyguard, "run", t.run)
    return t


def _seat(agent_id: str = "seat-id-at-startup") -> harness_mod.Seat:
    return harness_mod.Seat(agent_id=agent_id, name="LiteTUI", model="test-model")


def _app(seat, convo_id: str):
    """A LiteTUI with no Textual mount — same construction the sibling
    identity tests use. `_system` is captured rather than stubbed away: a
    rebind failure MUST be reported, and a test that discarded the report
    could not tell a loud failure from a silent one."""
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.seat = seat
    a.convo_id = convo_id
    a.said: list[str] = []
    a._system = a.said.append
    return a


def _switch(app, new_convo_id: str) -> None:
    """The one line both /new and /resume perform before syncing: point at a
    different conversation, then let the seam react."""
    app.convo_id = new_convo_id
    app._sync_seat_identity()


# ── the transition ──────────────────────────────────────────────────────────

def test_a_new_conversation_leaves_the_seat_REGISTERED_under_the_new_id(transport):
    """/new. The whole defect in one assertion: registered, and registered as
    the id the new conversation derives."""
    seat = _seat()
    seat.register()
    assert seat.registered, "precondition: the seat starts armed"

    app = _app(seat, "convo-alpha")
    _switch(app, "convo-bravo")

    want = harness_mod.agent_id_for_convo("convo-bravo")
    assert seat.agent_id == want
    assert seat.registered is True, "the seat went dark after /new"
    assert want in transport.registered_ids, (
        "nothing ever announced the new id — this is the shipped bug"
    )


def test_a_resumed_conversation_leaves_the_seat_REGISTERED_under_the_new_id(transport):
    """/resume. Same seam, and the case that matters most: a resumed
    conversation is the one whose id was already minted elsewhere."""
    seat = _seat()
    seat.register()
    app = _app(seat, "convo-alpha")

    _switch(app, "convo-resumed-from-disk")

    want = harness_mod.agent_id_for_convo("convo-resumed-from-disk")
    assert seat.agent_id == want
    assert seat.registered is True
    assert want in transport.registered_ids


def test_the_OLD_row_is_retired_exactly_once_and_before_the_swap(transport):
    """The stale row carries THIS process's pid, so every liveness check reads
    it as alive and keeps offering it as a delivery target. It has to go, and
    it has to go while `agent_id` still names it."""
    seat = _seat()
    seat.register()
    old_id = seat.agent_id

    app = _app(seat, "convo-alpha")
    _switch(app, "convo-bravo")

    assert transport.deregistered_ids == [old_id], (
        f"expected the old row retired once, got {transport.deregistered_ids}"
    )


def test_a_send_after_the_switch_claims_an_id_THE_FLEET_WAS_TOLD_ABOUT(transport):
    """The strengthened assertion (see module docstring).

    `send()` has no registration gate, so "a send reached transport" is true
    with the bug present and proves nothing. What the bug actually produces is
    a send whose `--from` names an id that appears in no register call.
    """
    seat = _seat()
    seat.register()
    app = _app(seat, "convo-alpha")
    _switch(app, "convo-bravo")

    assert seat.send("someone-else", "body") is True
    assert transport.sent_from_ids, "no send reached transport at all"
    from_id = transport.sent_from_ids[-1]
    assert from_id in transport.registered_ids, (
        f"sent as {from_id[:8]}, an id the fleet was never told about"
    )


def test_the_heartbeat_that_was_SUPPOSED_to_re_arm_it_now_actually_beats(transport):
    """The false docstring claimed the next tick would register the new id.
    It cannot: heartbeat() returns early unless `registered`. With the seat
    genuinely re-armed the beat now reaches transport — which is the state the
    old comment described and never produced."""
    seat = _seat()
    seat.register()
    app = _app(seat, "convo-alpha")
    _switch(app, "convo-bravo")

    before = len(transport.calls)
    assert seat.heartbeat() is True
    assert len(transport.calls) > before, "the beat never reached transport"


# ── honest failure ──────────────────────────────────────────────────────────

def test_a_FAILED_rebind_is_reported_and_never_claims_registration(monkeypatch):
    """Silent failure is what hid the original bug, so a rebind that cannot
    re-register must say so and must not leave a green flag behind."""
    t = _Transport(register_rc=1)
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    monkeypatch.setattr(harness_mod.ttyguard, "run", t.run)

    seat = _seat()
    seat.registered = True  # armed before the transport starts refusing
    app = _app(seat, "convo-alpha")

    _switch(app, "convo-bravo")

    assert seat.registered is False, "a refused registration must not read as armed"
    assert seat.error, "the reason was thrown away"
    assert app.said, "the failure was never surfaced to the transcript"
    assert "rebind" in app.said[-1].lower()


def test_CONTROL_an_unregistered_seat_adopts_the_id_WITHOUT_registering(transport):
    """Boot calls this seam before the startup registration runs — the model id
    is not known yet, so registering here would claim the row as model
    'unknown'. Adopting the id and staying quiet is correct, not a failure."""
    seat = _seat()
    assert seat.registered is False

    app = _app(seat, "convo-alpha")
    _switch(app, "convo-bravo")

    assert seat.agent_id == harness_mod.agent_id_for_convo("convo-bravo")
    assert seat.registered is False
    assert transport.registered_ids == [], "boot must not register early"
    assert app.said == [], "nothing failed, so nothing should be reported"


def test_CONTROL_an_unchanged_conversation_touches_the_transport_not_at_all(transport):
    """Re-syncing the same conversation must not churn the registry — a
    deregister/register pair per no-op switch would flap the roster."""
    seat = _seat()
    seat.register()
    app = _app(seat, "convo-alpha")
    baseline = len(transport.calls)

    app.convo_id = "convo-alpha"
    seat.agent_id = harness_mod.agent_id_for_convo("convo-alpha")
    app._sync_seat_identity()

    assert len(transport.calls) == baseline


# ── wiring: the repair must be REACHED ──────────────────────────────────────

def test_both_convo_switch_paths_still_call_the_seam():
    """An unwired repair is the defect, not the fix. Both places convo_id
    changes must reach the sync — asserted on source, because the alternative
    is mounting the whole app."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    for fn in ("_new_convo", "_resume"):
        body = src.split(f"def {fn}", 1)[1].split("\n    def ", 1)[0]
        assert "_sync_seat_identity()" in body, f"{fn} no longer syncs the seat"


def test_the_seam_uses_the_single_rebind_transition():
    """The two-step (deregister + `registered = False`) is what produced a seat
    that existed nowhere. It must not come back.

    🔴 GATE THE INSTRUCTION, NEVER THE STRING. The first version of this test
    asserted the literal text `registered = False` was absent from the function
    — and it FAILED the moment the fix landed, because the new docstring QUOTES
    that assignment in order to explain what was wrong with it. A string grep
    cannot tell an instruction from its own retraction. So this walks the AST
    and looks for a real assignment to `seat.registered`, which is the thing
    that must not return.
    """
    import ast

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_sync_seat_identity"
    )

    calls = {
        n.func.attr for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "rebind" in calls, "the seam stopped calling Seat.rebind"

    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            assert not (isinstance(tgt, ast.Attribute) and tgt.attr == "registered"), (
                "the silent-deregistration two-step is back: the seam assigns "
                "seat.registered directly instead of letting rebind() own it"
            )
