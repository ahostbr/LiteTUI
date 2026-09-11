"""T640 — the two side calls the agent loop makes get a model setting and one picker.

🔴 THE FOLD HAD NO SETTING AND ITS DEFAULT WAS INVISIBLE. `llm-tool-summ`'s
throwaway side call sent `model=self.model_id or "local-model"` — the MAIN model
— so on Codex every folded tool result was a Codex call nobody chose, and on a
local backend it competed for the big model's own slots. `subagent_model`
existed but was free text on the Model tab, where a typo is indistinguishable
from a model that is merely not loaded yet.

⚠️ AND A COLD PICK MUST NEVER BE LOADED TO SATISFY EITHER. T594/T609/T611: a
local backend JIT-loads whatever a request names, so an unattended side call may
name only a model that is ALREADY resident. The subagent RAISES on a cold pick
because a subagent with no model has nothing to do; the fold FALLS BACK, because
failing it would turn a configuration mistake into a degraded turn — and it says
so, because falling back silently is how a setting looks broken with nothing
anywhere admitting it was ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import model_residency
from litetui.settings_screen import loop_model_choices


class FakeBackend:
    def __init__(self, loaded=(), remote=False):
        self._loaded = list(loaded)
        self.remote = remote

    def loaded_models(self):
        return list(self._loaded)


class FakeApp:
    def __init__(self, loaded=(), remote=False, model_id="big-27b"):
        self.backend = FakeBackend(loaded, remote)
        self.model_rows = {}
        self.model_id = model_id


# ---------------------------------------------------------------------------
# the fold's model
# ---------------------------------------------------------------------------

def test_None_means_the_main_model_which_is_what_it_always_did():
    app = FakeApp(loaded=["big-27b", "minicpm5-2b"])
    model, note = model_residency.resolve_side_call_model(app, None)
    assert model == "big-27b"
    assert note is None


def test_a_resident_pick_is_carried_to_the_fold():
    """🔴 The card: the fold sends work to the model the user chose."""
    app = FakeApp(loaded=["big-27b", "minicpm5-2b"])
    model, note = model_residency.resolve_side_call_model(app, "minicpm5-2b")
    assert model == "minicpm5-2b"
    assert note is None


def test_a_NON_resident_local_pick_falls_back_AND_SAYS_SO():
    """🔴 Both halves. A fallback nobody is told about is the silent kind."""
    app = FakeApp(loaded=["big-27b"])
    model, note = model_residency.resolve_side_call_model(app, "minicpm5-2b")

    assert model == "big-27b", "a cold model was named — LM Studio would load it"
    assert note is not None, "it fell back silently"
    assert "minicpm5-2b" in note and "not loaded" in note
    assert "big-27b" in note, "the note does not say what it used instead"


def test_a_remote_backend_loads_nothing_so_its_pick_is_honoured():
    """A remote id costs one 404 and no VRAM; refusing it is a different rule."""
    app = FakeApp(loaded=[], remote=True, model_id="gpt-5-codex")
    model, note = model_residency.resolve_side_call_model(app, "o3-mini")
    assert model == "o3-mini"
    assert note is None


def test_CONTROL_the_empty_string_is_treated_as_unset_not_as_a_model_name():
    """The settings Select's blank option stores "" before _collect maps it."""
    app = FakeApp(loaded=["big-27b"])
    assert model_residency.resolve_side_call_model(app, "")[0] == "big-27b"
    assert model_residency.resolve_side_call_model(app, "   ")[0] == "big-27b"


# ---------------------------------------------------------------------------
# the picker's options
# ---------------------------------------------------------------------------

MODELS = ["big-27b", "minicpm5-2b", "cold-14b"]
LOADED = ["big-27b", "minicpm5-2b"]


def test_options_are_EXACTLY_the_backends_list_plus_the_sentinel():
    """🔴 No hand-kept literal. That is the thing that rots without an arm."""
    opts = loop_model_choices(MODELS, LOADED, False, "auto", None)
    assert [v for _, v in opts] == ["", "big-27b", "minicpm5-2b", "cold-14b"]
    assert opts[0][0] == "auto"


def test_resident_models_come_first_and_the_rest_are_MARKED_not_hidden():
    opts = loop_model_choices(MODELS, LOADED, False, "auto", None)
    labels = [lbl for lbl, _ in opts][1:]
    assert labels[0].startswith("big-27b") and "(loaded)" in labels[0]
    assert labels[1].startswith("minicpm5-2b") and "(loaded)" in labels[1]
    # Shown, so a model you HAVE is distinguishable from one you do not.
    assert labels[2].startswith("cold-14b") and "downloaded, not loaded" in labels[2]


def test_a_remote_backend_marks_nothing_resident_because_it_has_no_such_property():
    """Marking every Codex model "not loaded" would be a lie about the engine."""
    opts = loop_model_choices(["gpt-5-codex", "o3-mini"], [], True, "main model", None)
    assert [v for _, v in opts] == ["", "gpt-5-codex", "o3-mini"]
    assert all("loaded" not in lbl for lbl, _ in opts)


def test_a_persisted_value_the_server_never_heard_of_is_STILL_an_option():
    """Textual REFUSES a Select value absent from its options and the WHOLE
    PANEL fails to open. Models arrive asynchronously, so "not in the list yet"
    is the normal state for the first moment of every launch — the Model tab's
    own docblock records this crashing intermittently and reading as haunted.
    """
    opts = loop_model_choices(MODELS, LOADED, False, "auto", "a-model-from-last-week")
    values = [v for _, v in opts]
    assert "a-model-from-last-week" in values, "the settings panel would refuse to open"
    assert "not currently served" in {v: k for k, v in opts}["a-model-from-last-week"]


def test_CONTROL_a_persisted_value_that_IS_served_is_not_duplicated():
    """Without this, the guard above could just append unconditionally."""
    opts = loop_model_choices(MODELS, LOADED, False, "auto", "big-27b")
    assert [v for _, v in opts].count("big-27b") == 1


def test_an_empty_backend_still_yields_a_usable_picker():
    """Before any model list arrives, the sentinel alone must be selectable."""
    opts = loop_model_choices([], [], False, "auto", None)
    assert opts == [("auto", "")]
