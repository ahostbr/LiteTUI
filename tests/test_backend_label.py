"""The header must name the backend that is actually answering (T861).

🔴 WHAT RYAN SAW. Title bar: `LiteTUI — LM Studio · tools:13 · think:medium`,
while the chat said "Already on ninfer" and every failure was NInfer's. The
line responsible was one expression:

    engine = {"llamacpp": "llama.cpp",
              "codex": "Codex OAuth"}.get(self.backend.name, "LM Studio")

A hand-listed map whose DEFAULT is the literal string "LM Studio". `ninfer` was
not a key, so it fell through — and so would every backend added after that
line was written.

    A FALLBACK THAT NAMES ONE PARTICULAR THING IS NOT A FALLBACK, IT IS A GUESS
    THAT CANNOT ADMIT IT IS GUESSING. `self.backend.name` can never be wrong
    about which backend it is, so the truthful default was always sitting in
    the argument.

⬜ THE ARM THAT ENCODES THE FINDING is not "ninfer reads NInfer" — that one
would pass if someone simply added a fourth key to the map. It is
`test_an_unlabelled_backend_reads_its_own_name`: a backend nobody has thought
about yet must announce ITSELF, not somebody else. That is the property the map
could never have.
"""

from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui import llm_backend
from litetui.ninfer_backend import NInferBackend
from litetui.oauth_backend import OAuthBackend
from litetui.settings import Settings


def _label(backend) -> str:
    return getattr(backend, "label", None) or backend.name


# ── every backend names itself ───────────────────────────────────────────────


@pytest.mark.parametrize("cls, name, expected", [
    (llm_backend.LMStudioBackend, "lmstudio", "LM Studio"),
    (llm_backend.LlamaCppBackend, "llamacpp", "llama.cpp"),
    (NInferBackend, "ninfer", "NInfer"),
])
def test_each_backend_carries_its_own_label(cls, name, expected):
    """⬜ The three existing labels are UNCHANGED — this is the half that stops
    the fix from being a rename."""
    assert cls.name == name
    assert cls.label == expected


def test_the_oauth_backend_labels_itself_per_provider():
    """⬜ Its `name` comes from a setting, so its label is an INSTANCE
    attribute. A class-level one would have been fixed at import for every
    provider the class serves."""
    assert _label(OAuthBackend(Settings(backend="codex"))) == "Codex OAuth"


def _header_for(backend) -> str:
    """The REAL `update_header`, driven with the smallest state it reads.

    ⚠️ THIS WENT THROUGH `_label()` AT FIRST, WHICH IS A HELPER IN THIS FILE —
    so the arm below passed against the UNFIXED build. A red-before run is what
    said so: 7 failed, and the one that passed was the only one that mattered.

        AN ARM THAT EXERCISES THE TEST'S OWN COPY OF THE LOGIC MEASURES THE
        COPY. Call the thing that shipped.
    """
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.backend = backend
    a.tools_enabled = False
    a.thinking_level = ""
    a.model_id = ""
    a._model_thinking_levels = None
    captured: list[str] = []
    type(a).sub_title = property(lambda s: "", lambda s, v: captured.append(v))
    try:
        a.update_header()
    finally:
        del type(a).sub_title
    return captured[-1] if captured else ""


def test_an_unlabelled_backend_reads_its_own_name():
    """🔴 THE ARM THAT ENCODES THE FINDING.

    Adding "ninfer" to the old map would have satisfied every other arm in this
    file. It would NOT satisfy this one: a backend nobody anticipated must name
    ITSELF, through the shipped expression. That is the whole difference
    between a list and a property.
    """
    class _Future:
        name = "something-nobody-has-written-yet"

    header = _header_for(_Future())
    assert "something-nobody-has-written-yet" in header
    assert "LM Studio" not in header


def test_the_shipped_header_names_ninfer():
    """⬜ Ryan's case, through the same real expression."""
    class _Ninferish:
        name = "ninfer"
        label = "NInfer"

    assert "NInfer" in _header_for(_Ninferish())


def test_the_header_expression_is_not_a_hand_listed_map():
    """⬜ THE STRUCTURAL GUARD. The arms above all pass on a build where the map
    simply grew a fourth key — they are about VALUES. This one is about the
    SHAPE, and it is what a reader of app.py meets first.

    ⚠️ AND IT IS AN AST CHECK BECAUSE A SUBSTRING ONE WENT RED ON MY OWN
    COMMENT. The fix's comment QUOTES the expression it replaced, so
    `'get(self.backend.name, "LM Studio")' not in src` failed against prose
    describing the thing it was looking for — the same text-gate trap that has
    now bitten this repo three times.

        A TEXT GATE COUNTS THE PROSE *ABOUT* THE THING. Parse the code.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.LiteTUI.update_header)))
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "engine" for t in n.targets)]
    assert len(assigns) == 1, "expected exactly one `engine = ...` in the header"
    value = assigns[0].value

    # It must not be `<dict literal>.get(...)` — the shape that had to list
    # every backend and guessed for the rest.
    is_dict_get = (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "get"
        and isinstance(value.func.value, ast.Dict)
    )
    assert not is_dict_get, "the header is back to a hand-listed map"

    calls = [n.func.id for n in ast.walk(value)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "getattr" in calls, "the header must ASK the backend for its label"


# ── the sweep's other finds ──────────────────────────────────────────────────


def test_the_boot_banner_does_not_enumerate_backends():
    """🔴 FOUND BY THE SWEEP, not by the report. The banner read "LM Studio +
    llama.cpp" — a backend SET that reality outgrew when NInfer and Codex
    shipped. Nothing updates a string literal."""
    assert "LM Studio + llama.cpp" not in app_mod.LITETUI_SPLASH
    assert "/backend" in app_mod.LITETUI_SPLASH


def test_the_no_models_branch_does_not_call_every_other_backend_llamacpp():
    """🔴 THE SAME DEFECT, THIRD SITE IN ONE FILE. Two branches claimed to cover
    four backends: `if lmstudio ... else` told NInfer users to add a folder of
    GGUF files, for an engine that does not read GGUF at all."""
    import inspect

    src = inspect.getsource(app_mod.LiteTUI.connect)
    assert 'elif self.backend.name == "llamacpp"' in src, (
        "the llama.cpp copy must be reached by NAME, not by being the else")
