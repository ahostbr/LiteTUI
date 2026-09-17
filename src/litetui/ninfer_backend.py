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
import urllib.error
import urllib.request
from pathlib import Path

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
    decode = timings.get("predicted_per_second")
    prompt = timings.get("prompt_per_second")
    parts: list[str] = []
    if isinstance(prompt, (int, float)) and prompt > 0:
        parts.append(f"prompt {prompt:.0f} tok/s")
    if isinstance(decode, (int, float)) and decode > 0:
        parts.append(f"decode {decode:.1f} tok/s")
    return " · ".join(parts) if parts else None


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

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host: str | None = None
        self._model_id: str | None = None

    # -- discovery --------------------------------------------------------

    def host(self) -> str:
        if self._host is None:
            raise BackendError(
                "no NInfer engine is registered — start it from LiteSuite's "
                "Model Hub, then /model to pick it up."
            )
        return self._host

    def base_url(self) -> str:
        return f"{self.host()}/v1"

    @property
    def attached(self) -> bool:
        # 🔴 ALWAYS. See the module docstring: we never spawn, so the process is
        # never ours, so there is no state in which this is False.
        return True

    async def ensure_running(self) -> str:
        return await asyncio.to_thread(self._ensure_running_sync)

    def _ensure_running_sync(self) -> str:
        # An explicit address wins over discovery: a hand-started ninfer-serve
        # with no LiteSuite around has nothing to register itself in.
        explicit = str(getattr(self._settings, "ninfer_host", "") or "").strip().rstrip("/")
        host = explicit or discover_ninfer_host()
        if host is None:
            raise BackendError(
                "no NInfer engine is registered — start it from LiteSuite's "
                "Model Hub (Settings → NInfer), or set ninfer_host "
                "(LITETUI_NINFER_HOST) to a ninfer-serve you started by hand, "
                "then try again. LiteTUI attaches to that engine and never "
                "starts one itself."
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

    def shutdown(self) -> None:
        """Nothing to stop.

        🔴 DELIBERATELY EMPTY, AND THE COMMENT IS THE POINT. A `shutdown` that
        killed the engine would take down a process LiteSuite owns, that Ryan
        approved separately, and that another client may be using — from a TUI
        closing a tab.
        """
        self._host = None

    # -- read -------------------------------------------------------------

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

    def _model_info_sync(self, key: str) -> dict:
        body = self._get_json(f"{self.host()}/v1/models")
        for entry in body.get("data", []):
            if isinstance(entry, dict) and entry.get("id") == key:
                window = entry.get("max_model_len")
                return {
                    "id": key,
                    "loaded": True,
                    # The LIVE window, named as such. LiteSuite's own trim reads
                    # the same field for the same reason.
                    "context_length": window if isinstance(window, int) else None,
                    "max_context_length": window if isinstance(window, int) else None,
                }
        raise BackendError(f"{key!r} is not the model this NInfer engine serves.")

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

    # -- control, which this engine does not have -------------------------

    async def load(self, key: str, *, ctx: int | None = None) -> None:
        raise BackendError(self._frozen_reason("load a different model"))

    async def unload(self, key: str) -> None:
        raise BackendError(self._frozen_reason("unload the model"))

    async def apply_load_settings(self, key: str, cfg: dict) -> None:
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
