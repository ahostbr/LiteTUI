"""Xolotl as a LiteTUI backend — DeepSeek-V4.1-Flash (552B MoE) on one GPU, a pinned RAM
arena and NVMe.

Engine: https://github.com/Indras-Mirror/Xolotl (MIT), a Strata fork. THE USER, 2026-10-22:
*"i want litetui to support whatever endpoint it uses ... and list it as backend just like
everything else."*

WHAT IT IS: Xolotl serves through Strata's own server (`serve/server.py`, wrapped by
`serve_ds41.py` for DeepSeek-V4.1), so the endpoint is Strata's — OpenAI `/v1` (what this
backend sends turns to, like every local backend) and Anthropic `/v1/messages`, `/health`
saying whether the model is loaded, `/v1/models`. This backend IS `StrataBackend`: health,
model rows, the unloaded-is-not-ready rule, the frozen control plane and the shared engine
lifecycle are inherited, not copied. What differs is the launcher (`xolotl_engine`: where the
checkout is, which serve script, `--ctx`, the arena memory gate), the settings (`xolotl_*`),
the name, and the server's reasoning levels.
"""
from __future__ import annotations

from . import xolotl_engine
from .strata_backend import StrataBackend

#: `reasoning_effort` as serve_ds41.py accepts it (its EFFORT table: none, low = 50,
#: medium = 62, high = xhigh = 75, max = 100; anything else is a 400).
XOLOTL_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")


class XolotlBackend(StrataBackend):
    name = "xolotl"
    label = "Xolotl"
    _process_name = "Xolotl"
    _setting_prefix = "xolotl"
    _install_where = (f"LiteSuite's Model Hub (Xolotl), or {xolotl_engine.XOLOTL_REPO_URL} "
                      "(docs/WINDOWS.md), then xolotl_root in /settings")
    start_notice = ("Starting Xolotl — it pins its expert arena (tens of GB) in RAM and fills the GPU, "
                    "which takes a minute or more, and the PC can stall while it does…")

    @property
    def _engine(self):
        return xolotl_engine

    def reasoning_levels(self, model: str | None = None) -> list[str]:
        return list(XOLOTL_REASONING_LEVELS)
