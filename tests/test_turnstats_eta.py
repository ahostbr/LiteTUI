"""T070 O4-a RETRO-CONTROL — EtaState's behaviour, which shipped untested.

🔴 WHY THIS FILE EXISTS, AND WHY IT IS LATE: O4-a landed at 15f7668 on a green
`run_all`. A mutation control run afterwards showed that green was uninformative
— with `EtaState.learn` gutted (neither the token count nor the rate sample
recorded), the FULL SUITE stayed green: 1,123 passed, EXIT 0. No test named
EtaState at all. This is the same hole found in ElapsedState one landing later,
except that this one was already shipped, which is why it was retro-controlled
first.

⚠️ ONE TEST HERE CANNOT FAIL ALONE. `test_an_unreliable_turn_is_not_folded_in`
asserts an EMPTY samples list, which is also what a completely dead `learn`
produces — so it passes against the very break it is meant to guard. It is only
meaningful PAIRED with `test_a_reliable_turn_is_folded_into_the_median` directly
above it, which fails in that case. An assertion of absence needs a sibling that
asserts presence, or it is a threshold wearing a control's clothes.
"""
from litetui.turnstats import EtaState


def _always(prompt_tokens, first_token_s):
    return True


def _never(prompt_tokens, first_token_s):
    return False


def test_learn_records_the_token_count_as_the_next_turns_estimate():
    """The ETA is projected from the PREVIOUS turn's prompt size. Without this
    the estimate is None forever and render_progress silently degrades to
    elapsed-only -- an honest display of a broken measurement."""
    e = EtaState()
    assert e.estimate_tokens() is None
    e.learn(4096, 0.0, _never)
    assert e.estimate_tokens() == 4096


def test_a_reliable_turn_is_folded_into_the_median():
    e = EtaState()
    e.first_delta = 12.0
    e.learn(1000, 10.0, _always)          # 1000 tokens / 2.0s = 500 tok/s
    assert e.samples == [500.0]
    assert e.learned_rate() == 500.0


def test_an_unreliable_turn_is_not_folded_in():
    """A cache-hit turn reprocessed nothing, so its rate is misleadingly high
    and must never pollute the median.

    ⚠️ PAIRED TEST -- see the module docstring. Empty samples is also what a
    dead `learn` produces; the test above is what distinguishes them."""
    e = EtaState()
    e.first_delta = 12.0
    e.learn(1000, 10.0, _never)
    assert e.samples == []
    assert e.learned_rate() is None


def test_is_reliable_receives_the_measured_latency_not_a_guess():
    """`is_reliable` is INJECTED so this module never imports the text layer.
    That only works if it is handed the real numbers."""
    seen = []
    e = EtaState()
    e.first_delta = 12.5
    e.learn(800, 10.0, lambda pt, fts: seen.append((pt, fts)) or False)
    assert seen == [(800, 2.5)]


def test_turn_started_at_is_used_as_passed_and_zero_suppresses_the_sample():
    """`turn_started_at` is PASSED IN, not reached for -- that was the one
    read-coupling to the _elapsed family and paying it off is what made the
    two objects separable. A zero means no turn was timed, so no rate exists."""
    calls = []
    e = EtaState()
    e.first_delta = 12.0
    e.learn(1000, 0.0, lambda pt, fts: calls.append(fts) or True)
    assert calls == []                    # the predicate was never consulted
    assert e.samples == []


def test_record_first_delta_is_idempotent_within_a_turn():
    """first_token_s must measure the FIRST delta; later deltas must not move
    it, or the latency shrinks toward zero as the turn goes on."""
    e = EtaState()
    e.record_first_delta()
    first = e.first_delta
    assert first is not None
    e.record_first_delta()
    assert e.first_delta == first


def test_learn_clears_first_delta_so_the_next_turn_starts_clean():
    """Left set, the next turn would compute its latency from THIS turn's
    first delta -- a stale reading that still looks like a number."""
    e = EtaState()
    e.first_delta = 12.0
    e.learn(1000, 10.0, _always)
    assert e.first_delta is None


def test_learned_rate_is_the_median_of_several_samples():
    e = EtaState()
    for tokens, t0 in ((1000, 10.0), (3000, 10.0), (2000, 10.0)):
        e.first_delta = 12.0
        e.learn(tokens, t0, _always)      # 500, 1500, 1000 tok/s
    assert sorted(e.samples) == [500.0, 1000.0, 1500.0]
    assert e.learned_rate() == 1000.0
