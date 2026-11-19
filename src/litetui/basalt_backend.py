"""Basalt as a LiteTUI backend — Qwen3.8-Flash-Next on an RTX 50 card plus system RAM.

Engine: https://github.com/jesdga95/basalt (MIT), a Strata fork for NVIDIA Blackwell. RYAN,
2026-11-19: *"makes sure everything loads and acts exactly like any other backend and model in
Litetui"*.

WHAT IT IS: Basalt's server grew from Strata's `serve/`, and the endpoint LiteTUI uses is the
same — OpenAI `/v1`, `/health` saying whether the model is loaded, `/v1/models` with a status,
`POST /v1/load` and `/v1/unload`. This backend IS `StrataBackend`: health, model rows, the
unloaded-is-not-ready rule, freeing the card for a GPU job, the frozen control plane and the
shared engine lifecycle are inherited, not copied. What differs is the launcher (`basalt_engine`:
a model is one `.basalt` file and LiteTUI writes the run config), how the server names itself
(`"service": "basalt"`), the settings (`basalt_*`), the name, and the reasoning levels.
"""
from __future__ import annotations

from . import basalt_engine
from .strata_backend import StrataBackend

#: `reasoning_effort` as Basalt's server accepts it (serve/config.py --reasoning-effort choices).
BASALT_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")


class BasaltBackend(StrataBackend):
    name = "basalt"
    label = "Basalt"
    _process_name = "Basalt"
    _setting_prefix = "basalt"
    _install_where = (f"a release or a build from {basalt_engine.BASALT_REPO_URL} "
                      "(RTX 50 cards, CUDA 13), then basalt_root in /settings")
    start_notice = ("Starting Basalt — it fills the GPU and pins tens of GB of experts in RAM, which takes "
                    "most of a minute, and the PC can stall while it does…")

    @property
    def _engine(self):
        return basalt_engine

    # -- who answers -----------------------------------------------------------

    @staticmethod
    def _health_state(host: str, timeout: float = 2.0) -> str | None:
        """'loaded' | 'unloaded' | None from `GET /health`. A server that does not say it is
        Basalt is None, as if nothing were there. A server still loading its weights says
        `loaded: false` and is 'unloaded': not ready for a turn."""
        kind, body, _said = basalt_engine.probe(host, timeout)
        if kind != "basalt" or not isinstance(body, dict):
            return None
        return "unloaded" if body.get("loaded") is False else "loaded"

    def _foreign_note(self, host: str) -> str:
        if self._starting or (self._owned is not None and self._owned.alive):
            return ""
        clause = basalt_engine.foreign_clause(host, self.label)
        return f" ({clause})" if clause else ""

    def empty_state_hint(self) -> str:
        return "/model to pick a Basalt pack, then /engine start"

    # -- capabilities ------------------------------------------------------------

    def reasoning_levels(self, model: str | None = None) -> list[str]:
        return list(BASALT_REASONING_LEVELS)
