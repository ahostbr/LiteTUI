"""Collapsible assistant card: header text, manual fold, and the auto-collapse latch.

Ryan, 2026-09-16: the whole card folds like a thinking block, the header shows the
model while generating and "<one-line summary> - <model>" once summarised, and a
finished card folds itself on the way off screen.

These are the parts that are pure enough to test without a running app. The
geometry call itself (`_autocollapse_offscreen`) needs a live layout; what is
covered here is the decision the geometry feeds — including the LATCH, which is
the property that stops a height change from feeding back into another collapse.
"""

from litetui.widgets import AssistantMessage


def _card(model="qwen3-30b", settled=True):
    card = AssistantMessage()
    card.set_model_name(model)
    card.settled = settled
    return card


class TestHeader:
    def test_model_name_replaces_AI_while_generating(self):
        card = _card()
        assert "qwen3-30b" in card.border_title
        assert "AI" not in card.border_title

    def test_summary_then_model(self):
        card = _card()
        card.set_summary("Fixed the captions fetch")
        assert card.border_title.endswith("Fixed the captions fetch - qwen3-30b")

    def test_blank_summary_keeps_model_fallback(self):
        card = _card()
        card.set_summary("   ")
        assert card.summary is None
        assert card.border_title.endswith("qwen3-30b")

    def test_summary_is_forced_to_one_line(self):
        card = _card()
        card.set_summary("two\nlines   and  runs")
        assert "\n" not in card.border_title
        assert "two lines and runs" in card.border_title

    def test_no_model_falls_back_to_AI(self):
        card = _card(model="")
        assert card.border_title.endswith("AI")

    def test_marker_tracks_fold_state(self):
        card = _card()
        assert card.border_title.startswith(AssistantMessage.MARK_OPEN)
        card.set_collapsed(True)
        assert card.border_title.startswith(AssistantMessage.MARK_SHUT)
        card.set_collapsed(False)
        assert card.border_title.startswith(AssistantMessage.MARK_OPEN)

    def test_summary_survives_a_fold_toggle(self):
        # The header is rebuilt on every toggle; a card that lost its summary
        # when folded would read as the summary never having arrived.
        card = _card()
        card.set_summary("Did the thing")
        card.set_collapsed(True)
        assert "Did the thing" in card.border_title
        card.set_collapsed(False)
        assert "Did the thing" in card.border_title


class TestFold:
    def test_collapsed_class_drives_the_css(self):
        card = _card()
        assert not card.has_class("collapsed")
        card.set_collapsed(True)
        assert card.has_class("collapsed")

    def test_starts_expanded(self):
        assert not _card().collapsed


class TestAutocollapseLatch:
    def test_unsettled_card_never_folds(self):
        # A streaming card is the one being read.
        assert _card(settled=False).autocollapse() is False

    def test_settled_card_folds_once(self):
        card = _card()
        assert card.autocollapse() is True
        assert card.collapsed

    def test_second_call_is_a_no_op(self):
        card = _card()
        card.autocollapse()
        assert card.autocollapse() is False

    def test_manual_reopen_is_never_refolded(self):
        # THE LATCH. Without it, every autoscroll frame would re-fold a card the
        # reader deliberately opened, and the fold would fight the reader.
        card = _card()
        card.autocollapse()
        card.set_collapsed(False)          # reader opens it by hand
        assert card.autocollapse() is False
        assert not card.collapsed

    def test_already_folded_by_hand_is_not_latched_by_autocollapse(self):
        card = _card()
        card.set_collapsed(True)           # reader folded it themselves
        assert card.autocollapse() is False
