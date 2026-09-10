"""T569 — /think and /load open the picker instead of printing a list.

RYAN, 2026-09-10 19:4x, verbatim:

    "make litetui's "/think" cmd display a think modal dialog ... its all
    printed to the screen run ... also "/load" should show the model selector so
    the user can select the modal to load not rely on them to type it perfectly"

🔴 THE TWO COMMANDS FAILED DIFFERENTLY AND BOTH ARE COVERED HERE.
`/think` printed a list and asked you to retype one of its own words. `/load`
was worse: with no argument it SILENTLY LOADED whatever `/model` had selected —
it acted without showing the choice the user came for, so the menu was not just
missing, the command did something else instead.

⚠️ NO NEW SCREEN. Both go through `picker.pick`, the house picker with five
existing call sites. The risk in this change is therefore not "does a picker
work" — that is already covered — it is whether these two call sites pass the
right rows, whether the callback runs the SAME code the typed form runs, and
whether the RPC transport is left alone. Those are what the arms below measure.
"""
from __future__ import annotations

import asyncio

import pytest

from litetui import thinking_probe
from litetui.picker import PickerScreen
from litetui.plugins import misc, model_switch
from litetui.settings import THINKING_LEVELS


class _Backend:
    def __init__(self, name="llamacpp", remote=False):
        self.name = name
        self.remote = remote
        self.loaded: list[str] = []

    async def load(self, target):
        self.loaded.append(target)


class _StubApp:
    """Only what these two commands touch. A real LiteTUI is used in the pilot
    arms at the bottom; here the point is the DECISION, not the rendering."""

    def __init__(self, *, rpc=False, models=None, model_id="", levels=None):
        self.thinking_level = None
        self._model_thinking_levels = levels
        self._rpc = rpc
        self.backend = _Backend()
        self.available_models = list(models or [])
        self.model_id = model_id
        self.model_rows = {}
        self.messages: list[str] = []
        self.headers = 0
        self.workers: list = []
        self.connected = 0
        self.ctx_fetches = 0

    def system_message(self, text):
        self.messages.append(text)

    def update_header(self):
        self.headers += 1

    def connect(self):
        self.connected += 1

    def fetch_context_window(self):
        self.ctx_fetches += 1

    def run_worker(self, coro, **_kw):
        self.workers.append(coro)

    def drain(self):
        """Run whatever `_start_load` scheduled, so the load is observable."""
        for coro in self.workers:
            asyncio.run(coro)
        self.workers.clear()


@pytest.fixture
def picks(monkeypatch):
    """Capture `pick(...)` at BOTH call sites without opening a screen."""
    calls: list[dict] = []

    def fake(app, title, rows, callback, current=None, **kw):
        calls.append(
            {"title": title, "rows": rows, "callback": callback, "current": current}
        )

    monkeypatch.setattr(misc, "pick", fake)
    monkeypatch.setattr(model_switch, "pick", fake)
    return calls


# ── /think ──────────────────────────────────────────────────────────────────


def test_think_with_no_argument_opens_the_picker(picks):
    app = _StubApp()
    app.thinking_level = "medium"

    misc._cmd_think(app, "think", "")

    assert len(picks) == 1, "no argument still printed a list instead of asking"
    call = picks[0]
    assert call["title"] == "Thinking level"
    assert call["current"] == "medium"
    values = [v for v, _label in call["rows"]]
    assert values == list(THINKING_LEVELS) + ["unset"]


def test_the_current_level_is_preselected_as_unset_when_none(picks):
    # "unset" is a real choice, not the absence of one — it must be the row the
    # picker lands on, or the user cannot see what they are currently on.
    app = _StubApp()
    misc._cmd_think(app, "think", "")
    assert picks[0]["current"] == "unset"


