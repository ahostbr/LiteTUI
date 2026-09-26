"""CLI prompt arguments remain byte-exact through the first submit seam."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "prompt",
    [
        r"C:\a\t1\n2",
        r"Read C:\Projects\.scratch\t1010\BRIEF.md exactly.",
    ],
)
def test_prompt_reaches_first_submit_byte_exactly(tmp_path, monkeypatch, prompt):
    """Guard argv -> first_prompt -> submit; LiteTUI must never unescape it."""
    from litetui import cli, paths, shared_state

    seen = {}

    class App:
        def __init__(self, **kwargs):
            self.first_prompt = kwargs["first_prompt"]

        def _submit_text(self, text, *, alt_chord):
            seen["submitted"] = text

        def run(self, **kwargs):
            self._submit_text(self.first_prompt, alt_chord=False)

    monkeypatch.setitem(
        sys.modules,
        "litetui.app",
        SimpleNamespace(LiteTUI=App, wants_ansi_fallback=lambda: False),
    )
    monkeypatch.setitem(
        sys.modules,
        "litetui.image_viewer",
        SimpleNamespace(init_image_backend=lambda: None),
    )
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setattr(shared_state, "check_data_version", lambda root: None)
    monkeypatch.setattr(sys, "argv", ["litetui", "--prompt", prompt])

    cli.main()

    assert seen["submitted"] == prompt
