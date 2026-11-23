"""T1398: a model's reasoning-effort scale read from its own template, and what each level sends.

Ryan, 2026-11-23: "allow both manual number and presets low med high and xhigh with associated to
diffrent numbers ... DO THIS CORRECTLY BRO EVERY OTHER OPTION U LISTED WAS A PATCH OR HACK".
Acceptance (Inkling): /think low sends 0.2; /think 0.1 sends 0.1; high sends 0.9, not 0.99. Qwen still
gets what its template expects.

The templates are the REAL ones, copied byte for byte (tests/fixtures/effort_templates):
  inkling2.jinja      C:/ninfer-models/inkling2-src/hf/chat_template.jinja, sha256 53A9035F...C16DC, the
                      same bytes as the resource inside the served .ninfer artifact (read 2026-11-23)
  deepseek_v41.jinja  F:/AI_Models/litesuite/DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder/chat_template.jinja
  qwen38_strata.jinja ~/.litesuite/llm/strata/serve/chat_template.jinja (Qwen3.8: no number map)
"""
from pathlib import Path

import pytest

from litetui.effort_scale import (KWARGS, TOP, EffortScale, nearest_accepted, parse_effort_map)
from litetui.turn_engine import TurnEngine, request_thinks

FIXTURES = Path(__file__).parent / "fixtures" / "effort_templates"
INKLING = (FIXTURES / "inkling2.jinja").read_text(encoding="utf-8")
DEEPSEEK = (FIXTURES / "deepseek_v41.jinja").read_text(encoding="utf-8")
QWEN = (FIXTURES / "qwen38_strata.jinja").read_text(encoding="utf-8")

ALL = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _scale(template, names, *, number_wire=False):
    return EffortScale(names=tuple(names), numbers=parse_effort_map(template), number_wire=number_wire,
                       source="test", template_read=True)


INKLING_NINFER = _scale(INKLING, ALL)                       # NInfer today: no number channel
INKLING_LLAMA = _scale(INKLING, ALL, number_wire=True)      # llama.cpp: chat_template_kwargs is raw JSON
QWEN_SCALE = _scale(QWEN, ("none", "low", "medium", "xhigh"))
DEEPSEEK_NINFER = _scale(DEEPSEEK, ("none", "low", "high", "max"))
DEEPSEEK_LLAMA = _scale(DEEPSEEK, ("none", "low", "high", "max"), number_wire=True)


def _wire(scale, level, backend="ninfer"):
    """The extra_body a chat turn would carry for `level`."""
    req = TurnEngine.chat_request(model_id="m", messages=[], tools_enabled=False, max_tokens_tools=1,
                                  max_tokens_chat=1, request_overrides={}, thinking_level=level,
                                  backend_name=backend, effort_scale=scale)
    return req.get("extra_body", {})


# ── the templates' own maps ─────────────────────────────────────────────────────────────────────


def test_inklings_map_is_read_from_its_template():
    assert parse_effort_map(INKLING) == {"none": 0.0, "minimal": 0.1, "low": 0.2, "medium": 0.7,
                                         "high": 0.9, "xhigh": 0.99, "max": 0.99}


def test_deepseeks_map_is_read_from_its_template_and_stays_integer():
    numbers = parse_effort_map(DEEPSEEK)
    assert numbers == {"low": 50, "high": 75, "max": 100}
    assert all(type(v) is int for v in numbers.values())


def test_a_template_with_no_effort_numbers_has_no_map():
    """Qwen3.8 takes names only; its other `set` dicts (role tokens) are not effort maps."""
    assert parse_effort_map(QWEN) == {}
    assert parse_effort_map(None) == {}
    assert parse_effort_map('{%- set role_token = {"user": "<u>"} -%}') == {}
    assert parse_effort_map('{%- set m = {"low": true, "high": 1} -%}') == {}, "a bool is not a number"


