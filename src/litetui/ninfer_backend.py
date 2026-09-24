"""NInfer as a LiteTUI backend — the 5090 engine, attached, never spawned.

🔴 RYAN, 2026-09-17: *"LITETUI WAS ALWAYS THE END GOAL FOR NINFER and agents got
hung up on litesuites integration"*.

The design step is NOT this card. Ryan commissioned it on 2026-09-14 and it is
`Docs/CaseStudies/NInfer_CaseStudy_LiteTUI_5090Backend.md` — 128 lines, every
claim cited to `E:\\SAS\\REPO_CLONES\\ninfer`. Section 9 is adaptation guidance
and this module is built from it rather than re-derived. Three of its takeaways
are load-bearing here and none of them is guessable:

  * **Switch on the `code` field, not the status**, and map `service_unavailable`
    to a RELAUNCH rather than a retry. That is the lifecycle difference from
    llama.cpp.
  * **Health plus model-id are the whole discovery protocol.** There is no
    capability negotiation: poll `/health`, read `/v1/models`, pin the id.
  * **The server is a frozen capability box.** Vision, speculative backend, KV
    dtype, context ceiling and concurrency are all startup flags and the process
    cannot widen them later (`ninfer/docs/serving.md:49-50`).

🔴 WE ATTACH. WE DO NOT SPAWN, EVER — AND THAT IS A RULING, NOT A SIMPLIFICATION.
Starting `ninfer-serve` puts ~20 GiB on the card, and Ryan's standing rule
(`a-5fd08920`) is that no seat starts it outside LiteSuite. LiteSuite's Model Hub
owns the process, the approval and the VRAM gate; LiteTUI's job is to USE the
engine that is already up.

    A BACKEND THAT CANNOT SPAWN CANNOT ORPHAN 20 GiB OF VRAM. The llama.cpp
    backend needs its ownership rules (`_Owned`, tree-kill, attach-probe)
    because it spawns; this one does not need them because it does not.

⚠️ AND THE PORT IS ALLOCATED, NOT FIXED, so there is nothing to guess. LiteSuite
writes the live address into its own config as an `extraEndpoints` entry with
`kind: "ninfer"` when the engine becomes ready, and REMOVES it on stop — that
file is the discovery source, and its absence is the honest answer "no engine".
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import ninfer_engine
from .llm_backend import BackendError, ModelRow, _VramGate

#: Where LiteSuite keeps the config that names the live engine. The env var is
#: LiteSuite's own override, honoured so a test (or a second install) points
#: both apps at one directory rather than disagreeing about where "the" config is.
LITESUITE_LLM_DIR_ENV = "LITESUITE_LLM_DIR"


def litesuite_llm_dir() -> Path:
    override = os.environ.get(LITESUITE_LLM_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".litesuite" / "llm"


def discover_ninfer_host(config_path: Path | None = None) -> str | None:
    """The base URL of a running `ninfer-serve`, or None.

    🔴 READ, NEVER GUESSED. `ninfer-serve` is started on an ALLOCATED port, so
    there is no default to probe — LiteSuite writes the address it actually got
    into `extraEndpoints` once the engine is listening, and takes it out again
    on stop. A hardcoded 7480 would be right for one session and wrong for the
    next, and wrong in the direction that looks like the engine is down.

    ⬜ ABSENCE IS AN ANSWER, NOT AN ERROR. No file, no entry, unreadable JSON —
    all mean "no engine is registered", which is exactly what a user who has not
    started one should be told.
    """
    path = config_path or (litesuite_llm_dir() / "config.json")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    endpoints = body.get("extraEndpoints")
    if not isinstance(endpoints, list):
        return None
    for entry in endpoints:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "ninfer":
            continue
        base = entry.get("baseUrl")
        if isinstance(base, str) and base.strip():
            return base.rstrip("/")
    return None


# ── the error model ──────────────────────────────────────────────────────────

#: What each documented `code` means for the CALLER, from the case study §6 and
#: `ninfer/src/serve/generation_service.cpp:41-96`.
#:
#: 🔴 `service_unavailable` IS A RELAUNCH, NOT A RETRY, and that single row is
#: the reason this table exists rather than a status-code switch. The engine is
#: done — the process must be brought back from its launch config. Retrying it
#: the way you would retry a queue timeout produces a tight loop against a
#: corpse, which is exactly what a status-based handler does.
NINFER_ERROR_ACTION = {
    "context_length_exceeded": "trim",
    "server_overloaded": "backoff",
    "request_queue_timeout": "retry",
    "service_unavailable": "relaunch",
    # 🔴 THE ONE THE THINKING LEVELS CAN PRODUCE (T806). The engine accepts the
    # FIELD by protocol and the loaded TEMPLATE decides which values it exposes,
    # so an effort the template does not carry is a 400 raised BEFORE prompt
    # preparation (`ninfer/docs/serving.md:194`). Keep the error mapping as a
    # safeguard for artifacts whose template differs from LiteTUI's safe set.
    "reasoning_effort_not_supported": "thinking-level",
    # THE MEDIA CODES (T824). Read from ninfer/docs/serving.md, not guessed:
    # :44 'media requests and token-count requests fail with HTTP 400
    # vision_disabled', :331 media_budget_exceeded, and HTTP 413
    # request_too_large. Ryan hit the first with view_image on an artifact
    # started without --vision, and LiteTUI said only 'Something went wrong
    # talking to the model server.'
    #
    # The same page decides the history fix: 'A later request cannot enable
    # a capability omitted at startup' (:49). So a media part refused with
    # vision_disabled can NEVER succeed in this process.
    "vision_disabled": "vision-off",
    "media_budget_exceeded": "media-too-big",
    "request_too_large": "media-too-big",
}

#: The sentence each action turns into for a person reading a TUI.
_ACTION_TEXT = {
    "trim": "the prompt is longer than this engine's context — /compact or start a new conversation.",
    "backoff": "the engine's queue is full — wait a moment and send again.",
    "retry": "the engine did not admit the request in time — send it again.",
    "relaunch": (
        "the engine has stopped serving and must be restarted from LiteSuite's "
        "Model Hub — LiteTUI does not start it."
    ),
    "vision-off": (
        "this engine was started without vision, so it cannot look at "
        "images at all, and it cannot be switched on for one request. "
        "Restart it with vision enabled (LiteSuite's Model Hub) to send "
        "pictures."
    ),
    "media-too-big": (
        "the engine refused that attachment as too large: send a smaller "
        "image, or fewer of them in one message."
    ),
    "thinking-level": (
        "this model's chat template does not offer that thinking level — "
        "/thinking to pick another, or default to use the template's own."
    ),
}


def classify_ninfer_error(body: str | dict | None) -> tuple[str | None, str | None]:
    """`(code, action)` for an error body, or `(None, None)`.

    ⚠️ THE CODE IS NESTED IN SEVERAL SHAPES ON THIS WIRE — top level for the
    OpenAI dialect, under `error` for others. Reading only one of them would
    make the table look correct and fire on half the failures.
    """
    if body is None:
        return (None, None)
    data: object = body
    if isinstance(body, str):
        try:
            data = json.loads(body)
        except Exception:
            return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    code = data.get("code")
    if not isinstance(code, str):
        err = data.get("error")
        code = err.get("code") if isinstance(err, dict) else None
    if not isinstance(code, str):
        return (None, None)
    return (code, NINFER_ERROR_ACTION.get(code))


def ninfer_error_sentence(body: str | dict | None, fallback: str) -> str:
    """What to show a person. Falls back to the engine's own words."""
    _code, action = classify_ninfer_error(body)
    text = _ACTION_TEXT.get(action or "")
    return text or fallback


