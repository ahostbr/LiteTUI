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
    return _poison(monkeypatch, raw=True)


@pytest.fixture
def http_dials(monkeypatch):
    """As `dials`, but leaves RAW `socket.connect` alone. See its use below."""
    return _poison(monkeypatch, raw=False)


def _poison(monkeypatch, *, raw: bool) -> list[str]:
    seen: list[str] = []

    def _connect(self, address, *a, **kw):
        seen.append(f"socket.connect({address!r})")
        raise _DialledOut(seen[-1])

    def _create_connection(address, *a, **kw):
        seen.append(f"create_connection({address!r})")
        raise _DialledOut(seen[-1])

    if raw:
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


@pytest.mark.asyncio
async def test_MOUNTING_the_app_opens_no_socket_either(http_dials):
    """🔴 CONSTRUCTION WAS NEVER THE WHOLE SURFACE, AND THIS ARM IS WHY.

    The first version of this file tested `LiteTUI()` only. T239 then moved the
    boot connect off the constructor into an `on_mount` worker — the right fix
    for the app — and every mounted test app started dialling again, ~4s each,
    while this file stayed green because construction really was clean. The
    suite slid from 5:32 back past ten minutes and announced it as a tool
    timeout rather than a failure.

    A guard scoped to one lifecycle phase certifies that phase, not the app. So
    the subject here is the whole startup: build it, MOUNT it, let the workers
    run, and assert nothing dialled.

    🔴 IT USES `http_dials`, NOT `dials`, AND NOT OUT OF CONVENIENCE.
    Windows' ProactorEventLoop builds its own wakeup pipe with a real
    `socket.socket.connect` to 127.0.0.1. Poisoning that breaks the event loop
    itself before any app code runs — "AttributeError: 'ProactorEventLoop'
    object has no attribute '_ssock'" — so the arm would be reporting asyncio's
    dial, not the app's. `create_connection` stays poisoned: it is the door
    urllib and every http client here go through, and therefore the one the real
    defect used.

    ⇒ LIMIT, STATED RATHER THAN HIDDEN: a RAW-socket dial from app code would be
    missed at mount. The construction arm above still catches that, and nothing
    in this codebase opens a raw socket at startup today.
    """
    from _settle import settle_until

    dials = http_dials
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    # 📌 THE SERVER IS DECLARED HERE, BY THIS TEST, ON PURPOSE. conftest gives
    # the test world NO mcp configs, so the boot worker would otherwise never
    # run and this arm would pass by having nothing to do — the exact shape of
    # green it exists to rule out. Declaring one makes the worker run over a
    # server that looks perfectly real to it, so the assertion below is about
    # the chokepoint holding, not about there being nothing to hold.
    a.mcp.configs = {"probe": {"url": "http://127.0.0.1:7423/mcp", "type": "http"}}
    async with a.run_test(size=(120, 40)) as pilot:
        # Give the post-mount workers real frames to run in; a pass taken before
        # they start would be a pass about nothing.
        await settle_until(pilot, lambda: bool(dials))

    assert dials == [], (
        f"the app reached the network after mount: {dials}\n\n"
        "The boot connect moved to a worker in T239. It still must not dial "
        "during tests — see conftest.py's _never_dial_out_from_a_constructor, "
        "which stubs MCPManager.connect for exactly this."
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
