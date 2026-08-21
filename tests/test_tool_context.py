"""Tool-output context modes: off / llm-tool-mask / llm-tool-summ.

Pure-function tests — no model, no app, no live tool. The decision logic lives
outside the Textual widget precisely so it can be exercised like this.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tool_context import (  # noqa: E402
    DEFAULT_THRESHOLD_CHARS,
    MASK,
    MODES,
    NEVER_PROCESS,
    OFF,
    ROUTE_MASK,
    ROUTE_SUMMARISE,
    SUMM,
    VERBATIM,
    plan_tool_result,
    render_mask,
    render_summary,
    summarise_prompt,
)

BIG = "x" * (DEFAULT_THRESHOLD_CHARS + 500)
SMALL = "x" * 10


def test_CONTROL_off_is_verbatim_at_any_size():
    """`off` is the baseline the other two are measured against. If it ever
    starts processing, every comparison made with it is meaningless."""
    for raw in (SMALL, BIG, "y" * 500_000):
        plan = plan_tool_result(OFF, "bash", raw)
        assert plan.route == VERBATIM
        assert plan.context_text == raw
        assert plan.store_raw is False


def test_CONTROL_an_unknown_mode_keeps_everything():
    # A typo in a settings file must not silently start discarding tool output.
    # The safe direction is the one that keeps it.
    plan = plan_tool_result("llm-tool-summarise", "bash", BIG)
    assert plan.route == VERBATIM
    assert plan.context_text == BIG
    assert "unknown mode" in plan.reason


@pytest.mark.parametrize("mode", [MASK, SUMM])
def test_small_output_is_verbatim_in_every_mode(mode):
    # Below the threshold a summary can easily be LONGER than what it replaces,
    # and a side call would cost a prompt-eval to save nothing.
    plan = plan_tool_result(mode, "bash", SMALL)
    assert plan.route == VERBATIM
    assert plan.store_raw is False


@pytest.mark.parametrize("mode", [MASK, SUMM])
def test_view_image_is_never_processed(mode):
    # view_image returns image content, not prose. There is nothing to
    # summarise and a summary would silently destroy the payload.
    plan = plan_tool_result(mode, "view_image", BIG)
    assert plan.route == VERBATIM
    assert plan.context_text == BIG
    assert "never processed" in plan.reason


@pytest.mark.parametrize("mode", [MASK, SUMM])
def test_CONTROL_both_processing_modes_store_the_raw(mode):
    """THE LOAD-BEARING GUARANTEE. Both modes preserve the raw on disk, so they
    differ ONLY in what reaches the conversation. Drop this and two things break
    at once: the A/B stops being a fair comparison, and the feature turns into
    lossy compression an agent cannot detect."""
    plan = plan_tool_result(mode, "bash", BIG)
    assert plan.store_raw is True
    assert plan.route != VERBATIM


def test_mask_and_summ_take_different_routes():
    # Guards against the two modes collapsing into one implementation, which
    # would make the toggle look like it works while measuring nothing.
    assert plan_tool_result(MASK, "bash", BIG).route == ROUTE_MASK
    assert plan_tool_result(SUMM, "bash", BIG).route == ROUTE_SUMMARISE


def test_summarise_defers_its_text_until_the_side_call():
    plan = plan_tool_result(SUMM, "read", BIG)
    assert plan.context_text is None, "summary text cannot exist before the model produces it"


def test_mask_needs_no_model():
    plan = plan_tool_result(MASK, "read", BIG)
    assert plan.context_text is None  # rendered by render_mask, but with no model call
    assert plan.route == ROUTE_MASK


@pytest.mark.parametrize("mode", MODES)
def test_every_plan_states_a_reason(mode):
    # A mode that silently does nothing is indistinguishable from a mode that
    # is broken. The reason is what tells them apart in a log.
    for raw in (SMALL, BIG):
        assert plan_tool_result(mode, "bash", raw).reason


def test_threshold_is_exclusive_and_configurable():
    exact = "x" * 100
    assert plan_tool_result(MASK, "bash", exact, threshold_chars=100).route == ROUTE_MASK
    assert plan_tool_result(MASK, "bash", exact, threshold_chars=101).route == VERBATIM


class TestRendering:
    def test_mask_names_the_tool_the_size_and_the_pointer(self):
        out = render_mask("bash", "a\nb\nc", "tool-outputs/0042.txt")
        assert "bash" in out
        assert "tool-outputs/0042.txt" in out
        assert "5 chars" in out and "3 lines" in out

    def test_summary_carries_the_pointer_AND_the_summary(self):
        out = render_summary("read", BIG, "tool-outputs/7.txt", "  three functions changed  ")
        assert "tool-outputs/7.txt" in out
        assert "three functions changed" in out
        assert out.count("tool-outputs/7.txt") == 1

    def test_CONTROL_neither_renderer_can_omit_the_pointer(self):
        # Without the pointer both modes are destructive, and an agent cannot
        # tell "not in the output" from "removed by this feature".
        assert "ptr" in render_mask("bash", BIG, "ptr")
        assert "ptr" in render_summary("bash", BIG, "ptr", "s")

    def test_size_is_reported_from_the_RAW_not_the_replacement(self):
        # Reporting the placeholder's own length would tell the agent nothing
        # about what it is missing.
        out = render_summary("bash", "z" * 9999, "p", "tiny")
        assert "9999 chars" in out


class TestSummarisePrompt:
    def test_it_anchors_on_the_task_not_the_last_message(self):
        p = summarise_prompt("find the failing migration", "bash", BIG)
        assert "find the failing migration" in p

    def test_it_demands_identifiers_verbatim(self):
        # Identifiers are what a later turn needs and they are cheap; prose is
        # what is expensive. A summariser told only "be brief" drops the paths.
        p = summarise_prompt("t", "read", BIG)
        low = p.lower()
        for token in ("verbatim", "path", "line number", "id", "error"):
            assert token in low

    def test_it_forbids_inventing_relevance(self):
        assert "inventing relevance" in summarise_prompt("t", "bash", BIG)

    def test_the_raw_is_included(self):
        p = summarise_prompt("t", "bash", "NEEDLE-12345")
        assert "NEEDLE-12345" in p


def test_CONTROL_the_never_process_set_is_not_empty():
    # If this empties, view_image output starts getting summarised and the
    # image payload is destroyed with no error.
    assert NEVER_PROCESS
    assert "view_image" in NEVER_PROCESS
