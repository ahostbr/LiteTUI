"""Strata as a LiteTUI backend — Qwen3.8-Flash-Next on one GPU plus system RAM.

Engine: https://github.com/Niko1221/Strata (MIT). LiteSuite's Model Hub installs it and
downloads the model; `strata_engine.py` is the launcher and carries the memory numbers.

THE SAME SHAPE AS NINFER, AND THE SAME CODE WHERE IT IS THE SAME THING: one model per
process, fixed when the engine starts, so `/model` picks the prepared model the next
`/engine start` serves and every load/unload verb is a refusal that says why. Start,
stop and the ownership proof are `engine_lifecycle.OwnedEngineLifecycle`, shared with
`NInferBackend`.

WHAT DIFFERS:
  * NO REGISTRY. Strata's port is a setting of the install (8080), so the address is
    `strata_host` or the chosen config's port — nothing is discovered.
  * THE SERVER CAN BE UP WITH THE MODEL UNLOADED, and its next request would reload it
    (tens of GB). `/health` says which, and an unloaded server is treated as NOT ready:
    a chat turn must never be what puts a model back in memory.
  * NO 5090 GATE. Strata runs on RTX 20-series and newer and on AMD; what it needs is
    RAM, which `strata_engine.refuse_reason` checks at start.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from . import strata_engine
from .engine_lifecycle import OwnedEngineLifecycle
from .llm_backend import BackendError, ModelRow

#: `reasoning_effort` as Strata's server accepts it (serve/server.py: "none, low, medium or high").
STRATA_REASONING_LEVELS = ("none", "low", "medium", "high")


class StrataBackend(OwnedEngineLifecycle):
    name = "strata"
    label = "Strata"
    _process_name = "Strata"
    #: Said by `/engine start` immediately before the spawn.
    start_notice = ("Starting Strata — it loads tens of GB into RAM, which takes a minute or more, "
                    "and the PC can stall while it does…")

    @property
    def _engine(self):
        """The launcher the shared start/stop lifecycle drives (engine_lifecycle.py)."""
        return strata_engine

    def __init__(self, settings) -> None:
        self._settings = settings
        self._init_lifecycle()

    # -- where it is ---------------------------------------------------------

    def host(self) -> str:
        """Owned engine first, then the setting or the chosen config's port. Re-asked every
        time: /model can change the config, and the engine may start after the app did."""
        return self._host or strata_engine.default_host(self._settings)

    def base_url(self) -> str:
        return f"{self.host()}/v1"

    def empty_state_hint(self) -> str:
        return "/model to pick a prepared Strata model, then /engine start"

    def reload_hint(self, rec: dict | None = None) -> str:
        return "/engine start on the Strata backend (LiteTUI starts the server for the chosen model)"

    # -- health --------------------------------------------------------------

    @staticmethod
    def _health_state(host: str, timeout: float = 2.0) -> str | None:
        """'loaded' | 'unloaded' | None (nothing answering) from `GET /health`."""
        try:
            req = urllib.request.Request(f"{host}/health", headers={"User-Agent": "LiteTUI"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
        except Exception:  # noqa: BLE001 - refused, timed out, not JSON: nothing usable is there
            return None
        if not isinstance(body, dict):
            return None
        return "unloaded" if body.get("loaded") is False else "loaded"

    @classmethod
    def _health(cls, host: str, timeout: float = 2.0) -> bool:
        """Ready to take a turn: answering AND holding its model."""
        return cls._health_state(host, timeout) == "loaded"

    async def ensure_running(self) -> str:
        return await asyncio.to_thread(self._ensure_running_sync)

    def _ensure_running_sync(self) -> str:
        host = self.host()
        state = self._health_state(host)
        if state == "loaded":
            if self._owned is not None and self._owned.alive:
                return f"ok (engine started by LiteTUI, pid {getattr(self._owned.proc, 'pid', '?')})"
            return "ok"
        if state == "unloaded":
            raise BackendError(
                f"the Strata server at {host} is up but has unloaded its model, and a request would "
                "load it again (tens of GB of RAM). Load it from that server's own page, or stop it "
                "and /engine start.")
        raise BackendError(
            f"no Strata server is answering at {host} — /engine start to load the chosen model, or "
            f"start it yourself and set strata_host. Install: LiteSuite's Model Hub (Strata), "
            f"or {strata_engine.STRATA_REPO_URL}.")

    # -- stop / status (start is OwnedEngineLifecycle.start_engine) ------------

    def stop_engine(self) -> str:
        if self._owned is None:
            host = self.host()
            if self._health_state(host) is not None:
                return (f"the Strata server at {host} was not started by this LiteTUI — stop it where "
                        "it was started (its window, or the app that ran it).")
            return "no engine is running."
        host = self._owned.host
        result = self.shutdown()
        if getattr(result, "main_exited", False) and not getattr(result, "retained", True):
            return f"stopped the Strata server LiteTUI started at {host}."
        return (f"stop requested for the Strata server at {host}, but exit was NOT confirmed "
                "(it may still be running) — try /engine stop again.")

    def engine_status(self) -> str:
        host = self.host()
        said = {"loaded": "answering, model loaded", "unloaded": "answering, model UNLOADED",
                None: "NOT answering"}[self._health_state(host)]
        if self._owned is not None:
            return f"LiteTUI-owned Strata server at {host}: {said} (log {self._owned.log_path})"
        if said == "NOT answering":
            path = strata_engine.strata_config(self._settings)
            chosen = path.stem.removeprefix("strata-") if path else "none chosen — /model"
            return f"no Strata server at {host}. Model for the next /engine start: {chosen}."
        return f"attached Strata server at {host}: {said}"

    # -- models --------------------------------------------------------------

    def _get_json(self, url: str, timeout: float = 10.0) -> dict:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LiteTUI"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise BackendError(f"Strata returned {e.code} for {url}.") from e
        except Exception as e:
            raise BackendError(
                f"no Strata server is answering at {self.host()} — /engine start, or check strata_host "
                f"({type(e).__name__}).") from e

    def _entries(self) -> list[dict]:
        data = self._get_json(f"{self.host()}/v1/models").get("data")
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]] \
            if isinstance(data, list) else []

    @staticmethod
    def _loaded(entry: dict) -> bool:
        status = entry.get("status")
        return not isinstance(status, dict) or status.get("value", "loaded") == "loaded"

    def _list_sync(self) -> list[ModelRow]:
        return [ModelRow(key=e["id"], path=None, source="server", loaded=self._loaded(e))
                for e in self._entries()]

    async def list_models(self) -> list[ModelRow]:
        return await asyncio.to_thread(self._list_sync)

    def loaded_models(self) -> list[str]:
        """Sync on purpose (`model_residency.resident_models` calls it inside the Textual loop)."""
        try:
            return [r.key for r in self._list_sync() if r.loaded]
        except BackendError:
            return []

    def subagent_model_states(self) -> dict[str, str]:
        """What the server SAYS, for admission. Unlike `_loaded`, a row without a
        status value is never read as loaded here: it is "unknown"."""
        states = {}
        for entry in self._entries():
            status = entry.get("status")
            value = status.get("value") if isinstance(status, dict) else None
            states[entry["id"]] = value if isinstance(value, str) else "unknown"
        return states

    def _model_info_sync(self, key: str) -> tuple[int | None, str | None, bool] | None:
        """``(window, type, loaded)`` — the three-tuple every caller unpacks.

        The window is `meta.n_ctx`, the `--max-context` the engine was started with. The type
        is what the server states: a model without "image" among its input modalities is 'llm',
        which is what makes `view_image` refuse up front instead of sending a picture the engine
        was started without."""
        for entry in self._entries():
            if entry["id"] != key:
                continue
            window = (entry.get("meta") or {}).get("n_ctx")
            modalities = (entry.get("architecture") or {}).get("input_modalities") or []
            return (window if isinstance(window, int) else None,
                    "vlm" if "image" in modalities else "llm", self._loaded(entry))
        return None

    async def model_info(self, key: str):
        return await asyncio.to_thread(self._model_info_sync, key)

    async def ensure_chat_ready(self, key: str | None) -> None:
        await asyncio.to_thread(self._chat_ready_sync, key)

    def _chat_ready_sync(self, key: str | None) -> None:
        if not key:
            raise BackendError("no model is selected — /model to pick one before sending.")
        rows = {r.key: r for r in self._list_sync()}
        if key not in rows:
            only = ", ".join(sorted(rows)) or "none"
            raise BackendError(
                f"{key!r} is not what this Strata server serves (it serves {only}). One model per "
                "process — /model to choose another, then /engine stop and /engine start.")
        if not rows[key].loaded:
            raise BackendError(
                "the Strata server has unloaded its model; sending would load it again (tens of GB "
                "of RAM). Load it from the server's own page, or /engine stop then /engine start.")

    # -- capabilities and per-request ----------------------------------------

    def reasoning_levels(self, model: str | None = None) -> list[str]:
        return list(STRATA_REASONING_LEVELS)

    def request_overrides(self, key: str | None) -> dict:
        from litetui.llm_backend import _merged_overrides

        return _merged_overrides(self._settings, key)

    # -- the seat --------------------------------------------------------------

    def seat_snapshot(self, model_id: str) -> dict | None:
        try:
            info = self._model_info_sync(model_id)
        except BackendError:
            return None
        if info is None or not info[2]:
            return None
        return {"identifier": model_id, "context": info[0], "parallel": 1, "status": "idle", "queued": 0}

    def seat_suspend(self, rec: dict) -> str | None:
        return ("suspend unsupported: a Strata server holds its model for the life of the process — "
                "/engine stop frees the RAM and VRAM")

    def seat_resume(self, rec: dict) -> str | None:
        if self.seat_snapshot(rec.get("identifier") or "") is not None:
            return None
        return f"the Strata server is no longer serving {rec.get('identifier')!r} — " + self.reload_hint(rec)

    # -- control, which this engine does not have ------------------------------

    async def load(self, key: str, *, ctx: int | None = None, notice=None) -> None:
        raise BackendError(self._frozen_reason("load a different model"))

    async def unload(self, key: str) -> None:
        raise BackendError(self._frozen_reason("unload the model"))

    async def apply_load_settings(self, key: str, cfg: dict, *, notice=None) -> None:
        raise BackendError(self._frozen_reason("change the load settings"))

    @staticmethod
    def _frozen_reason(what: str) -> str:
        return (f"Strata cannot {what} while it is running — the model and its context are fixed when "
                "the server starts. /model picks the model and strata_max_context (in /settings) the "
                "context; then /engine stop and /engine start.")