def test_the_picker_and_the_typed_form_run_ONE_body(picks):
    """🔴 THE ARM THAT MATTERS: picking `high` must say exactly what typing it says.

    Everything after the assignment is caveat — which backend ignores graded
    levels, which levels the model advertises. A second copy for the picker
    would rot, and it would show up as a user who used the MENU being told less
    than a user who typed. Compared byte-for-byte rather than by shape.
    """
    typed = _StubApp(levels=["off", "low", "high"])
    typed.backend.name = "lmstudio"
    misc._cmd_think(typed, "think", "high")

    picked = _StubApp(levels=["off", "low", "high"])
    picked.backend.name = "lmstudio"
    misc._cmd_think(picked, "think", "")
    picks[0]["callback"]("high")

    assert picked.thinking_level == typed.thinking_level == "high"
    assert picked.messages == typed.messages, (
        "the picker and the typed command produced different text — they are "
        "two implementations, which is exactly what this refactor removed"
    )
    assert picked.headers == typed.headers == 1


def test_escape_changes_nothing(picks):
    """Esc resolves with None and the callback STILL FIRES (picker.pick's own
    docstring). A callback that treated None as a value would unset the level
    every time somebody looked at the menu and changed their mind."""
    app = _StubApp()
    app.thinking_level = "low"
    misc._cmd_think(app, "think", "")
    before = list(app.messages)

    picks[0]["callback"](None)

    assert app.thinking_level == "low"
    assert app.messages == before


def test_unset_is_offered_and_applies(picks):
    app = _StubApp()
    app.thinking_level = "high"
    misc._cmd_think(app, "think", "")
    picks[0]["callback"]("unset")
    assert app.thinking_level is None
    assert "unset" in app.messages[-1].lower()


def test_the_model_own_levels_are_offered_when_it_has_them(picks):
    app = _StubApp(levels=["off", "high"])
    misc._cmd_think(app, "think", "")
    assert [v for v, _ in picks[0]["rows"]] == ["off", "high", "unset"]


def test_a_model_level_is_always_something_the_typed_form_accepts():
    """The property `_thinking_rows` leans on, asserted rather than assumed.

    If the probe ever advertised a level outside `THINKING_LEVELS`, the picker
    would offer a value `/think <level>` rejects — the menu and the command
    would disagree about what is valid. That is a property of `thinking_probe`,
    which is free to change, so it is pinned here.
    """
    probe_levels = set(thinking_probe.GRADED_LEVELS) | {"off"}
    assert probe_levels <= set(THINKING_LEVELS), (
        f"the probe can advertise {probe_levels - set(THINKING_LEVELS)}, which "
        "/think would refuse"
    )


def test_rpc_never_opens_a_screen_and_still_answers(picks):
    """🔴 T558-A's lesson. A dialog pushed over RPC waits for a keyboard nobody
    is holding, and the caller is a MODEL — it would hang on a turn that can
    never complete. The text output is the answer on this transport."""
    app = _StubApp(rpc=True)

    misc._cmd_think(app, "think", "")

    assert not picks, "a screen was pushed over RPC"
    assert app.messages, "RPC got no answer at all"
    assert "Levels:" in app.messages[0] and "unset" in app.messages[0]


# ── /load ───────────────────────────────────────────────────────────────────


def test_load_with_no_argument_opens_the_model_picker(picks):
    app = _StubApp(models=["alpha", "beta"], model_id="beta")

    model_switch._cmd_load(app, "load", "")

    assert len(picks) == 1, "no-arg /load did not offer the selector"
    assert picks[0]["title"] == "Load a model"
    assert [v for v, _ in picks[0]["rows"]] == ["alpha", "beta"]
    assert picks[0]["current"] == "beta"
    assert not app.workers, (
        "it started a load as well as asking — the pre-T569 behaviour was to "
        "load silently, and doing both is worse than either"
    )


def test_the_picked_model_is_the_one_loaded(picks):
    # Not `app.model_id`: the whole point is loading something OTHER than the
    # currently selected model without retyping its name.
    app = _StubApp(models=["alpha", "beta"], model_id="beta")
    model_switch._cmd_load(app, "load", "")
    picks[0]["callback"]("alpha")
    app.drain()
    assert app.backend.loaded == ["alpha"]


