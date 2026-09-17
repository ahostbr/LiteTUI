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

⚠️ ORDER MATTERS AND THE DISCRIMINATOR IS **EXTERNAL** INERT, NOT TOTAL INERT.
This step's characteristic failure is a SILENTLY INERT site — a stale write that
still succeeds and binds a name nothing reads — and silent failures have no
bisection if they all land together. But an inert site INSIDE the file that is
moving leaves WITH the methods and resolves itself, so only inert sites OUTSIDE
it can stay green and wrong:

    landing         total inert   files   INERT OUTSIDE app.py  <- the risk
    O4-a  _eta            6         1              0
    O4-b  _elapsed       13         1              0
    O4-c  _tps           19         3              8   test_footer.py 7,
                                                       test_footer_fields.py 1

⇒ The order is not ascending-inert, it is ZERO-RISK, ZERO-RISK, THEN ALL OF IT,
and the mutation gate is owed only by O4-c and only against those two files.
Ascending total inert and ascending external inert happen to agree here; they
will not always, and the next person applies whichever one is written down.
(Measured at `e0e2d93` by SilverBolt; `tools/move_cost.py` now prints the
external count directly, because the first read of its output derived 3 where
the answer was 8 — wrong in the direction that declares a risky landing safe.)
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime

from litetui.fmt import fmt_dur


def format_turn_stop_line(
    *,
    started_at: float,
    final_tps: float | None,
    stopped: bool,
    show_line: bool,
    show_time: bool,
    now_monotonic: float | None = None,
    now_local: datetime | None = None,
) -> str | None:
    """Format the presentation-only line that settles one terminal turn.

    ``started_at`` is the request/turn clock, deliberately independent of the
    first-token clock used to calculate generation speed. Both clocks describe
    useful but different parts of the turn and must not be folded together.
    """
    if not show_line:
        return None
    now = time.monotonic() if now_monotonic is None else now_monotonic
    elapsed = fmt_dur(now - started_at)
    verb = f"stopped after {elapsed}" if stopped else f"Cooked for {elapsed}"
    if final_tps is not None:
        verb += f" · {final_tps:.1f} tok/s"
    if show_time:
        local = datetime.now().astimezone() if now_local is None else now_local
        verb += f" · done {local.strftime('%I:%M %p').lstrip('0')}"
    return verb


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


class ElapsedState:
    """The in-flight clock: which bubble is counting, since when, and the task
    that repaints it.

    THE RENDERER IS NOT HERE ON PURPOSE. `LiteTUI._elapsed_repaint` reads this
    object, `tps` and `EtaState` together — it is a VIEW over all three families
    — so it stays on the app and this class only asks the app to RUN it. That
    is a call-coupling, not a state-coupling: nothing outside this object writes
    `task` / `body` / `body_t0` any more, which is the property O4 exists to buy.

    `ensure_running` is the fold-in of TWO byte-identical five-line blocks that
    sat inline in `_tool_begin` and the compaction-card path, each reaching in to
    assign `_elapsed_task` directly. They had to be swept for the move regardless;
    writing the same restart twice against a new receiver would have carried the
    duplication across instead of paying it off.
    """

    def __init__(self, app) -> None:
        self._app = app
        self.task = None
        self.body = None            # AnswerBody in its pre-token phase
        self.body_t0: float = 0.0

    def cancel(self) -> None:
        task = self.task
        if task is not None and not task.done():
            task.cancel()
        self.task = None

    def start(self, body, *, started_at: float | None = None) -> None:
        """Begin counting for ``body`` from the whole turn's start.

        Agent turns may open several assistant bubbles across tool rounds. The
        caller passes one shared ``started_at`` so each bubble's live clock —
        and the eventual stop line — describes the whole user turn rather than
        restarting after every tool result.
        """
        self.cancel()
        self.body = body
        self.body_t0 = time.monotonic() if started_at is None else started_at
        self._spawn()

    def stop_body(self) -> None:
        """The bubble stopped being pre-token. The LOOP keeps running — tool
        calls and the compaction card still need it — so this clears only the
        body, never the task."""
        self.body = None

    def ensure_running(self) -> None:
        """Restart the shared loop if it has retired. It self-retires after ~1s
        idle, and a tool call or a compaction can begin with nothing else in
        flight — without this the clock never ticks for them."""
        if self.task is None or self.task.done():
            self._spawn()

    def _spawn(self) -> None:
        # RuntimeError == no running loop (the unit tests construct the app
        # outside one). A missing clock must never take down the turn.
        try:
            self.task = asyncio.create_task(self._app._elapsed_repaint())
        except RuntimeError:
            self.task = None


