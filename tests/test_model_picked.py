"""`on_model_picked` — the model picker's callback, and the guard that keeps a
resident model from being reloaded for nothing.

WHY THIS FILE EXISTS. The member is reached ONLY as a `push_screen` callback
reference (`plugins/model_switch.py`, the `app.on_model_picked` argument) and is
never called by name anywhere in the tree, so `git grep -l on_model_picked --
tests/` returns ZERO.

CORRECTED AFTER THE FACT, AND THE CORRECTION IS THE POINT. This docstring first
concluded from that ZERO that "nothing accidentally covered it". THAT WAS WRONG.
`tests/test_modals.py` drives the picker WIRING and never names the symbol; run
against the full suite with this file deselected, it already covers three of the
four arms:

    gut the body                     -> 2 RED in test_modals.py   already covered
    remove the whole guard           -> 1 RED in test_modals.py   already covered
    drop only the same-model clause  -> 1141 passed, GREEN        UNWATCHED

ZERO MENTIONS IS NOT ZERO COVERAGE. The original mutations were run against this
file alone, which measures THIS FILE'S sensitivity, not the codebase's coverage.

So what this file adds is one genuinely unwatched arm --
`test_repicking_the_current_model_does_not_reload_it` is the only test in the
tree that watches it -- plus explicit, named coverage beside implicit wiring
coverage, which a rename can silently detach. Not a closed hole.
(Not measured: whether the empty-string case is separately covered; `not
model_id` folds None and "" together and only the same-model clause was isolated.)

The guard is not cosmetic. `_apply_context_length` reloads the model on the
llama.cpp backend -- evicting resident weights -- so re-picking the model you
are already on must do nothing at all.

RED AT main FROM 4558d4e UNTIL T576, AND THE SHAPE IS WORTH KEEPING. That commit
added a fifth effect to the callback -- `if self.backend.name == "lmstudio":
self._probe_thinking()` -- and `PickDouble` had no `backend`, so the two arms
that drive a real switch died on AttributeError while the three that return
early stayed green. A partially-red file reads as a working file in a suite
summary; the three passes are what made it survivable.
    A DOUBLE IS A CLAIM ABOUT WHAT THE SUBJECT TOUCHES, so it goes stale exactly
    when the subject grows, and nothing links the two.
The fix is not `backend = SimpleNamespace(name="x")` to quiet the error: that
would make the arms green while asserting nothing about the branch that broke
them. Both sides of it are pinned below.
"""
from types import SimpleNamespace

from litetui.app import LiteTUI

picked = LiteTUI.on_model_picked


class PickDouble(SimpleNamespace):
    """Records the effects a real switch is supposed to drive.

    `backend` IS NOT PADDING TO STOP AN AttributeError (T576). `on_model_picked`
    ends with `if self.backend.name == "lmstudio": self._probe_thinking()`, added
    by 4558d4e (T540-1) so that switching model on LM Studio re-discovers the new
    model's real thinking levels. A double that carried a bare `backend` would
    make the arms green while pinning nothing; the name is a real backend's --
    `LlamaCppBackend.name == "llamacpp"`, `LMStudioBackend.name == "lmstudio"`
    (llm_backend.py:604, :1306) -- and both sides of that branch are asserted
    below.

    llamacpp is the default because the same-model guard exists for it: a
    re-apply there evicts resident weights. The three arms that were already
    green keep exactly the behaviour they had.

    ⚠️ THE METHOD NAMES ARE THE PUBLIC SPELLINGS SINCE T698, and that is a fact
    about the fold rather than a preference. `on_model_picked` no longer has a
    body: it calls `plugins.model_switch.switch_model`, which drives
    `update_header` / `fetch_context_window` / `system_message` /
    `apply_context_length`. The real app carries both spellings (app.py aliases
    the private ones), but a SimpleNamespace carries only what it is given — so
    a double still answering `_update_header` would record zero effects and
    every count below would read 0 while the switch worked perfectly.

    `available_models` is likewise not padding: `switch_model` REFUSES a target
    it does not contain, which is the one behaviour the fold tightened. The old
    copy assigned whatever it was handed.
    """

    def __init__(self, model_id="alpha", backend="llamacpp"):
        super().__init__(
            model_id=model_id,
            available_models=["alpha", "beta"],
            headers=0,
            fetches=0,
            applied=0,
            said=[],
            probes=0,
            emits=0,
            _model_thinking_levels=None,
            backend=SimpleNamespace(name=backend),
        )

    def update_header(self):
        self.headers += 1

    def fetch_context_window(self):
        self.fetches += 1

    def system_message(self, message):
        self.said.append(message)

    def apply_context_length(self):
        self.applied += 1

    def _probe_thinking(self):
        self.probes += 1

    def _rpc_emit_model_state(self):
        # Self-guarding in the real app (app.py:3247 returns unless `_rpc`), so
        # the plugin calls it unconditionally and always did the same thing as
        # app.py's `if getattr(self, "_rpc", False)` wrapper. Counted here so
        # the arm below can say that out loud instead of implying a change.
        self.emits += 1


