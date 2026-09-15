"""Ryan's caching release gates: TPS denominator, reconnect rebase, post-compaction.

Each test names the gate it closes. The three gates are his words — "Those are
our release gates" (liteask a-4695d280) — and cover exactly the C10 items left
open: the TPS denominator contract, the reconnect counter rebase, and the
post-compaction usage reconciliation.
"""

import json
from pathlib import Path

from litetui.codex_usage import NativeUsage
from litetui.turnstats import TpsState

EVIDENCE = (Path(__file__).resolve().parents[1] / "Docs" / "Plans"
            / "codex-host-parity-evidence" / "astra-six-mode-usage.json")


def snapshot(inp, out, *, cached=0, reasoning=0, total=None):
    last = {
        "inputTokens": inp,
        "outputTokens": out,
        "cachedInputTokens": cached,
        "reasoningOutputTokens": reasoning,
        "totalTokens": inp + out,
    }
    return {"last": last, "total": total or dict(last), "modelContextWindow": 258400}


# ── GATE 1: the TPS denominator/numerator contract ──────────────────────────

def test_output_tokens_already_include_reasoning_so_tps_must_not_add_them():
    """🔴 The measured contract, not an assumption.

    On the live six-mode Astra run the only sample with reasoning is `max`:
    in=34638 out=23 reasoning=16 total=34661. `in + out == total` exactly,
    while `in + out + reasoning` is 34677 and matches nothing. So outputTokens
    is INCLUSIVE of reasoningOutputTokens, and a TPS numerator that adds
    reasoning on top would double-count every thinking token.
    """
    rows = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    reasoning_rows = [r for r in rows if r["latest"].get("reasoningOutputTokens")]
    assert reasoning_rows, "evidence file carries no sample with reasoning tokens"
    for row in rows:
        last = row["latest"]
        inp, out = last["inputTokens"], last["outputTokens"]
        total, reasoning = last["totalTokens"], last["reasoningOutputTokens"]
        assert inp + out == total, row["effort"]
        if reasoning:
            # The discriminating arm: only inclusive accounting satisfies both.
            assert inp + out + reasoning != total, row["effort"]


def test_turn_usage_is_measured_from_the_conversation_baseline_not_the_turn():
    """🔴 THE FIELD IS NOT WHAT ITS NAME SAYS, and this is why no TPS numerator
    can be taken from it.

    `turn_usage` / `completion_tokens` are the delta from the CONVERSATION
    baseline captured when the meter was constructed — they grow monotonically
    for the whole conversation. Dividing that by one turn's elapsed seconds
    would report a tok/s that climbs every turn and is wrong by the ratio of
    conversation length to turn length. A per-turn output delta does not exist
    on this object; see the caching handoff before wiring native tok/s.
    """
    meter = NativeUsage(fresh=True)
    first = meter.update(snapshot(1000, 10, reasoning=4))
    assert first.completion_tokens == 10
    second = meter.update(
        snapshot(1200, 8, total={"inputTokens": 2200, "outputTokens": 33,
                                 "reasoningOutputTokens": 20, "totalTokens": 2233})
    )
    # 33 since the baseline — NOT 23, which is what a per-turn reading implies.
    assert second.completion_tokens == 33
    assert second.turn_usage["reasoningOutputTokens"] == 20
    # The only per-request figure the server gives us, for contrast.
    assert second.latest_request_usage["outputTokens"] == 8


def test_final_cannot_settle_a_rate_without_a_started_clock():
    """🔴 This is the defect the native path had: its deltas never ticked, so
    t0 stayed None and `final` could never fire — no tok/s was ever published
    for the Codex backend. Ticking is what makes settling possible."""
    never_ticked = TpsState()
    assert never_ticked.final(500) is None

    ticked = TpsState()
    ticked.tick(now=100.0)                      # first delta only sets t0
    ticked.tick(now=100.5)
    assert ticked.t0 == 100.0
    assert ticked.final(500) is not None


