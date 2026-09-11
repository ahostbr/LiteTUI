"""T631 — `ready` names the BACKEND as well as the model.

WHY THE HOST NEEDS IT. LiteSuite's Frontier Chat pill shows "LiteTUI (Auto)" for
a litetui thread and never the model that actually answers. It cannot do better
on its own: `buildLiteTuiArgs` (LiteTuiAdapter.ts:187-204) passes --rpc, --cwd,
--tool-profile, --mode and --model, and NO backend — the child picks its backend
from its own persisted settings, so the host has no way to know whether a turn
went to LM Studio, llama.cpp, or a remote provider. The one process that knows
is this one, and `ready` is where it already says everything else about itself.

⬜ NON-LIVE ON PURPOSE. tests/test_rpc.py's `test_ready_event` asserts the same
payload but sits behind `_needs_backend`, so it runs only where a real backend is
up — and on this machine starting one can load a model into a GPU somebody else
is using. These arms drive `_rpc_emit_ready` directly against a shell app, so the
payload's SHAPE is pinned everywhere while the live arm keeps proving the wire.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as app_mod


def _app(*, backend_name="lmstudio", model_id="qwen/a", loaded=("qwen/a",), rpc=True):
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a._rpc = rpc
    a.model_id = model_id
    a.available_models = ["qwen/a", "ektome-b"]
    a.backend = SimpleNamespace(name=backend_name, loaded_models=lambda: list(loaded))
    a._active_tool_profile = "interactive"
    a.emitted: list[dict] = []
    a._rpc_emit = a.emitted.append
    return a


def _ready(a) -> dict:
    asyncio.run(app_mod.LiteTUI._rpc_emit_ready.__wrapped__(a))
    assert len(a.emitted) == 1, f"expected exactly one ready event, got {a.emitted}"
    return a.emitted[0]


def test_ready_names_the_backend_that_will_answer():
    """🔴 The field the host has no other way to learn."""
    assert _ready(_app(backend_name="lmstudio"))["backend"] == "lmstudio"


def test_the_backend_is_read_from_the_backend_not_hard_coded():
    """A second value, so the arm above cannot pass on a literal."""
    assert _ready(_app(backend_name="llamacpp"))["backend"] == "llamacpp"


def test_the_fields_ready_already_carried_are_untouched():
    """
    The host reads this payload; adding a key must not move an existing one.
    `model` in particular is what T594's resolution writes, and the pill will
    read it.
    """
    ready = _ready(_app(model_id="qwen/a", loaded=("qwen/a",)))
    assert ready["type"] == "ready"
    assert ready["model"] == "qwen/a"
    assert ready["tool_profile"] == "interactive"
    assert "version" in ready and "cwd" in ready


def test_a_substitution_still_reports_the_model_AND_the_reason():
    """
    🔴 THE CASE THE HOST'S PILL IS ACTIVELY WRONG ABOUT. T594 substitutes the one
    resident model when the requested one is cold; `ready` then names the model
    that will really answer and says why in `model_note`. Both halves travel, and
    the backend travels beside them — the host needs all three to render
    "<model> @ <backend>, because <reason>" instead of the slug it asked for.
    """
    ready = _ready(_app(model_id="qwen/a", loaded=("ektome-b",)))
    assert ready["model"] == "ektome-b"
    assert "not loaded" in ready["model_note"]
    assert ready["backend"] == "lmstudio"


def test_model_note_is_ABSENT_when_nothing_was_substituted():
    """
    Absent, not empty: the host branches on presence, and a "" note would render
    an explanation box for a thread that has nothing to explain.
    """
    assert "model_note" not in _ready(_app(model_id="qwen/a", loaded=("qwen/a",)))


def test_a_backend_with_no_name_reports_None_rather_than_a_string():
    """
    ⚠️ A TEST DOUBLE IS A REAL CASE HERE. `make_backend` is stubbed in a dozen
    suites and plugins predate the seam, so `backend` can be an object with no
    `name` — or absent entirely. Reporting the string "None" would put the word
    None in Ryan's pill; null lets the host render nothing.
    """
    a = _app()
    a.backend = SimpleNamespace(loaded_models=lambda: ["qwen/a"])
    assert _ready(a)["backend"] is None

    b = _app()
    b.backend = None
    assert _ready(b)["backend"] is None
