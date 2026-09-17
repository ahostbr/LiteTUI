"""`resident_models` must answer from inside the event loop (T816-adjacent).

🔴 RYAN TYPED `/settings` AND GOT A TRACEBACK INSTEAD OF A DIALOG.
`settings_ui.py:60` calls `model_residency.resident_models(app)` from a command
handler, i.e. INSIDE Textual's running loop, and the `list_models` branch was
`asyncio.run(...)` — which raises `RuntimeError: asyncio.run() cannot be called
from a running event loop`. The dialog never opened.

    A BRANCH THAT ONLY WORKS OFF THE EVENT LOOP CANNOT BE REACHED FROM THE UI.
    It was reachable only by a backend with NO `loaded_models`, so the three
    backends that have one never met it, and no arm called this from inside a
    loop.

The fallback was correct the whole time and already held the answer: Ryan's own
traceback shows `rows = {'qwen3_6_35b_a3b': ModelRow(..., loaded=True)}` in the
locals of the frame that raised.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from litetui import model_residency


class _Row:
    def __init__(self, key, loaded):
        self.key, self.loaded = key, loaded


def _app_without_loaded_models():
    """A backend shaped like NInfer's was: async `list_models`, no `loaded_models`."""
    async def list_models():
        return [_Row("served-model", True)]

    return SimpleNamespace(
        backend=SimpleNamespace(list_models=list_models, remote=False),
        model_rows={"served-model": _Row("served-model", True)},
    )


def test_it_answers_from_inside_a_running_loop_instead_of_raising():
    """🔴 THE ARM FOR RYAN'S CRASH. The call site is a command handler, so the
    only honest reproduction is from inside a loop — which is exactly the
    condition no previous arm created."""
    async def inside_the_loop():
        return model_residency.resident_models(_app_without_loaded_models())

    loaded, remote = asyncio.run(inside_the_loop())
    assert loaded == {"served-model"}
    assert remote is False


def test_off_the_loop_the_fresh_query_is_still_used():
    """⬜ THE CONTROL, so the guard is not just "always use the snapshot".

    Off-loop callers (`resolve_side_call_model` runs in a worker thread) keep
    the re-query. Proved by making the two sources DISAGREE: the live listing
    says `fresh`, the stale snapshot says `snapshot`. A guard that skipped the
    query unconditionally would return `snapshot` here.
    """
    async def list_models():
        return [_Row("fresh", True)]

    app = SimpleNamespace(
        backend=SimpleNamespace(list_models=list_models, remote=False),
        model_rows={"snapshot": _Row("snapshot", True)},
    )
    loaded, _ = model_residency.resident_models(app)
    assert loaded == {"fresh"}


def test_a_backend_with_loaded_models_never_reaches_either_path():
    """⬜ The preferred branch still wins, on or off the loop — that is what
    made this defect invisible to llama.cpp and LM Studio."""
    app = SimpleNamespace(
        backend=SimpleNamespace(
            loaded_models=lambda: ["resident", ""],
            list_models=None,
            remote=False,
        ),
        model_rows={},
    )

    async def inside() -> tuple:
        return model_residency.resident_models(app)

    assert model_residency.resident_models(app)[0] == {"resident"}
    assert asyncio.run(inside())[0] == {"resident"}
