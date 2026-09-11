"""T625 — what a deferred-tool discovery COSTS on the real codex endpoint.

🔴 RUNNABLE, NOT RUN. Every other artefact in this directory is offline; this one
sends real requests on Ryan's ChatGPT subscription. It refuses to start without
`--i-am-spending-quota`, and the refusal names the cost, because a probe that
spends money on an accidental invocation is the kind of thing that only has to
happen once.

WHAT IT MEASURES, AND WHY NOT HIT RATE. `astra_cache_audit_probe.py` establishes
the PRECONDITION offline: one `tool_search` permanently adds a schema to the
top-level tools array, which sits at the front of the cacheable prompt. What
nobody has is the price. The deferral plugin's own docblock records an
~11,700-token saving out of 21,727 on the FIRST request; that is a one-turn
figure, and the cost it is traded against is per-discovery and unmeasured.

    A CACHE HIT RATE CANNOT ANSWER THIS. A session that invalidates its prefix
    often still shows a high hit rate, because the surviving suffix is large and
    every turn after the break hits against the NEW prefix. The number that
    decides it is TOTAL `cache_write_tokens` across the session — what you paid
    to re-establish a prefix you already had.

SHAPE. N identical turns through the real transport, with ONE `tool_search`
inserted mid-run, and the same N turns with no discovery as the control. The two
arms differ in exactly one event, so any divergence after turn K is attributable.

    ⚠️ THE CONTROL IS NOT OPTIONAL AND IS NOT "ARM B WITH DEFERRAL OFF". Two arms
    that differ in their TOOL SET differ in their prompt from turn 1, so their
    caches are never comparable. Both arms here carry the SAME deferred set and
    the same prompts; only the discovery differs. Turns 1..K-1 must therefore
    agree between the arms — that agreement is what proves the harness is
    measuring the discovery and not the weather.

READS THE FIELDS T624 RETAINED (`741ddbd`): `cached_tokens` and
`cache_write_tokens` reach us through `_usage` and survive `collect`. Before that
commit this script could not have been written, which is what Astra's audit
recorded as a blocker and what is no longer true.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui.model_transport import OAuthTransport, collect

TURNS = 6
DISCOVERY_AT = 3  # 1-based turn index after which the tools array grows

STABLE_SYSTEM = (
    "You are a terse assistant. Answer in at most five words. "
    "Do not call tools unless asked to."
)

BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "discover",
            "description": "List the agents currently online.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]

#: The schema a discovery would append. Shaped like a real deferred tool rather
#: than a stub: a tiny one would understate the invalidation, and the point is
#: what a REAL tool_search costs.
DISCOVERED_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from disk and return its contents. Supports an optional "
            "line range, a byte limit, and a choice of text or binary handling. "
            "Paths are resolved against the workspace root unless absolute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read."},
                "start": {"type": "integer", "description": "First line, 1-based."},
                "end": {"type": "integer", "description": "Last line, inclusive."},
                "binary": {"type": "boolean", "description": "Return base64 instead of text."},
            },
            "required": ["path"],
        },
    },
}


def _prompt(i: int) -> str:
    """Identical shape every turn, so prompt length is not a variable."""
    return f"Turn {i:02d}: name one primary colour."


async def _one_arm(transport, model: str, *, discover_at: int | None) -> list[dict]:
    """N turns on one growing conversation. Returns a row per turn."""
    messages = [{"role": "system", "content": STABLE_SYSTEM}]
    tools = [dict(t) for t in BASE_TOOLS]
    rows: list[dict] = []

    for i in range(1, TURNS + 1):
        if discover_at is not None and i == discover_at + 1:
            # The event under test: the array grows, permanently, mid-session.
            tools.append(DISCOVERED_TOOL)

        messages.append({"role": "user", "content": _prompt(i)})
        started = time.perf_counter()
        stream = await transport.create(
            model=model, messages=messages, tools=tools, max_tokens=32
        )
        result = await collect(stream)
        elapsed_ms = (time.perf_counter() - started) * 1000

        usage = result.usage
        reply = result.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})

        rows.append(
            {
                "turn": i,
                "tools": len(tools),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "cached_tokens": getattr(usage, "cached_tokens", None),
                "cache_write_tokens": getattr(usage, "cache_write_tokens", None),
                "latency_ms": round(elapsed_ms),
            }
        )
    return rows


def _table(title: str, rows: list[dict]) -> str:
    head = f"{title}\n  turn  tools  prompt   cached   written  latency_ms"
    body = "\n".join(
        "  {turn:>4}  {tools:>5}  {prompt:>6}  {cached:>7}  {written:>7}  {lat:>10}".format(
            turn=r["turn"],
            tools=r["tools"],
            prompt=r["prompt_tokens"] if r["prompt_tokens"] is not None else "-",
            cached=r["cached_tokens"] if r["cached_tokens"] is not None else "-",
            written=r["cache_write_tokens"] if r["cache_write_tokens"] is not None else "-",
            lat=r["latency_ms"],
        )
        for r in rows
    )
    return f"{head}\n{body}"


def _verdict(control: list[dict], treated: list[dict]) -> dict:
    """The one number, plus the check that the arms were comparable."""
    def total(rows, key):
        return sum(r[key] or 0 for r in rows)

    # Turns before the discovery must agree, or the arms differ in something
    # other than the event under test and nothing below means anything.
    pre = DISCOVERY_AT
    comparable = [
        (c["prompt_tokens"], t["prompt_tokens"])
        for c, t in zip(control[:pre], treated[:pre])
    ]
    return {
        "arms_comparable_before_discovery": all(a == b for a, b in comparable),
        "prompt_tokens_before_discovery": comparable,
        "total_cache_write_control": total(control, "cache_write_tokens"),
        "total_cache_write_after_discovery": total(treated, "cache_write_tokens"),
        "extra_write_tokens_caused_by_one_discovery": (
            total(treated, "cache_write_tokens") - total(control, "cache_write_tokens")
        ),
        "total_latency_ms_control": total(control, "latency_ms"),
        "total_latency_ms_treated": total(treated, "latency_ms"),
        "NOTE": (
            "Compare `extra_write_tokens_caused_by_one_discovery` against the "
            "~11,700 tokens deferral saves on request 1 (tool_search.py docblock, "
            "qwen3.5-9b tokenizer, 2026-09-10). Deferral wins only if the saving "
            "exceeds this cost times the number of discoveries in a real session."
        ),
    }


async def _main(args) -> int:
    transport = OAuthTransport("codex")
    print(f"control arm: {TURNS} turns, no discovery", flush=True)
    control = await _one_arm(transport, args.model, discover_at=None)
    print(f"treated arm: {TURNS} turns, one discovery after turn {DISCOVERY_AT}", flush=True)
    treated = await _one_arm(transport, args.model, discover_at=DISCOVERY_AT)

    print()
    print(_table("CONTROL — tools array never changes", control))
    print()
    print(_table(f"TREATED — one tool appended after turn {DISCOVERY_AT}", treated))
    print()
    print(json.dumps(_verdict(control, treated), indent=2))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="gpt-6-astra")
    p.add_argument(
        "--i-am-spending-quota",
        action="store_true",
        help="required: this sends real requests on Ryan's ChatGPT subscription",
    )
    a = p.parse_args()
    if not a.i_am_spending_quota:
        print(
            f"REFUSED. This probe sends {TURNS * 2} real requests to "
            "chatgpt.com/backend-api/codex/responses on Ryan's subscription.\n"
            "Every other artefact here is offline; this one is not.\n"
            "Re-run with --i-am-spending-quota when the quota has been released.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_main(a)))
