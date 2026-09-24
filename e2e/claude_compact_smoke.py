"""Explicit LIVE proof: LiteTUI's own /compact on the Claude backend, end to end.

A real Claude session learns two facts, /compact runs (the cache warning is
answered "Send anyway"), and a follow-up in the FRESH session must still know
both facts from the summary alone. Isolated LiteTUI state, official CLI auth.
Saves SVG screenshots and a JSON record under artifacts/.

    set LITETUI_CLAUDE_LIVE=1 && python e2e/claude_compact_smoke.py
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

import litetui
from litetui import app as app_mod
from litetui import paths, settings


async def settle(app, pilot, groups=("init", "chat")):
    for _ in range(1200):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in groups for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


async def answer_cache_warning(app, pilot):
    """Press "Send anyway" on the SAME warning a model/effort switch shows."""
    from litetui.claude_cache import CacheWarningBody
    for _ in range(300):
        await pilot.pause(0.1)
        for screen in app.screen_stack:
            found = screen.query(CacheWarningBody)
            if found:
                body = found.first()
                text = str(body.query_one("#cache-text").render())
                body.query_one("#cache-send").press()
                return text
    raise AssertionError("no cache warning appeared for a manual /compact")


async def main():
    print("litetui from:", litetui.__file__)
    out = Path("artifacts")
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="litetui-claude-compact-") as directory:
        paths.CONVO_DIR = Path(directory) / "convos"
        settings.settings_path = lambda root=None: Path(directory) / "settings.json"
        original_load = settings.load
        cfg = settings.Settings(backend="claude", backend_chosen=True, default_model="sonnet",
                                skills_enabled=False, mcp_enabled=False, tools_enabled=True,
                                autocompact_enabled=False, wake_after_compact=False,
                                clear_screen_after_compact=False)
        settings.load = lambda *a, **k: cfg
        app = app_mod.LiteTUI()
        settings.load = original_load
        emitted = []
        app._rpc_emit = emitted.append
        async with app.run_test(size=(140, 60)) as pilot:
            await settle(app, pilot)
            assert app.backend.name == "claude", app.backend.name
            app._submit_text("Remember two facts: the code is ORCHID-826 and the codename is BLUE HERON. "
                             "Also in flight: the PARSER-REWRITE is owed to Sam by Friday. Use no tools this turn "
                             "(so only the compaction's STEP 1 can persist it). Reply only READY.", False)
            await settle(app, pilot)
            ledger = app._claude_ledger
            old = dict(ledger.selected)
            assert old["session_id"], "first turn bound a native session"
            assert app.ctx_max and app.ctx_loaded, ("the window from the result frame", app.ctx_max)

            # Phase 2 (plan claude-backend-litetui-identity): STEP 1 persists the store.
            handoff = app.convo_dir / "handoff.md"
            handoff_before = handoff.stat().st_mtime if handoff.exists() else None
            assert handoff_before is None or "PARSER-REWRITE" not in handoff.read_text(encoding="utf-8").upper(),                 "turn 1 already persisted it; STEP 1 would prove nothing"
            app._handle_command("/compact")
            warning = await answer_cache_warning(app, pilot)
            await settle(app, pilot)
            app.save_screenshot(filename="claude-compact-after.svg", path=str(out.resolve()))
            compaction = [e for e in emitted if e.get("type") == "compaction"]
            assert compaction and compaction[-1]["reason"] == "compacted", compaction
            new = dict(ledger.selected)
            assert new["id"] != old["id"] and new.get("seed"), new
            assert app.backend.session is None, "the summarised session was closed"
            assert handoff.exists() and handoff.stat().st_mtime != handoff_before, "STEP 1 wrote handoff.md"
            handoff_text = handoff.read_text(encoding="utf-8")
            assert "PARSER-REWRITE" in handoff_text.upper(), handoff_text

            app._submit_text("From what you know, what are the code and the codename? "
                             "Reply only with both, nothing else.", False)
            await settle(app, pilot)
            answer = app.conversation[-1].get("content") or ""
            app.save_screenshot(filename="claude-compact-followup.svg", path=str(out.resolve()))
            fresh = dict(ledger.selected)
            assert fresh.get("session_id") and fresh["session_id"] != old["session_id"], "a FRESH session"
            assert handoff_text.strip()[:80] in (fresh.get("system_prompt") or ""), "session 2's prompt carries it"
            assert "ORCHID-826" in answer and "BLUE HERON" in answer.upper(), answer

            # LiteTUI's OWN threshold on Claude: 1% of the window, so the next
            # finished turn is over it and the compaction is automatic.
            app.settings.autocompact_enabled = True
            app.settings.autocompact_at_percent = 1
            app._submit_text("Also remember: the port is 7460. Reply only NOTED.", False)
            await settle(app, pilot)
            await settle(app, pilot)   # the scheduled _maybe_autocompact runs its own worker
            autos = [e for e in emitted if e.get("type") == "compaction"]
            assert len(autos) == 2 and autos[-1]["reason"] == "compacted", autos
            third = dict(ledger.selected)
            assert third["id"] != fresh["id"] and "7460" in third.get("seed", ""), third
            app.settings.autocompact_enabled = False
            app._submit_text("What are the code, the codename and the port? Reply only with the three.", False)
            await settle(app, pilot)
            final = app.conversation[-1].get("content") or ""
            app.save_screenshot(filename="claude-compact-auto.svg", path=str(out.resolve()))
            record = {
                "litetui": litetui.__file__,
                "sessions": [old["session_id"], fresh.get("session_id"), dict(ledger.selected).get("session_id")],
                "window": app.ctx_max,
                "cache_warning_text": warning,
                "compaction_events": autos,
                "manual_seed": new["seed"], "auto_seed": third["seed"], "handoff_after_compact": handoff_text,
                "followup_answer": answer, "final_answer": final,
                "turn_ends": [e for e in emitted if e.get("type") == "turn_end"],
            }
            (out / "claude-compact-smoke.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps({k: record[k] for k in ("sessions", "window", "compaction_events",
                                                     "followup_answer", "final_answer")}, indent=2))
            print("--- cache warning ---\n" + warning)
            print("--- manual seed ---\n" + new["seed"])
            print("--- auto seed ---\n" + third["seed"])
            assert all(x in final for x in ("ORCHID-826", "7460")) and "BLUE HERON" in final.upper(), final
            print("PASS live Claude: manual /compact + LiteTUI autocompact -> fresh sessions -> remembered")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit model probe")
    asyncio.run(main())
