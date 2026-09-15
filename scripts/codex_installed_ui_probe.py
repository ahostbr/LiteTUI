"""Synthetic interactive acceptance against an installed wheel, without inference."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace


async def probe(output, screenshots):
    with tempfile.TemporaryDirectory(prefix="litetui installed ui ") as directory, ExitStack() as cleanup:
        root = Path(directory)
        os.environ.update(LITETUI_NO_HARNESS="1", LITETUI_BACKEND="lmstudio",
                          LITETUI_DATA_ROOT=str(root))
        from litetui import app as app_module
        from litetui import paths, runtime_log
        from litetui.codex_app_server import AppServer
        from litetui.codex_tool_ui import CodexToolUI
        from litetui.settings_screen import SettingsBody, SettingsScreen
        from litetui.widgets import ToolMessage

        # The process-wide recorder outlives run_test; close this probe's sink
        # before Windows attempts to remove its temporary data directory.
        cleanup.callback(lambda: runtime_log._ACTIVE.close() if runtime_log._ACTIVE else None)

        assert sys.prefix != sys.base_prefix
        assert Path(app_module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        paths.CONVO_DIR = root / ".convos"
        paths.LLAMA_DIR = root / ".llama"

        async def forbidden(*args, **kwargs):
            raise AssertionError("Synthetic UI probe must not start Codex")

        AppServer.start = forbidden
        app = app_module.LiteTUI()
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        screenshots.mkdir(parents=True, exist_ok=True)
        async with app.run_test(size=(110, 40)) as pilot:
            app.backend = SimpleNamespace(name="codex", remote=True, shutdown=lambda: None,
                                          reasoning_levels=lambda _: ["low", "medium", "high", "xhigh", "max", "ultra"])
            app.model_id = "gpt-6-astra"
            ui = CodexToolUI(app, thread_id="synthetic", turn_id="installed")
            item = {"type": "commandExecution", "id": "command", "command": "synthetic inspection"}
            await ui.item(item)
            ui.progress({"itemId": "command", "delta": "Checking synthetic files\n"})
            await ui.item({**item, "status": "completed", "exitCode": 0,
                           "aggregatedOutput": "Synthetic result\nSecond line", "durationMs": 1250}, True)
            await pilot.pause()
            card = list(app.query(ToolMessage))[-1]
            assert not card.expanded and card._took == 1.25
            card.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(card.header)
            await pilot.pause()
            assert card.expanded and "Synthetic result" in card.body.content.plain
            for width in (48, 110):
                await pilot.resize_terminal(width, 40)
                await pilot.pause()
                assert card.expanded and card.region.right <= app.size.width
                app.save_screenshot(f"installed-codex-{width}.svg", path=str(screenshots))
            app.push_screen(SettingsScreen(app.settings))
            await pilot.pause()
            body = app.screen.query_one(SettingsBody)
            assert body.query_one("#f-temperature").disabled
            assert body.query_one("#f-default_context_length").disabled
            assert not body.query_one("#f-thinking_level").disabled
            app.screen.action_cancel()
            await pilot.pause()
            ui.finish()
        result = {"installed_module_provenance": True, "native_inference_forbidden": True,
                  "tool_card_click_expands": True, "resize_preserves_expansion": True,
                  "widths": [48, 110], "native_duration_preserved": True,
                  "codex_settings_scope": True, "synthetic_events_only": True}
    result["temporary_data_removed"] = not root.exists()
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output.resolve(), args.screenshots.resolve()))
