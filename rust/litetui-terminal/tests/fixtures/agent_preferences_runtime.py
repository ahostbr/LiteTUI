"""Run registered agent-preference dispatch against disposable settings, denying every index, model and launch boundary."""

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
    attempts.append("skills refresh, model load, provider request or seat launch attempt")
    raise AssertionError(attempts[-1])


async def serve(app):
    from litetui import gui_rpc, skills, small_task_dispatch, subagent_dispatch

    actual = gui_rpc.dispatch

    def dispatch(owner, command):
        if not command["type"].startswith("gui.agent.native_"):
            return actual(owner, command)
        with ExitStack() as guard:
            for module, names in (
                (skills, ("discover_all", "write_cache")),
                (small_task_dispatch, ("dispatch",)),
                (subagent_dispatch, ("dispatch",)),
            ):
                for name in names:
                    if hasattr(module, name):
                        guard.enter_context(patch.object(module, name, forbidden))
            return actual(owner, command)

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "agent-guards.json").write_text(json.dumps(attempts), encoding="utf-8")


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
