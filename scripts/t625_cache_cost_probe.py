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

from litetui.model_transport import NS, OAuthTransport

TURNS = 6
DISCOVERY_AT = 3  # 1-based turn index after which the tools array grows

STABLE_SYSTEM = (
    "You are a terse assistant. Answer in at most five words. "
    "Do not call tools unless asked to."
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "src" / "litetui" / "schemas"

#: The tool a discovery appends. Held back from the base set so the treated arm's
#: growth is a REAL schema arriving, not a synthetic one bolted on.
DISCOVERED_SCHEMA = "read.json"


def _load_catalogue() -> tuple[list[dict], dict]:
    """The REAL tool schemas this app ships, as the wire would carry them.

    🔴 RUN 1 MEASURED NOTHING BECAUSE ITS PROMPT WAS 69-258 TOKENS. Two toy
    tools put the whole conversation two orders of magnitude below the prompt
    cache's minimum, so every `cached_tokens` came back 0 — and a zero from a
    cache that never engaged is indistinguishable, in the table, from a zero
    that means the discovery was free. The fix is not a bigger stub: a made-up
    array of the right SIZE would still have the wrong SHAPE, and the thing
    under test is what a real `tool_search` append does to a real prefix.
    src/litetui/schemas/ is that prefix — 17 schemas, 29,368 bytes on disk.
    """
    files = sorted(SCHEMA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"REFUSED: no tool schemas under {SCHEMA_DIR}")
    base, discovered = [], None
    for f in files:
        spec = json.loads(f.read_text(encoding="utf-8"))
        # The schemas are stored as the function body; the wire wants it wrapped.
        tool = spec if spec.get("type") == "function" else {"type": "function", "function": spec}
        if f.name == DISCOVERED_SCHEMA:
            discovered = tool
        else:
            base.append(tool)
    if discovered is None:
        raise SystemExit(f"REFUSED: {DISCOVERED_SCHEMA} is not in {SCHEMA_DIR}")
    return base, discovered


BASE_TOOLS, DISCOVERED_TOOL = _load_catalogue()


def _prompt(i: int) -> str:
    """Identical shape every turn, so prompt length is not a variable."""
    return f"Turn {i:02d}: name one primary colour."


class FakeTransport:
    """Runs the whole probe end to end with ZERO requests.

    🔴 THIS EXISTS BECAUSE RUN 1 SPENT A REQUEST ON A ONE-LINE BUG IN THIS FILE.
    The probe called `collect()` on a result `create()` had already collected,
    and died on the first turn — AFTER the HTTP round trip, so the request was
    sent and paid for and then the script fell over. The spend-guard stops an
    ACCIDENTAL invocation; it cannot stop a DELIBERATE one that is broken, and
    "RUNNABLE, NOT RUN" is a claim about intent rather than a measurement.
        AN UNRUN SCRIPT IS AN UNVERIFIED SCRIPT.
    So the paid path is now reachable only after the same code has executed
    against this, for free, on every turn of both arms.

    ⚠️ IT IS A SHAPE CHECK, NOT A SIMULATION. The numbers it returns are canned
    and mean nothing; what it proves is that the loop, the accounting, the
    verdict block and the table all survive a full run. A green fake arm says
    the plumbing holds, never that the cache behaves this way.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        # ⚠️ DERIVED FROM THE ARGUMENTS, NEVER FROM A COUNTER. The first cut used
        # `self.calls`, which keeps counting across the arm boundary — so the
        # treated arm's turn 1 was billed as turn 7 and
        # `arms_comparable_before_discovery` came back FALSE on a run where the
        # arms were identical by construction. A FAKE THAT FAILS THE REAL
        # CONTROL TEACHES PEOPLE TO IGNORE THE CONTROL. `messages` grows within
        # an arm and resets between them, which is exactly the shape wanted.
        # Carries the seven fields `_usage` produces (T624, 741ddbd) with
        # cached_tokens NON-ZERO — the field run 1 could not get off the floor.
        prompt = 400 * len(kwargs.get("tools") or []) + 20 * len(kwargs.get("messages") or [])
        details = {"cached_tokens": int(prompt * 0.9), "cache_write_tokens": 128}
        return NS(
            choices=[NS(message=NS(content="red", tool_calls=[]), finish_reason="stop")],
            usage=NS(
                prompt_tokens=prompt,
                completion_tokens=3,
                total_tokens=prompt + 3,
                cached_tokens=details["cached_tokens"],
                cache_write_tokens=details["cache_write_tokens"],
                input_tokens_details=details,
                usage_details={"prompt_tokens": prompt},
            ),
        )


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
        # 🔴 `create()` ALREADY COLLECTS UNLESS YOU ASK FOR A STREAM.
        # model_transport.py:567 ends `return result if kwargs.get("stream", False)
        # else await collect(result)`. This probe never passed stream=True, so it
        # was handed the FINISHED result and then called `collect()` on it a
        # second time - `TypeError: 'async for' requires an object with __aiter__,
        # got types.SimpleNamespace`, on the first turn of the control arm.
        #
        # ⚠️ AND THE REQUEST HAD ALREADY BEEN SENT AND PAID FOR. The crash is
        # AFTER the HTTP round trip, so the failure looked like a broken probe
        # and was also a spent request. A CRASH DOWNSTREAM OF THE SIDE EFFECT
        # STILL HAS THE SIDE EFFECT - which is exactly why this file shipped with
        # a spend-guard and exactly what "RUNNABLE, NOT RUN" could not protect:
        # an unrun script is an unverified script, and the only way to find this
        # was to spend the thing the guard exists to protect.
        result = await transport.create(
            model=model, messages=messages, tools=tools, max_tokens=32
        )
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
    transport = FakeTransport() if args.fake else OAuthTransport("codex")
    if args.fake:
        print("FAKE TRANSPORT — no requests will be sent.", flush=True)
    print(
        f"tools: {len(BASE_TOOLS)} base + 1 discovered, "
        f"{sum(len(json.dumps(t)) for t in BASE_TOOLS)} bytes of base schema",
        flush=True,
    )
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
        "--fake",
        action="store_true",
        help="run end to end against a canned transport; sends nothing, costs nothing",
    )
    p.add_argument(
        "--i-am-spending-quota",
        action="store_true",
        help="required: this sends real requests on Ryan's ChatGPT subscription",
    )
    a = p.parse_args()
    # --fake spends nothing, so it is not gated. That is the point: the free path
    # must be the easy one, or nobody exercises it before the paid one.
    if not a.fake and not a.i_am_spending_quota:
        print(
            f"REFUSED. This probe sends {TURNS * 2} real requests to "
            "chatgpt.com/backend-api/codex/responses on Ryan's subscription.\n"
            "Every other artefact here is offline; this one is not.\n"
            "Re-run with --i-am-spending-quota when the quota has been released.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_main(a)))