# ── the timings line ─────────────────────────────────────────────────────────


def decode_rate_from_timings(timings: object) -> float | None:
    """The engine's OWN decode rate, or None.

    🔴 BETTER THAN THE ONE LiteTUI COMPUTES, AND THAT IS WHY IT IS REUSED RATHER
    THAN ADDED BESIDE IT. `TpsState.final` divides the server's token count by
    the CLIENT's wall clock, so queueing and admission are charged to the model:
    on a busy engine the turn reads slower than it ran. `predicted_per_second`
    is measured inside the decode loop (`ninfer/docs/serving.md:238`).

    Its docstring already says "settle to the exact figure the server reports" —
    this is a more exact figure arriving at a seam that was built for it.

    ⬜ ONE FUNCTION, SO THE STATUS LINE AND THE tok/s FIELD CANNOT DISAGREE.
    `format_timings` renders what this returns; nothing parses the object twice.
    """
    if not isinstance(timings, dict):
        return None
    rate = timings.get("predicted_per_second")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return float(rate) if rate > 0 else None


def format_timings(timings: object) -> str | None:
    """The llama.cpp-compatible `timings` object as one status line.

    🔴 THIS IS THE NUMBER RYAN HAS BEEN ASKING FOR ALL DAY — *"66toks is less
    than lmstudio etc"* / *"whole point is a toks improvement"*. The engine
    reports prompt and decode rates PER REQUEST (`ninfer/docs/serving.md:238`),
    so a TUI can show what the turn actually ran at instead of a number someone
    measured once.

    ⬜ RETURNS None RATHER THAN ZEROS when the fields are absent. A status line
    reading "0.0 tok/s" is a measurement; a missing one is the absence of a
    build that reports it.
    """
    if not isinstance(timings, dict):
        return None
    decode = decode_rate_from_timings(timings)
    prompt = timings.get("prompt_per_second")
    parts: list[str] = []
    if isinstance(prompt, (int, float)) and prompt > 0:
        parts.append(f"prompt {prompt:.0f} tok/s")
    if isinstance(decode, (int, float)) and decode > 0:
        parts.append(f"decode {decode:.1f} tok/s")
    return " · ".join(parts) if parts else None


