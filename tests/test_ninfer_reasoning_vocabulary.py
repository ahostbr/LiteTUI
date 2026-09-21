"""One template-safe reasoning vocabulary across NInfer's controls.

The server protocol parses seven OpenAI effort spellings, but the shipped
Qwen3.8 artifact rejects `minimal`, `high`, and `max` in its Jinja template.
The UI therefore offers `off`, `low`, `medium`, and `xhigh`; `off` becomes
`none` on the wire. The arms below keep both control paths in agreement and
preserve the wider-protocol error shape as a compatibility check.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from litetui.app import _plain_backend_error
from litetui.ninfer_backend import (
    NINFER_REASONING_LEVELS,
    NInferBackend,
    classify_ninfer_error,
    ninfer_error_sentence,
)
from litetui.plugins.misc import UNSET, _thinking_rows
from litetui.settings import THINKING_LEVELS, Settings
from litetui.thinking_capabilities import set_thinking, thinking_capabilities

#: The engine's own words, copied verbatim from a live 400 and nowhere else:
#:
#:   POST http://127.0.0.1:58755/v1/chat/completions
#:   {"model":"qwen3_6_35b_a3b", ..., "reasoning_effort":"ultra"}   -> HTTP 400
#:
#: This remains evidence for the engine's wider protocol vocabulary; it is not
#: the list exposed by LiteTUI because the loaded template is narrower.
ENGINE_400_MESSAGE = ("reasoning_effort must be one of none, minimal, low, "
                      "medium, high, xhigh, or max")


def _app(backend_name="ninfer", *, model="qwen3_6_35b_a3b", backend=None,
         discovered=None, graded=(), levels=None):
    class App:
        pass

    a = App()
    if backend is None:
        backend = type("B", (), {})()
        if levels is not None:
            backend.reasoning_levels = lambda _m=None, _l=levels: list(_l)
    backend.name = backend_name
    a.backend = backend
    a.model_id = model
    a._model_thinking_levels = discovered
    a.settings = Settings(lmstudio_graded_thinking_models=list(graded))
    a.thinking_level = None
    return a


def _ninfer_app():
    return _app("ninfer", backend=NInferBackend.__new__(NInferBackend))


# ── the vocabulary is the engine's ───────────────────────────────────────────


def test_the_vocabulary_is_exactly_the_template_safe_set():
    """Protocol acceptance is insufficient when the loaded template rejects it."""
    assert list(NINFER_REASONING_LEVELS) == ["none", "low", "medium", "xhigh"]


def test_template_unsupported_levels_are_not_offered_or_accepted():
    """The Qwen3.8 artifact raises `invalid_prompt` for these values."""
    app = _ninfer_app()
    for level in ("minimal", "high", "max"):
        assert level not in _offered(app)
        with pytest.raises(ValueError):
            set_thinking(app, level)


def test_a_level_the_engine_does_not_take_is_still_refused():
    """⬜ THE CONTROL. The refusal machinery was never broken — the table it
    consulted was short. `ultra` is the exact level whose 400 produced the
    message this file pins."""
    app = _ninfer_app()
    for level in ("ultra", "banana"):
        with pytest.raises(ValueError):
            set_thinking(app, level)


def test_off_is_the_ui_spelling_of_the_engines_none():
    """⬜ ONE TRANSLATION, ONE PLACE. The engine takes `none`; a person picks
    `off`. If those two ever stopped being the same fact the vocabularies would
    only LOOK equal."""
    levels = thinking_capabilities(_ninfer_app())["levels"]
    assert "off" in levels and "none" not in levels
    assert "none" in NINFER_REASONING_LEVELS


# ── the two vocabularies agree ───────────────────────────────────────────────


def _offered(app):
    return [lv for lv, _ in _thinking_rows(app) if lv != UNSET]


def _accepted(app):
    return [lv for lv in thinking_capabilities(app)["levels"] if lv != "default"]


@pytest.mark.parametrize("app,tag", [
    (_ninfer_app(), "ninfer"),
    (_app("codex", levels=["minimal", "low", "medium", "high", "xhigh"]), "codex"),
    (_app("llamacpp"), "llamacpp"),
    (_app("lmstudio", discovered=["low", "medium", "high"],
          graded=("qwen3_6_35b_a3b",)), "lmstudio-graded"),
])
def test_what_think_offers_is_what_set_thinking_accepts(app, tag):
    """🔴 THE ARM FOR THE REAL DEFECT. Before this, ninfer offered `high` from
    one door and refused it at the other.

    It compares the app with ITSELF, so it cannot be satisfied by both sides
    being wrong in the same way — which is precisely what a single hard-coded
    expected list here would have allowed."""
    assert _offered(app) == _accepted(app), tag


def test_every_level_the_picker_offers_can_actually_be_set():
    """🔴 THE STRONGER FORM, and the one a user feels: a row you can pick and
    then be told is unsupported is a control that does nothing."""
    app = _ninfer_app()
    for level in _offered(app):
        set_thinking(app, level)
        assert app.thinking_level == level


def test_a_backend_that_reports_nothing_falls_back_instead_of_showing_nothing():
    """🔴 ADDED BECAUSE A MUTANT SURVIVED. `backend_levels` returns None rather
    than `[]` when a backend answers with an empty list, and its docstring says
    why — but nothing pinned it, so replacing `or None` with `[]` passed every
    arm in this file.

        A DOCSTRING IS NOT AN ASSERTION. The distinction it described was
        exactly the one no test could see.

    `[]` would read as "this backend supports no levels" and leave the picker
    empty; None means "it did not answer", which is what the fallback is for.
    """
    from litetui.thinking_capabilities import backend_levels

    app = _app("whatever", levels=[])
    assert backend_levels(app) is None
    assert _offered(app) == list(THINKING_LEVELS)


# ── the known remaining gap, pinned rather than hidden ───────────────────────


def test_an_lmstudio_model_outside_the_graded_list_still_disagrees():
    """🔴 NOT FIXED, NOT EXEMPTED, PINNED — AND IT PREDATES THIS CARD.

    For an LM Studio model that is NOT in `lmstudio_graded_thinking_models`,
    `_resolve_reasoning_effort` drops the level on the wire, so
    `thinking_capabilities` reports NOTHING while `/think` still offers the
    global list. The two doors disagree for that one case.

    This arm exists so the gap cannot widen unnoticed and cannot be mistaken
    for the defect T839 fixed. Which way it should close — `/think` hiding
    levels that are silently dropped, or `thinking_capabilities` reporting them
    with a caveat — is a UX decision, raised and not taken here.
    """
    app = _app("lmstudio", discovered=["low", "medium", "high"], graded=())
    assert _accepted(app) == []
    assert _offered(app) == ["low", "medium", "high"]


# ── T837-b: the error class whose `code` is null ─────────────────────────────


def _bad_reasoning_effort():
    """The live 400, rebuilt: `code` is **null** on this class, which is the
    whole point of the arms below."""
    request = httpx.Request("POST", "http://127.0.0.1:58755/v1/chat/completions")
    return openai.BadRequestError(
        ENGINE_400_MESSAGE,
        response=httpx.Response(400, request=request),
        body={"error": {"code": None, "message": ENGINE_400_MESSAGE,
                        "param": "reasoning_effort",
                        "type": "invalid_request_error"}})


def test_this_error_class_carries_no_code_at_all():
    """🔴 THE SHAPE FACT NEONRELAY'S T829 NEEDS. `context_length_exceeded`
    carries a real string code; THIS class carries null, so a consumer keying
    on `code` gets None for every one of them. The discriminators that ARE
    present are `param` and `type`."""
    body = _bad_reasoning_effort().body
    assert body["error"]["code"] is None
    assert classify_ninfer_error(body) == (None, None)
    assert ninfer_error_sentence(body, "") == ""
    assert body["error"]["param"] == "reasoning_effort"
    assert body["error"]["type"] == "invalid_request_error"


def test_the_user_still_gets_the_engines_own_sentence():
    """🔴 AND THE CONSEQUENCE IS FINE, WHICH IS WORTH PINNING TOO. With no code
    to classify, `_plain_backend_error` falls through to the status branch
    (T824) and hands over the engine's own words — not the old "Something went
    wrong talking to the model server."""
    said = _plain_backend_error(_bad_reasoning_effort(), "ninfer")
    assert "400" in said
    assert "must be one of" in said
    assert "Something went wrong" not in said
