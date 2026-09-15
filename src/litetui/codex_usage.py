"""Snapshot accounting; native context occupancy is not cumulative token spend."""

from types import SimpleNamespace

FIELDS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "reasoningOutputTokens",
)


def counts(value):
    value = value if isinstance(value, dict) else {}
    return {
        key: value[key]
        for key in FIELDS
        if type(value.get(key)) is int and value[key] >= 0
    }


class NativeUsage:
    def __init__(self, previous=None, *, fresh=False):
        self.baseline = (
            counts(previous)
            if previous is not None
            else ({key: 0 for key in FIELDS} if fresh else {})
        )
        self.observed_totals = dict(self.baseline)
        self.previous = None
        self.rebased = False

    def update(self, payload):
        latest, cumulative = counts(payload.get("last")), counts(payload.get("total"))
        snapshot = {
            "last": latest,
            "total": cumulative,
            "modelContextWindow": payload.get("modelContextWindow"),
        }
        if snapshot == self.previous:
            return None
        if any(
            cumulative[k] < v
            for k, v in self.observed_totals.items()
            if k in cumulative
        ):
            self.rebased = True
        # An omitted counter is unknown in this snapshot, not forgotten history.
        # Keep prior observations solely for reset detection; never fill missing
        # output fields from them or fabricate a current cumulative value.
        self.observed_totals.update(cumulative)
        self.previous = snapshot
        aggregate = (
            {}
            if self.rebased
            else {
                k: v - self.baseline[k]
                for k, v in cumulative.items()
                if k in self.baseline and v >= self.baseline[k]
            }
        )
        if self.rebased:
            # A reconnect or a compaction restarts the native counters. THIS
            # snapshot cannot express a turn delta and stays unknown above —
            # but the run that follows is monotonic again, so re-baseline onto
            # it instead of reporting unknown for the rest of the conversation.
            # observed_totals is REPLACED, not updated: a key absent from the
            # reset snapshot would otherwise keep its pre-reset high-water mark
            # and make the next snapshot that carries it look like a new reset.
            self.baseline = dict(cumulative)
            self.observed_totals = dict(cumulative)
            self.rebased = False
        # Retain absent fields as unknown, including cache writes. A cache hit
        # changes billing/reuse, never the occupied context size.
        context = latest.get("totalTokens")
        if context is None and "inputTokens" in latest and "outputTokens" in latest:
            context = latest["inputTokens"] + latest["outputTokens"]
        window = payload.get("modelContextWindow")
        window = window if type(window) is int and window > 0 else None
        return SimpleNamespace(
            prompt_tokens=aggregate.get("inputTokens"),
            completion_tokens=aggregate.get("outputTokens"),
            total_tokens=aggregate.get("totalTokens"),
            cached_tokens=aggregate.get("cachedInputTokens"),
            cache_write_tokens=aggregate.get("cacheWriteInputTokens"),
            input_tokens_details={
                "cached_tokens": aggregate.get("cachedInputTokens"),
                "cache_write_tokens": aggregate.get("cacheWriteInputTokens"),
            },
            context_tokens=context,
            max_context_tokens=window,
            latest_request_usage=latest,
            thread_usage=cumulative,
            turn_usage=aggregate,
            usage_details={"last": latest, "total": cumulative, "turn": aggregate},
        )
