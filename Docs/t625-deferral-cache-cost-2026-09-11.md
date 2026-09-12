# T625 — what one tool discovery costs the prompt cache

Run: `PYTHONPATH=src python scripts/t625_cache_cost_probe.py --model gpt-5.6-luna --i-am-spending-quota`
2026-09-11, model **`gpt-5.6-luna`** exactly as Ryan typed it (approved, liteask a-191e32e0).
12 real requests to `chatgpt.com/backend-api/codex/responses`, plus 1 wasted earlier (below).

## 🔴 The headline number is 0 and it does NOT mean deferral is free

`extra_write_tokens_caused_by_one_discovery: 0` — because **every cache counter was 0 on every
one of the 12 turns, in both arms.** Not missing: the table renders `None` as `-` and printed
`0`, so the API genuinely reported `input_tokens_details.cached_tokens = 0` throughout.

**The prompts were 69–258 tokens.** Prompt caching does not engage at that size. So the run
measured *"the cache never engaged"*, not *"a discovery costs nothing"* — and those two
readings produce the identical cell.

> **A ZERO FROM AN INSTRUMENT THAT NEVER ENGAGED LOOKS EXACTLY LIKE A ZERO THAT MEANS NO COST.**
> The card's question is still open. Nothing here licenses a claim that deferral is cheap.

## What the run does establish

**The experiment design is sound.** `arms_comparable_before_discovery: true` — turns 1–3 match
exactly across arms (69/69, 89/89, 109/109), so the two arms differed in the one event under
test and nothing else. That check was the point of building it this way, and it passed.

**A discovery is visible in the prompt, just not in the cache counters:**

| | control | treated |
| --- | --- | --- |
| turn 3 prompt tokens | 109 | 109 |
| turn 4 prompt tokens | 129 | **218** (tools 1 → 2) |
| total latency, 6 turns | 12,410 ms | 16,476 ms (+33%) |

One appended tool roughly doubled the prompt from the next turn on and never came back down —
which is the T625 precondition, now confirmed on live requests rather than offline.
⚠️ The latency column is 6 samples per arm over a network; treat +33% as a direction, not a
measurement.

## What would answer the card

The probe must run with a prompt **above the cache floor**, i.e. a realistic tool array
(the docblock's ~11,700 tokens) rather than 1–2 toy tools. That is a probe change plus another
paid run, and it is Ryan's quota — not something to spend on my own judgement.

## ⚠️ One request was spent on a bug, and the guard could not have stopped it

The first invocation crashed with
`TypeError: 'async for' requires an object with __aiter__ method, got types.SimpleNamespace`
on the **first turn of the control arm**. Cause: `OAuthTransport.create()` ends
`return result if kwargs.get("stream", False) else await collect(result)`
(`src/litetui/model_transport.py:567`) — it **already collects** unless you ask for a stream.
The probe never passed `stream=True`, so it was handed the finished result and called
`collect()` on it a second time.

**The crash is downstream of the HTTP round trip, so the request was sent and paid for before
the probe failed.** A CRASH AFTER THE SIDE EFFECT STILL HAS THE SIDE EFFECT. The spend-guard
protects against an *accidental invocation*; it cannot protect against a *deliberate* one that
is broken. This file shipped as "RUNNABLE, NOT RUN" — and an unrun script is an unverified
script. The only thing that could have found this without spending was a fake transport, which
the probe did not have.

**Incidental but useful: `gpt-5.6-luna` is accepted by the API.** The request succeeded (no
model-name rejection, no 4xx), which answers the open question about that name — Ryan's codex
config lists only `gpt-5.6-sol`.

## Raw verdict block

```json
{
  "arms_comparable_before_discovery": true,
  "prompt_tokens_before_discovery": [[69, 69], [89, 89], [109, 109]],
  "total_cache_write_control": 0,
  "total_cache_write_after_discovery": 0,
  "extra_write_tokens_caused_by_one_discovery": 0,
  "total_latency_ms_control": 12410,
  "total_latency_ms_treated": 16476
}
```

🔴 Not verified by Ryan.
