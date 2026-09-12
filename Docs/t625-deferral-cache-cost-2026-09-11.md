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

---

# Run 2 — the floor is cleared, and the metric the card rests on does not exist here

Run: `PYTHONPATH=src python scripts/t625_cache_cost_probe.py --model gpt-5.6-luna --i-am-spending-quota`
2026-09-12, after `71029b4` (real tool catalogue + free `--fake` arm). 12 requests.
Tool array: **16 real schemas from `src/litetui/schemas/`, 22,246 bytes**, with `read.json`
held back as the discovered one.

## ✅ The floor IS cleared — run 1's explanation was right

Prompts went from run 1's 69–258 tokens to **4,209** on turn 1, and `cached_tokens` reached
**3,584** — twice. So the cache does engage at this size, and run 1's zero really was "the
prompt was too small", not "the discovery is free".

## 🔴 But `cache_write_tokens` is 0 on all 12 turns, in both arms

`extra_write_tokens_caused_by_one_discovery` is the number this card was built to produce, and
it is **total `cache_write_tokens`, treated minus control**. This endpoint never populates that
field — not once, at any prompt size, in either arm, including the two turns where
`cached_tokens` was plainly non-zero.

> **THE CARD'S CHOSEN METRIC IS NOT REPORTED BY THIS PROVIDER.** That is not a null result and
> it is not a bug in the probe: `_usage` reads `input_tokens_details`, and the field simply is
> not there. No amount of further spending on this endpoint will produce it.

## ⚠️ And caching is INTERMITTENT, which is its own finding

`cached_tokens` was non-zero on exactly **2 of 12 turns** — control turn 5 and treated turn 6 —
and 0 on the other ten, at prompts that never dropped below 4,209 tokens.

| arm | turn | prompt | cached |
| --- | --- | --- | --- |
| control | 5 | 4,293 | 3,584 |
| treated | 6 | 4,438 | 3,584 |

A prefix that is eligible for caching is not therefore cached. Any future measurement here has
to treat a cache hit as a **probabilistic event** and take enough samples to estimate a rate —
a 12-turn run cannot distinguish "the discovery evicted the prefix" from "this turn happened
not to hit", and that is exactly the confusion a single-run verdict would have published.

## 🔴 The control assertion failed, and it caught a real flaw in my harness

`arms_comparable_before_discovery: false`. The arms diverge before the discovery — by **one to
two tokens**:

| turn | control | treated |
| --- | --- | --- |
| 1 | 4,209 | 4,209 |
| 2 | 4,230 | 4,229 |
| 3 | 4,251 | 4,249 |

Cause: the probe appends **the model's real reply** to the history, and the reply is not
deterministic. Its length differs between arms, so from turn 2 the prompts differ by that
difference — in a run whose entire premise is that the arms are identical until the discovery.

> **THE ARMS WERE NOT IDENTICAL, AND WITHOUT THIS ASSERTION THE TABLE WOULD HAVE LOOKED FINE.**
> A one-token drift is invisible next to a 4,209-token prompt and would never have been
> spotted by eye. This is the check earning its place a second time.

The fix is to append a **fixed** assistant turn instead of the real reply: the reply's content
is not under test, and letting it into the prompt makes the model a variable in its own
experiment. Not applied yet — it changes what is measured and the next run costs quota.

## Where this leaves the card

- The **precondition** is confirmed twice over: a discovery permanently grows the tools array
  (4,251 → 4,398 prompt tokens at the discovery turn, and it never comes back down).
- The **cost in cache writes** cannot be measured on this endpoint at all, because the field is
  never populated.
- Answering the card as written would need a provider that reports cache writes, or a different
  proxy for the cost (e.g. measuring `cached_tokens` *absence* after a discovery across enough
  samples to beat the intermittency above).

🔴 Not verified by Ryan. No further quota spent on my own judgement.

## Raw verdict block — run 2

```json
{
  "arms_comparable_before_discovery": false,
  "prompt_tokens_before_discovery": [[4209, 4209], [4230, 4229], [4251, 4249]],
  "total_cache_write_control": 0,
  "total_cache_write_after_discovery": 0,
  "extra_write_tokens_caused_by_one_discovery": 0,
  "total_latency_ms_control": 17187,
  "total_latency_ms_treated": 11989
}
```

⚠️ The latency totals are 6 samples per arm over a network and one control turn took 7,633 ms
on its own. Treated being *faster* here is noise, not a result.
