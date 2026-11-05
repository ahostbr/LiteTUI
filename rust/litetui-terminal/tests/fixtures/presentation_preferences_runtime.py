"""Run registered presentation preference dispatch against disposable settings, denying UI side effects."""

import json
import os
import runpy
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
attempts = []


def forbidden(*_args, **_kwargs):
    attempts.append("screen repaint or scroll attempt")
    raise AssertionError(attempts[-1])


async def serve(app):
    from litetui import gui_rpc

    actual = gui_rpc.dispatch

    def dispatch(owner, command):
        if not command["type"].startswith("gui.presentation.native_"):
            return actual(owner, command)
        with ExitStack() as guard:
            for name in ("_refresh_prompt_controls", "_scroll_down"):
                guard.enter_context(patch.object(owner, name, forbidden, create=True))
            return actual(owner, command)

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "presentation-guards.json").write_text(
            json.dumps(attempts), encoding="utf-8"
        )


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
