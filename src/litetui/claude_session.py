"""Persistent Claude SDK owner; no host model/tool loop and no automatic replay.

SDK imports are optional. A dedicated owner task connects, sends commands and
closes; one reader forwards native messages. Interrupt must drain a ResultMessage
before another prompt is admitted. Failure disconnects instead of guessing.

TWO DIFFERENT "THE TURN IS OVER" QUESTIONS, AND THEY HAVE DIFFERENT ANSWERS:

  * `_terminal` — THE READER saw a ResultMessage. That is what `interrupt` waits
    for, because the question it asks is whether the PROVIDER drained, and the
    provider does not care whether a UI has painted anything yet.
  * `lifecycle.active_turn` — THE HOST consumed that ResultMessage out of
    `events()`. That is what gates the next admission, because a turn whose
    terminal event is still sitting in the queue would otherwise let the next
    prompt start against a result the host has not seen.

Collapsing the two is how a next admission ends up looking at an old result, so
the reader sets `_terminal` and the consumer calls `finish_turn` — never both in
one place.

A WAITER MUST NEVER OUTLIVE THE OWNER. Every exit path settles every queued
command and the in-flight one; the owner is the only thing that can resolve a
reply future, so a command still waiting when `_run` returns is a permanent
hang. That is also why the SDK import and client construction happen INSIDE the
try: a missing `claude_agent_sdk` raised before it and left `start()` awaiting a
`_ready` that nothing would ever complete.
"""
from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

from litetui.backend_session import BackendSession

# ---- the owned CLI child --------------------------------------------------
# 🔴 A PID IS NOT AN IDENTITY. Windows reuses pids — measured in this repo's own
# restart probe, where a new claude.exe came back on the previous one's pid 6.4s
# later. Every recorded child therefore carries its creation time, and nothing is
# killed unless BOTH still match. Without that, forced cleanup is a coin flip that
# can terminate an unrelated process.
#
# WE NEVER SEARCH FOR claude.exe BY NAME. Only the pid this session's own transport
# handed us is ever touched; this box routinely has a dozen live claude.exe
# belonging to the operator.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_KERNEL32 = None
if sys.platform == "win32":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]


def _creation_filetime(pid):
    """Creation time of a LIVE pid, or None. Native and sub-second.

    Returns None off Windows, which makes `_child_alive` false and disables forced
    cleanup rather than killing a pid whose identity cannot be proven.
    """
    if _KERNEL32 is None:
        return None
    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        if not _KERNEL32.GetProcessTimes(handle, ctypes.byref(created),
                                         ctypes.byref(exited), ctypes.byref(kernel),
                                         ctypes.byref(user)):
            return None
        code = wintypes.DWORD()
        if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != _STILL_ACTIVE:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        _KERNEL32.CloseHandle(handle)


def _record_owned_child(client):
    """The pid the SDK just spawned, with its identity. Never raises.

    Reaches into SDK internals on purpose and defensively: losing the ability to
    reap a child is worth a diagnostic, never a failed connect.
    """
    try:
        transport = getattr(client, "_transport", None)
        process = getattr(transport, "_process", None)
        pid = getattr(process, "pid", None)
        if pid is None:
            return None
        pid = int(pid)
        return {"pid": pid, "created": _creation_filetime(pid)}
    except Exception:  # noqa: BLE001 - discovery must never break a connect
        return None


def _child_alive(child):
    if not child or child.get("created") is None:
        return False
    return _creation_filetime(child["pid"]) == child["created"]


def _force_kill_tree(pid):
    """Kill one pid tree and say what happened — including when it did not work.

    A cleanup routine that reports success it did not achieve is worse than one
    that fails loudly: the caller stops looking.
    """
    try:
        from litetui import ttyguard
        done = ttyguard.run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"taskkill did not run: {type(exc).__name__}: {exc}"
    text = (done.stdout or done.stderr or "").strip()[:200]
    return text or f"taskkill exit {done.returncode}"


class CommandRefused(RuntimeError):
    """A command the host may not run right now — the session stays alive.

    THE DISTINCTION THIS CLASS EXISTS FOR: a refusal is a host-side admission
    decision ("not while a turn is open"), while an exception out of a client
    call means the SDK transport itself failed and nothing about the session can
    be trusted afterwards. Both used to re-raise into the owner, so declining
    /model during a turn DESTROYED the Claude session instead of declining.
    """


