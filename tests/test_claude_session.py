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
