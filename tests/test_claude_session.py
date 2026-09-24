"""Behavioral owner/reader/cancellation contracts; no SDK installation needed.

A few tests reach for `session._events` / `session._terminal`. That is deliberate:
the contract under test is the ORDER of two observable facts (the reader has the
result vs. the host has consumed it), and there is no public way to stand between
them. Everything else goes through start/query/events/interrupt/close/set_model.
"""
import asyncio
from dataclasses import dataclass

import pytest

from litetui.claude_session import ClaudeSession


@dataclass
class ResultMessage:
    session_id: str = "native-1"


class Client:
    def __init__(self, options):
        self.messages = asyncio.Queue()
        self.owner = None
        self.closed = False
        self.sent = []
        self.readers = 0
        self.drain = True
        self.models = []
        self.interrupted = False
        #: Set to an Event to park inside query() until the test releases it.
        self.block_query = None
        self.query_started = False
        #: Swallow the first reader cancel, so a bound cannot be mistaken for
        #: "the reader happened to die quickly".
        self.stubborn_reader = False

    async def connect(self):
        self.owner = asyncio.current_task()

    async def get_server_info(self):
        return {"pid": 123}

    async def query(self, prompt):
        assert asyncio.current_task() is self.owner
        self.query_started = True
        self.sent.append(prompt)
        if self.block_query is not None:
            await self.block_query.wait()

    async def set_model(self, model):
        assert asyncio.current_task() is self.owner
        self.models.append(model)

    async def receive_messages(self):
        self.readers += 1
        assert self.readers == 1
        if self.stubborn_reader:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                return
        while True:
            message = await self.messages.get()
            if isinstance(message, Exception):
                raise message
            yield message

    async def interrupt(self):
        assert asyncio.current_task() is self.owner
        self.interrupted = True
        if self.drain:
            await self.messages.put(ResultMessage())

    async def disconnect(self):
        assert asyncio.current_task() is self.owner
        self.closed = True


async def settle(predicate, limit=200):
    """Yield to the loop until `predicate` holds; no wall-clock sleeps."""
    for _ in range(limit):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


@pytest.mark.asyncio
async def test_sequential_turns_single_owner_reader():
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    try:
        await session.query("one", "first")
        with pytest.raises(RuntimeError, match="not ready"):
            session.lifecycle.start_turn("two")
        await client.messages.put(ResultMessage())
        assert len([m async for m in session.events()]) == 1
        await session.query("two", "second")
        await client.messages.put(ResultMessage())
        assert len([m async for m in session.events()]) == 1
        assert client.sent == ["first", "second"]
        assert session.session_id == "native-1"
        assert client.readers == 1
    finally:
        await session.close()
    assert client.closed


@pytest.mark.asyncio
async def test_interrupt_drains_before_next_turn():
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    try:
        await session.query("one", "first")
        await session.interrupt()
        # A DRAINED PROVIDER IS NOT YET A FINISHED TURN. interrupt() returns once
        # the READER has the terminal result; admission stays closed until the
        # host consumes it, or the next prompt starts against a result nobody saw.
        assert session.lifecycle.active_turn == "one"
        assert len([m async for m in session.events()]) == 1
        assert session.lifecycle.active_turn is None
        await session.query("two", "second")
    finally:
        await session.close()
    assert client.closed


@pytest.mark.asyncio
async def test_interrupt_timeout_disconnects_not_ready():
    client = Client(None)
    client.drain = False
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    with pytest.raises(TimeoutError):
        await session.interrupt(timeout=0.02)
    assert client.closed
    assert "uncertain" in session.lifecycle.failure
    with pytest.raises(RuntimeError, match="uncertain"):
        await session.query("two", "must not send")
    assert client.sent == ["first"]


@pytest.mark.asyncio
async def test_reader_failure_closes_owned_client():
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    await client.messages.put(ValueError("bad frame"))
    with pytest.raises(ValueError, match="bad frame"):
        async for _ in session.events():
            pass
    await session.close()
    assert client.closed
    assert "bad frame" in session.lifecycle.failure


