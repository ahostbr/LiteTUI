"""Source contracts required by native Rust model/thinking controls; no provider."""
from __future__ import annotations

import asyncio
from inspect import unwrap
from types import SimpleNamespace as NS

import pytest

from litetui import gui_rpc, thinking_capabilities
from litetui.app import LiteTUI


@pytest.mark.parametrize("level", [None, "low", "high"])
def test_current_capability_read_reports_actual_selection(level):
    app = NS(backend=NS(name="codex", reasoning_levels=lambda _: ["low", "high"]),
             model_id="model-A", thinking_level=level)
    result = gui_rpc.dispatch(app, {"type": "gui.models.capabilities"})
    assert result["thinking"]["level"] == level
    assert result["thinking"]["levels"] == ["default", "low", "high"]


@pytest.mark.parametrize("level", ["low", "default"])
def test_failed_owned_thinking_set_does_not_change_runtime_override(level):
    class Rejecting:
        backend = NS(name="codex", reasoning_levels=lambda _: ["low", "high"])
        model_id = "model-A"
        settings = NS(model_infer_overrides={"model-A": {"reasoning_effort": "high"}})

        @property
        def thinking_level(self):
            return "high"

        @thinking_level.setter
        def thinking_level(self, _value):
            raise OSError("controlled owned-write failure")

    app = Rejecting()
    with pytest.raises(OSError, match="owned-write"):
        thinking_capabilities.set_thinking(app, level)
    assert app.settings.model_infer_overrides["model-A"]["reasoning_effort"] == "high"
    assert app.thinking_level == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "backend", "none"])
async def test_context_response_retains_its_request_model_and_backend(change):
    entered = asyncio.Event()
    release = asyncio.Event()
    refreshed = []

    async def model_info(model):
        assert model == "model-A"
        entered.set()
        await release.wait()
        return 64000, "text", True

    app = NS(model_id="model-A", backend=NS(model_info=model_info),
             ctx_max=8000, model_type="old", ctx_loaded=False,
             _refresh_ctx_label=lambda: refreshed.append(True))
    worker = asyncio.create_task(unwrap(LiteTUI.fetch_context_window)(app))
    await entered.wait()
    if change == "model":
        app.model_id = "model-B"
    elif change == "backend":
        app.backend = NS(model_info=model_info)
    release.set()
    await worker
    if change == "none":
        assert (app.ctx_max, app.model_type, app.ctx_loaded) == (64000, "text", True)
        assert refreshed == [True]
    else:
        assert (app.ctx_max, app.model_type, app.ctx_loaded) == (8000, "old", False)
        assert refreshed == []
