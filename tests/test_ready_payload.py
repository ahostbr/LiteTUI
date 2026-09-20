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
from litetui import llm_backend, rpc


def _app(*, backend_name="lmstudio", model_id="qwen/a", loaded=("qwen/a",), rpc=True):
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a._rpc = rpc
    a.model_id = model_id
    a.available_models = ["qwen/a", "ektome-b"]
    a.model_rows = {
        key: llm_backend.ModelRow(key, None, "test", loaded=key in loaded)
        for key in a.available_models
    }
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


def test_ready_carries_the_real_switchable_catalogue_and_marks_current():
    """T684: the host menu is the child's backend list, not a host table."""
    a = _app(model_id="qwen/a")
    a.backend.models = {
        "qwen/a": {"display_name": "Qwen A"},
        "ektome-b": {"display_name": "Ektome B"},
    }

    ready = _ready(a)

    assert ready["models"] == [
        {"slug": "qwen/a", "name": "Qwen A", "current": True, "loaded": True},
        {"slug": "ektome-b", "name": "Ektome B", "current": False, "loaded": False},
    ]


def test_ready_catalogue_falls_back_to_slug_when_backend_has_no_display_name():
    ready = _ready(_app())
    assert [row["name"] for row in ready["models"]] == ["qwen/a", "ektome-b"]


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


def test_list_models_uses_the_same_catalogue_shape_as_ready(monkeypatch):
    a = _app()
    replies = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kwargs: replies.append((args, kwargs)))

    rpc._dispatch(a, {"type": "list_models", "id": "models-1"})

    assert replies == [
        (("models-1",), {"ok": True, "result": _ready(_app())["models"]})
    ]


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


# ── T647: what the child was ASKED for, beside what it is RUNNING as ─────────


def _cli_app(requested, *, settings_default="autonomous"):
    a = _app()
    a._cli_tool_profile = requested
    a._active_tool_profile = requested or settings_default
    return a


def test_ready_reports_the_profile_the_child_is_RUNNING_as():
    """Unchanged, and pinned because the field already existed and is correct:
    `_active_tool_profile` holds a plain name on every path."""
    assert _ready(_cli_app("interactive"))["tool_profile"] == "interactive"


def test_ready_ALSO_reports_what_the_host_ASKED_for():
    """🔴 THE FIELD T577 NEEDED AND DID NOT HAVE.

    With only `tool_profile`, a child reporting "autonomous" is ambiguous: it
    means EITHER the host passed --tool-profile autonomous OR the host passed
    nothing and the child fell back to its own settings default, which is
    autonomous. Those are a deliberate choice and a fail-open, and they were
    indistinguishable on the wire — so T577's profile had to be DERIVED from
    reading defaults instead of READ.
    """
    assert _ready(_cli_app("interactive"))["tool_profile_requested"] == "interactive"


def test_an_ABSENT_flag_reports_null_and_NOT_the_fallback():
    """🔴 The whole point. null says "nobody asked"; "autonomous" would say
    "somebody asked for autonomous", and the difference is the audit."""
    ready = _ready(_cli_app(None))

    assert ready["tool_profile_requested"] is None
    assert ready["tool_profile"] == "autonomous"  # what it actually runs as


def test_a_REQUESTED_profile_that_differs_from_the_active_one_is_visible():
    """
    If the two ever disagree — a flag the child did not understand, a later
    /profile change — the pair says so rather than the reader having to trust
    that one implies the other.
    """
    a = _cli_app("interactive")
    a._active_tool_profile = "scheduled"
    ready = _ready(a)

    assert (ready["tool_profile_requested"], ready["tool_profile"]) == (
        "interactive",
        "scheduled",
    )


# ── T869 — the wait before `ready`, and what ends it ──────────────────────


def _rounds(a, *, sleeps_then_models=None, monkeypatch=None) -> int:
    """Run `_rpc_emit_ready` and count the poll rounds it actually slept.

    ⭐ COUNTING ROUNDS, NOT SECONDS. A wall-clock assertion on a 10s loop is
    a 10s test and a flaky one; the round count is the same fact measured
    where it is exact.
    """
    rounds = 0
    real_sleep = app_mod.asyncio.sleep

    async def counting_sleep(delay):
        nonlocal rounds
        rounds += 1
        if sleeps_then_models is not None and rounds >= sleeps_then_models:
            a.available_models = ["qwen/a"]
        await real_sleep(0)

    monkeypatch.setattr(app_mod.asyncio, "sleep", counting_sleep)
    _ready(a)
    return rounds


def _empty_app(**kw):
    a = _app(**kw)
    a.available_models = []
    a.model_rows = {}
    return a


def test_ready_does_not_wait_once_connect_has_finished_without_models(monkeypatch):
    """🔴 THE DEFECT. Measured against a real `--rpc` child on 2026-09-17 with
    nothing loaded: `ready` at 11112ms, ~10s of it this poll running its full
    twenty rounds for a list `connect` had already finished not-finding. Every
    headless child paid it, and nothing in the output said a wait had happened.
    """
    a = _empty_app()
    a._connect_settled = True

    assert _rounds(a, monkeypatch=monkeypatch) == 0


def test_the_wait_SURVIVES_for_the_case_it_exists_for(monkeypatch):
    """CONTROL, and the reason the round count was not simply shortened.

    While `connect` is still running the list may yet arrive, and this loop is
    what gives it time to. An exit that fired here would be a faster boot that
    announces a model list the child was about to have.
    """
    a = _empty_app()
    a._connect_settled = False

    assert _rounds(a, monkeypatch=monkeypatch) == 20


def test_a_list_that_ARRIVES_mid_wait_still_ends_the_wait_immediately(monkeypatch):
    """The third case, so neither arm above can pass on "always break" or
    "never break": models appearing on round 3 must stop the poll at 3."""
    a = _empty_app()
    a._connect_settled = False

    assert _rounds(a, sleeps_then_models=3, monkeypatch=monkeypatch) == 3


def test_ready_waits_for_cli_application_before_reporting_effective_state():
    async def scenario():
        a = _app()
        a._cli_args_done = asyncio.Event()
        task = asyncio.create_task(app_mod.LiteTUI._rpc_emit_ready.__wrapped__(a))
        await asyncio.sleep(.02)
        assert not a.emitted
        a._cli_launch_error = 'requested model unavailable'
        a._cli_args_done.set()
        await asyncio.wait_for(task, 1)
        assert a.emitted[0]['launch_status'] == 'blocked'
        assert a.emitted[0]['launch_error'] == 'requested model unavailable'
    asyncio.run(scenario())
