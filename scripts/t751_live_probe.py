"""T751 — LIVE check of 0.23.1's default Codex path (Ryan, a-749bf780: "run a test urself").

Sends real requests on the ChatGPT subscription (3 turns + 1 compaction summary, gpt-5.5, low).
Refuses without --i-am-spending-quota. Runs the REAL app (Textual pilot, real OAuthBackend, real
tools) under a throwaway LITETUI_DATA_ROOT so nothing of Ryan's is touched.

PROVES, or fails to:
  1. transport on the default path is OAuthTransport with prompt_cache_key == convo_id
  2. a tool call is executed by LiteTUI's own loop (a role=tool message in app.conversation)
  3. /compact runs LiteTUI's compaction (its own system lines, never "Codex is compacting")
  4. cached_tokens > 0 in last_usage on a later turn (prompt cache engaged)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

if "--i-am-spending-quota" not in sys.argv:
    raise SystemExit("REFUSED: pass --i-am-spending-quota (3 gpt-5.5 turns + 1 compaction summary)")

ROOT = Path(tempfile.mkdtemp(prefix="t751-live-"))
os.environ["LITETUI_DATA_ROOT"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import model_transport, settings as settings_mod  # noqa: E402

settings_mod.save(settings_mod.Settings(
    backend="codex", default_model="gpt-5.5", thinking_level="low",
    tools_enabled=True, tool_iterations=4, autocompact_enabled=False,
    wake_after_compact=False, clear_screen_after_compact=False,
    compact_keep_recent=1, backend_chosen=True,
), ROOT)

from litetui import app as app_mod  # noqa: E402

out: dict = {"data_root": str(ROOT), "turns": []}
transports: list[str] = []
_real_for_app = model_transport.for_app


def _spy(app):
    t = _real_for_app(app)
    transports.append(f"{type(t).__name__}(prompt_cache_key={getattr(t, 'prompt_cache_key', None)!r})")
    return t


model_transport.for_app = _spy


async def settle(a, pilot, extra=10):
    for _ in range(3000):
        await pilot.pause()
        if not a._chat_running():
            break
    for _ in range(extra):
        await pilot.pause()


async def main():
    a = app_mod.LiteTUI()
    said: list[str] = []
    real_system = a._system
    a._system = lambda msg, *x, **k: (said.append(str(msg)), real_system(msg, *x, **k))
    async with a.run_test(size=(120, 40)) as pilot:
        for _ in range(600):
            await pilot.pause(0.05)
            if any(s.startswith("Connected") for s in said):
                break
        out["connect"] = [s for s in said if s.startswith(("Connected", "agent loop", "Default model"))]
        out["backend"] = type(a.backend).__name__
        out["has_app_server"] = hasattr(a.backend, "app_server")
        out["model"] = a.model_id
        out["convo_id"] = a.convo_id
        if not a.model_id:
            out["error"] = "never connected: " + " | ".join(said[-5:])
            return

        async def turn(text):
            n0 = len(said)
            t0 = time.time()
            a._append({"role": "user", "content": text})
            a._stream()
            await settle(a, pilot)
            roles = [m.get("role") for m in a.conversation]
            last = a.conversation[-1]
            out["turns"].append({
                "prompt": text,
                "seconds": round(time.time() - t0, 1),
                "roles_after": roles,
                "assistant_tail": (last.get("content") or "")[-200:] if last.get("role") == "assistant" else None,
                "last_usage": a.last_usage,
                "ctx_used": a.ctx_used, "tps": a.tps,
                "system_lines": said[n0:],
            })

        await turn('Use the bash tool to run exactly: python -c "print(6*7)"  — then reply with only the number it printed.')
        await turn("What number did the tool print? Reply with only the number.")
        n0 = len(said)
        a._handle_command("/compact")
        await settle(a, pilot, extra=20)
        out["compact_system_lines"] = said[n0:]
        out["roles_after_compact"] = [m.get("role") for m in a.conversation]
        await turn("In five words, what did we do so far?")
    out["transports"] = transports


asyncio.run(main())
Path(ROOT / "t751-live-result.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
print(json.dumps(out, indent=1, default=str))
