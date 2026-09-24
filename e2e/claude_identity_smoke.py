"""Live: on the Claude backend, Claude runs under LiteTUI's prompt and without Claude Code's
auto-memory index (plan claude-backend-litetui-identity, phase 1).

DISTINGUISHING by construction: auto-memory only loads when the working folder has a
Claude Code memory index, so the workspace is pointed at a folder that HAS one
(LITETUI_IDENTITY_WS, default C:\\Projects\\LiteTUI), and a CONTROL session runs with the
auto-memory switch removed. The control must see the index; the real session must not.
Tools are off: nothing in that folder is touched.
"""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import app as app_mod  # noqa: E402
from litetui import claude_backend, paths, settings  # noqa: E402

WS = Path(os.environ.get("LITETUI_IDENTITY_WS", r"C:\Projects\LiteTUI"))
#: A phrase that exists ONLY in Claude Code's auto-memory index for C:\Projects\LiteTUI
#: (~/.claude/projects/C--Projects-LiteTUI/memory/MEMORY.md), never in LiteTUI's prompt.
#: "MEMORY.md" itself is useless here: LiteTUI's store section describes its own memory.md.
TRAP = "Truncation reads as completeness"
ASK = ("Three answers, one per line, nothing else. "
       "LITE=yes if your system prompt contains the words 'You are a helpful AI assistant', else LITE=no. "
       "PRESET=yes if it contains the words 'You are Claude Code', else PRESET=no. "
       f"TRAP=yes if anything in your context contains the phrase '{TRAP}', else TRAP=no.")


async def settle(app, pilot, seconds=240):
    for _ in range(seconds * 10):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in ("init", "chat") for w in app.workers):
            return
    raise AssertionError("TUI worker timed out")


async def ask(auto_memory_off: bool):
    saved_env = dict(claude_backend.AUTO_MEMORY_ENV)
    if not auto_memory_off:
        claude_backend.AUTO_MEMORY_ENV.clear()
    try:
        with tempfile.TemporaryDirectory(prefix="litetui-claude-identity-") as directory:
            paths.CONVO_DIR = Path(directory) / "convos"
            paths.ROOT = WS
            settings.settings_path = lambda root=None: Path(directory) / "settings.json"
            original_load = settings.load
            cfg = settings.Settings(backend="claude", backend_chosen=True, default_model="default",
                skills_enabled=False, mcp_enabled=False, tools_enabled=False,
                autocompact_enabled=False, wake_after_compact=False)
            settings.load = lambda *a, **k: cfg
            app = app_mod.LiteTUI()
            settings.load = original_load
            async with app.run_test(size=(125, 42)) as pilot:
                await settle(app, pilot)
                app._submit_text(ASK, False)
                await settle(app, pilot)
                answer = app.conversation[-1].get("content") or ""
                prompt = app._claude_ledger.selected.get("system_prompt") or ""
                await app.backend.close()
                return answer, prompt
    finally:
        claude_backend.AUTO_MEMORY_ENV.clear()
        claude_backend.AUTO_MEMORY_ENV.update(saved_env)


async def main():
    assert (Path.home() / ".claude" / "projects").exists()
    control, _ = await ask(auto_memory_off=False)
    real, prompt = await ask(auto_memory_off=True)
    first = paths.SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").split()[:8]
    report = {"workspace": str(WS), "control_answer": control, "answer": real,
              "prompt_head": prompt[:300], "systemprompt_first_words": " ".join(first)}
    out = Path("artifacts")
    out.mkdir(exist_ok=True)
    (out / "claude-identity-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1))
    assert TRAP in (Path.home() / ".claude/projects/C--Projects-LiteTUI/memory/MEMORY.md").read_text(encoding="utf-8")
    assert re.search(r"TRAP\s*=\s*yes", control, re.I), "control must see the auto-memory index, or this proves nothing"
    assert re.search(r"TRAP\s*=\s*no", real, re.I), real
    assert re.search(r"LITE\s*=\s*yes", real, re.I) and re.search(r"PRESET\s*=\s*no", real, re.I), real
    print("PASS LiteTUI prompt, no auto-memory index (control saw it)")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit model probe")
    asyncio.run(main())
