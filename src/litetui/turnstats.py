"""Per-turn measurement state, split off `LiteTUI` — T070 step O4.

O4 is the first step in this task whose PURPOSE is to shrink the shared-state
knot rather than to move method count, so what lands here is STATE with its
owner, not helpers.

THREE FAMILIES, THREE OBJECTS, LANDED SEPARATELY. Measured before choosing: the
`_elapsed` / `_eta` / `_tps` families are **write-disjoint** — every field has
exactly one writing family — and **read-coupled**, which is why they are three
owners rather than one bundle and why the RENDERER stays behind:

    _eta_learn        READ  _elapsed_body_t0        (paid off: passed in, below)
    _elapsed_repaint  READ  tps  and CALL  _eta_*   (it is a VIEW, not state)

`_elapsed_repaint` consumes all three by nature. Moving it would make one object
reach into two others — the coupling relocated rather than removed — so it stays
on the app and reads from these.

⚠️ ORDER MATTERS AND IT IS ASCENDING INERT EXPOSURE, not size: `_eta` (6 inert
sites, 1 file) → `_elapsed` (13, 1 file) → `_tps` (19, 4 files). This step's
characteristic failure is a SILENTLY INERT site — a stale write that still
succeeds and binds a name nothing reads — and silent failures have no bisection
if they all land together. Three narrow windows beat one wide one precisely
because the suite cannot see this class of rot.
"""

from __future__ import annotations

import statistics
import time


class EtaState:
    """A rolling median of prompt-eval tokens/sec, and the projection it feeds.

    Learned ONLY from turns that demonstrably reprocessed the prompt (the
    KV-cache gate in `is_reliable_rate_sample`). `last_prompt_tokens` is the
    previous turn's count, used as the ESTIMATE this turn's ETA is projected
    from; `first_delta` is this turn's first-delta time, for first-token
    latency. All optional/None until a reliable turn has happened.
    """

    def __init__(self) -> None:
        self.samples: list[float] = []
        self.last_prompt_tokens: int | None = None
        self.first_delta: float | None = None

    def record_first_delta(self) -> None:
        """Stamp the first delta of the current turn. first_token_s is then
        `first_delta - turn_started_at`. Idempotent: only the first delta sets
        it, so later deltas don't move it."""
        if self.first_delta is None:
            self.first_delta = time.monotonic()

    def learn(self, prompt_tokens, turn_started_at: float, is_reliable) -> None:
        """End of a turn: remember this turn's token count as the ESTIMATE for
        the next turn's ETA, and if this turn is a reliable sample (first-token
        latency at the floor), fold its rate into the median. A cache-hit turn
        does not touch the median, so its misleadingly low rate never pollutes
        the ETA.

        🔴 `turn_started_at` IS PASSED IN, NOT REACHED FOR. On the class this
        read `self._elapsed_body_t0` — a field the `_elapsed` family OWNS. That
        was the one read-coupling between these two families, and taking it as an
        argument is the coupling PAID OFF rather than carried across the split.
        `is_reliable` is injected for the same reason in the other direction: it
        keeps this module from importing the text layer for one predicate.
        """
        if prompt_tokens:
            self.last_prompt_tokens = int(prompt_tokens)
        first = self.first_delta
        self.first_delta = None
        if first is not None and turn_started_at > 0.0:
            first_token_s = first - turn_started_at
            if is_reliable(prompt_tokens, first_token_s):
                self.samples.append(prompt_tokens / first_token_s)

    def learned_rate(self):
        """The rolling median of reliable rate samples, or None if none yet.
        A None rate makes render_progress elapsed-only, the honest state before
        a single reliable turn has happened."""
        if not self.samples:
            return None
        return statistics.median(self.samples)

    def estimate_tokens(self):
        """The token count the next turn's ETA is projected from: the most
        recent real count. None until the first turn reports usage."""
        return self.last_prompt_tokens