def test_escaping_the_load_picker_loads_nothing(picks):
    app = _StubApp(models=["alpha"], model_id="alpha")
    model_switch._cmd_load(app, "load", "")
    picks[0]["callback"](None)
    app.drain()
    assert app.backend.loaded == []


def test_a_known_typed_name_still_loads_directly(picks):
    """`/load <name>` is muscle memory and scripting. It must not grow a prompt."""
    app = _StubApp(models=["alpha", "beta"])
    model_switch._cmd_load(app, "load", "beta")
    app.drain()
    assert app.backend.loaded == ["beta"]
    assert not picks


def test_an_unknown_name_opens_the_picker_with_near_matches_on_top(picks):
    """🔴 RYAN'S ACTUAL COMPLAINT — "not rely on them to type it perfectly".

    A typo used to reach `backend.load()` and come back as an error line that
    restated the name without helping. The near match goes FIRST because the
    row the user wants should be under the cursor, not somewhere in a list of
    forty.
    """
    # 🔴 THE NEAR MATCH IS LAST IN THE ROSTER ON PURPOSE. With it first, this
    # arm passes on the ORDER OF THIS FIXTURE rather than on the floating — a
    # mutation that deleted `first=near` stayed GREEN until this line changed.
    app = _StubApp(models=["llama-3.3-70b", "gemma-27b", "qwen3-coder-30b"])

    model_switch._cmd_load(app, "load", "qwen3-codr-30b")

    assert not app.workers, "a typo was sent to the backend anyway"
    assert len(picks) == 1
    assert next(v for v, _ in picks[0]["rows"]) == "qwen3-coder-30b"
    assert {v for v, _ in picks[0]["rows"]} == set(app.available_models), (
        "floating the near matches DROPPED the other models — the picker must "
        "still offer everything"
    )
    assert any("qwen3-codr-30b" in m for m in app.messages), (
        "it opened a picker without saying why"
    )


def test_an_unknown_name_with_nothing_discovered_still_loads(picks):
    """CONTROL — the guard is gated on there being models to compare against.

    With an empty roster there is nothing to match and nothing to show, and
    refusing would break `/load <name>` for anyone whose backend lists nothing:
    a strictly worse failure than the typo it guards.
    """
    app = _StubApp(models=[])
    model_switch._cmd_load(app, "load", "some-model")
    app.drain()
    assert app.backend.loaded == ["some-model"]
    assert not picks


def test_rpc_load_keeps_the_old_silent_behaviour(picks):
    app = _StubApp(rpc=True, models=["alpha", "beta"], model_id="beta")
    model_switch._cmd_load(app, "load", "")
    app.drain()
    assert not picks, "a screen was pushed over RPC"
    assert app.backend.loaded == ["beta"]


def test_a_remote_backend_is_untouched(picks):
    app = _StubApp(models=["alpha"])
    app.backend.remote = True
    model_switch._cmd_load(app, "load", "")
    assert not picks
    assert "Remote models need no loading" in app.messages[0]


# ── the screen really is pushed ─────────────────────────────────────────────
#
# Everything above monkeypatches `pick`, which proves the CALL and not the
# screen. These two drive the real app: a helper proven in isolation that its
# caller never actually reaches is a failure this workspace has shipped before.


@pytest.mark.asyncio
async def test_think_pushes_a_real_picker_screen():
    from litetui import app as app_mod

    a = app_mod.LiteTUI()
    a.convo_dir = None
    a.skills = []
    async with a.run_test() as pilot:
        misc._cmd_think(a, "think", "")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), (
            f"no picker on screen — got {type(a.screen).__name__}"
        )


@pytest.mark.asyncio
async def test_load_pushes_a_real_picker_screen():
    from litetui import app as app_mod

    a = app_mod.LiteTUI()
    a.convo_dir = None
    a.skills = []
    a.available_models = ["alpha", "beta"]
    a.model_id = "alpha"
    async with a.run_test() as pilot:
        model_switch._cmd_load(a, "load", "")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), (
            f"no picker on screen — got {type(a.screen).__name__}"
        )
