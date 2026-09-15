from litetui.codex_usage import NativeUsage


def snapshot(inp, out, cached, *, total=None):
    last = {
        "inputTokens": inp,
        "outputTokens": out,
        "cachedInputTokens": cached,
        "totalTokens": inp + out,
    }
    return {"last": last, "total": total or dict(last), "modelContextWindow": 258400}


def test_context_is_latest_request_and_turn_usage_is_deduplicated_cumulative_delta():
    meter = NativeUsage(fresh=True)
    first = snapshot(2000, 100, 1800)
    u = meter.update(first)
    assert u.total_tokens == u.context_tokens == 2100
    assert meter.update(first) is None
    u = meter.update(
        snapshot(
            2200,
            200,
            2000,
            total={
                "inputTokens": 4200,
                "outputTokens": 300,
                "cachedInputTokens": 3800,
                "totalTokens": 4500,
            },
        )
    )
    assert u.context_tokens == 2400
    assert u.total_tokens == 4500
    assert u.cached_tokens == 3800
    assert u.cache_write_tokens is None
    resumed = NativeUsage(meter.previous["total"])
    u = resumed.update(
        snapshot(
            2500,
            50,
            2300,
            total={
                "inputTokens": 6700,
                "outputTokens": 350,
                "cachedInputTokens": 6100,
                "totalTokens": 7050,
            },
        )
    )
    assert u.total_tokens == 2550 and u.prompt_tokens == 2500
    assert u.thread_usage["totalTokens"] == 7050


def test_missing_baseline_and_counter_reset_do_not_fabricate_usage():
    meter = NativeUsage()
    u = meter.update(snapshot(2000, 100, 1800))
    assert u.total_tokens is None and u.context_tokens == 2100
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(2000, 100, 1800))
    u = meter.update(snapshot(500, 20, 300))
    assert u.total_tokens is None and u.context_tokens == 520
    assert u.turn_usage == {}
    u = meter.update({})
    assert u.context_tokens is None and u.cache_write_tokens is None


def test_counter_reset_is_detected_across_missing_usage_snapshots():
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(2000, 100, 1800))
    missing = meter.update({"last": {"totalTokens": 50}})
    assert missing.total_tokens is None and missing.context_tokens == 50
    reset = meter.update(snapshot(500, 20, 300))
    assert reset.turn_usage == {}
    assert reset.total_tokens is None
    assert reset.context_tokens == 520


def test_partial_snapshot_does_not_forget_counter_history():
    meter = NativeUsage(fresh=True)
    meter.update(snapshot(2000, 100, 1800))
    partial = meter.update({"total": {"inputTokens": 2200}, "last": {"totalTokens": 2200}})
    assert partial.prompt_tokens == 2200 and partial.completion_tokens is None
    reset = meter.update({"total": {"inputTokens": 2500, "outputTokens": 50},
                          "last": {"totalTokens": 550}})
    assert reset.turn_usage == {} and reset.context_tokens == 550

    growing = NativeUsage(fresh=True)
    growing.update(snapshot(2000, 100, 1800))
    growing.update({})
    valid = growing.update(snapshot(2500, 150, 2000))
    assert valid.total_tokens == 2650 and valid.cached_tokens == 2000