def test_picking_a_new_model_switches_and_drives_all_four_effects():
    d = PickDouble("alpha")
    picked(d, "beta")
    assert d.model_id == "beta"
    assert (d.headers, d.fetches, d.applied) == (1, 1, 1)
    assert d.said == ["Switched to: beta"]


def test_the_announcement_names_the_new_model_not_the_old():
    """The message reads `self.model_id` AFTER the assignment. Hoisting the
    announcement above the assignment would still print a plausible line --
    naming the model you just left."""
    d = PickDouble("alpha")
    picked(d, "beta")
    assert "beta" in d.said[0] and "alpha" not in d.said[0]


def test_escaping_the_picker_changes_nothing():
    """PickerScreen dismisses with None when the user presses Esc."""
    d = PickDouble("alpha")
    picked(d, None)
    assert d.model_id == "alpha"
    assert (d.headers, d.fetches, d.applied, d.said) == (0, 0, 0, [])


def test_an_empty_choice_changes_nothing():
    d = PickDouble("alpha")
    picked(d, "")
    assert d.model_id == "alpha"
    assert (d.headers, d.fetches, d.applied, d.said) == (0, 0, 0, [])


def test_repicking_the_current_model_does_not_reload_it():
    """The whole point of the second guard arm: on llama.cpp a re-apply evicts
    the resident weights, so picking what you already have must be free."""
    d = PickDouble("alpha")
    picked(d, "alpha")
    assert d.applied == 0
    assert (d.headers, d.fetches, d.said) == (0, 0, [])


def test_lmstudio_reprobes_thinking_levels_for_the_model_just_picked():
    """4558d4e (T540-1): the level set is a property of the MODEL, not the app,
    so switching model on LM Studio has to go and ask again. `_probe_thinking`
    is what asks."""
    d = PickDouble("alpha", backend="lmstudio")
    picked(d, "beta")
    assert d.probes == 1


def test_a_non_lmstudio_backend_does_not_probe():
    """The other side of the same branch, and the reason this pair exists rather
    than one arm: an assertion that only ever sees `probes == 1` is satisfied by
    deleting the `if` and probing unconditionally, which would put a probe on a
    backend that cannot answer it."""
    d = PickDouble("alpha", backend="llamacpp")
    picked(d, "beta")
    assert d.probes == 0
    # and the switch itself still happened -- the guard narrows one effect, not all
    assert d.model_id == "beta"
    assert (d.headers, d.fetches, d.applied) == (1, 1, 1)


def test_the_picker_goes_through_the_ONE_switch_path() -> None:
    """🔴 T698: `on_model_picked` was a second copy of `switch_model`.

    Seven effects written out twice, so every future change to a switch had to
    be made in two places and looked complete after the first. The callback now
    delegates, and this pins that the delegation actually reaches the shared
    body rather than a lookalike — `test_context_length.py` proves it does
    nothing ELSE, by AST.

    ⬜ AND THE REFUSAL IS NEW. `switch_model` declines a target that is not in
    `available_models`; the old copy assigned whatever it was handed. The picker
    only offers `available_models`, so nothing live loses a path — a STALE pick
    is now declined instead of naming a model the server does not have.
    """
    d = PickDouble("alpha")
    picked(d, "gamma")          # never offered, not available

    assert d.model_id == "alpha", "a model the server does not have was selected"
    assert (d.headers, d.fetches, d.applied, d.said) == (0, 0, 0, [])


def test_the_rpc_emit_was_never_conditional_in_EFFECT() -> None:
    """The drift I reported when I found this duplication was COSMETIC, and the
    record should say so. app.py wrapped `_rpc_emit_model_state()` in
    `if getattr(self, "_rpc", False)`; the plugin calls it bare. But the method
    itself opens with that exact check (app.py:3247), so both spellings always
    did the same thing. The fold is justified by the duplication, not by a bug
    in it — and an arm that quietly implied otherwise would be worse than no
    arm."""
    import inspect

    from litetui.app import LiteTUI

    src = inspect.getsource(LiteTUI._rpc_emit_model_state)
    assert 'if not getattr(self, "_rpc", False):' in src, src[:400]
