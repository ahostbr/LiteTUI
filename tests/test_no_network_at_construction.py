"""Constructing a LiteTUI must not open a socket. T237(C).

TWO REASONS, and the second one is the one that will still matter after the
first is forgotten.

🔴 REASON ONE — IT WAS 4.0 SECONDS, IN EVERY TEST THAT BUILT AN APP.
Measured 2026-09-03 with tests/_probe_floor.py, 12 reps per arm:

    noop (pytest overhead only)               ~0.0 ms
    LiteTUI() constructed, never started    4316.7 ms
    full `async with app.run_test()`        4364.2 ms

`run_test()` — the compositor boot and teardown everyone assumed was the cost —
is 47.5 ms of that, one percent. cProfile put 4.038 s of the constructor in
`mcp_client.load -> connect -> start -> _post -> urllib.urlopen`: a real HTTP
POST to `http://localhost:7423/mcp`, declared in `.mcp.json` at the repo root.
LiteSuite is normally down while the suite runs, so each app waited to be
REFUSED — timed directly three times at 4.033 s / 4.051 s / 4.105 s,
"[WinError 10061] the target machine actively refused it".

🔴 REASON TWO — THE SUITE'S ANSWER DEPENDED ON WHETHER ANOTHER APP WAS RUNNING.
With 7423 down the manager connects nothing and `tool_specs()` carries no MCP
tools. With LiteSuite UP the same constructor reaches a real server and the tool
set is DIFFERENT — and test_tools_registered.py and test_tool_schemas.py assert
on that set. Every green recorded on 2026-09-03 was taken with the bridge down.
A green that is a property of which processes happen to be running is not a
result; the speed was merely how we noticed.

⚠️ WHAT THIS TEST IS FOR IS THE NEXT ONE, NOT THIS ONE. The conftest stub
(`_never_dial_out_from_a_constructor`) removes today's call. It cannot stop the
next server added to `.mcp.json`, a new probe in `__init__`, or somebody deleting
the fixture — and every one of those reintroduces both problems SILENTLY, showing
up only as a suite that got slow again and nobody timing it. So this poisons the
socket layer itself and builds an app: it fails on ANY outbound connection from a
constructor, whatever added it.

📌 IT DELIBERATELY DOES NOT ASSERT THE STUB EXISTS. Binding to the fixture's name
would make it pass for a stub that no longer works. The subject is the BEHAVIOUR
— no socket — so any future mechanism that achieves it passes, and any mechanism
that stops achieving it fails.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m


class _DialledOut(RuntimeError):
    """Raised by the poisoned socket layer. Carries the address it tried."""


@pytest.fixture
def dials(monkeypatch):
    """Every outbound connect is RECORDED, then blocked. Returns the record.

    🔴 THE RECORD IS THE DETECTOR. RAISING IS NOT.
    The first version of this fixture only raised, and the test asserted the
    constructor did not raise — which passed against the UNFIXED code, because
    `MCPManager.connect` catches every exception and RETURNS the error text
    instead ("An error is RETURNED rather than raised because every caller wants
    to carry on with the others", mcp_client.py). The exception was swallowed
    exactly as designed, the test saw a clean construction, and the guard was
    green for a dial it had just watched happen. An assertion satisfiable by the
    wrong answer.

    A list survives being swallowed. The raise stays only to stop the socket
    actually opening.

    Both entry points are poisoned: `socket.socket.connect` for the raw path and
    `socket.create_connection` for http.client, which is what urllib uses and is
    therefore the one today's defect went through. A guard missing the second
    could not fail for the reason it was written.
    """
    seen: list[str] = []

    def _connect(self, address, *a, **kw):
        seen.append(f"socket.connect({address!r})")
        raise _DialledOut(seen[-1])

    def _create_connection(address, *a, **kw):
        seen.append(f"create_connection({address!r})")
        raise _DialledOut(seen[-1])

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    return seen


def test_constructing_the_app_opens_no_socket(dials):
    m.LiteTUI()
    assert dials == [], (
        f"LiteTUI.__init__ reached the network: {dials}\n\n"
        "A constructor must not dial out. On this box that call is a ~4.0s wait "
        "to be refused in EVERY test that builds an app, and when the service IS "
        "up it changes the tool set the suite measures. Note the constructor did "
        "not RAISE — mcp_client.connect swallows the error by design — so only "
        "the recorded attempt shows it happened. If a new server or probe needs "
        "to run at startup it belongs behind an explicit call the tests can "
        "decline; see conftest.py's _never_dial_out_from_a_constructor."
    )


def test_the_detector_actually_fires(dials):
    """NEGATIVE CONTROL — a detector nobody has seen fail is not evidence.

    The test above passes if the app is clean AND if this fixture silently does
    nothing. Only this arm separates the two. It also pins the swallowing that
    made the first version of this file a false pass: urllib's failure is caught
    by nothing here, but the RECORD is what the subject test reads either way.
    """
    import urllib.request

    with pytest.raises(_DialledOut):
        socket.create_connection(("127.0.0.1", 7423), timeout=1)
    assert dials, "the poison did not record the raw create_connection"

    before = len(dials)
    with pytest.raises(Exception):
        urllib.request.urlopen("http://127.0.0.1:7423/mcp", timeout=1)
    assert len(dials) > before, (
        f"urllib did not go through the poisoned layer; recorded: {dials}"
    )
