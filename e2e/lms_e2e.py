"""REAL LM Studio E2E — the SDK control plane that replaced `lms load`.

Proves by OUTCOME, not by transport: after the SDK load, LM Studio's native
API reports the model LOADED at (about) the asked window; after unload, not
loaded. A streamed completion runs through the app's exact client path.

Exit 2 (loud) when LM Studio is not serving — never a silent skip.
Restores the box state it found useful: reloads the model at 32k at the end
(the live thinking test wants a resident thinking model).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import llm_backend
from litetui.settings import Settings

ART = Path(__file__).resolve().parent / "artifacts" / "dual-backend"
ART.mkdir(parents=True, exist_ok=True)
MARKER = "LITETUI-LMS-E2E-OK"
MODEL = "qwen3.5-0.8b"
results: list[str] = []


def step(msg: str) -> None:
    results.append(msg)
    print(f"  [ok] {msg}", flush=True)


def fail(code: int, msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    (ART / "lms_e2e_result.json").write_text(
        json.dumps({"ok": False, "failed": msg, "passed": results}, indent=2),
        encoding="utf-8",
    )
    sys.exit(code)


def main() -> None:
    s = Settings()
    backend = llm_backend.LMStudioBackend(s)

    # ── 1: LM Studio must be serving ──────────────────────────────────────
    try:
        rows = asyncio.run(backend.list_models())
    except llm_backend.BackendError as e:
        fail(2, f"{e} — start LM Studio (or `lms server start`).")
    if not any(r.key == MODEL for r in rows):
        fail(2, f"{MODEL!r} not among LM Studio's models — download it there first.")
    step(f"LM Studio serving; {len(rows)} models listed")

    # ── 2: SDK load at an asked window ────────────────────────────────────
    try:
        asyncio.run(backend.unload(MODEL))   # a stale instance would keep ITS config
    except llm_backend.BackendError:
        pass                                  # not loaded — fine
    asyncio.run(backend.load(MODEL, ctx=4096))
    info = asyncio.run(backend.model_info(MODEL))
    if not info or not info[2]:
        fail(1, f"model_info after SDK load: {info!r} — not loaded")
    if not (4096 <= (info[0] or 0) <= 4160):
        fail(1, f"asked ctx 4096, native API reports {info[0]!r}")
    step(f"SDK-loaded at ctx {info[0]} — proven by the native API's readout, not the call's return")

    # ── 3: streamed completion through the app's client path ──────────────
    from openai import AsyncOpenAI

    async def stream_marker() -> str:
        client = AsyncOpenAI(base_url=backend.base_url(), api_key="litetui")
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content":
                       f"Repeat this exact token once and nothing else: {MARKER}"}],
            stream=True, max_tokens=512,
        )
        text = ""
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta:
                text += chunk.choices[0].delta.content or ""
                text += getattr(chunk.choices[0].delta, "reasoning_content", "") or ""
        return text

    text = asyncio.run(stream_marker())
    if MARKER not in text:
        fail(1, f"marker not in streamed output: {text[:200]!r}")
    step(f"streamed completion carries the marker ({len(text)} chars)")

    # ── 4: SDK unload frees the seat — the capability LiteTUI never had ───
    asyncio.run(backend.unload(MODEL))
    info = asyncio.run(backend.model_info(MODEL))
    if info and info[2]:
        fail(1, "unload reported success but the native API still shows loaded")
    step("SDK-unloaded — native API confirms not loaded")

    # ── restore: leave the box the way the live tests want it ─────────────
    asyncio.run(backend.load(MODEL, ctx=32768))
    step("restored: model reloaded at 32k for the live suite")

    (ART / "lms_e2e_result.json").write_text(
        json.dumps({"ok": True, "passed": results}, indent=2), encoding="utf-8"
    )
    print(f"\nALL {len(results)} STEPS PROVED — LM Studio SDK control plane")


if __name__ == "__main__":
    main()
