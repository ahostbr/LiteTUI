"""The /load picker's context-length row: a toggle + a token count that set the
context for THIS load, pre-filled from the saved default.

Guards the wiring that shipped broken once already: the picker load path must
carry the chosen context into `_start_load`. Ryan, live: "add a checkbox and
number picker on the load modal screen that lets users directly set the context
right then."

These are unit tests of the reader + the override threading — the Textual widget
rendering needs the runtime and is exercised by the picker tests.
"""
from __future__ import annotations

import types

from litetui.plugins import model_switch


class _FakeWidget:
    def __init__(self, value):
        self.value = value


class _FakeBody:
    """Stands in for PickerBody: query_one returns the row's two controls."""
    def __init__(self, on, raw):
        self._w = {"#load-ctx-on": _FakeWidget(on), "#load-ctx-val": _FakeWidget(raw)}

    def query_one(self, selector, _cls=None):
        return self._w[selector]


def _read(on, raw):
    app = types.SimpleNamespace()
    model_switch._read_load_ctx(app, _FakeBody(on, raw))
    return app._load_ctx_override


def test_toggle_off_loads_at_server_default():
    # OFF wins even when a number is typed — the modal is the live choice.
    assert _read(False, "150000") is None


def test_toggle_on_with_a_number_overrides():
    assert _read(True, "150000") == 150000


def test_toggle_on_strips_thousands_commas():
    assert _read(True, "150,000") == 150000


def test_toggle_on_blank_applies_nothing():
    assert _read(True, "") is None


def test_toggle_on_garbage_applies_nothing():
    assert _read(True, "lots") is None


def test_missing_controls_fall_back_to_default():
    app = types.SimpleNamespace()

    class _Empty:
        def query_one(self, *_a, **_k):
            raise KeyError("no such control")

    model_switch._read_load_ctx(app, _Empty())
    assert app._load_ctx_override is model_switch._USE_DEFAULT


def test_on_load_picked_threads_the_override_and_clears_it(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_switch, "_start_load",
        lambda app, mid, ctx=model_switch._USE_DEFAULT: calls.append((mid, ctx)),
    )
    app = types.SimpleNamespace(_load_ctx_override=150000)
    model_switch._on_load_picked(app, "qwen3.5-4b-mtp")
    assert calls == [("qwen3.5-4b-mtp", 150000)]
    # cleared so a later plain load does not inherit it
    assert app._load_ctx_override is model_switch._USE_DEFAULT


def test_on_load_picked_cancel_loads_nothing_and_clears(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_switch, "_start_load",
        lambda app, mid, ctx=model_switch._USE_DEFAULT: calls.append((mid, ctx)),
    )
    app = types.SimpleNamespace(_load_ctx_override=999)
    model_switch._on_load_picked(app, None)          # Esc
    assert calls == []
    assert app._load_ctx_override is model_switch._USE_DEFAULT
