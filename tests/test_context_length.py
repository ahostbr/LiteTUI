"""The context the app believes must be the context the model has.

Ryan: "when it loads a model the context set in settings isnt being respected
keeps default to 8k".

Three faults, stacked, and the middle one is why fixing the obvious one alone
would not have worked:

  1. NOTHING APPLIED THE SETTING ON A MODEL SWITCH. `_apply_context_length()`
     had exactly one caller — a /settings save where the value CHANGED. Boot
     deliberately does not call it (that path used to load a 27B on every app
     start and every test). So picking a model just set `model_id` and let LM
     Studio JIT-load it at the server default. That is the 8k.

  2. A NOT-LOADED MODEL REPORTED ITS CEILING AS ITS WINDOW. The reader took
     `loaded_context_length or max_context_length`, so a merely-installed model
     answered 262,144 — a number the session does not have. A switch-time apply
     that trusted it would conclude the window already exceeded 120,000 and
     load nothing, so fault 1 could not be fixed without this.

  3. THE NO-OP GUARD COULD NEVER FIRE. It compared the READOUT to the REQUEST,
     and LM Studio clamps: ask 120,000, get 120,064.

And the number from fault 2 fed `_maybe_autocompact`, which DIVIDES by it: 80%
of 262,144 is 209,715 tokens, unreachable inside an 8k window. Auto-compact
would never fire and the model would blow its real context instead.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-ctx-"))

LOADED, CEILING = 120064, 262144


class _Recorder:
    """Loads now go through the backend seam (backend.load(key, ctx=n)) —
    the `lms load` argv this used to capture is the SDK's job since the
    dual-backend split. What the contract cares about is unchanged: WHETHER
    a load happened, and at what window."""

    def __init__(self) -> None:
        self.calls: list = []

    @property
    def load_calls(self) -> list:
        return self.calls


@pytest.fixture
def recorder():
    return _Recorder()


def _app(ctx=120000, recorder=None, info=None):
    a = app_mod.LiteTUI()
    a.settings = Settings(default_model="qwen/qwen3.8-27b", default_context_length=ctx)
    a.model_id = "qwen/qwen3.8-27b"
    a._system = lambda *_a, **_k: None
    a._fetch_ctx_window = lambda: None
    if recorder is not None:
        async def _load(key, *, ctx=None):
            recorder.calls.append((key, ctx))
        a.backend.load = _load
    if info is not None:
        async def _model_info(mid):
            return info
        a.backend.model_info = _model_info
    return a


# ── fault 2: a ceiling is not a window ────────────────────────────────────
# The parsing moved into LMStudioBackend when the dual-backend seam landed;
# the CONTRACT it protects did not move an inch.

def _lms_info(monkeypatch, payload):
    from litetui import llm_backend
    monkeypatch.setattr(
        llm_backend, "_http_json", lambda url, body=None, timeout=10: payload
    )
    return llm_backend.LMStudioBackend(Settings())._model_info_sync("m")


def test_a_not_loaded_model_is_reported_as_not_loaded(monkeypatch):
    payload = {"data": [{"id": "m", "max_context_length": CEILING, "type": "llm"}]}
    got = _lms_info(monkeypatch, payload)
    assert got == (CEILING, "llm", False), got
    assert got[2] is False, "a ceiling must never be reported as a loaded window"


def test_a_loaded_model_reports_its_loaded_window(monkeypatch):
    payload = {"data": [{"id": "m", "loaded_context_length": LOADED,
                         "max_context_length": CEILING, "type": "llm"}]}
    assert _lms_info(monkeypatch, payload) == (LOADED, "llm", True)


# ── auto-compact must not divide by a ceiling ─────────────────────────────
def test_autocompact_refuses_when_the_window_is_only_a_ceiling():
    a = _app()
    fired = []
    a._handle_command = lambda c: fired.append(c)
    a.ctx_max, a.ctx_used, a.ctx_loaded = CEILING, 250_000, False
    a._maybe_autocompact()
    assert fired == [], "compacted against a window the session does not have"


def test_control_autocompact_DOES_fire_on_a_real_loaded_window():
    """Proves the refusal above is about `ctx_loaded`, not about the numbers.

    Without this, the test above would pass just as well if auto-compact had
    been broken outright — which is the failure mode the refusal risks
    introducing: turning a wrong number into no number.
    """
    a = _app()
    fired = []
    a._handle_command = lambda c: fired.append(c)
    a.ctx_max, a.ctx_used, a.ctx_loaded = LOADED, int(LOADED * 0.95), True
    a._maybe_autocompact()
    assert fired == ["/compact"], fired


def test_a_stale_window_is_re_read_once_the_model_is_resident():
    """Refusing to guess must not become refusing to ever fire.

    At boot the model is often not loaded, so the threshold correctly declines.
    The first message then makes LM Studio load it — and if nothing asks again,
    ctx_loaded stays False for the whole session and auto-compact never runs.
    """
    a = _app()
    asked = []
    a._fetch_ctx_window = lambda: asked.append(True)

    a.ctx_loaded = False
    a._resync_ctx_if_stale()
    assert asked, "a stale window was never re-read"

    asked.clear()
    a.ctx_loaded = True
    a._resync_ctx_if_stale()
    assert asked == [], "a known-good window must not be re-read every turn"


# ── fault 3: the clamp ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_already_satisfied_window_is_not_reloaded(recorder, monkeypatch):
    """120,064 satisfies a request for 120,000. Reloading evicts 17GB to
    change nothing — and the OLD guard (readout == request) could never see
    this case, because LM Studio clamps."""
    a = _app(ctx=120000, recorder=recorder, info=(LOADED, "llm", True))
    async with a.run_test() as pilot:
        a._apply_context_length()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls == [], recorder.load_calls


@pytest.mark.asyncio
async def test_a_not_loaded_model_IS_loaded_at_the_configured_window(recorder, monkeypatch):
    """The actual bug: nothing applied the setting, so LM Studio JIT-loaded at
    its own default."""
    a = _app(ctx=120000, recorder=recorder, info=(CEILING, "llm", False))
    async with a.run_test() as pilot:
        a._apply_context_length()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls == [("qwen/qwen3.8-27b", 120000)], recorder.calls


@pytest.mark.asyncio
async def test_force_lowers_a_window_that_already_exceeds_the_request(recorder, monkeypatch):
    """A /settings change is an instruction, not a suggestion.

    "at least as much is fine" is right for a model switch and WRONG here:
    lowering the window to free VRAM is a legitimate thing to ask for, and a
    >= guard would silently ignore it.
    """
    a = _app(ctx=32000, recorder=recorder, info=(LOADED, "llm", True))
    async with a.run_test() as pilot:
        a._apply_context_length(force=True)
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls == [("qwen/qwen3.8-27b", 32000)], recorder.calls


# ── fault 1: the switch applies it ────────────────────────────────────────
def test_every_explicit_model_switch_applies_the_setting():
    """Source-level, deliberately: the switch paths are separate code paths and
    the bug was one of OMISSION. A behavioural test on one of them would leave
    the others free to regress.

    Boot is asserted NOT to apply, by the same reading — that rule is what
    stopped the app loading a 27B on every start.

    🔴 THE COUNT IS A FLOOR, NOT AN EQUALITY (T694). It read `== 3` and had gone
    red on pristine `main`, because `/model <n>` and `/model <name>` were
    CONSOLIDATED into one `switch_model` (model_switch.py:52), which the rpc
    host also calls. Nothing was lost, and the arm was counting an old source
    LAYOUT while calling itself a check on a rule — so a good change read as a
    missing switch.

        A COUNT PINNED TO A LAYOUT GOES RED WHEN THE LAYOUT IMPROVES.

    🔴 IT HAPPENED AGAIN ONE CARD LATER, WHICH IS THE REAL LESSON (T698).
    `on_model_picked` was the last duplicate of `switch_model`; folding it left
    ONE announcement in the tree, and the per-file check below went red saying
    the site was "gone". It was right to ask and wrong to conclude: the site was
    ABSORBED, not deleted. So the rule is stated properly now — the tree may
    hold any number of announcements, every one must apply the setting, and a
    path that no longer announces must DELEGATE to one that does. That last
    clause is `test_the_picker_only_DELEGATES`, and it is what makes a
    disappearing site provable rather than merely plausible.

    ⚠️ THE FLOOR STILL CANNOT DEFEND ITSELF, so at least one file must carry
    the marker. A rewording everywhere would otherwise empty the site list and
    the pairing loop would pass over nothing — 0-of-0 and 0-of-1 are the same
    green.
    """
    # The runtime is app.py + the model_switch plugin since the split. Since
    # T698 every announcement lives in the plugin; app.py is still read because
    # a new switch path added there must be caught, not assumed absent.
    app_src = Path(app_mod.__file__).read_text(encoding="utf-8")
    plug_src = (Path(app_mod.__file__).parent / "plugins" / "model_switch.py").read_text(encoding="utf-8")
    sites = []
    for src_text, marker in ((app_src, 'self._system(f"Switched to: {self.model_id}")'),
                             (plug_src, 'app.system_message(f"Switched to: {app.model_id}")')):
        sites.extend(src_text.split(marker)[1:])
    assert len(sites) >= 1, (
        "no switch announcement was found in either file. Either every switch "
        "path was removed or the announcement was reworded — and a reworded one "
        "makes this whole gate pass over an empty list"
    )

    # Each one is followed by an apply.
    for chunk in sites:
        head = chunk[:400]
        # TWO SPELLINGS, TWO HOMES: app.py calls the private alias, the plugin
        # calls the public name since S5. Matching only one makes this gate
        # measure an ADDRESS instead of the rule it is named for.
        assert ("self._apply_context_length()" in head
                or "app.apply_context_length()" in head), (
            "a model switch that does not apply the configured context length "
            "leaves LM Studio to JIT-load at its own default"
        )

    connect = app_src.split("Connected — model:", 1)[0][-1200:]
    # The SHORT form deliberately: it is a substring of the private spelling, so
    # this one negative catches BOTH `self._apply_context_length()` and a future
    # `self.apply_context_length()`. A negative assertion should over-match --
    # under-matching is how it goes vacuous without anyone noticing.
    assert "apply_context_length()" not in connect.replace(
        "# _apply_context_length()", ""), "connect must never load a model"


def test_the_picker_only_DELEGATES() -> None:
    """🔴 THE CLAUSE THAT MAKES AN ABSENT ANNOUNCEMENT PROVABLE (T698).

    `on_model_picked` used to be a second copy of `switch_model` — the same
    seven effects written out again, so every future change to a switch had to
    be made twice and looked complete after the first. It now calls the shared
    path, which is why the arm above finds one announcement where it used to
    find two.

    ⚠️ "CALLS `switch_model`" IS NOT ENOUGH, AND THAT EXACT ESCAPE HATCH HAS
    ALREADY COST THIS REPO A CARD. A T690 detector accepted "delegates to
    something guarded" and stayed GREEN under a mutation, because the function
    it cleared ALSO had an unguarded branch — an escape hatch not scoped to the
    whole body is an escape hatch for the whole body. So this parses the body
    and requires that it does nothing ELSE: no assignment, and no call but the
    delegation.
    """
    import ast

    tree = ast.parse(Path(app_mod.__file__).read_text(encoding="utf-8"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "on_model_picked"),
        None,
    )
    assert fn is not None, "on_model_picked is gone — the picker has no callback"

    # T838: the callback reaches the shared body through the REGISTRY now,
    # so the thing to pin is the ROUTE, not a bare name. It used to look for
    # a Call to the Name `switch_model`; app.py may no longer say that name,
    # because `test_plugin_dogfood` forbids app.py importing a plugin
    # submodule at all -- deferred or not. `_handle_command("/model ...")`
    # resolves to `_cmd_model` -> `switch_model`, which is the same one body.
    #
    # ⚠️ THIS IS A WEAKER ASSERTION THAN THE ONE IT REPLACES AND THAT IS
    # STATED RATHER THAN HIDDEN: a method call cannot be resolved to its
    # target by AST. What still holds it down is the BEHAVIOURAL arm in
    # tests/test_model_picked.py, which drives the callback and checks the
    # switch's effects -- including that a model the server does not have is
    # refused. A structural arm that can only see the door plus a
    # behavioural arm that walks through it is the pair that works here.
    methods = [
        n.func.attr for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    calls = [
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "switch_model" in calls or "_handle_command" in methods, (
        "the picker does not reach the shared switch path — if it grew its own "
        "body again, it needs its own announcement and its own apply, and the "
        "floor above needs raising to match"
    )
    assert [c for c in calls if c != "switch_model"] == [], calls

    assigned = [
        t.attr for n in ast.walk(fn) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Attribute)
    ]
    assert assigned == [], (
        f"the picker assigns {assigned} itself instead of leaving it to "
        f"`switch_model` — that is how the two copies drifted the first time"
    )
