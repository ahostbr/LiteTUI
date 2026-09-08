"""PromptInput up-arrow history recall."""

from litetui.widgets import PromptInput


def _make_input() -> PromptInput:
    """Build a PromptInput outside a running Textual app (data-model only)."""
    inp = PromptInput.__new__(PromptInput)
    inp._history = []
    inp._hist_idx = -1
    inp._draft = ""
    return inp


class TestPushHistory:
    def test_appends(self):
        inp = _make_input()
        inp.push_history("hello")
        inp.push_history("world")
        assert inp._history == ["hello", "world"]

    def test_deduplicates_consecutive(self):
        inp = _make_input()
        inp.push_history("same")
        inp.push_history("same")
        assert inp._history == ["same"]

    def test_allows_non_consecutive_repeat(self):
        inp = _make_input()
        inp.push_history("a")
        inp.push_history("b")
        inp.push_history("a")
        assert inp._history == ["a", "b", "a"]

    def test_ignores_empty(self):
        inp = _make_input()
        inp.push_history("")
        assert inp._history == []

    def test_resets_index(self):
        inp = _make_input()
        inp.push_history("one")
        inp._hist_idx = 0
        inp.push_history("two")
        assert inp._hist_idx == -1


class TestHistoryNavigation:
    """Exercise the index arithmetic that on_key drives."""

    @staticmethod
    def _nav_up(inp: PromptInput) -> str | None:
        """Simulate what on_key(up) does, return the value it would set."""
        if not inp._history:
            return None
        if inp._hist_idx == -1:
            inp._draft = getattr(inp, "_fake_value", "")
            inp._hist_idx = len(inp._history) - 1
        elif inp._hist_idx > 0:
            inp._hist_idx -= 1
        else:
            return None
        return inp._history[inp._hist_idx]

    @staticmethod
    def _nav_down(inp: PromptInput) -> str | None:
        if inp._hist_idx == -1:
            return None
        if inp._hist_idx < len(inp._history) - 1:
            inp._hist_idx += 1
            return inp._history[inp._hist_idx]
        inp._hist_idx = -1
        return inp._draft

    def test_up_recalls_last(self):
        inp = _make_input()
        inp.push_history("first")
        inp.push_history("second")
        assert self._nav_up(inp) == "second"

    def test_up_up_recalls_older(self):
        inp = _make_input()
        inp.push_history("first")
        inp.push_history("second")
        self._nav_up(inp)
        assert self._nav_up(inp) == "first"

    def test_down_after_up_restores_draft(self):
        inp = _make_input()
        inp._fake_value = "typing..."
        inp.push_history("old")
        self._nav_up(inp)
        assert self._nav_down(inp) == "typing..."

    def test_up_at_top_stays(self):
        inp = _make_input()
        inp.push_history("only")
        self._nav_up(inp)
        assert self._nav_up(inp) is None
        assert inp._hist_idx == 0