#: The reasoning efforts LiteTUI offers for NInfer.
#:
#: The server protocol parses a broader OpenAI vocabulary, but the loaded chat
#: template is the authority that formats a request. The shipped Qwen3.8
#: artifact rejects `minimal`, `high`, and `max` at
#: `artifact:chat_template.jinja:55`, before tokenization.
#:
#: Ryan's 2026-09-21 contract is the template-safe set below. `none` is the
#: wire spelling; the shared thinking UI renders it as `off`.
NINFER_REASONING_LEVELS = ("none", "low", "medium", "xhigh")


# ── the backend ──────────────────────────────────────────────────────────────


class NInferBackend(_VramGate):
    """`ninfer-serve`, attached. One artifact per process, for the life of it.

    SHAPE: single-model by construction — there is no `/models/load`, no
    `/models/unload` and no router role, so every control-plane verb the other
    two backends implement is a refusal here. They are refusals that SAY WHY and
    name where the control actually lives, because a silent no-op would let the
    Model screen look like it changed something.
    """

    name = "ninfer"
    #: The name a HUMAN reads in the header. Each backend carries its own
    #: (T861) because the header used to pick it out of a hand-listed map whose
    #: default was the literal string "LM Studio" — so NInfer, and anything
    #: added after that line was written, was announced as LM Studio.
    label = "NInfer"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host: str | None = None
        self._model_id: str | None = None
        #: The engine WE started via /engine start (Ryan a-35456da0: "LiteTUI may
        #: start it"), else None = attached to one somebody else runs.
        self._owned: ninfer_engine.OwnedEngine | None = None
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
        so a cancelled spawn thread still blocks a stop. start_engine is NInfer's only
        load/start door (load() is frozen), so this covers new loads."""
        with self._lifecycle_lock:
            if self._stopping or self._starting:
                return False
            self._stopping = True
            return True

    def end_stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = False

    # -- discovery --------------------------------------------------------

    #: What `base_url()` answers when no engine can be found. A port nothing
    #: can listen on, so a request cannot silently reach the wrong server.
    DEAD_HOST = "http://127.0.0.1:0"

    def _resolve_host(self) -> str | None:
        """Explicit setting, then discovery. Re-asked every time on purpose.

        🔴 RESOLVED LAZILY BECAUSE THE ENGINE OUTLIVES NEITHER SIDE'S ORDER.
        The app is CONSTRUCTED before anything calls `ensure_running`, and the
        engine may be started after LiteTUI is already open. Caching the answer
        at `__init__` would pin "no engine" for the life of the process.
        """
        explicit = str(getattr(self._settings, "ninfer_host", "") or "").strip().rstrip("/")
        return explicit or self._host or discover_ninfer_host()

    def empty_state_hint(self) -> str:
        """What to tell the user when this backend has no models to offer.

        🔴 T860. The model commands used to hardcode "try /reconnect" and
        "/model first". With no engine registered, every one of those is a
        remedy this state has removed — and the two steps that DO work,
        `/model` to choose an artifact and `/engine start` to serve it, were
        named nowhere the user was looking.

        Only this backend defines the method. The others are read through
        `getattr` and keep today's words untouched, which is the difference
        between a fix and a find-and-replace.
        """
        return "/model to pick an artifact, then /engine start"

    def host(self) -> str:
        host = self._resolve_host()
        if host is None:
            raise BackendError(
                "no NInfer engine is registered — start it from LiteSuite's "
                "Model Hub, or set ninfer_host (LITETUI_NINFER_HOST), then "
                "/model to pick it up."
            )
        return host

    def base_url(self) -> str:
        """The OpenAI client's address — AND IT MUST NEVER RAISE.

        🔴 THIS IS WHY THE APP COULD NOT BOOT ON THIS BACKEND, and no unit arm
        could see it. `LiteTUI.__init__` builds its `AsyncOpenAI` client from
        `backend.base_url()` at CONSTRUCTION (`app.py:1381`), long before
        anything calls `ensure_running`. The first version raised there when no
        engine was registered, so selecting this backend with the engine down
        did not produce a message — it produced a traceback before the TUI
        existed.

            EVERY ARM CONSTRUCTED THE BACKEND DIRECTLY AND NONE BOOTED THE APP.
            The defect lived in the one line between the two, and driving a real
            turn is what found it.

        The other two backends cannot hit this: their host is a SETTING, present
        whether or not anything is listening. Ours is discovered, so absence is
        a state they never have.

        ⬜ A DEAD PORT, NOT A PLAUSIBLE ONE. Returning some default would let a
        request reach whatever happens to be on it; port 0 cannot be connected
        to, so the failure is immediate and belongs to no other server.
        `ensure_running` still runs before turns and gives the sentence that
        names where to start one.
        """
        return f"{self._resolve_host() or self.DEAD_HOST}/v1"

    @property
    def attached(self) -> bool:
        """True unless /engine start made the process ours. The ownership rule
        stays: the ONLY engine shutdown() will ever stop is one we started."""
        return self._owned is None

    async def ensure_running(self) -> str:
        return await asyncio.to_thread(self._ensure_running_sync)

    def _ensure_running_sync(self) -> str:
        if self._owned is not None and self._owned.alive and self._health(self._owned.host):
            self._host = self._owned.host
            return f"ok (engine started by LiteTUI, pid {getattr(self._owned.proc, 'pid', '?')})"
        # An explicit address wins over discovery: a hand-started ninfer-serve
        # with no LiteSuite around has nothing to register itself in.
        explicit = str(getattr(self._settings, "ninfer_host", "") or "").strip().rstrip("/")
        host = explicit or discover_ninfer_host()
        if host is None:
            raise BackendError(
                "no NInfer engine is registered — start it from LiteSuite's "
                "Model Hub (Settings → NInfer), or set ninfer_host "
                "(LITETUI_NINFER_HOST) to a ninfer-serve you started by hand, "
                "or /engine start to have LiteTUI start one (Ryan a-35456da0)."
            )
        if not self._health(host):
            raise BackendError(
                f"an NInfer engine is registered at {host} but is not answering "
                "/health — it may still be loading weights (about 8 seconds) or "
                "may have stopped."
            )
        self._host = host
        return "ok"

    @staticmethod
    def _health(host: str, timeout: float = 2.0) -> bool:
        """`GET /health`, which is UNAUTHENTICATED (`serving.md:68-70`)."""
        try:
            req = urllib.request.Request(f"{host}/health", headers={"User-Agent": "LiteTUI"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def shutdown(self) -> "object":
        """Stop the engine ONLY if /engine start made it ours; otherwise nothing.

        🔴 A shutdown that killed an engine LiteSuite (or Ryan, by hand) started
        would take down a process approved separately, that another client may be
        using — from a TUI closing a tab. Attached = leave it. Owned = ours to stop.
        """
        result = self.shutdown_owned()
        if not result.owned:         # attached / no owned engine: forget the host (nothing to race)
            with self._lifecycle_lock:
                self._host = None
        return result

    def shutdown_owned(self):
        """Stop our OWNED engine via the unified ninfer_engine.terminate_owned proof, under
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
        result = ninfer_engine.terminate_owned(owned)     # OUTSIDE the lock (bounded but slow)
        if not result.retained:
            with self._lifecycle_lock:
                if self._owned is owned:                  # same object -> clear owned + host together
                    self._owned = None
                    self._host = None
        return result

    # -- owning the engine (Ryan a-35456da0: "LiteTUI may start it") ----------

    async def start_engine(self, *, notice=None) -> str:
        """Spawn ninfer-serve under LiteTUI's VRAM gate; refuse if any engine is up.

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
        with self._lifecycle_lock:
            if self._stopping:
                raise BackendError("the NInfer engine is stopping — wait for it to finish, then /engine start.")
            if self._starting:
                raise BackendError("a NInfer engine start is already in progress.")
            if self._owned is not None:
                raise BackendError("an owned NInfer engine is already tracked — /engine stop it before starting another.")
            self._starting = True
        try:
            key = "ninfer-serve"

            def _spawn_and_publish():
                try:
                    owned = ninfer_engine.start(
                        self._settings, healthy=self._health, notice=notice)
                except ninfer_engine.EngineStartFailed as exc:
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

            async with self.vram_guard(key):
                if self._stopping:   # a stop claimed the backend after our entry check
                    raise BackendError("the NInfer engine is stopping — wait for it to finish, then /engine start.")
                # await_preparation runs the blocking spawn off-loop and JOINS the thread
                # on cancellation (a raw to_thread would orphan it), then re-raises.
                owned = await agent_preparation.await_preparation(_spawn_and_publish)
            return f"started ninfer-serve pid {getattr(owned.proc, 'pid', '?')} at {owned.host} ({owned.model_id})"
        finally:
            with self._lifecycle_lock:
                self._starting = False

    def stop_engine(self) -> str:
        if self._owned is None:
            # 🔴 NO HANDLE IS NOT THE SAME AS NOT OURS. `self._owned` answers
            # "did THIS PROCESS start it" and is lost on every restart; the
            # registry entry LiteTUI wrote answers "did LITETUI start it" and
            # carries the pid. Reading only the URL here is what told Ryan
            # (2026-09-18) his own engine "was not started by LiteTUI" while
            # the record beside it said `owner: "litetui"` — and left 10.7 GB
            # resident with no way to stop it from the app that spawned it.
            from litetui import ninfer_engine

            entry = ninfer_engine.registered_entry()
            pid = entry.get("pid") if entry else None
            is_litetui = (entry is not None and entry.get("owner") == "litetui"
                          and entry.get("kind") == "ninfer"
                          and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0)
            if is_litetui:
                host = entry.get("baseUrl")
                if ninfer_engine.stop_registered(entry):
                    # Currently unreachable (stop_registered fails closed); a future
                    # identity-verified path may confirm and act.
                    return f"stopped the LiteTUI-registered NInfer engine at {host} (pid {pid})."
                # FAIL CLOSED: we hold no live handle for this pid and cannot verify the
                # process's identity — a pid-only kill could hit a REUSED pid. Do not kill,
                # keep the record, and do NOT mislabel our own registered engine as external.
                return (f"a LiteTUI-registered NInfer engine is recorded at {host} (pid {pid}), "
                        f"but this session holds no handle for it and cannot safely verify that "
                        f"pid — a pid-only kill could hit a reused process. Stop it via its "
                        f"originating owner (LiteSuite's Model Hub) or your process manager; the "
                        f"record is preserved.")
            reg = entry.get("baseUrl") if entry else discover_ninfer_host()
            if reg:
                return f"the engine at {reg} was not started by LiteTUI — stop it where it was started (LiteSuite's Model Hub, or the shell that ran it)."
            return "no engine is running."
        host = self._owned.host
        result = self.shutdown()
        # The message must reflect the PROOF, not assume success: shutdown() returns a
        # TerminalShutdown and may retain on an unconfirmed exit.
        if getattr(result, "main_exited", False) and not getattr(result, "retained", True):
            if getattr(result, "tree", "unknown") == "confirmed":
                # 'confirmed' proves the job's ASSIGNED members drained — not that every
                # descendant is gone (a child spawned before job assignment is not a
                # member). So the wording stays scoped, never "the whole tree".
                return (f"stopped the engine LiteTUI started at {host} — its main process "
                        f"and assigned members are confirmed gone.")
            return (f"stopped the engine LiteTUI started at {host} — the main process "
                    f"exited, but child-process VRAM release is unconfirmed.")
        return (f"stop requested for the engine at {host}, but exit was NOT confirmed "
                f"(it may still be running) — try /engine stop again.")

    def engine_concurrency(self) -> int | None:
        """The `--max-concurrency` the RUNNING engine was started with, or None
        when it cannot be read. Owned: our own argv. Attached: the process table.
        The flag is startup-only and no route reports it, so the SETTING is not
        the answer — an engine somebody else started carries their flags."""
        if self._owned is not None:
            return ninfer_engine.concurrency_in(self._owned.args)
        host = self._resolve_host()
        try:
            port = int(str(host).rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return None
        argv = ninfer_engine.running_argv(port)
        return ninfer_engine.concurrency_in(argv) if argv else None

    def _capacity_note(self) -> str:
        n = self.engine_concurrency()
        if n is None:
            return " · concurrency unknown (no ninfer-serve on that port in the process table)"
        return (f" · concurrency {n} (--max-concurrency, a startup flag; lanes share "
                "the --max-context KV pool)")

    def engine_status(self) -> str:
        if self._owned is not None:
            alive = self._owned.alive and self._health(self._owned.host)
            return (f"LiteTUI-owned engine at {self._owned.host}: {'answering' if alive else 'NOT answering'}"
                    f"{self._capacity_note()} (log {self._owned.log_path})")
        explicit = str(getattr(self._settings, "ninfer_host", "") or "").strip().rstrip("/")
        if not explicit:
            # Same correction as `stop_engine`: a LiteTUI-owned entry must not
            # report as "attached", or status and stop disagree about who owns
            # the engine and the user is told to go stop it somewhere else.
            from litetui import ninfer_engine

            entry = ninfer_engine.registered_entry()
            if entry and entry.get("owner") == "litetui" and entry.get("pid"):
                host = entry["baseUrl"]
                return (f"LiteTUI-owned engine at {host} (pid {entry['pid']}, from the "
                        f"registry — this session did not start it): "
                        f"{'answering' if self._health(host) else 'NOT answering'}"
                        f"{self._capacity_note()} · /engine stop")
        reg = explicit or discover_ninfer_host()
        if reg is None:
            return "no engine registered — /engine start (LiteTUI starts one), or start it from LiteSuite's Model Hub."
        return (f"attached engine at {reg}: {'answering' if self._health(reg) else 'NOT answering'}"
                f"{self._capacity_note()}")

    async def list_models(self) -> list[ModelRow]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[ModelRow]:
        """The one model this process serves, from `/v1/models`.

        ⚠️ `max_model_len` IS THE FIELD, and it is the one LiteSuite read wrong
        for a week (T789: the parser read `max_context`, which is the name of the
        ENGINE OPTION and appears nowhere on this wire). Read from the emitter,
        `ninfer/src/serve/openai_common.cpp:188`.
        """
        body = self._get_json(f"{self.host()}/v1/models")
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise BackendError(
                f"the NInfer engine at {self._host} listed no models — it is up "
                "but serving nothing, which should not happen."
            )
        rows: list[ModelRow] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if not isinstance(key, str) or not key:
                continue
            # Resident by construction: this server loaded its artifact at
            # startup and serves it for life. `loaded` is not a guess here.
            rows.append(ModelRow(key=key, path=None, source="server", loaded=True))
        if rows:
            self._model_id = rows[0].key
        return rows

    async def model_info(self, key: str):
        return await asyncio.to_thread(self._model_info_sync, key)

    def _model_info_sync(self, key: str) -> tuple[int | None, str | None, bool] | None:
        """``(window, type, loaded)``, or None when this server does not serve it.

        🔴 A TUPLE, NOT A DICT, AND THAT SHAPE IS THE WHOLE CONTRACT.
        The first version of this returned a four-key dict. Every caller in the
        app unpacks three values -- ``app.py:4205``
        ``self.ctx_max, self.model_type, self.ctx_loaded = got``,
        ``app.py:4148`` ``cur, _typ, is_loaded = info``, ``app.py:3418``
        ``info[2]`` -- so a dict of four keys raised
        ``ValueError: too many values to unpack (expected 3)`` INSIDE the
        ``ctx`` worker, and that killed the app a moment after ``connect()``
        had listed the model successfully.

            THE CRASH DID NOT LOOK LIKE THIS DEFECT. It looked like
            ``"models": []``, because the only measurement anyone had taken was
            a ``gui.state`` read that raced ``connect()`` and then found a dead
            process. The empty list was the RACE; the app dying was this line.

        ⬜ NO TYPE, RATHER THAN A PLAUSIBLE ONE. ``/v1/models`` carries no
        ``type`` field -- there is no llm/vlm discriminator on this wire -- and
        the app reads that value only to refuse ``view_image`` with an LM
        Studio-shaped sentence (``app.py:5373``). Reporting ``"llm"`` here
        would be inventing a fact the server never stated in order to produce a
        message about a different program. None is what the engine said.

        ⬜ ``loaded`` IS TRUE BY CONSTRUCTION. One artifact per process,
        loaded at startup and served for life -- there is no cold row here, so
        the ceiling-vs-window trap the other two backends guard against cannot
        arise: ``max_model_len`` IS the live window.
        """
        body = self._get_json(f"{self.host()}/v1/models")
        for entry in body.get("data", []):
            if isinstance(entry, dict) and entry.get("id") == key:
                window = entry.get("max_model_len")
                return (window if isinstance(window, int) else None, None, True)
        return None

    def _get_json(self, url: str, timeout: float = 10.0) -> dict:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LiteTUI"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
            raise BackendError(ninfer_error_sentence(raw, f"NInfer returned {e.code}.")) from e
        except Exception as e:
            raise BackendError(f"NInfer at {self._host} did not answer: {e}") from e

    async def ensure_chat_ready(self, key: str | None) -> None:
        await asyncio.to_thread(self._chat_ready_sync, key)

    def _chat_ready_sync(self, key: str | None) -> None:
        """Can this model take a turn right now?

        ⬜ THE ANSWER IS ALMOST ALWAYS YES, AND FOR A DIFFERENT REASON THAN LM
        STUDIO'S. LM Studio says yes because it JIT-loads; this says yes because
        the artifact was loaded at startup and cannot be unloaded — there is no
        cold state to warm. What it CAN rule out is naming a model this process
        does not serve, which on a one-artifact server is decisive.
        """
        if not key:
            raise BackendError("no model is selected — /model to pick one before sending.")
        served = {row.key for row in self._list_sync()}
        if key not in served:
            only = ", ".join(sorted(served)) or "none"
            raise BackendError(
                f"{key!r} is not what this NInfer engine serves (it serves {only}). "
                "One artifact per process — start a different one from LiteSuite's Model Hub."
            )

    # -- capabilities -----------------------------------------------------

    def reasoning_levels(self, model: str | None = None) -> list[str]:
        """The template-safe thinking levels LiteTUI offers for NInfer.

        🔴 RYAN, 12:1x: *"agents have seem to try to rebuild every system for
        every backend over and again ... were writing the same code to do the
        same thing in a slightly different way over and over for each backend."*

        `thinking_capabilities()` asks `backend.reasoning_levels(model)` FIRST
        and only falls through to `name == "llamacpp"` / `name == "lmstudio"`
        branches when a backend does not answer. Those two branches exist
        because neither backend ever implemented this — not because the seam was
        missing. Implementing it is the whole of the thinking integration: no
        arm is added to `thinking_capabilities`, no `ninfer` name appears there,
        and the /thinking screen renders from what this returns.

        `_resolve_reasoning_effort` sends these values verbatim, maps the UI's
        `off` to the wire's `none`, and folds stale wider-vocabulary values from
        older conversations onto this set before they reach the template.

        ⬜ THE MODEL ARGUMENT IS ACCEPTED AND UNUSED, DELIBERATELY. One artifact
        per process: the model cannot differ from the one this engine serves, so
        a per-model answer would be the same answer with a false implication
        that it varies.
        """
        return list(NINFER_REASONING_LEVELS)

    def reload_hint(self, rec: dict | None = None) -> str:
        """What a human should do to bring this seat's model back (T806).

        🔴 THE DEFAULT WAS AN `lms load` LINE, WHICH RESTORES NOTHING HERE.
        `seat_guard._write_breadcrumb` writes a recovery note for the case where
        a resume failed and the agent's own brain is gone — and its own docstring
        says the instruction "must match the ENGINE the seat lives on". It
        branched on `name == "llamacpp"` and sent everything else to LM Studio's
        CLI, so an NInfer seat got a command for a program that has never heard
        of a `.ninfer` artifact.

            A FALLBACK IS A DECISION ABOUT EVERY BACKEND THAT DOES NOT HAVE A
            BRANCH, INCLUDING THE ONES THAT DO NOT EXIST YET.

        ⬜ DUCK-TYPED ON PURPOSE. `seat_guard` asks `getattr(backend,
        "reload_hint", None)`, so the other two backends keep their existing
        behaviour with no edit to `llm_backend.py` — the file Sentinel is adding
        the BACKENDS registry to this hour.
        """
        return (
            "start the NInfer engine from LiteSuite's Model Hub (Settings → NInfer); "
            "LiteTUI attaches to it and cannot start it itself"
        )

    # -- per-request, and the seat -----------------------------------------
    #
    # 🔴 EVERY METHOD BELOW IS CALLED WITHOUT A `hasattr` GUARD, so a
    # backend that omits one does not degrade -- it raises AttributeError from
    # inside a Textual worker and takes the turn (or the tool) with it. That is
    # how `request_overrides` was found: the FIRST turn ever driven through this
    # backend died at `app.py:5901` with
    # `'NInferBackend' object has no attribute 'request_overrides'`, after 57
    # arms had passed.
    #
    #     A MISSING METHOD IS NOT A MISSING FEATURE HERE. It is a crash, and the
    #     surface is discoverable: diff what the app calls on `backend.` against
    #     `dir(NInferBackend)` rather than waiting for each one to fire.

    def request_overrides(self, key: str | None) -> dict:
        """Global sampling defaults plus this model's Inference-tab overrides.

        ⬜ THE SIBLINGS' EXACT CALL, REUSED RATHER THAN RESTATED.
        `llm_backend.py:1480` and `:1732` are both `return
        _merged_overrides(self._settings, key)`; this engine speaks the same
        OpenAI-compatible request, so a third spelling of one rule would be the
        duplication Ryan named: *"were writing the same code to do the same
        thing in a slightly different way over and over for each backend."*
        """
        from litetui.llm_backend import _merged_overrides

        return _merged_overrides(self._settings, key)

    def loaded_models(self) -> list[str]:
        """Ids resident right now. Read-only; starts nothing.

        🔴 ITS ABSENCE CRASHED `/settings` ON THIS BACKEND.
        `model_residency.resident_models` prefers this method and falls back to
        `asyncio.run(list_models())`, which RAISES inside the Textual loop --
        so a backend without it makes the Settings dialog unopenable rather
        than slightly slower. llama.cpp and LM Studio both have it
        (`llm_backend.py:1584`), which is why nobody had met the fallback.

            THIS IS THE SAME CLASS AS THE FOUR METHODS IN 61e98a2, AND MY
            SURFACE ARM MISSED IT because its file list did not include
            `model_residency.py` or `plugins/settings_ui.py`. The arm's own
            docstring says it is scoped to what it reads; that scope is where
            this got through. The list is widened with this commit.

        ⬜ SYNC, AND THAT IS THE WHOLE POINT -- an async answer here is what
        the caller cannot use. One artifact per process, resident from startup,
        so the served id IS the resident set.
        """
        try:
            rows = self._list_sync()
        except BackendError:
            return []
        return [r.key for r in rows if r.loaded]

    def seat_snapshot(self, model_id: str) -> dict | None:
        """The seat's live load config, or None when this engine is not it.

        ⬜ RESIDENT OR ABSENT, NEVER "IDLE BUT UNLOADED". One artifact per
        process, loaded at startup and served for life -- so "is it loaded" and
        "is this the model this engine serves" are THE SAME QUESTION here, and
        `model_info` already answers it.
        """
        try:
            info = self._model_info_sync(model_id)
        except BackendError:
            return None
        if info is None:
            return None
        window, _type, _loaded = info
        return {
            "identifier": model_id,
            "context": window,
            # `--max-concurrency` of the RUNNING process (T892): the argv we
            # spawned, or the process table for an attached engine; None only
            # when neither can be read. Never the setting — that is a wish.
            "parallel": self.engine_concurrency(),
            "status": "idle",
            "queued": 0,
        }

    def seat_suspend(self, rec: dict) -> str | None:
        """Refuse by name. THE PROCESS IS THE MODEL -- there is no unload.

        ⬜ THE REFUSAL IS THE DESIGNED PATH, NOT A FAILURE.
        `seat_guard.suspend` returns this sentence to the caller
        (`studio_tool.py:273`, `listen_tool.py:424`), which then decides whether
        to generate without freeing the seat -- exactly what an ATTACHED llama
        server does today (`llm_backend.py:1455`). Raising instead, or returning
        None as though VRAM had been freed, would both be lies: the second one
        would let an image job start against a full card.
        """
        return (
            "suspend unsupported: ninfer-serve holds its artifact for the life "
            "of the process — stop the engine itself (LiteSuite's Model Hub) to "
            "free that VRAM"
        )

    def seat_resume(self, rec: dict) -> str | None:
        """None when the engine is still serving it; a sentence when it is not.

        ⬜ REACHABLE EVEN THOUGH `seat_suspend` ALWAYS REFUSES. `resume` runs
        from the breadcrumb path too, after a crash nobody here observed -- so
        "nothing was suspended" is an assumption, not a fact, and this asks.
        """
        if self.seat_snapshot(rec.get("identifier") or "") is not None:
            return None
        return (
            "the NInfer engine is no longer serving "
            f"{rec.get('identifier')!r} — " + self.reload_hint(rec)
        )

    # -- control, which this engine does not have -------------------------

    # `notice` is accepted and NEVER called (T873): these three refuse without
    # doing anything, so there is no work to announce. Taking the parameter
    # keeps the signature uniform for a caller that hands one to whichever
    # backend answers — a refusal path raising TypeError on the way to its own
    # message is the defect class this card exists for.

    async def load(self, key: str, *, ctx: int | None = None, notice=None) -> None:
        raise BackendError(self._frozen_reason("load a different model"))

    async def unload(self, key: str) -> None:
        raise BackendError(self._frozen_reason("unload the model"))

    async def apply_load_settings(self, key: str, cfg: dict, *, notice=None) -> None:
        raise BackendError(self._frozen_reason("change the load settings"))

    @staticmethod
    def _frozen_reason(what: str) -> str:
        """Why the control plane is empty, in words a user can act on.

        🔴 A NAMED REFUSAL, NOT A SILENT NO-OP. Every knob worth changing —
        context ceiling, KV dtype, speculative backend, vision, concurrency — is
        a STARTUP flag, and `ninfer-serve` cannot widen any of them afterwards
        (`ninfer/docs/serving.md:49-50`). A Model screen that accepted the change
        and did nothing would be the same defect as a disabled button that says
        nothing: the user would believe it worked.
        """
        return (
            f"NInfer cannot {what} while it is running — every setting is fixed at "
            "startup and the server serves one artifact for its life. Change it in "
            "LiteSuite's Model Hub and restart the engine there."
        )
