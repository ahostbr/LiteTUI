"""Live Codex through the actual Textual turn and compact consumers; temp state."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import app as app_mod
from litetui import paths, settings

ART = Path(__file__).resolve().parent / "artifacts" / "oauth"


async def settle(app, pilot):
    for _ in range(240):
        await pilot.pause(0.25)
        if not any(w.is_running and w.group in ("init", "chat") for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


async def main():
    ART.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="litetui-oauth-ui-") as directory:
        paths.CONVO_DIR = Path(directory) / "convos"
        settings.settings_path = lambda root=None: Path(directory) / "settings.json"
        original_load = settings.load
        cfg = settings.Settings(
            backend="codex",
            backend_chosen=True,
            default_model="gpt-5.5",
            skills_enabled=False,
            mcp_enabled=False,
            tools_enabled=False,
            autocompact_enabled=False,
            wake_after_compact=False,
            compact_keep_recent=0,
            clear_screen_after_compact=False,
        )
        settings.load = lambda *a, **k: cfg
        app = app_mod.LiteTUI()
        settings.load = original_load
        async with app.run_test(size=(125, 42)) as pilot:
            await settle(app, pilot)
            assert app.backend.name == "codex" and app.model_id == "gpt-5.5"
            app.conversation = [
                {
                    "role": "system",
                    "content": "You are LiteTUI. Follow the user instructions exactly.",
                }
            ]
            app._all_tools = list
            app._append(
                {
                    "role": "user",
                    "content": "Remember that the test password is ORCHID. Reply ORCHID only.",
                }
            )
            app._stream()
            await settle(app, pilot)
            assert "ORCHID" in (app.conversation[-1].get("content") or ""), (
                "No live answer"
            )
            app._append({"role": "user", "content": "Continue remembering ORCHID."})
            app._append({"role": "assistant", "content": "ORCHID"})
            app._compact()
            await settle(app, pilot)
            assert (
                "Summary of earlier conversation" in app.conversation[1]["content"]
            ), "Compaction failed"
            assert "ORCHID" in app.conversation[1]["content"], "Compaction lost context"
            app._append(
                {
                    "role": "user",
                    "content": "What test password did I tell you? Reply with just that word.",
                }
            )
            app._stream()
            await settle(app, pilot)
            assert "ORCHID" in (app.conversation[-1].get("content") or ""), (
                "Post-compaction continuation failed"
            )
            app.save_screenshot(filename="codex-tui.svg", path=str(ART))
            print(
                json.dumps(
                    {
                        "live_tui_chat": "PASS",
                        "live_compaction": "PASS",
                        "post_compact_continuation": "PASS",
                        "model": app.model_id,
                    }
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
