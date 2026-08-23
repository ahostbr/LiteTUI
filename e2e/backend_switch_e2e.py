"""REAL cross-backend E2E — one conversation, two engines.

Boots the actual LiteTUI app (Textual pilot), holds a real turn on the
llama.cpp backend (our router, spawned by the app itself), flips to
LM Studio with /backend, holds another real turn — and asserts ONE
continuous conversation crossed the engines.

Exit 2 (loud) when a precondition is missing. Both engines end unloaded
except LM Studio's 32k restore, matching lms_e2e's convention.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The app under test must think llamacpp is its configured engine, spawn on
# a port no live LiteTUI uses, and never meet the first-boot picker.
os.environ["LITETUI_BACKEND"] = "llamacpp"
os.environ["LITETUI_LLAMA_HOST"] = "http://localhost:7472"
os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import llm_backend
from litetui import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-switch-e2e-"))

from litetui import app as app_mod
from litetui.plugins.model_switch import _switch_backend

ART = Path(__file__).resolve().parent / "artifacts" / "dual-backend"
ART.mkdir(parents=True, exist_ok=True)
results: list[str] = []


def step(msg: str) -> None:
    results.append(msg)
    print(f"  [ok] {msg}", flush=True)


def fail(code: int, msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    (ART / "switch_e2e_result.json").write_text(
        json.dumps({"ok": False, "failed": msg, "passed": results}, indent=2),
        encoding="utf-8",
    )
    sys.exit(code)


async def wait_turn_done(a, timeout_s: float = 240.0) -> bool:
    start = time.monotonic()
    await asyncio.sleep(1.0)
    while time.monotonic() - start < timeout_s:
        running = any(
            getattr(w, "group", None) == "chat" and getattr(w, "is_running", False)
            for w in a.workers
        )
        if not running:
            return True
        await asyncio.sleep(0.5)
    return False


async def send(a, pilot, text: str) -> None:
    inp = a.query_one("#message-input")
    inp.value = text
    await pilot.press("enter")
    ok = await wait_turn_done(a)
    if not ok:
        fail(1, f"turn never finished: {text!r}")


async def main() -> None:
    if not llm_backend.llama_available():
        fail(2, f"llama-server not installed at {llm_backend.LLAMA_EXE}")
    if not llm_backend._healthy("http://localhost:1234"):
        fail(2, "LM Studio not serving on :1234 — `lms server start` first.")

    a = app_mod.LiteTUI()
    a.settings.llama_attach_hosts = []    # this run owns its router, always
    async with a.run_test(size=(120, 32)) as pilot:
        # ── 1: connect on llamacpp — the app spawns its own router ────────
        for _ in range(120):
            await pilot.pause(0.25)
            if a.available_models:
                break
        if a.backend.name != "llamacpp" or not a.available_models:
            fail(1, f"no llamacpp connect: backend={a.backend.name} models={len(a.available_models)}")
        small = [k for k in a.available_models if "0.8b" in k or "0.6b" in k]
        if not small:
            fail(2, "no 0.8B-class GGUF discovered for the llama turn")
        a.model_id = small[0]
        step(f"llamacpp connected; model {a.model_id}")

        # ── 2: a real turn on OUR engine (JIT: /load happens explicitly) ──
        await asyncio.wait_for(a.backend.load(a.model_id), timeout=300)
        await send(a, pilot, "Reply with exactly one word: ping")
        msgs_after_llama = len(a.conversation)
        if msgs_after_llama < 3:   # system + user + assistant
            fail(1, f"llama turn produced no assistant message ({msgs_after_llama})")
        step(f"real turn on llama.cpp ({msgs_after_llama} messages)")

        # ── 3: /backend flip mid-conversation ─────────────────────────────
        before = list(a.conversation)
        _switch_backend(a, "lmstudio")
        for _ in range(60):
            await pilot.pause(0.25)
            if a.available_models and a.backend.name == "lmstudio":
                break
        if a.conversation[:len(before)] != before:
            fail(1, "the switch REWROTE history")
        if "qwen3.5-0.8b" in a.available_models:
            a.model_id = "qwen3.5-0.8b"
        step(f"flipped to LM Studio; history intact; model {a.model_id}")

        # ── 4: a real turn on LM Studio, same conversation ────────────────
        await send(a, pilot, "Reply with exactly one word: pong")
        if len(a.conversation) < msgs_after_llama + 2:
            fail(1, "LM Studio turn added nothing to the SAME conversation")
        step(f"real turn on LM Studio; one conversation, {len(a.conversation)} messages, two engines")

    # ── cleanup: our router dies with the app (atexit) — verify ──────────
    a.backend_llama = None
    time.sleep(1.0)

    (ART / "switch_e2e_result.json").write_text(
        json.dumps({"ok": True, "passed": results}, indent=2), encoding="utf-8"
    )
    print(f"\nALL {len(results)} STEPS PROVED — one conversation across two engines")


if __name__ == "__main__":
    asyncio.run(main())
