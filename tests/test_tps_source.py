"""Which clock produced the tok/s, and why the number alone cannot say (T806).

🔴 THE AMBIGUITY IS THE POINT. `TpsState.final` prefers the engine's
`timings.predicted_per_second` and otherwise divides the server's token count
by the CLIENT's wall clock. BOTH settle into the same field, so a published
figure is silent about its own instrument.

Measuring NInfer through LiteTUI, that had to be resolved by ARITHMETIC ON TWO
NUMBERS — an implied elapsed of 0.909 s against an observed stream window of
0.812 s, which left 0.097 s to prefill 6,471 tokens (~66,700 tok/s) and so
could not be a client clock. That is an inference, and it carries an
assumption (prefill is not near-instant) that a faster engine could one day
break.

    A MEASUREMENT WHOSE INSTRUMENT IS UNKNOWN CANNOT BE COMPARED ACROSS
    ENGINES — and comparing across engines is the whole purpose of the number
    (Ryan: *"66toks is less than lmstudio etc"* / *"whole point is a toks
    improvement"*).

These arms pin the discrimination itself: a REPORTED rate and a COMPUTED rate
of the same magnitude must not be indistinguishable.
"""

from __future__ import annotations

import time

from litetui.turnstats import TpsState


def _started(seconds_ago: float) -> TpsState:
    t = TpsState()
    t.start()
    t.t0 = time.monotonic() - seconds_ago
    return t


def test_a_reported_rate_wins_and_is_not_the_wall_clock_figure():
    """🔴 THE ENGINE'S FIGURE, NOT THE CLIENT'S — and they differ here by
    construction, so a fallback that ignored `reported_rate` cannot pass by
    coincidence. 141 tokens over 2.0 s of client clock is ~70.5 tok/s; the
    engine says 155.0 because its own count excludes queueing and admission."""
    t = _started(2.0)
    assert t.final(141, reported_rate=155.0) == 155.0


def test_without_a_reported_rate_it_is_the_client_wall_clock():
    """⬜ THE CONTROL. Same tokens, same elapsed, no engine figure — the
    published number is now the client's, and it is materially different."""
    t = _started(2.0)
    got = t.final(141)
    assert got is not None and 65.0 < got < 76.0


def test_the_guards_hold_whoever_measured():
    """⬜ A REPORTED RATE DOES NOT BUY PAST THE TWO GUARDS. A turn that never
    started, or that produced no tokens, is nothing to publish however
    confident the engine is — and a non-positive rate is not a rate."""
    assert TpsState().final(141, reported_rate=155.0) is None   # never started
    assert _started(2.0).final(0, reported_rate=155.0) is None  # no tokens
    # A zero/negative report falls back rather than publishing nonsense.
    fell_back = _started(2.0).final(141, reported_rate=0.0)
    assert fell_back is not None and fell_back < 100.0