@dataclass
class _Command:
    kind: str
    value: Any
    reply: asyncio.Future


def _settleable(exc: BaseException) -> BaseException:
    """A cancellation is not something to hand a waiting caller verbatim.

    Setting `CancelledError` on a reply future makes the AWAITING task look
    cancelled, which would propagate this session's teardown into unrelated
    callers as if they had been cancelled themselves.
    """
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(f"Claude session stopped: {type(exc).__name__}")


@dataclass
class ClaudeSession:
    options: Any
    client_factory: Any = None
    lifecycle: BackendSession = field(default_factory=BackendSession)
    info: dict = field(default_factory=dict)
    session_id: str | None = None
    #: The effort level this CLI runs at: the spawn's --effort, then each live
    #: set_effort. None = the model's own default (never set on this process).
    effort: str | None = None
    #: Bound on each owned-teardown step. A field so a test can shrink it;
    #: never long enough to make a stuck reader look like a hang.
    #: Grace for a cooperative close before escalation. A field so a test can
    #: shrink it; 12s of real waiting per test is how a suite stops being run.
    settle_timeout: float = 12
    cleanup_timeout: float = 10
    #: {pid, created} for the CLI child this session spawned, or None.
    owned_child: dict | None = field(default=None, init=False)
    _owner: asyncio.Task | None = field(default=None, init=False)
    _commands: asyncio.Queue = field(default_factory=asyncio.Queue, init=False)
    _events: asyncio.Queue = field(default_factory=asyncio.Queue, init=False)
    _terminal: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _ready: asyncio.Future | None = field(default=None, init=False)

    async def start(self):
        if self._owner is None:
            self.effort = getattr(self.options, "effort", None)
            self._ready = asyncio.get_running_loop().create_future()
            self._owner = asyncio.create_task(self._run(), name="claude-sdk-owner")
        await asyncio.shield(self._ready)
        if self._owner.done():
            raise RuntimeError(self.lifecycle.failure or "Claude session disconnected")
        return self.info

    async def _run(self):
        generation = self.lifecycle.begin()
        client = None
        reader = None
        pending = None
        try:
            if self.client_factory is None:
                # INSIDE the try: a missing optional SDK is the most likely
                # failure here, and raising it outside left every caller of
                # start()/_command() waiting on futures nothing would settle.
                from claude_agent_sdk import ClaudeSDKClient
                self.client_factory = ClaudeSDKClient
            client = self.client_factory(self.options)
            await client.connect()
            self.owned_child = _record_owned_child(client)
            self.info = await client.get_server_info() or {}
            self.lifecycle.ready(generation)
            self._ready.set_result(self.info)
            reader = asyncio.create_task(self._read(client, generation), name="claude-sdk-reader")
            while True:
                pending = await self._commands.get()
                if pending.kind == "close":
                    pending.reply.set_result(None)
                    pending = None
                    break
                try:
                    if pending.kind == "query":
                        turn_id, prompt = pending.value
                        try:
                            self.lifecycle.start_turn(turn_id)
                        except RuntimeError as exc:
                            # Admission, not transport: the host queues its own
                            # input, so a busy session declines and keeps serving.
                            raise CommandRefused(str(exc)) from exc
                        self._terminal.clear()
                        await client.query(prompt)
                    elif pending.kind == "interrupt":
                        if self.lifecycle.active_turn:
                            await client.interrupt()
                    elif pending.kind == "model":
                        if self.lifecycle.active_turn:
                            raise CommandRefused("Cannot change Claude model during a turn")
                        await client.set_model(pending.value)
                    elif pending.kind == "effort":
                        if self.lifecycle.active_turn:
                            raise CommandRefused("Cannot change Claude effort during a turn")
                        # No public SDK method (0.2.159). The CLI's own control
                        # request; measured live on CLI 2.1.281: get_settings'
                        # applied.effort went low -> high on the same process.
                        await client._query._send_control_request(
                            {"subtype": "apply_flag_settings", "settings": {"effortLevel": pending.value}})
                        self.effort = pending.value
                    if not pending.reply.done():
                        pending.reply.set_result(None)
                except CommandRefused as exc:
                    if not pending.reply.done():
                        pending.reply.set_exception(exc)
                    pending = None
                    continue
                except Exception as exc:
                    if not pending.reply.done():
                        pending.reply.set_exception(exc)
                    raise
                # Cleared only once the reply is settled. Clearing it in a
                # `finally` hid the in-flight command from the handler below, so
                # a cancellation between get() and settle stranded its caller.
                pending = None
        except BaseException as exc:  # noqa: BLE001 - owns SDK cleanup across cancellation
            self.lifecycle.disconnected(f"{type(exc).__name__}: {exc}")
            settle = _settleable(exc)
            if pending is not None and not pending.reply.done():
                pending.reply.set_exception(settle)
            if not self._ready.done():
                self._ready.set_exception(settle)
            self._events.put_nowait(settle)
        finally:
            # EVERY STEP BOUNDED, NONE OF THEM MOVED OFF THIS TASK. `cleanup_steps`
            # would be the obvious helper, but it runs each step under
            # `asyncio.wait_for`, which wraps it in a CHILD task — and the SDK
            # client was connected from this one. Disconnecting from a different
            # task is precisely the AnyIO affinity hazard plan 2.5 says to
            # validate, so the bound here is `asyncio.timeout`, which cancels in
            # place and never hops tasks.
            # ponytail: on the owner-cancelled path (close()'s 12s escalation)
            # these awaits re-raise CancelledError immediately and the disconnect
            # is recorded as a cleanup error rather than completed. Shielding it
            # would need a fresh task and give the affinity problem back; left as
            # a known ceiling, not silently traded away.
            # A CONNECT THAT FAILED AFTER SPAWNING still owns a process. The happy
            # path records at connect; this covers the path where connect raised
            # between spawn and return, which would otherwise leave a child nobody
            # could reap.
            if client is not None and self.owned_child is None:
                self.owned_child = _record_owned_child(client)
            if reader:
                reader.cancel()
                try:
                    async with asyncio.timeout(self.cleanup_timeout):
                        await asyncio.gather(reader, return_exceptions=True)
                except BaseException as exc:  # noqa: BLE001 - diagnostics only
                    self.lifecycle.cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            if client is not None:
                try:
                    async with asyncio.timeout(self.cleanup_timeout):
                        await client.disconnect()
                except BaseException as exc:  # noqa: BLE001 - diagnostics only
                    self.lifecycle.cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            if not self.lifecycle.failure:
                self.lifecycle.disconnected()
            self._drain()
            self._events.put_nowait(None)

    async def _reap_owned_child(self, reason):
        """Force-kill this session's own CLI child if it is still alive.

        Only ever the recorded pid, only while its creation time still matches, and
        only the subprocess — the SDK client is never touched from here, because
        calling disconnect from a second task is the AnyIO affinity hazard plan 2.5
        tells us to avoid.
        """
        child = self.owned_child
        if child is None:
            return
        if child.get("created") is None:
            # A pid with no provable identity. Killing on a pid alone can terminate
            # an unrelated process, so we refuse — and SAY so, because reporting
            # nothing here would read as "there was nothing to clean up".
            self.lifecycle.cleanup_errors.append(
                f"{reason}; owned Claude CLI pid {child['pid']} could not be "
                "identity-verified, so it was NOT killed — process cleanup is "
                "unverified")
            return
        if not _child_alive(child):
            return                        # it exited on its own; nothing to report
        detail = await asyncio.to_thread(_force_kill_tree, child["pid"])
        # LOOK AGAIN. taskkill can report success for a process that is still
        # terminating, or fail outright; either way the only honest answer comes
        # from re-checking the identity.
        outcome = ("was force-killed" if not _child_alive(child)
                   else "SURVIVED a force-kill and may still be running")
        self.lifecycle.cleanup_errors.append(
            f"{reason}; owned Claude CLI pid {child['pid']} {outcome}: {detail}")

    def _drain(self, error=None):
        """Settle every queued command. Only the owner can resolve a reply."""
        while not self._commands.empty():
            command = self._commands.get_nowait()
            if not command.reply.done():
                command.reply.set_exception(
                    error or RuntimeError("Claude session disconnected")
                )

    async def _read(self, client, generation):
        try:
            async for message in client.receive_messages():
                if generation != self.lifecycle.generation:
                    return
                data = getattr(message, "data", {})
                session_id = getattr(message, "session_id", None) or data.get("session_id")
                if session_id:
                    self.session_id = session_id
                if type(message).__name__ == "ResultMessage":
                    # The PROVIDER drained. Admission stays closed until the host
                    # consumes this out of events(); see the module docstring.
                    self._terminal.set()
                await self._events.put(message)
            raise RuntimeError("Claude stream ended before disconnect")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reader failure invalidates admission
            self.lifecycle.disconnected(f"{type(exc).__name__}: {exc}")
            await self._events.put(exc)
            # Wake owner so failures close the actual SDK child, not only UI state.
            reply = asyncio.get_running_loop().create_future()
            self._commands.put_nowait(_Command("close", None, reply))

    async def _command(self, kind, value=None):
        await self.start()
        reply = asyncio.get_running_loop().create_future()
        await self._commands.put(_Command(kind, value, reply))
        # The owner can finish between start()'s check and this put, and then
        # nothing is left to drain what we just queued.
        if self._owner is not None and self._owner.done():
            self._drain()
        return await asyncio.shield(reply)

    async def query(self, turn_id, prompt):
        await self._command("query", (turn_id, prompt))

    async def set_model(self, model):
        await self._command("model", model)

    async def set_effort(self, effort):
        """Switch a named effort level live. There is no live way back to the
        model default: effortLevel=None clears the flag but the applied level
        stays (measured), so a return to default needs a new process."""
        await self._command("effort", effort)

    async def events(self):
        while True:
            message = await self._events.get()
            if message is None:
                raise RuntimeError(self.lifecycle.failure or "Claude disconnected")
            if isinstance(message, BaseException):
                raise message
            yield message
            if type(message).__name__ == "ResultMessage":
                # The host now HAS the terminal event, so the next admission
                # cannot be looking at this one. Deliberately after the yield:
                # a consumer that abandons the generator never reopens admission.
                self.lifecycle.finish_turn(self.lifecycle.active_turn)
                return

    async def interrupt(self, timeout=15):
        try:
            async with asyncio.timeout(timeout):
                await self._command("interrupt")
                if self.lifecycle.active_turn:
                    await self._terminal.wait()
        except asyncio.CancelledError:
            # OUR CALLER GAVE UP; THE PROVIDER DID NOT FAIL. The turn loop
            # cancels its stop-watcher in the finally of every turn, so calling
            # that a drain failure would stamp "uncertain" on deliveries that
            # completed and tear down a session that is still healthy.
            # `asyncio.timeout` surfaces its own expiry as TimeoutError, so a
            # real drain timeout still reaches the handler below.
            raise
        except BaseException:
            self.lifecycle.failure = "Claude interrupt did not drain; delivery is uncertain"
            await self.close()
            raise

    async def close(self):
        owner = self._owner
        if owner is None:
            return
        if not owner.done():
            reply = asyncio.get_running_loop().create_future()
            self._commands.put_nowait(_Command("close", None, reply))
            try:
                async with asyncio.timeout(self.settle_timeout):
                    await asyncio.shield(owner)
            except TimeoutError:
                # KILL THE CHILD FIRST, THEN CANCEL. Measured before this existed
                # (artifacts/claude-close-escalation-before.json): close() never
                # returned, the owner stayed alive, the claude.exe survived, and
                # cleanup_errors was EMPTY — cleanup succeeded by silence. Killing
                # the child first also unwedges the SDK's pending I/O, so the owner
                # usually unwinds on its own instead of being abandoned.
                await self._reap_owned_child(
                    f"close did not settle within {self.settle_timeout:.0f}s")
                owner.cancel()
                # NOT gather(): on a cancellation-resistant teardown that waits
                # forever, which is the whole defect. asyncio.wait observes.
                _done, pending = await asyncio.wait({owner},
                                                   timeout=self.cleanup_timeout)
                if pending:
                    # Says nothing about the child: the reaper above reports that
                    # separately and only if it verified it. Two claims, each owned
                    # by the code that can actually check it.
                    self.lifecycle.cleanup_errors.append(
                        f"owner task still running {self.cleanup_timeout:.0f}s after "
                        "cancellation; this session has stopped waiting for it")
        else:
            await asyncio.gather(owner, return_exceptions=True)
        # THE LAST WORD, on every path. A graceful close leaves nothing behind; if
        # one did, that is a cleanup error rather than silence.
        await self._reap_owned_child("owned child outlived close")