@pytest.mark.asyncio
async def test_next_admission_is_refused_until_the_host_consumes_the_result():
    """The reader seeing a result must not reopen admission on its own."""
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    try:
        await session.query("one", "first")
        await client.messages.put(ResultMessage())
        assert await settle(lambda: session._events.qsize() == 1)
        assert session._terminal.is_set()               # the provider drained
        assert session.lifecycle.active_turn == "one"   # the host has not consumed
        with pytest.raises(RuntimeError, match="not ready"):
            await session.query("two", "must not be admitted")
        assert client.sent == ["first"]
        assert len([m async for m in session.events()]) == 1
        assert session.lifecycle.active_turn is None
        await session.query("two", "second")
        assert client.sent == ["first", "second"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_cancelling_an_interrupt_is_not_a_drain_failure():
    """A cancelled stop-watcher must not stamp uncertainty on a healthy session.

    The turn loop cancels its watcher in the finally of EVERY turn, so this path
    runs on ordinary completions, not only on errors.
    """
    client = Client(None)
    client.drain = False  # no result arrives, so interrupt parks on _terminal
    session = ClaudeSession(None, lambda _: client)
    try:
        await session.query("one", "first")
        watcher = asyncio.create_task(session.interrupt())
        assert await settle(lambda: client.interrupted), "interrupt never reached the client"
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert session.lifecycle.failure is None
        assert not client.closed
        # Still usable: the real terminal result still completes the turn.
        await client.messages.put(ResultMessage())
        assert len([m async for m in session.events()]) == 1
        await session.query("two", "second")
        assert client.sent == ["first", "second"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_a_factory_that_raises_settles_start_instead_of_hanging():
    """A missing optional SDK must fail the caller, not park it forever.

    Same code path as `from claude_agent_sdk import ClaudeSDKClient`: both now run
    inside the owner's try, where a raise reaches the handler that completes
    `_ready`. Previously either raised before the try and `start()` awaited a
    future nothing would ever resolve.
    """
    def explode(_options):
        raise ModuleNotFoundError("No module named 'claude_agent_sdk'")

    session = ClaudeSession(None, explode)
    with pytest.raises(ModuleNotFoundError):
        await asyncio.wait_for(session.start(), timeout=2)
    assert "ModuleNotFoundError" in session.lifecycle.failure
    # A second attempt reports rather than hangs, and close() stays safe.
    with pytest.raises(ModuleNotFoundError):
        await asyncio.wait_for(session.start(), timeout=2)
    await asyncio.wait_for(session.close(), timeout=2)


@pytest.mark.asyncio
async def test_a_failed_connect_settles_the_queued_command():
    client = Client(None)

    async def refuse():
        client.owner = asyncio.current_task()
        raise ConnectionError("claude executable not found")

    client.connect = refuse
    session = ClaudeSession(None, lambda _: client)
    with pytest.raises(ConnectionError, match="not found"):
        await asyncio.wait_for(session.query("one", "first"), timeout=2)
    assert client.sent == []
    await asyncio.wait_for(session.close(), timeout=2)


@pytest.mark.asyncio
async def test_owner_cancelled_mid_command_settles_its_waiter():
    """A command in flight when the owner dies must raise, never hang.

    The in-flight reference used to be cleared in a `finally`, so a cancellation
    between taking the command and settling its reply left the caller awaiting a
    future no owner remained to resolve. The caller also must not be handed a
    CancelledError, which would make IT look cancelled.
    """
    client = Client(None)
    client.block_query = asyncio.Event()
    session = ClaudeSession(None, lambda _: client)
    caller = asyncio.create_task(session.query("one", "first"))
    assert await settle(lambda: client.query_started), "query never reached the client"
    session._owner.cancel()
    with pytest.raises(RuntimeError, match="Claude session stopped"):
        await asyncio.wait_for(caller, timeout=2)
    await asyncio.wait_for(session.close(), timeout=2)


@pytest.mark.asyncio
async def test_a_command_after_the_owner_exits_does_not_hang():
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    await client.messages.put(ResultMessage())
    assert len([m async for m in session.events()]) == 1
    await session.close()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.set_model("opus"), timeout=2)


@pytest.mark.asyncio
async def test_cleanup_is_bounded_when_the_reader_ignores_cancellation():
    """An unkillable reader is reported, not waited on forever."""
    client = Client(None)
    client.stubborn_reader = True
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = 0.02
    await session.query("one", "first")
    await asyncio.wait_for(session.close(), timeout=5)
    assert session.lifecycle.cleanup_errors, "a swallowed cancel must be reported"
    assert any(
        "Timeout" in error or "Cancelled" in error
        for error in session.lifecycle.cleanup_errors
    ), session.lifecycle.cleanup_errors
    await asyncio.sleep(0.1)  # let the stubborn reader retire before loop teardown


@pytest.mark.asyncio
async def test_set_model_is_refused_during_a_turn_and_reaches_the_caller():
    client = Client(None)
    session = ClaudeSession(None, lambda _: client)
    try:
        await session.query("one", "first")
        with pytest.raises(RuntimeError, match="during a turn"):
            await asyncio.wait_for(session.set_model("opus"), timeout=2)
        await client.messages.put(ResultMessage())
        assert len([m async for m in session.events()]) == 1
        await asyncio.wait_for(session.set_model("opus"), timeout=2)
        assert client.models == ["opus"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_close_without_a_start_is_a_no_op():
    session = ClaudeSession(None, lambda _: Client(None))
    await asyncio.wait_for(session.close(), timeout=2)
    assert session.lifecycle.failure is None


# ---- stuck close: bounded, forced, and never silent -------------------------
# Baseline before this behaviour existed, measured with the real SDK in
# artifacts/claude-close-escalation-before.json: close() never returned, the owner
# stayed alive, the real claude.exe survived, and cleanup_errors was EMPTY.
#
# 🔴 EVERY WEDGED TEST RELEASES ITS WEDGE IN A `finally`. Releasing at the end of
# the body instead means a failing assert leaves the owner parked in the wedge, and
# the loop teardown then hangs the WHOLE RUN rather than reporting one failure.
# Measured: a first cut did exactly that and cost two timed-out runs.


class FakeChild:
    """Stands in for the CLI subprocess the SDK spawns."""

    def __init__(self, pid=424242, created=999):
        self.pid = pid
        self.created = created
        self.killed = False


class WedgedClient(Client):
    """A client whose disconnect never returns and ignores cancellation.

    The exact shape the live probe reproduced; it is what made the old
    `await asyncio.gather(owner)` wait forever.
    """

    def __init__(self, options, child):
        super().__init__(options)
        self.release = asyncio.Event()
        self.child = child
        # Where _record_owned_child looks: client._transport._process.pid
        self._transport = type("T", (), {"_process": child})()

    async def disconnect(self):
        """Never returns, ignores cancellation — until the CHILD dies.

        The real transport's disconnect awaits `self._process.wait()`, so killing
        the child is what lets it finish. Modelling that is the only way a test can
        see whether the fix kills the child BEFORE abandoning the owner.
        """
        self.closed = True
        while not self.release.is_set() and not self.child.killed:
            try:
                await asyncio.wait_for(self.release.wait(), timeout=0.01)
            except (asyncio.CancelledError, TimeoutError):
                pass


@pytest.fixture
def owned(monkeypatch):
    """Route the native process helpers at a fake child; record every kill.

    `state["created"]` is what the helpers report NOW, so a test can change the
    identity behind a pid after the session recorded it — which is the only way to
    exercise pid reuse.
    """
    from litetui import claude_session as mod

    child = FakeChild()
    killed: list[int] = []
    state = {"created": child.created}

    def creation_filetime(pid):
        if int(pid) != child.pid or child.killed:
            return None
        return state["created"]

    def force_kill_tree(pid):
        assert int(pid) == child.pid, f"killed a pid we do not own: {pid}"
        child.killed = True
        killed.append(int(pid))
        return "SUCCESS: terminated"

    monkeypatch.setattr(mod, "_creation_filetime", creation_filetime)
    monkeypatch.setattr(mod, "_force_kill_tree", force_kill_tree)
    return child, killed, state


async def close_under_bound(session, client, timeout=20):
    """Close, observing with asyncio.wait, and always release the wedge.

    NEVER wait_for: on a cancellation-resistant target it cancels the inner task and
    then awaits it, so the bound hangs too. That trap cost two nine-minute runs
    before the live probe became readable, and it is the same mistake the product
    code made.
    """
    closing = asyncio.ensure_future(session.close())
    done, _pending = await asyncio.wait([closing], timeout=timeout)
    return bool(done)


@pytest.mark.asyncio
async def test_a_stuck_close_is_bounded_and_force_kills_only_the_owned_child(owned):
    child, killed, _state = owned
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert session.owned_child == {"pid": child.pid, "created": child.created}
        assert await close_under_bound(session, client), (
            "close() did not return; the escalation is still unbounded")
        assert killed == [child.pid]
        assert child.killed
        assert any(f"pid {child.pid} was force-killed" in error
                   for error in session.lifecycle.cleanup_errors), (
            session.lifecycle.cleanup_errors)
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_stuck_close_never_reports_success_silently(owned):
    child, _killed, _state = owned
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert await close_under_bound(session, client)
        # Sentinel's pass condition: silent success is a fail.
        assert session.lifecycle.cleanup_errors, "a forced teardown reported nothing"
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_reused_pid_is_never_killed(owned):
    """🔴 The identity check, and it is not theoretical.

    The restart probe measured Windows handing a new claude.exe the pid a dead one
    had used 6.4s earlier. If liveness were pid-only, forced cleanup would kill
    whatever now holds that number.
    """
    child, killed, state = owned
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert session.owned_child["created"] == child.created
        state["created"] = child.created + 1   # a different process holds it now
        assert await close_under_bound(session, client)
        assert killed == [], "killed a pid whose identity no longer matched"
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_clean_close_kills_nothing(owned):
    """The reaper stays out of the way when the SDK tore down properly."""
    child, killed, _state = owned
    client = Client(None)
    client._transport = type("T", (), {"_process": child})()
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    await client.messages.put(ResultMessage())
    assert len([m async for m in session.events()]) == 1
    child.killed = True   # the real disconnect reaped it; it is no longer alive
    await session.close()
    assert killed == []
    assert session.lifecycle.cleanup_errors == []


@pytest.mark.asyncio
async def test_missing_sdk_internals_disable_the_reaper_without_breaking_connect():
    """The pid is read out of SDK privates; losing it costs a diagnostic only."""
    client = Client(None)          # no _transport at all
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    assert session.owned_child is None
    await client.messages.put(ResultMessage())
    assert len([m async for m in session.events()]) == 1
    await session.close()
    assert client.closed


@pytest.mark.asyncio
async def test_killing_the_child_first_lets_the_owner_finish_not_be_abandoned(owned):
    """Why the stuck path reaps BEFORE it cancels.

    Killing the owned child unwedges the SDK's pending I/O, so the owner unwinds on
    its own. Cancel first and you abandon a task that still holds the transport.
    """
    child, killed, _state = owned
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert await close_under_bound(session, client)
        assert killed == [child.pid]
        assert session._owner.done(), "the owner was abandoned rather than unwedged"
        assert not any("still running" in error
                       for error in session.lifecycle.cleanup_errors), (
            session.lifecycle.cleanup_errors)
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_child_that_outlives_a_graceful_close_is_still_reaped(owned):
    """Why close() reaps unconditionally at the end.

    The SDK returned from disconnect and the process is somehow STILL there. Every
    earlier guard has already passed, so this is the only thing left between the app
    and a stray CLI child — and it must say so rather than exit quietly.
    """
    child, killed, _state = owned
    client = Client(None)                       # a graceful disconnect
    client._transport = type("T", (), {"_process": child})()
    session = ClaudeSession(None, lambda _: client)
    await session.query("one", "first")
    await client.messages.put(ResultMessage())
    assert len([m async for m in session.events()]) == 1
    await session.close()                       # child.killed is still False
    assert killed == [child.pid]
    assert any("outlived close" in error
               for error in session.lifecycle.cleanup_errors), (
        session.lifecycle.cleanup_errors)


@pytest.mark.asyncio
async def test_an_unverifiable_child_is_not_killed_and_says_it_is_unverified(owned,
                                                                            monkeypatch):
    """A pid with no provable identity must not be killed — and must not be silent.

    This is the SDK-internals-moved case, and the non-Windows case. Killing on a pid
    alone could terminate an unrelated process; reporting nothing would read as
    "there was nothing to clean up". Neither is acceptable, so it refuses and says
    the cleanup is unverified.
    """
    from litetui import claude_session as mod

    child, killed, _state = owned
    monkeypatch.setattr(mod, "_creation_filetime", lambda pid: None)
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert session.owned_child == {"pid": child.pid, "created": None}
        assert await close_under_bound(session, client)
        assert killed == [], "killed a pid whose identity could not be proven"
        assert any("unverified" in error
                   for error in session.lifecycle.cleanup_errors), (
            session.lifecycle.cleanup_errors)
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_child_that_survives_the_kill_is_reported_as_surviving(owned,
                                                                      monkeypatch):
    """taskkill can report success for a process that is still there.

    The only honest answer comes from looking again, so the outcome is re-checked
    and reported as SURVIVED rather than as a kill that worked.
    """
    from litetui import claude_session as mod

    child, _killed, _state = owned
    attempts: list[int] = []

    def kill_that_does_not_work(pid):
        attempts.append(int(pid))
        return "SUCCESS: terminated"      # claims success, child stays alive

    monkeypatch.setattr(mod, "_force_kill_tree", kill_that_does_not_work)
    client = WedgedClient(None, child)
    session = ClaudeSession(None, lambda _: client)
    session.cleanup_timeout = session.settle_timeout = 0.05
    try:
        await session.query("one", "first")
        assert await close_under_bound(session, client)
        # TWICE, and that is correct: the stuck path tried, the child was still
        # alive at close()'s last word, so it tried again. Two attempts and two
        # honest reports beat one attempt and a claim.
        assert attempts == [child.pid, child.pid]
        assert any("SURVIVED a force-kill" in error
                   for error in session.lifecycle.cleanup_errors), (
            session.lifecycle.cleanup_errors)
    finally:
        client.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_set_effort_sends_the_cli_flag_setting_and_is_refused_during_a_turn():
    from types import SimpleNamespace

    client = Client(None)
    sent = []

    async def control(request):
        assert asyncio.current_task() is client.owner
        sent.append(request)
        return {}
    client._query = SimpleNamespace(_send_control_request=control)
    session = ClaudeSession(SimpleNamespace(effort="high"), lambda _: client)
    try:
        await session.start()
        assert session.effort == "high", "the spawn's --effort is the starting level"
        await session.query("one", "first")
        with pytest.raises(RuntimeError, match="during a turn"):
            await asyncio.wait_for(session.set_effort("max"), timeout=2)
        assert session.effort == "high"
        await client.messages.put(ResultMessage())
        assert len([m async for m in session.events()]) == 1
        await asyncio.wait_for(session.set_effort("max"), timeout=2)
        assert sent == [{"subtype": "apply_flag_settings", "settings": {"effortLevel": "max"}}]
        assert session.effort == "max"
    finally:
        await session.close()
