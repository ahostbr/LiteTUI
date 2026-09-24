"""Explicit live Claude TUI smoke with isolated LiteTUI state, official CLI auth."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import app as app_mod
from litetui import paths, settings


async def settle(app, pilot):
    for _ in range(320):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in ("init", "chat") for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


async def main():
    with tempfile.TemporaryDirectory(prefix="litetui-claude-ui-") as directory:
        paths.CONVO_DIR = Path(directory) / "convos"
        settings.settings_path = lambda root=None: Path(directory) / "settings.json"
        original_load = settings.load
        cfg = settings.Settings(backend="claude", backend_chosen=True, default_model="default",
            skills_enabled=False, mcp_enabled=False, tools_enabled=False,
            autocompact_enabled=False, wake_after_compact=False)
        settings.load = lambda *a, **k: cfg
        app = app_mod.LiteTUI()
        settings.load = original_load
        emitted = []
        app._rpc_emit = emitted.append
        async with app.run_test(size=(125, 42)) as pilot:
            await settle(app, pilot)
            assert app.backend.name == "claude" and app.model_id == "default", app.model_id
            app._submit_text("Remember ORCHID-826. Reply only READY.", False)
            await settle(app, pilot)
            assert any(e.get("type") == "turn_end" for e in emitted), emitted[-8:]
            assert emitted[-1].get("error") is None, emitted[-1]
            app._submit_text("What is the remembered word? Reply only with it.", False)
            await settle(app, pilot)
            answer = app.conversation[-1].get("content") or ""
            assert "ORCHID-826" in answer, answer
            ledger = app._claude_ledger
            session_id = ledger.selected["session_id"]
            assert session_id
            assert not ledger.pending(ledger.selected["id"])
            await app.backend.close()
            # Recreate both backend and ledger to exercise restart persistence,
            # rather than retaining the Python SDK or in-memory delivery state.
            from litetui.claude_backend import ClaudeBackend
            app.backend = ClaudeBackend(cfg)
            app._claude_ledger = None
            app._submit_text("What is the remembered word? Reply only with it.", False)
            await settle(app, pilot)
            assert "ORCHID-826" in (app.conversation[-1].get("content") or "")
            assert ledger.selected["session_id"] == session_id
            out = Path("artifacts")
            out.mkdir(exist_ok=True)
            app.save_screenshot(filename="claude-tui-smoke.svg", path=str(out.resolve()))
            (out / "claude-tui-smoke.json").write_text(json.dumps({
                "backend": app.backend.name, "two_turn_context": True,
                "exact_resume": True, "session_id": session_id,
                "turn_end_events": [e for e in emitted if e.get("type") == "turn_end"],
            }, indent=2), encoding="utf-8")
            print("PASS live TUI two turns + exact resume")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit model probe")
    asyncio.run(main())
