"""Live: an image attached on the Claude backend reaches Claude as a file path it opens.

Ryan, 2026-09-24: "claude didnt want to take the image, we should convert it to a path on
disk for claude and paste it to him then". The image is attached the way Ctrl+V does
(`pending_image`), submitted through the real submit door, and Claude must name what is in
it. Isolated LiteTUI state; the official CLI's own auth.
"""
import asyncio
import base64
import io
import json
import os
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from PIL import Image  # noqa: E402

from litetui import app as app_mod  # noqa: E402
from litetui import paths, settings  # noqa: E402


async def settle(app, pilot, seconds=300):
    for _ in range(seconds * 10):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in ("init", "chat") for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


def two_colour_png():
    img = Image.new("RGB", (256, 128), (220, 20, 20))
    img.paste((20, 40, 220), (128, 0, 256, 128))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


async def main():
    with tempfile.TemporaryDirectory(prefix="litetui-claude-image-") as directory:
        paths.CONVO_DIR = Path(directory) / "convos"
        settings.settings_path = lambda root=None: Path(directory) / "settings.json"
        original_load = settings.load
        cfg = settings.Settings(backend="claude", backend_chosen=True, default_model="default",
            skills_enabled=False, mcp_enabled=False, tools_enabled=True,
            autocompact_enabled=False, wake_after_compact=False)
        settings.load = lambda *a, **k: cfg
        app = app_mod.LiteTUI()
        settings.load = original_load
        emitted = []
        app._rpc_emit = emitted.append
        async with app.run_test(size=(125, 42)) as pilot:
            await settle(app, pilot)
            assert app.backend.name == "claude", app.backend.name
            app.pending_image = two_colour_png()
            app._submit_text("What colour is the left half of this image, and what colour is the right half? "
                             "Answer as: LEFT=<colour> RIGHT=<colour>", False)
            await settle(app, pilot)
            sent = app._claude_ledger.selected["entries"][-1]["content"]
            saved = sorted((app.convo_dir / "images").glob("*.png"))
            answer = app.conversation[-1].get("content") or ""
            out = Path("artifacts")
            out.mkdir(exist_ok=True)
            app.save_screenshot(filename="claude-image-smoke.svg", path=str(out.resolve()))
            report = {"sent_to_claude": sent, "saved": [str(p) for p in saved], "answer": answer,
                      "tool_events": [e for e in emitted if "tool" in str(e.get("type", ""))][:10],
                      "turn_end": [e for e in emitted if e.get("type") == "turn_end"]}
            (out / "claude-image-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({k: report[k] for k in ("sent_to_claude", "saved", "answer")}, indent=1))
            assert isinstance(sent, str) and str(saved[0]) in sent, sent
            assert len(saved) == 1, saved
            assert "red" in answer.lower() and "blue" in answer.lower(), answer
            print("PASS Claude opened the attached image by its path and named both colours")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit model probe")
    asyncio.run(main())
