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
    _START_NOTICE = ("Starting Basalt — it fills the GPU and pins tens of GB of experts in RAM, which takes "
                     "most of a minute, and the PC can stall while it does…")

    @property
    def _engine(self):
        return basalt_engine

    @property
    def start_notice(self) -> str:
        """Said by `/engine start` immediately before the spawn. Names any output-changing setting
        the free-text field turned on: Basalt prints that warning into a log nobody is reading."""
        lossy = basalt_engine.lossy_settings(self._settings)
        if not lossy:
            return self._START_NOTICE
        return (f"{self._START_NOTICE} WARNING: {', '.join(lossy)} is on in Basalt's extra settings: "
                "faster, but it changes what the model writes (Basalt calls these lossy).")

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

    def image_input_off_note(self, settings=None) -> str | None:
        """The first line `view_image` gives a model when this server says it takes text only but
        the chosen pack carries its image projector, or None when the pack has none (then "this
        model cannot see images" is simply true). Ryan, 2026-11-20: his server had been started
        with images off and the report read as a broken tool; it has to say what to do."""
        settings = self._settings if settings is None else settings
        pack = basalt_engine.basalt_pack(settings)
        if pack is None or not basalt_engine.pack_has_vision(pack):
            return None
        if basalt_engine.effective_vision(settings, pack) == "off":
            return ("[view_image: image input is switched off for Basalt (Settings > Basalt > image input), so "
                    "this is a measurement report (colours, shapes, positions), not the picture. The pack has its "
                    "image projector: set image input to Automatic or dynamic, then /engine stop and /engine start.]\n")
        return ("[view_image: this Basalt server was started with image input off, so this is a measurement report "
                "(colours, shapes, positions), not the picture. The pack has its image projector and a new start "
                "turns image input on: /engine stop, then /engine start.]\n")

    def reasoning_levels(self, model: str | None = None) -> list[str]:
        return list(BASALT_REASONING_LEVELS)
