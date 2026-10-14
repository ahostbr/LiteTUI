"""Loading model weights must never be a side effect.

WHAT HAPPENED. `_apply_context_length()` shells out to
`lms load <model> --context-length N`. It was wired into the CONNECT path, so
every app boot loaded a model — including every test that constructs LiteTUI.
Running the suite repeatedly, or several boots at once, loads several 27B models
concurrently and can OOM the machine.

Loading weights is expensive, slow, and DESTRUCTIVE to whatever is already
resident. It must follow an explicit human act and nothing else.

The guard is behavioural, not a grep: it constructs the app and drives connect
with `lms` replaced by a recorder, then asserts the recorder was never called.
A source scan would pass the moment someone reintroduced the call through a
helper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _owned_seat import owned_app

from litetui import app as app_mod
from litetui.settings import Settings


class _Recorder:
    """Record model loads; leave unrelated process queries untouched."""

    def __init__(self, run) -> None:
        self.calls: list[list[str]] = []
        self._run = run

    def __call__(self, cmd, **kw):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        if not (len(argv) >= 2 and Path(str(argv[0])).stem.lower() == "lms" and argv[1] == "load"):
            # A load spy must not fabricate success for nvidia-smi or other tools.
            return self._run(cmd, **kw)
        self.calls.append(argv)

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    @property
    def load_calls(self) -> list:
        argv_loads = [
            c for c in self.calls
            if len(c) >= 2 and Path(str(c[0])).stem.lower() == "lms" and c[1] == "load"
        ]
        return argv_loads + self.backend_loads


@pytest.fixture
def recorder(monkeypatch):
    """Watches BOTH load channels: the legacy `lms load` argv through
    ttyguard, and the dual-backend seam (backend.load). "nothing loads
    unless the user asked" must hold whichever road a load takes."""
    from litetui import gpu_gate, llm_backend

    gpu_gate.is_rtx_5090.cache_clear()
    r = _Recorder(app_mod.ttyguard.run)
    r.backend_loads = []
    monkeypatch.setattr(app_mod.ttyguard, "run", r)

    async def _load(self, key, *, ctx=None):
        r.backend_loads.append((key, ctx))

    monkeypatch.setattr(llm_backend.LMStudioBackend, "load", _load)
    monkeypatch.setattr(llm_backend.LlamaCppBackend, "load", _load)
    try:
        yield r
    finally:
        gpu_gate.is_rtx_5090.cache_clear()


def _app_with_a_context_preference():
    a = owned_app(initial_backend="lmstudio", initial_model="qwen/qwen3.8-27b")
    a._connect = lambda: None  # startup discovery is outside this test
    # The exact real-world configuration that caused it: a saved default model
    # and a saved context length.
    a.settings = Settings(
        default_model="qwen/qwen3.8-27b",
        default_context_length=120000,
    )
    a.model_id = "qwen/qwen3.8-27b"
    return a


@pytest.mark.asyncio
async def test_booting_the_app_loads_no_model(recorder):
    a = _app_with_a_context_preference()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    async with a.run_test() as pilot:
        await pilot.pause()
    assert recorder.load_calls == [], (
        f"booting the app shelled out to lms load: {recorder.load_calls}"
    )


@pytest.mark.asyncio
async def test_connecting_loads_no_model(recorder):
    """The regression itself: connect used to call _apply_context_length()."""
    a = _app_with_a_context_preference()
    a._fetch_ctx_window = lambda: None

    from litetui.llm_backend import ModelRow

    discovered = []
    async def list_models():
        discovered.append(a.backend.name)
        return [ModelRow(key="qwen/qwen3.8-27b", path=None, source="server", loaded=True)]

    a.backend.list_models = list_models
    a._probe_thinking = lambda: None  # thinking discovery is unrelated to model loading
    async with a.run_test() as pilot:
        # Startup uses the suppressed private alias; drive its real public implementation.
        connection = a.connect()
        await connection.wait()
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert discovered == ["lmstudio"]
    assert a._gui_connection_success and a._connect_settled
    assert a.available_models == ["qwen/qwen3.8-27b"]
    assert recorder.load_calls == [], (
        f"connecting shelled out to lms load: {recorder.load_calls}"
    )


@pytest.mark.asyncio
async def test_an_explicit_settings_change_IS_allowed_to_load(recorder):
    """The negative control.

    Without this the suite would pass just as well if the feature were deleted,
    and "nothing ever loads" is not the requirement — "nothing loads UNLESS THE
    USER ASKED" is.
    """
    a = _app_with_a_context_preference()
    a._fetch_ctx_window = lambda: None
    a._system = lambda *_a, **_k: None
    async with a.run_test() as pilot:
        changed = Settings(
            default_model="qwen/qwen3.8-27b",
            default_context_length=64000,  # DIFFERENT — an explicit change
        )
        a._on_settings_saved(changed)
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls, "an explicit context-length change loaded nothing"
    assert recorder.load_calls[0] == ("qwen/qwen3.8-27b", 64000)


@pytest.mark.asyncio
async def test_saving_settings_without_changing_the_length_loads_nothing(recorder):
    """Re-saving the same value must not move resident weights."""
    a = _app_with_a_context_preference()
    a._fetch_ctx_window = lambda: None
    a._system = lambda *_a, **_k: None
    async with a.run_test() as pilot:
        same = Settings(
            default_model="qwen/qwen3.8-27b",
            default_context_length=120000,  # unchanged
        )
        a._on_settings_saved(same)
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls == [], (
        f"an unchanged setting reloaded the model: {recorder.load_calls}"
    )