# ── NInfer's nearest-accepted rule, mirrored exactly (chat_template.cpp:91-103) ──────────────────


@pytest.mark.parametrize("requested,accepted,expected", [
    ("high", ("low", "medium", "xhigh"), "xhigh"),     # tie at distance 1: UP wins
    ("max", ("low", "medium", "xhigh"), "xhigh"),
    ("minimal", ("low", "medium", "xhigh"), "low"),
    ("medium", ("low", "high", "max"), "high"),        # DeepSeek: tie, up
    ("xhigh", ("low", "high", "max"), "max"),          # tie, up
    ("minimal", ("low", "high", "max"), "low"),
    ("low", ("low", "high"), "low"),                   # accepted -> itself
    ("ultra", ("low",), "ultra"),                      # outside the order -> unchanged
    ("high", (), "high"),                              # nothing accepted -> unchanged
])
def test_nearest_accepted_mirrors_ninfer(requested, accepted, expected):
    assert nearest_accepted(requested, accepted) == expected


# ── the acceptance rows: Inkling ────────────────────────────────────────────────────────────────


def test_inkling_low_sends_low_which_its_template_maps_to_0_2():
    assert _wire(INKLING_NINFER, "low")["reasoning_effort"] == "low"
    assert INKLING_NINFER.number_for("low") == 0.2


def test_inkling_high_sends_high_0_9_not_xhigh_0_99():
    """The defect: turn_engine's fold sent `xhigh` for every `high` turn."""
    extra = _wire(INKLING_NINFER, "high")
    assert extra["reasoning_effort"] == "high"
    assert INKLING_NINFER.number_for(extra["reasoning_effort"]) == 0.9


def test_inkling_minimal_is_not_folded_to_low():
    assert _wire(INKLING_NINFER, "minimal")["reasoning_effort"] == "minimal"


def test_inkling_0_1_on_a_server_that_carries_numbers_goes_as_the_number():
    extra = _wire(INKLING_LLAMA, 0.1, "llamacpp")
    assert extra["chat_template_kwargs"] == {"reasoning_effort": 0.1}
    assert "reasoning_effort" not in extra, "a top-level string would overwrite the kwargs on llama.cpp"


def test_inkling_0_1_on_ninfer_today_goes_as_minimal_which_the_template_maps_to_0_1():
    resolved = INKLING_NINFER.resolve(0.1)
    assert (resolved.placement, resolved.value) == (TOP, "minimal")
    assert "0.1 is sent as 'minimal'" in resolved.note


def test_inkling_off_goes_through_the_template_on_every_engine():
    """llama.cpp erases a top-level "none" (server-common.cpp:1498-1505) and Inkling has no thinking
    switch, so it thought at 0.9. Through the kwargs, "none" reaches the map (level 0)."""
    for scale, backend in ((INKLING_NINFER, "ninfer"), (INKLING_LLAMA, "llamacpp")):
        extra = _wire(scale, "off", backend)
        assert extra["chat_template_kwargs"] == {"reasoning_effort": "none"}
        assert "reasoning_effort" not in extra
        assert request_thinks(extra) is False, "off must not get a thinking budget"


def test_a_manual_0_is_a_choice_not_a_blank():
    assert _wire(INKLING_LLAMA, 0, "llamacpp")["chat_template_kwargs"] == {"reasoning_effort": 0}


def test_a_number_the_server_cannot_carry_and_the_map_lacks_is_refused_with_what_works():
    why = INKLING_NINFER.refuse_number(0.5)
    assert "cannot send a number" in why
    assert "0, 0.1, 0.2, 0.7, 0.9, 0.99" in why
    assert INKLING_LLAMA.refuse_number(0.5) is None
    assert "outside" in INKLING_LLAMA.refuse_number(1.5)


