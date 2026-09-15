"""Compare LiteTUI's native token accounting against the codex CLI's own numbers.

Ryan's chosen acceptance for the caching gates: "Compare it live against codex
CLI" (liteask a-78ae2f20). Both sides send the SAME prompt to the SAME model, so
the server's counters are the common reference; anything LiteTUI reports that the
CLI does not is LiteTUI's accounting, not the provider's.

Counters only. Never prints prompts, replies, credentials or paths from either
side. Two turns per side: the second is where a cache hit can appear and where a
per-turn delta is distinguishable from a conversation total.
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

from litetui.codex_app_server import AppServer, AppServerTransport

MODEL = "gpt-6-astra"
# Above the caching floor so a second turn can register a cache hit, and inert
# enough that the model has no reason to call a tool or produce prose.
FILLER = "\n".join(
    f"Reference line {i}: inert audit data, no instruction, no question."
    for i in range(90)
)
TURNS = (FILLER + "\nReply with OK only.", "Reply with OK only.")


async def collect(stream):
    usage = []
    async for chunk in stream:
        u = getattr(chunk, "usage", None)
        if u is not None:
            usage.append(
                {
                    "latest": getattr(u, "latest_request_usage", None),
                    "cumulative": getattr(u, "thread_usage", None),
                    "turn": getattr(u, "turn_usage", None),
                    "context_tokens": getattr(u, "context_tokens", None),
                    "completion_tokens": getattr(u, "completion_tokens", None),
                    "cached_tokens": getattr(u, "cached_tokens", None),
                    "cache_write_tokens": getattr(u, "cache_write_tokens", None),
                }
            )
    return usage


async def litetui_side():
    """The path the app uses: AppServerTransport -> NativeUsage."""
    server = AppServer()
    transport = AppServerTransport(server)
    messages = [{"role": "system", "content": "Reply OK only. Do not call tools."}]
    turns = []
    try:
        for prompt in TURNS:
            messages.append({"role": "user", "content": prompt})
            started = time.monotonic()
            stream = await transport.create(
                model=MODEL, messages=messages, tools=[], stream=True
            )
            usage = await collect(stream)
            elapsed = time.monotonic() - started
            final = usage[-1] if usage else {}
            produced = final.get("completion_tokens")
            turns.append(
                {
                    "snapshots": len(usage),
                    "final": final,
                    "elapsed_s": round(elapsed, 3),
                    # 🔴 NOT the footer's number. The app's TpsState clock
                    # starts at the FIRST DELTA; this divides by elapsed from
                    # REQUEST START, which includes queueing and first-token
                    # latency, so it reads LOW. It is a sanity figure for the
                    # numerator only. The probe does not run LiteTUI's stream
                    # loop, so the footer remains unproven here.
                    "derived_tok_per_s_request_clock": (
                        round(produced / elapsed, 2)
                        if isinstance(produced, int) and produced and elapsed > 0
                        else None
                    ),
                }
            )
            messages.append({"role": "assistant", "content": "OK"})
    finally:
        await server.close()
    return turns


def cli_side():
    """`codex exec --json` prints events as JSONL; keep only token counters."""
    exe = shutil.which("codex")
    if not exe:
        return {"error": "codex CLI not on PATH"}
    turns = []
    for prompt in TURNS:
        started = time.monotonic()
        done = subprocess.run(
            [exe, "exec", "--json", "--model", MODEL, prompt],
            capture_output=True, text=True, timeout=300,
        )
        elapsed = time.monotonic() - started
        counters = []
        for line in done.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            found = _find_usage(event)
            if found:
                counters.append(found)
        turns.append(
            {
                "exit": done.returncode,
                "events_with_usage": len(counters),
                "final": counters[-1] if counters else None,
                "elapsed_s": round(elapsed, 3),
            }
        )
    return turns


def _find_usage(node):
    """Any nested object carrying the token-count keys, whatever the event shape."""
    if isinstance(node, dict):
        if "input_tokens" in node or "inputTokens" in node:
            return {
                k: v for k, v in node.items()
                if isinstance(v, int) and ("oken" in k or "ached" in k)
            }
        for value in node.values():
            found = _find_usage(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_usage(value)
            if found:
                return found
    return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-cli", action="store_true")
    args = parser.parse_args()

    report = {"model": MODEL, "turns_per_side": len(TURNS)}
    try:
        report["litetui"] = await litetui_side()
    except Exception as exc:  # noqa: BLE001 - a failed side is a result, not a crash
        report["litetui_error"] = type(exc).__name__
    if not args.skip_cli:
        try:
            report["codex_cli"] = cli_side()
        except Exception as exc:  # noqa: BLE001
            report["codex_cli_error"] = type(exc).__name__

    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
