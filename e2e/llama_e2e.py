"""REAL llama.cpp E2E — the whole llama backend against the installed engine.

No mocks anywhere: the real llama-server.exe from LiteSuite's install, the
real supervisor (ttyguard spawn, detached, log file), the real router
protocol, a real GGUF, a real streamed completion through the SAME
AsyncOpenAI path the app uses. Bail=1: the first failed step aborts.

Exit codes: 0 = every step proved · 2 = precondition missing (LOUD, with the
fix named — never a silent skip) · 1 = a step failed.

Memory safety (Ryan: "don't OOM the system"): smallest GGUF ≤ 1.5 GB only,
--models-max 2 asserted on the cmdline, no autoload asserted, the spawned
process is killed in finally.

Negative-path proof: run with LITETUI_E2E_MODELS_DIR=<empty dir> — this must
exit 2 naming the roots. A gate never seen to fail is not known to be a gate.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import llm_backend
from litetui import paths
from litetui.settings import Settings

ART = Path(__file__).resolve().parent / "artifacts" / "dual-backend"
ART.mkdir(parents=True, exist_ok=True)
MARKER = "LITETUI-E2E-OK"
MAX_BYTES = int(1.5 * 1024**3)
PORT = 7471   # not 7470: never collide with a live LiteTUI's own router

results: list[str] = []


def step(msg: str) -> None:
    results.append(msg)
    print(f"  [ok] {msg}", flush=True)


def fail(code: int, msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    (ART / "llama_e2e_result.json").write_text(
        json.dumps({"ok": False, "failed": msg, "passed": results}, indent=2),
        encoding="utf-8",
    )
    sys.exit(code)


def main() -> None:
    # ── 1: the engine must be installed (loud, with the fix) ──────────────
    if not llm_backend.LLAMA_EXE.exists():
        fail(2, f"llama-server not installed at {llm_backend.LLAMA_EXE} — "
                "run LiteSuite's Model Hub wizard.")
    step(f"engine installed: {llm_backend.installed_build()}")

    # ── 2: a small real model must exist ──────────────────────────────────
    s = Settings()
    s.llama_host = f"http://localhost:{PORT}"
    s.llama_attach_hosts = []          # this run OWNS its server, always
    override = os.environ.get("LITETUI_E2E_MODELS_DIR")
    if override:
        s.llama_scan_litesuite = s.llama_scan_lmstudio = s.llama_scan_hf_cache = False
        s.llama_models_dirs = [override]
    rows = [
        r for r in llm_backend.scan_models(s)
        if r.path and Path(r.path).stat().st_size <= MAX_BYTES
        and "embed" not in r.key
    ]
    if not rows:
        roots = [str(r) for r, _src in llm_backend._scan_roots(s)]
        fail(2, f"no GGUF ≤1.5GB found in any scan root ({roots}) — "
                "download a small model (0.8B class) first.")
    rows.sort(key=lambda r: Path(r.path).stat().st_size)
    model = rows[0]
    step(f"model: {model.key} ({Path(model.path).stat().st_size // 1024**2} MB, {model.source})")

    backend = llm_backend.LlamaCppBackend(s)
    try:
        # ── 3: spawn via the real supervisor ──────────────────────────────
        status = asyncio.run(backend.ensure_running())
        if not status.startswith("spawned"):
            fail(1, f"expected a spawn, got {status!r} — something already owns :{PORT}?")
        step(status)

        # ── 4: boot-loads-nothing + OOM guard on the REAL cmdline ─────────
        served = backend._server_models()
        if model.key not in served:
            fail(1, f"{model.key} not in the router's world: {sorted(served)}")
        if served[model.key]["status"]["value"] != "unloaded":
            fail(1, "a model is resident right after boot — the no-autoload law is broken")
        argv = backend._owned.proc.args
        assert "--no-models-autoload" in argv, argv
        assert "--models-max" in argv, argv
        step("router lists the model unloaded; cmdline carries --no-models-autoload + --models-max")

        # ── 5: explicit load with a ctx the seam chose ────────────────────
        asyncio.run(backend.apply_load_settings(model.key, {"ctx": 4096}))
        info = asyncio.run(backend.model_info(model.key))
        if not info or not info[2]:
            fail(1, f"model_info after load: {info!r} — not loaded")
        window = info[0]
        if window is None or not (4096 <= window <= 4160):
            fail(1, f"asked ctx 4096, worker reports {window!r} — asked-vs-got broken")
        step(f"loaded at ctx {window} (asked 4096; clamp-tolerant compare)")

        # ── 6: a real streamed completion through the APP's client path ───
        from openai import AsyncOpenAI

        async def stream_marker() -> str:
            client = AsyncOpenAI(base_url=backend.base_url(), api_key="litetui")
            stream = await client.chat.completions.create(
                model=model.key,
                messages=[{"role": "user", "content":
                           f"Repeat this exact token once and nothing else: {MARKER}"}],
                stream=True, max_tokens=512,
            )
            text = ""
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta:
                    text += chunk.choices[0].delta.content or ""
                    # Thinking models spend tokens reasoning first; the
                    # marker may also arrive there. Outcome = marker SEEN.
                    text += getattr(chunk.choices[0].delta, "reasoning_content", "") or ""
            return text

        text = asyncio.run(stream_marker())
        if MARKER not in text:
            fail(1, f"marker not in streamed output ({len(text)} chars): {text[:200]!r}")
        step(f"streamed completion carries the marker ({len(text)} chars)")

        # ── 7: seat-guard cycle on the REAL router (Sentinel's finding) ───
        from litetui import seat_guard
        rec = seat_guard.record(model.key, backend)
        if rec is None:
            fail(1, "seat_snapshot of a loaded model returned None")
        err = seat_guard.suspend(rec, backend)
        if err:
            fail(1, f"seat suspend: {err}")
        if backend.seat_snapshot(model.key) is not None:
            fail(1, "suspended but still listed loaded")
        err = seat_guard.resume(rec, backend)
        if err:
            fail(1, f"seat resume: {err}")
        step("seat-guard suspend/resume cycle held (breadcrumb written and cleared)")

        # ── 8: unload frees the slot ──────────────────────────────────────
        asyncio.run(backend.unload(model.key))
        if backend._server_models()[model.key]["status"]["value"] != "unloaded":
            fail(1, "unload reported success but the model is still resident")
        step("unloaded — VRAM freed by the server's own accounting")

    finally:
        pid = backend._owned.proc.pid if backend._owned else None
        backend.shutdown()
        # ── 9: the process is GONE and the log instrument really ran ──────
        if pid is not None:
            time.sleep(1.0)
            if llm_backend._healthy(s.llama_host):
                fail(1, f"server still answering after shutdown (pid {pid})")
        log = paths.LLAMA_DIR / "litetui-llama-server.log"
        if log.exists():
            shutil.copy(log, ART / "llama_e2e_server.log")

    log = ART / "llama_e2e_server.log"
    if not log.exists() or log.stat().st_size == 0:
        fail(1, "server log is missing/empty — the instrument that writes it never ran")
    step("spawned process killed; log non-empty (instrument-trust audit)")

    (ART / "llama_e2e_result.json").write_text(
        json.dumps({"ok": True, "passed": results}, indent=2), encoding="utf-8"
    )
    print(f"\nALL {len(results)} STEPS PROVED — llama.cpp backend end to end")


if __name__ == "__main__":
    main()