def test_a_saved_number_that_cannot_go_lands_on_the_nearest_mapped_number_and_says_so():
    resolved = INKLING_NINFER.resolve(0.5)          # 0.7 is 0.2 away, 0.2 is 0.3 away
    assert (resolved.placement, resolved.value) == (TOP, "medium")
    assert resolved.note.startswith("0.5 -> medium (0.7)")


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), -0.1, "0.1x"])
def test_bad_numbers_are_refused(bad):
    assert INKLING_LLAMA.refuse_number(bad) is not None


# ── Qwen3.8: names only ─────────────────────────────────────────────────────────────────────────


def test_qwen_gets_the_names_its_template_takes():
    assert _wire(QWEN_SCALE, "xhigh")["reasoning_effort"] == "xhigh"
    assert _wire(QWEN_SCALE, "low")["reasoning_effort"] == "low"
    assert _wire(QWEN_SCALE, "off")["reasoning_effort"] == "none", "Qwen's off is its thinking switch"


def test_a_saved_high_on_qwen_lands_on_xhigh_and_the_ui_is_told():
    """Not omitted (that would hand the turn to the template default): the nearest accepted name."""
    resolved = QWEN_SCALE.resolve("high")
    assert (resolved.placement, resolved.value) == (TOP, "xhigh")
    assert resolved.note == "high -> xhigh: this model's template has no 'high'"


def test_qwen_has_no_numbers_to_choose():
    assert "no effort numbers" in QWEN_SCALE.refuse_number(0.5)
    assert QWEN_SCALE.resolve(0.5).placement is None


# ── DeepSeek: names low/high/max, integers 1-100 ─────────────────────────────────────────────────


def test_a_saved_medium_on_deepseek_lands_on_high_75():
    resolved = DEEPSEEK_NINFER.resolve("medium")
    assert resolved.value == "high"
    assert resolved.note == "medium -> high (75): this model's template has no 'medium'"


def test_deepseek_60_is_sent_where_numbers_travel_and_refused_where_they_do_not():
    assert _wire(DEEPSEEK_LLAMA, 60, "llamacpp")["chat_template_kwargs"] == {"reasoning_effort": 60}
    assert "50, 75, 100" in DEEPSEEK_NINFER.refuse_number(60)
    assert DEEPSEEK_NINFER.resolve(75).value == "high"
    assert "whole numbers" in DEEPSEEK_LLAMA.refuse_number(60.5)


# ── labels and the budget ───────────────────────────────────────────────────────────────────────


def test_labels_carry_the_number_a_preset_sends():
    assert INKLING_NINFER.label("high") == "high · 0.9"
    assert INKLING_NINFER.label("none") == "off · 0"
    assert INKLING_NINFER.label("xhigh") == "xhigh · 0.99"
    assert QWEN_SCALE.label("xhigh") == "xhigh"


def test_a_number_gets_the_unscaled_budget():
    """Only Strata/Xolotl/Basalt scale the budget by level name, and none of them carries a number."""
    req = TurnEngine.chat_request(model_id="m", messages=[], tools_enabled=False, max_tokens_tools=1,
                                  max_tokens_chat=100, request_overrides={}, thinking_level=0.1,
                                  backend_name="ninfer", effort_scale=INKLING_NINFER, thinking_budget=1000)
    assert req["extra_body"]["thinking_budget"] == 1000
    assert req["max_tokens"] == 1100


def test_the_compaction_request_resolves_through_the_same_scale():
    req = TurnEngine.compact_request(model_id="m", messages=[], max_tokens=10, thinking_level="high",
                                     tools_enabled=False, backend_name="ninfer", effort_scale=QWEN_SCALE)
    assert req["extra_body"]["reasoning_effort"] == "xhigh"


def test_a_template_that_was_not_read_sends_the_name_verbatim_and_says_so():
    unread = EffortScale(names=("none", "low", "medium", "xhigh"), source="backend list")
    resolved = unread.resolve("high")
    assert (resolved.placement, resolved.value, resolved.note) == (TOP, "high", "template not read")


