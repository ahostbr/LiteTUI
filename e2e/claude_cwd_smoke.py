"""Live: on the Claude backend, Claude works in the folder LiteTUI was launched from
(plan claude-backend-litetui-identity, phase 3). Ryan: "it should use whatever its cwd is
i just ran it from there".

Run it FROM the folder under test; that folder is where Claude's `pwd` must land:

    cd C:\\Projects\\LiteBench
    set LITETUI_CLAUDE_LIVE=1 && python C:\\Projects\\LiteTUI\\e2e\\claude_cwd_smoke.py

Isolated LiteTUI state; the only command Claude is asked to run is `pwd`.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import app as app_mod  # noqa: E402
from litetui import paths, settings  # noqa: E402


async def settle(app, pilot, seconds=240):
    for _ in range(seconds * 10):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in ("init", "chat") for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


async def main():
    launched = Path.cwd().resolve()
    with tempfile.TemporaryDirectory(prefix="litetui-claude-cwd-") as directory:
        paths.CONVO_DIR = Path(directory) / "convos"
        settings.settings_path = lambda root=None: Path(directory) / "settings.json"
        original_load = settings.load
        cfg = settings.Settings(backend="claude", backend_chosen=True, default_model="sonnet",
            skills_enabled=False, mcp_enabled=False, tools_enabled=True,
            autocompact_enabled=False, wake_after_compact=False)
        settings.load = lambda *a, **k: cfg
        app = app_mod.LiteTUI()
        settings.load = original_load
        async with app.run_test(size=(125, 42)) as pilot:
            await settle(app, pilot)
            app._submit_text("Run `pwd` with your Bash tool and reply with only its output.", False)
            await settle(app, pilot)
            answer = app.conversation[-1].get("content") or ""
            segment = app._claude_ledger.selected
            report = {"launched_from": str(launched), "segment_workspace": segment["workspace"],
                      "answer": answer, "paths_root": str(paths.ROOT)}
            print(json.dumps(report, indent=1))
            await app.backend.close()
    assert segment["workspace"] == str(launched), report
    assert launched.name.lower() in answer.lower(), answer
    assert str(paths.ROOT).lower() != str(launched).lower(), "launch from a folder that is not LiteTUI's own"
    print(f"PASS Claude's cwd is the launch folder ({launched})")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit model probe")
    asyncio.run(main())