def test_a_live_estimate_is_withheld_until_there_is_something_to_divide_by():
    tps = TpsState()
    assert tps.tick(now=10.0) is None           # nothing to divide by yet
    assert tps.tick(now=10.1) is None           # under the 0.4s floor
    assert tps.tick(now=10.5) is not None


# ── GATES 2 and 3: reconnect rebase and post-compaction reconciliation ──────

def test_a_counter_reset_rebaselines_instead_of_blinding_the_meter_forever():
    """🔴 Gate 2 (reconnect) and gate 3 (post-compaction) are ONE defect.

    Both restart the native counters. The meter detected the reset and latched
    `rebased` — which was never cleared — so every later turn reported turn
    usage as unknown for the rest of the conversation. The reset snapshot itself
    still cannot express a delta, but the run after it is monotonic again.
    """
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(2000, 100))
    meter.update(snapshot(2200, 150, total={"inputTokens": 4200, "outputTokens": 250,
                                            "totalTokens": 4450}))

    reset = meter.update(snapshot(500, 20))     # reconnect / compaction
    assert reset.turn_usage == {}, "the reset snapshot itself has no honest delta"
    assert reset.total_tokens is None
    assert reset.context_tokens == 520          # occupancy is still observable

    resumed = meter.update(
        snapshot(700, 40, total={"inputTokens": 1200, "outputTokens": 60,
                                 "totalTokens": 1260})
    )
    assert resumed.turn_usage != {}, "the meter stayed blind after the reset"
    assert resumed.prompt_tokens == 700         # 1200 - 500
    assert resumed.completion_tokens == 40      # 60 - 20
    assert resumed.total_tokens == 740          # 1260 - 520


def test_a_second_reset_is_still_detected_after_the_first_rebaseline():
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(2000, 100))
    meter.update(snapshot(500, 20))                     # first reset
    meter.update(snapshot(700, 40, total={"inputTokens": 1200, "outputTokens": 60,
                                          "totalTokens": 1260}))
    second = meter.update(snapshot(100, 5))             # reset again
    assert second.turn_usage == {}
    after = meter.update(snapshot(200, 10, total={"inputTokens": 300, "outputTokens": 15,
                                                  "totalTokens": 315}))
    assert after.prompt_tokens == 200 and after.completion_tokens == 10


def test_a_partial_reset_snapshot_does_not_fake_a_reset_on_the_next_one():
    """🔴 The trap in the fix. `observed_totals` is REPLACED on a rebase, not
    updated: a key absent from the reset snapshot would otherwise keep its
    pre-reset high-water mark, and the next snapshot carrying that key would
    read as yet another reset — blinding the meter on alternate turns forever.
    """
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(9000, 500, cached=8000))
    # The reset snapshot omits cachedInputTokens entirely.
    reset = meter.update({"total": {"inputTokens": 100, "outputTokens": 5,
                                    "totalTokens": 105},
                          "last": {"totalTokens": 105}})
    assert reset.turn_usage == {}
    # cachedInputTokens returns, far BELOW its pre-reset 8000. Stale high-water
    # marks would classify this as a fresh reset; a replaced baseline does not.
    resumed = meter.update({"total": {"inputTokens": 200, "outputTokens": 10,
                                      "cachedInputTokens": 150, "totalTokens": 210},
                            "last": {"totalTokens": 210}})
    assert resumed.turn_usage != {}, "a stale high-water mark forged a second reset"
    assert resumed.prompt_tokens == 100         # 200 - 100


def test_unknown_stays_unknown_and_no_cache_hit_rate_is_ever_claimed():
    """Ryan's standing honesty constraint, kept through the rebase change."""
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(1000, 10))
    partial = meter.update({"total": {"inputTokens": 1500}, "last": {"totalTokens": 1510}})
    assert partial.completion_tokens is None            # absent != zero
    assert partial.cached_tokens is None
    assert partial.cache_write_tokens is None
    empty = meter.update({})
    assert empty.context_tokens is None
    assert empty.max_context_tokens is None