class TpsState:
    """Generation speed for the turn in flight: delta bookkeeping and the rate.

    🔴 `tps` ITSELF IS NOT HERE, AND THAT IS NOT AN OVERSIGHT. On the app it is a
    Textual `reactive` WITH a watcher — assigning it triggers the repaint AND
    `watch_tps -> _refresh_ctx_label()`. Moving it to a plain attribute on this
    object would silently delete both effects while every unit test kept passing,
    which is exactly the failure class this whole step is sequenced around. So
    the split is at the honest seam: this object owns the PRIVATE bookkeeping and
    COMPUTES the rate; `_stream` publishes it. `tick` and `final` therefore return
    `float | None` — a value to publish, or nothing — rather than writing through
    to the app. Nothing here reaches into the app at all.
    """

    def __init__(self) -> None:
        self.t0: float | None = None
        self.n = 0
        #: `n` SPLIT BY KIND, never counted separately — see `tick`.
        #: reasoning + content == n, always, because one increment site
        #: partitions the same event rather than a second counter tallying
        #: the same deltas. Two counts of one thing that must agree is the
        #: drift class this file would otherwise be introducing.
        self.reasoning = 0
        self.content = 0
        self.painted = 0.0

    def start(self) -> None:
        """A new turn: forget the previous one entirely."""
        self.t0 = None
        self.n = 0
        self.reasoning = 0
        self.content = 0
        self.painted = 0.0

    def tick(self, now: float | None = None, *,
             reasoning: bool = False) -> float | None:
        """One streamed delta arrived. Returns a live estimate to publish, or
        None — see `final` for the figure that supersedes it.

        Repaints at most 4x/second. The footer is one Static, but this runs on
        every token of every turn, and a repaint per token on a 27B is a real
        cost paid to render a number that changes in the third decimal.
        """
        now = time.monotonic() if now is None else now
        if self.t0 is None:
            self.t0 = now
            return None         # nothing to divide by yet
        self.n += 1
        # THE SAME INCREMENT, PARTITIONED. Not a second tally: the split has
        # to sum to `n` or the header and the footer could disagree about a
        # turn nobody could then reconcile.
        if reasoning:
            self.reasoning += 1
        else:
            self.content += 1
        elapsed = now - self.t0
        if elapsed >= 0.4 and now - self.painted >= 0.25:
            self.painted = now
            return self.n / elapsed
        return None

    def final(
        self, completion_tokens: int, *, reported_rate: float | None = None
    ) -> float | None:
        """Settle to the exact figure the server reports, or None to leave the
        live estimate standing.

        The live number counts STREAM DELTAS, which are only approximately
        tokens. `usage.completion_tokens` is the server's own count and includes
        reasoning tokens, so it matches what the model actually generated.

        🔴 `reported_rate` IS A MORE EXACT FIGURE ARRIVING AT A SEAM BUILT FOR
        ONE (T806). The arithmetic below divides the server's token count by the
        CLIENT's wall clock, so queueing and admission are charged to the model
        and a busy engine reads slower than it ran. An engine that measures its
        own decode loop — llama.cpp and NInfer both publish `timings.
        predicted_per_second` — already knows the answer.

            RYAN: *"66toks is less than lmstudio etc"* / *"whole point is a toks
            improvement"*. A comparison between engines is only worth making if
            the number is of the same thing, and the engine's own figure is the
            one that is.

        ⬜ IT DOES NOT SKIP THE GUARDS BELOW. A reported rate on a turn that
        never started, or that produced no tokens, is still nothing to publish —
        the same two conditions, for the same reasons, whoever measured.
        """
        if self.t0 is None or not completion_tokens:
            return None
        if reported_rate is not None and reported_rate > 0:
            return float(reported_rate)
        elapsed = time.monotonic() - self.t0
        if elapsed > 0:
            return completion_tokens / elapsed
        return None
