"""Start/stop ownership for a backend whose engine is ONE process LiteTUI may own.

Moved here verbatim from `NInferBackend` (T0374) so a second single-process engine
(Strata) uses the SAME claims, locks and terminal-shutdown proof instead of a copy —
the user, 2026-09-17: *"agents ... rebuild every system for every backend over and
again instead of making modular resuable pieces"*.

A subclass supplies:
  * `_engine`       — the launcher module: `start(settings, healthy=, notice=)` returning
                      an `OwnedEngine`, `terminate_owned(owned)`, `EngineStartFailed`;
  * `label`         — the engine's name in sentences ("NInfer", "Strata");
  * `_process_name` — what the started process is called ("ninfer-serve");
  * `_health(host)` and `self._settings`.
"""
from __future__ import annotations

import threading

from .llm_backend import BackendError, _VramGate


class OwnedEngineLifecycle(_VramGate):
    _engine = None
    label = "engine"
    _process_name = "the engine"

    def _init_lifecycle(self) -> None:
        self._host: str | None = None
        #: The engine WE started via /engine start (the user a-35456da0: "LiteTUI may
        #: start it"), else None = attached to one somebody else runs.
        self._owned = None
        #: Mutually-exclusive lifecycle claims, both set SYNCHRONOUSLY (no await between
        #: the check and the set) so start and stop can never run concurrently on the
        #: same backend. _starting is held for the WHOLE spawn — including the
        #: cancellation JOIN — because a spawn thread outlives a cancelled await, so a
        #: start that is cancelled must still block a stop until its thread has finished
        #: (otherwise the stop snapshots none/old state and the late spawn orphans a
        #: process). _stopping is held for the whole off-loop stop.
        self._starting = False
        self._stopping = False
        #: Serializes every read/write of _starting/_stopping/_owned/_host across the event
        #: loop AND the spawn/stop worker threads. Held only for SYNC critical sections (no
        #: await inside), so it never stalls the loop; the long terminate_owned runs OUTSIDE it.
        self._lifecycle_lock = threading.Lock()

    def begin_stop(self) -> bool:
        """Claim a stop-in-progress, atomically under the lifecycle lock (no await inside).

        False (reject, do not overlap) if a stop is already running OR a start is in flight
        (_starting). _starting is a DEDICATED claim held across the spawn's cancellation join,
        so a cancelled spawn thread still blocks a stop. start_engine is the only
        load/start door (load() is frozen), so this covers new loads."""
        with self._lifecycle_lock:
            if self._stopping or self._starting:
                return False
            self._stopping = True
            return True

    def end_stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = False

    @property
    def attached(self) -> bool:
        """True unless /engine start made the process ours. The ownership rule
        stays: the ONLY engine shutdown() will ever stop is one we started."""
        return self._owned is None

    def shutdown(self) -> "object":
        """Stop the engine ONLY if /engine start made it ours; otherwise nothing.

        🔴 A shutdown that killed an engine LiteSuite (or the user, by hand) started
        would take down a process approved separately, that another client may be
        using — from a TUI closing a tab. Attached = leave it. Owned = ours to stop.
        """
        result = self.shutdown_owned()
        if not result.owned:         # attached / no owned engine: forget the host (nothing to race)
            with self._lifecycle_lock:
                self._host = None
        return result

    def shutdown_owned(self):
        """Stop our OWNED engine via the launcher's unified terminate_owned proof, under
        the lifecycle lock for the capture and the clear (the long terminate runs OUTSIDE the
        lock so the loop never stalls).

        Clear self._owned AND self._host TOGETHER, ONLY on a confirmed terminal state AND
        ONLY while _owned is still the SAME object — so a concurrent replacement is never torn
        down and its host is never cleared. Any uncertain outcome retains the entire ownership
        for a retry. Attached engines are never killed (owned=False)."""
        from .llm_backend import TerminalShutdown

        with self._lifecycle_lock:
            owned = self._owned
        if owned is None:
            return TerminalShutdown(owned=False)
        result = self._engine.terminate_owned(owned)     # OUTSIDE the lock (bounded but slow)
        if not result.retained:
            with self._lifecycle_lock:
                if self._owned is owned:                  # same object -> clear owned + host together
                    self._owned = None
                    self._host = None
        return result

    async def start_engine(self, *, notice=None) -> str:
        """Spawn the engine under LiteTUI's VRAM gate; refuse if any engine is up.

        `notice()` is forwarded to the launcher, which calls it immediately before
        the spawn and never on a refusal (T865). It runs on the worker thread, so a
        UI caller must marshal it back itself.
        """
        from litetui import agent_preparation

        # Claim _starting SYNCHRONOUSLY, before the first await, and reject a concurrent
        # start or a stop already in flight. Held (via the finally, which runs only AFTER
        # the spawn's cancellation JOIN) for the whole spawn, so a stop cannot be admitted
        # while a possibly-cancelled spawn thread is still running.
        # Serialized lifecycle gate (all sync, no await between the checks and the claim):
        # refuse a start while a stop is in flight, while another start is in flight, OR while
        # an owned engine is still TRACKED — starting a second would overwrite self._owned and
        # silently lose the handle to the first (its VRAM). Resolve the existing one
        # (/engine stop) first.
        stopping = f"the {self.label} engine is stopping — wait for it to finish, then /engine start."
        with self._lifecycle_lock:
            if self._stopping:
                raise BackendError(stopping)
            if self._starting:
                raise BackendError(f"a {self.label} engine start is already in progress.")
            if self._owned is not None:
                raise BackendError(f"an owned {self.label} engine is already tracked — /engine stop it before starting another.")
            self._starting = True
        try:
            def _spawn_and_publish():
                try:
                    owned = self._engine.start(
                        self._settings, healthy=self._health, notice=notice)
                except self._engine.EngineStartFailed as exc:
                    # A failed start may leave a resident engine whose cleanup was not
                    # confirmed. PUBLISH the retained OwnedEngine (under the lock) so /engine
                    # stop can retry it. Unconditional + cannot lose a handle: the entry gate
                    # refused if _owned was already set, and the _starting claim blocks any
                    # concurrent start/stop from touching _owned during the spawn.
                    if exc.retained_owned is not None:
                        with self._lifecycle_lock:
                            self._owned = exc.retained_owned
                            self._host = exc.retained_owned.host
                    raise
                # Publish INSIDE the thread, before returning (under the lock): a cancellation
                # delivered after the join must not lose an engine that actually started.
                with self._lifecycle_lock:
                    self._owned = owned
                    self._host = owned.host
                return owned

            async with self.vram_guard(self._process_name):
                if self._stopping:   # a stop claimed the backend after our entry check
                    raise BackendError(stopping)
                # await_preparation runs the blocking spawn off-loop and JOINS the thread
                # on cancellation (a raw to_thread would orphan it), then re-raises.
                owned = await agent_preparation.await_preparation(_spawn_and_publish)
            return f"started {self._process_name} pid {getattr(owned.proc, 'pid', '?')} at {owned.host} ({owned.model_id})"
        finally:
            with self._lifecycle_lock:
                self._starting = False