def test_a_per_model_override_number_beats_the_global_level():
    req = TurnEngine.chat_request(model_id="m", messages=[], tools_enabled=False, max_tokens_tools=1,
                                  max_tokens_chat=1, request_overrides={"reasoning_effort": 0.1},
                                  thinking_level="high", backend_name="llamacpp", effort_scale=INKLING_LLAMA)
    assert req["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": 0.1}


# ── Prefix fcb8e996 (1): the compaction re-pin renders the chat turn's effort byte for byte ──────


def _effort_fields(extra):
    return {"top": extra.get("reasoning_effort"),
            "kwargs": (extra.get("chat_template_kwargs") or {}).get("reasoning_effort")}


@pytest.mark.parametrize("scale,level,backend", [
    (INKLING_NINFER, "high", "ninfer"),       # a name, top-level
    (INKLING_LLAMA, 0.1, "llamacpp"),         # a number, in the template kwargs
    (INKLING_NINFER, "off", "ninfer"),        # Inkling's off, "none" in the template kwargs
    (QWEN_SCALE, "off", "ninfer"),            # Qwen's off, top-level "none"
])
def test_the_pinned_compaction_carries_the_chat_turns_effort_in_the_same_place(scale, level, backend):
    """app.py stores (value, placement) from the chat request and re-pins the cache-friendly
    compaction with pin_effort. The compaction's own level (compact_thinking_level "low") must not
    survive, and the effort must sit where the chat turn put it: a top-level "none" and a kwargs
    "none" render different prompts on llama.cpp."""
    from litetui.turn_engine import chat_effort, pin_effort

    chat = _wire(scale, level, backend)
    value, placement = chat_effort(chat)
    compact = TurnEngine.compact_request(model_id="m", messages=[], max_tokens=10, thinking_level="low",
                                         tools_enabled=False, backend_name=backend, effort_scale=scale,
                                         pinned_effort=value)
    pinned = pin_effort(compact.get("extra_body") or {}, value, placement)
    assert _effort_fields(pinned) == _effort_fields(chat)


def test_pin_effort_with_no_chat_effort_removes_the_compactions_own():
    from litetui.turn_engine import pin_effort

    assert pin_effort({"reasoning_effort": "low", "return_progress": True}, None, None) == {"return_progress": True}
    assert pin_effort({"chat_template_kwargs": {"reasoning_effort": 0.1}}, None, None) == {}
    assert pin_effort({}, "xhigh", None) == {"reasoning_effort": "xhigh"}, "an older caller's bare value"


# ── Prefix fcb8e996 (2): a number that cannot be sent says so where the user looks ───────────────


def test_a_global_number_on_a_model_with_no_numbers_says_not_sent_in_the_header_and_the_line():
    from litetui.effort_scale import describe

    chip, line = describe(0.1, QWEN_SCALE, "NInfer")
    assert chip == "think:0.1 (not sent)"
    assert line == "0.1 not sent: this model's template has no effort numbers, so the template default applies."


def test_a_number_on_a_backend_with_no_scale_says_not_sent():
    from litetui.effort_scale import describe

    chip, line = describe(0.1, None, "Strata")
    assert chip == "think:0.1 (not sent)"
    assert line.startswith("0.1 not sent: Strata takes level names only")
    assert describe("high", None, "Strata") == ("think:high", "")


def test_the_header_names_the_number_and_any_substitution():
    from litetui.effort_scale import describe

    assert describe("high", INKLING_NINFER)[0] == "think:high · 0.9"
    assert describe(0.1, INKLING_NINFER)[0] == "think:0.1 (as minimal)"
    assert describe(0.1, INKLING_LLAMA)[0] == "think:0.1"
    assert describe("high", QWEN_SCALE)[0] == "think:high -> xhigh"
    assert describe("off", INKLING_NINFER)[0] == "think:off · 0"
    assert describe(None, INKLING_NINFER)[0] == "think:default"