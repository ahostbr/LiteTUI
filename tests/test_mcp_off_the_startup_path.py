"""Constructing the app must not wait on an MCP server. T239.

THE PRODUCT HALF of what T237(C) found in the suite. `LiteTUI.__init__` called
`mcp.load()`, which connects every declared server inline. `.mcp.json` declares
LiteSuite's bridge at http://localhost:7423/mcp, and with LiteSuite down that
POST waits to be REFUSED — timed directly, three times, 2026-09-03:

    4.033s / 4.051s / 4.105s
    "[WinError 10061] the target machine actively refused it"

the Windows dual-stack localhost path (::1 then 127.0.0.1, with retries), not a
timeout anyone configured. So every launch with LiteSuite down paid ~4 seconds
before its first frame. T237(C) stopped the TESTS paying it with a conftest stub;
this file is about the APP, and the stub is deliberately declined here
(`real_mcp_load`) — a test of "does the constructor dial" that runs under a
fixture preventing dials would prove nothing.

⚠️ MOVING IT TO A COROUTINE ON THE EVENT LOOP WOULD NOT HAVE BEEN A FIX. That
turns "four seconds before the first frame" into "four seconds with the UI up and
frozen" — the same wait behind a screen that now looks alive. Each connect runs
in a thread, and the arm below is a real elapsed-time bound, not an assertion
that some coroutine was scheduled.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_mcp_load

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m

#: The measured refusal, rounded down. The bound below has to sit well under it
#: or a pass would be consistent with the defect still being present.
REFUSAL_SECONDS = 4.0

#: 🔴 DERIVED FROM BOTH SIDES, AFTER I GUESSED IT WRONG ONCE.
#:
#: My first bound was 200 ms, written before measuring anything but the defect.
#: Six constructions on this box, with the fix in place:
#:
#:     259  177  218  179  180  176 ms   (min 176, max 259, mean 198)
#:
#: 200 ms sat ON THE MEDIAN. The first run failed at 275 ms — not because the
#: dial was back, but because the bound was a coin flip, and a coin-flip bound
#: teaches everyone to re-run until it is green.
#:
#: 1000 ms is ~4x above the observed maximum and ~4x below the 4000 ms defect,
#: so it has margin in BOTH directions: it cannot flake on a loaded box, and it
#: cannot pass with a refused connect in the constructor. The negative control
#: below is what keeps that second half honest.
BOUND_MS = 1000


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def test_construction_does_not_wait_for_an_unreachable_server():
    """See BOUND_MS for how the number was derived, and how I got it wrong."""
    t0 = time.perf_counter()
    m.LiteTUI()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < BOUND_MS, (
        f"LiteTUI.__init__ took {elapsed_ms:.0f} ms. It is dialling out again — "
        f"a refused localhost connect costs ~{REFUSAL_SECONDS * 1000:.0f} ms on "
        "this box. The connect belongs in _mcp_connect, after the first frame."
    )


def test_the_configs_are_still_read_at_construction():
    """Moving the CONNECT must not move the READ.

    /settings, `describe()` and the /mcp dialog list what is DECLARED. If the
    constructor stopped reading the files too, those surfaces would be empty
    until a background worker finished — a different bug wearing this fix's
    clothes, and one that only shows on a slow server.
    """
    a = make_app()
    assert a.mcp.configs, (
        "no server configs after construction — .mcp.json at the repo root "
        "declares at least one, and the dialog reads this"
    )


@pytest.mark.parametrize(
    "err,expect",
    [("connection refused", True), (None, False)],
    ids=["refused_is_announced", "connected_is_announced_too"],
)
@pytest.mark.asyncio
async def test_what_connect_RETURNS_becomes_a_visible_line(err, expect):
    """🔴 `connect()` RETURNS its error instead of raising, by design.

    That is the right contract for a loop over many servers, and it means
    NOTHING UPSTREAM SEES A FAILURE unless someone reads the return value. A
    tool that simply never appears is indistinguishable from one the model chose
    not to call — which is the confusion this whole surface exists to remove.

    ⚠️ THE SUBJECT HERE IS THE ANNOUNCE, NOT THE NETWORK, so `connect` is faked.
    Driving this against the real refused 7423 would work and would make the
    test wait the full ~4 s to do it — reintroducing, inside the suite, the exact
    cost this commit removes from the app. The bound test above is where the
    real timing is measured; this is where the reporting is.

    Both polarities, because "a failure is announced" is also satisfied by an
    app that announces everything, and "a success is announced" by one that
    never distinguishes them. The error text itself must survive to the screen.
    """
    a = make_app()
    said: list[str] = []
    a.system_message = lambda text: said.append(str(text))
    a.mcp.connect = lambda name: err

    async with a.run_test(size=(120, 40)) as pilot:
        await settle_until(pilot, lambda: any("mcp" in s.lower() for s in said))

    joined = " | ".join(said)
    assert "mcp" in joined.lower(), (
        f"nothing was said about MCP at all; the app said: {said!r}"
    )
    if expect:
        assert "connection refused" in joined, (
            f"the failure was reported without its reason: {said!r}"
        )
        assert "[!]" in joined, f"a failure was reported as ordinary news: {said!r}"
    else:
        assert "[!]" not in joined, f"a SUCCESS was reported as a failure: {said!r}"


@pytest.mark.asyncio
async def test_a_reachable_server_reaches_the_DISPATCH_map_not_just_the_specs():
    """The half that a specs-only check cannot see.

    `mcp_plugin` registers MCP as a dynamic provider: its SPECS callable is
    re-read every turn, so a late server's schemas reach the model unaided. Its
    DISPATCH callable reads a map cached in `__init__`. Miss the rebuild and the
    model is offered a tool the loop cannot route — "unknown tool", which reads
    as a bad model rather than a stale cache.

    So this asserts on the map, with a fake server rather than a live one: a
    test that needed a real MCP server on 7423 would be exactly the
    is-another-app-running dependency T237(C) removed.
    """
    a = make_app()

    class _FakeServer:
        name = "fake"
        tools = [{"name": "fake_tool", "description": "d", "inputSchema": {}}]

        def call(self, tool, args):        # pragma: no cover - not invoked here
            return "ok"

        def stop(self):
            pass

    a.mcp.servers["fake"] = _FakeServer()
    a.rebuild_mcp_dispatch()
    assert any("fake" in str(k) for k in a.mcp_dispatch), (
        f"a connected server's tools never reached the dispatch map: "
        f"{list(a.mcp_dispatch)!r}"
    )


def test_the_bound_would_actually_catch_the_old_behaviour():
    """NEGATIVE CONTROL — the bound has to be able to fail.

    A 200 ms assertion proves nothing unless the thing it excludes really costs
    more than that HERE, on this box, today. This times the refused connect
    itself. If LiteSuite is ever running when this suite runs, 7423 answers, the
    connect is fast, and this arm skips rather than pretending it measured
    something.
    """
    t0 = time.perf_counter()
    try:
        socket.create_connection(("localhost", 7423), timeout=REFUSAL_SECONDS * 2)
    except OSError:
        pass
    else:
        pytest.skip("something IS listening on 7423 — the refusal cannot be timed")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms > BOUND_MS, (
        f"a refused connect to 7423 took only {elapsed_ms:.0f} ms, which is under "
        f"the {BOUND_MS} ms bound the test above uses. That bound can no longer "
        "distinguish the fix from the defect and must be re-derived."
    )
