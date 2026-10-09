"""CLI prompt arguments remain byte-exact through the first submit seam."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from litetui.app import LiteTUI
from litetui.llm_backend import ModelRow


@pytest.mark.parametrize(
    "prompt",
    [
        r"C:\a\t1\n2",
        r"Read C:\ExampleProjects\.scratch\t1010\BRIEF.md exactly.",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        r"C:\a\t1\n2",
        r"Read C:\ExampleProjects\.scratch\t1010\BRIEF.md exactly.",
    ],
)
async def test_real_app_dispatches_first_prompt_byte_exactly(prompt):
    """Exercise the real app.py first-prompt dispatch, not a CLI class double."""
    app = LiteTUI(rpc=True, first_prompt=prompt)
    app._connect_settled = True
    app.available_models = ["model"]
    app.model_rows = {
        "model": ModelRow(key="model", path=None, source="test", loaded=True)
    }
    app._model_id = "model"
    app._cli_initial_model = None
    submitted = []

    async def ready(**_kwargs):
        return True

    app._ensure_chat_ready = ready
    app._submit_text = lambda text, **_kwargs: submitted.append(text)

    await LiteTUI._apply_cli_args.__wrapped__(app)

    assert submitted == [prompt]
