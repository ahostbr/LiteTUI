"""COMMIT 2 (ETA) — pure projection logic, testable without a Textual app or model.

Two pure functions carry the feature:

  render_progress(t0, now, prompt_tokens=None, learned_rate=None)
      Elapsed (always honest) + an ETA appended ONLY when both a learned
      prompt-eval rate and a prompt-token count are available. Never divides by
      zero. Backward-compatible: the two new args are optional, so the pre-ETA
      callers (tool_display_parts, the pre-token answer bubble) keep working.

  is_reliable_rate_sample(prompt_tokens, first_token_s, floor=0.25)
      The KV-cache gate. A rate sample is only trustworthy when the turn
      demonstrably REPROCESSED the prompt (first-token latency at/above `floor`).
      A cache-hit turn's prompt_tokens/latency is a misleading rate; admitting it
      into the median would make a large post-compact turn predict minutes.

ETA is a PROJECTION, not a measurement: this turn's ETA = the PREVIOUS turn's
prompt_tokens / the median of RELIABLE rates. A confidently-wrong ETA is worse
than an honest elapsed counter, so until a reliable sample exists the bubble
shows elapsed only.
"""
from statistics import median

from litetui.app import is_reliable_rate_sample, render_progress
from litetui.fmt import fmt_dur


# --- render_progress: the ETA gate (when the ETA appears at all) -------------
def test_eta_absent_without_rate():
    """No learned rate -> elapsed only, no 'est', no crash."""
    out = render_progress(0.0, 1.2, prompt_tokens=1000, learned_rate=None)
    assert "1.2s" in out, out
    assert "…" in out, out
    assert "est" not in out, out


def test_eta_absent_with_zero_rate_no_div_by_zero():
    """A zero rate must yield elapsed-only, NOT a ZeroDivisionError."""
    out = render_progress(0.0, 1.2, prompt_tokens=1000, learned_rate=0)
    assert "1.2s" in out, out
    assert "est" not in out, out


def test_eta_absent_without_tokens():
    """A rate with no token count to apply it to -> elapsed only."""
    out = render_progress(0.0, 1.2, prompt_tokens=None, learned_rate=100)
    assert "est" not in out, out
    out2 = render_progress(0.0, 1.2, prompt_tokens=0, learned_rate=100)
    assert "est" not in out2, out2


def test_eta_present_when_both_present():
    """rate>0 AND tokens>0 -> elapsed + ETA, and ETA == tokens/rate."""
    out = render_progress(0.0, 1.2, prompt_tokens=1000, learned_rate=100)
    # Elapsed part still honest.
    assert "1.2s" in out and "…" in out, out
    # ETA part: 1000 tokens / 100 tokens-per-s = 10.0s.
    assert "est" in out, out
    assert fmt_dur(1000 / 100) in out, out


def test_eta_is_a_projection_of_prior_prompt_tokens():
    """The ETA applies the learned rate to the token count handed in (here the
    previous turn's prompt_tokens), so a bigger estimate -> a bigger ETA."""
    small = render_progress(0.0, 1.0, prompt_tokens=500, learned_rate=100)
    big = render_progress(0.0, 1.0, prompt_tokens=5000, learned_rate=100)
    assert fmt_dur(5.0) in small, small
    assert fmt_dur(50.0) in big, big


def test_backward_compatible_two_arg_call():
    """Pre-ETA callers pass only (t0, now) and must keep getting elapsed-only."""
    out = render_progress(100.0, 101.2)
    assert "1.2s" in out and "est" not in out, out


# --- is_reliable_rate_sample: the KV-cache gate ------------------------------
def test_reliable_sample_accepted_above_floor():
    assert is_reliable_rate_sample(10000, 0.5) is True
    assert is_reliable_rate_sample(10000, 0.25) is True  # exactly at the floor


def test_cache_hit_sample_rejected_below_floor():
    """A cache-hit turn (fast first token) is EXCLUDED from the median."""
    assert is_reliable_rate_sample(10000, 0.1) is False
    assert is_reliable_rate_sample(10000, 0.05) is False


def test_gate_requires_positive_tokens():
    assert is_reliable_rate_sample(0, 0.5) is False
    assert is_reliable_rate_sample(None, 0.5) is False


def test_gate_requires_a_first_token_latency():
    assert is_reliable_rate_sample(10000, None) is False


def test_custom_floor_is_honoured():
    assert is_reliable_rate_sample(10000, 0.3, floor=0.5) is False
    assert is_reliable_rate_sample(10000, 0.6, floor=0.5) is True


# --- The Sentinel-required scenario ------------------------------------------
def test_large_post_compact_turn_not_given_minutes_eta():
    """A 100k-token post-compact turn must not be handed a minutes-long ETA
    computed from a cache-hit (unreliable) rate sample.

    With the gate OFF, the sole sample is a cache-hit turn: a tiny reprocessed
    token count divided by fixed overhead -> a very LOW rate. 100k tokens over
    that rate is tens of minutes. With the gate ON, that sample is excluded, so
    no reliable rate exists yet and the bubble honestly shows elapsed only.
    """
    # The only turn we've seen so far was a cache hit (small new tail, overhead).
    cache_hit_tokens, cache_hit_first = 50, 0.2
    unreliable_rate = cache_hit_tokens / cache_hit_first  # 250 t/s, misleadingly low
    big_turn_tokens = 100_000

    # If we naively admitted the cache-hit sample, the ETA would be minutes.
    naive_eta_s = big_turn_tokens / unreliable_rate
    assert naive_eta_s >= 120, naive_eta_s  # >= 2 minutes — the confidently-wrong ETA

    # The gate rejects that sample...
    assert is_reliable_rate_sample(cache_hit_tokens, cache_hit_first) is False

    # ...so the learned rate is None (no reliable samples) and the bubble
    # shows elapsed only — no ETA at all. That is the honest outcome.
    learned_rate = None  # median of the empty reliable set
    out = render_progress(0.0, 1.5, prompt_tokens=big_turn_tokens, learned_rate=learned_rate)
    assert "1.5s" in out and "…" in out, out
    assert "est" not in out, out


def test_gate_admits_a_real_reprocess_turn_and_eta_becomes_reasonable():
    """Once a turn demonstrably reprocesses the prompt (first-token >= floor),
    its rate is reliable and a 100k-token turn gets a sane projected ETA."""
    reliable_tokens, reliable_first = 20_000, 0.5  # rate = 40,000 t/s
    assert is_reliable_rate_sample(reliable_tokens, reliable_first) is True

    learned_rate = median([reliable_tokens / reliable_first])
    out = render_progress(0.0, 1.5, prompt_tokens=100_000, learned_rate=learned_rate)
    assert "est" in out, out
    # 100k / 40k t/s = 2.5s — a projection, not minutes.
    assert fmt_dur(100_000 / learned_rate) in out, out


def test_median_uses_only_reliable_samples():
    """Mixing one cache-hit sample in would drag the median down; the gate keeps
    it out, so the median reflects only real reprocess turns."""
    reliable_rates = [20_000 / 0.5, 30_000 / 0.6]           # 40k, 50k t/s
    cache_hit_rate = 50 / 0.2                              # 250 t/s (unreliable)

    clean = median(reliable_rates)
    polluted = median(reliable_rates + [cache_hit_rate])

    assert clean == 45_000, clean
    assert polluted < clean, (polluted, clean)  # the cache hit pulls the median down
    # ...and the gate is what keeps it out:
    assert is_reliable_rate_sample(50, 0.2) is False
    assert is_reliable_rate_sample(20_000, 0.5) is True
    assert is_reliable_rate_sample(30_000, 0.6) is True
