"""T911: Claude prompt-cache lifetime pin, footer health/countdown, cold warning."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from litetui import claude_cache as cc
from litetui.claude_backend import CACHE_TTL_SECONDS, cache_env
from litetui.claude_events import ClaudeUsage
from litetui.claude_persistence import ClaudeLedger

T0 = 1_000_000.0


def usage(read=0, written=0, uncached=0, source="message"):
    return ClaudeUsage(source=source, input_tokens=uncached, cache_read_tokens=read, cache_creation_tokens=written)


# ── 3. lifetime pin ──────────────────────────────────────────────────────────

def test_cache_env_pins_one_hour_and_blanks_inherited_switches():
    env = cache_env({"DISABLE_PROMPT_CACHING_OPUS": "1", "FORCE_PROMPT_CACHING_5M": "1", "PATH": "x"})
    assert env["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert env["FORCE_PROMPT_CACHING_5M"] == "" and env["DISABLE_PROMPT_CACHING_OPUS"] == ""
    assert env["DISABLE_PROMPT_CACHING"] == ""
    assert "PATH" not in env  # only the cache keys are overridden


def test_the_options_carry_the_cache_env(monkeypatch):
    from litetui import claude_backend as cb
    seen = {}
    fake = SimpleNamespace(ClaudeAgentOptions=lambda **kw: seen.update(kw) or kw)
    monkeypatch.setattr(cb, "sdk_module", lambda: fake)
    backend = cb.ClaudeBackend.__new__(cb.ClaudeBackend)
    backend.settings = SimpleNamespace(claude_executable="")
    asyncio.run(backend._options(cwd="."))
    assert seen["env"]["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"


# ── 2. footer health + countdown ─────────────────────────────────────────────

def test_footer_label_shows_hit_rate_and_time_left_then_goes_cold():
    clock = cc.CacheClock(segment_id="s")
    assert clock.label(T0) is None  # nothing observed yet: no footer field
    clock.observe(usage(read=9000, written=500, uncached=500), now=T0)
    text, _ = clock.label(T0 + 600)
    assert text == "cache warm 90% 50m"
    text, style = clock.label(T0 + CACHE_TTL_SECONDS - 60)
    assert text.endswith("1:00") and style == "#e8a33d"  # the last two minutes are amber
    assert clock.label(T0 + CACHE_TTL_SECONDS + 1)[0] == "cache cold"


def test_a_first_write_reads_new_and_a_result_frame_is_ignored():
    clock = cc.CacheClock()
    assert not clock.observe(usage(read=5, source="result"), now=T0)
    clock.observe(usage(written=4000, uncached=10), now=T0)
    assert clock.label(T0)[0].startswith("cache new 0%")


def test_each_reading_turn_resets_the_countdown():
    clock = cc.CacheClock()
    clock.observe(usage(read=1), now=T0)
    clock.observe(usage(read=1), now=T0 + 3000)
    assert clock.remaining(T0 + 3000) == CACHE_TTL_SECONDS


# ── 1. cold-restart warning ──────────────────────────────────────────────────

def warm(model="opus"):
    clock = cc.CacheClock(model=model)
    clock.observe(usage(read=100), now=T0)
    return clock


def test_warm_live_session_same_model_needs_no_warning():
    assert cc.cold_reason(warm(), live=True, resuming=False, model="opus", now=T0 + 60) is None


def test_model_switch_on_a_live_session_warns():
    kind, text = cc.cold_reason(warm(), live=True, resuming=False, model="sonnet", now=T0 + 60)
    assert kind == "model" and "opus to sonnet" in text


def test_idle_past_the_lifetime_warns_and_so_does_the_last_two_minutes():
    assert cc.cold_reason(warm(), live=True, resuming=False, model="opus", now=T0 + 4000)[0] == "expired"
    assert cc.cold_reason(warm(), live=True, resuming=False, model="opus",
                          now=T0 + CACHE_TTL_SECONDS - 30)[0] == "expired"


def test_resume_warns_when_expired_or_unknown_but_not_when_recent():
    clock = cc.CacheClock()
    assert cc.cold_reason(clock, live=False, resuming=True, model="opus", used_at=None, now=T0)[0] == "resume"
    assert cc.cold_reason(clock, live=False, resuming=True, model="opus", used_at=T0 - 7200, now=T0)[0] == "resume"
    assert cc.cold_reason(clock, live=False, resuming=True, model="opus", used_at=T0 - 600, now=T0) is None


def test_a_brand_new_session_has_nothing_to_warn_about():
    assert cc.cold_reason(cc.CacheClock(), live=False, resuming=False, model="opus", now=T0) is None


def test_headless_host_is_warned_first_and_confirms_by_sending_again():
    events, notes = [], []
    app = SimpleNamespace(_rpc=object(), _rpc_emit=events.append, _system=notes.append)
    cold = ("model", "Switching model.")
    assert asyncio.run(cc.confirm_cold(app, cold)) is False
    assert events[0]["type"] == "cache_warning" and "Send again" in notes[0]
    assert asyncio.run(cc.confirm_cold(app, cold)) is True
    assert asyncio.run(cc.confirm_cold(app, cold)) is False  # the go-ahead is used up


def test_the_ledger_remembers_the_last_cache_use_for_a_resume(tmp_path):
    ledger = ClaudeLedger(tmp_path)
    seg = ledger.select_segment(str(tmp_path))
    ledger.note_cache(seg["id"], T0, "opus")
    again = ClaudeLedger(tmp_path).segment(seg["id"])
    assert again["cache_used_at"] == T0 and again["cache_model"] == "opus"


def test_an_effort_change_on_a_live_session_warns_like_a_model_switch():
    clock = warm()
    clock.effort = "high"
    kind, text = cc.cold_reason(clock, live=True, resuming=False, model="opus", effort="max", now=T0 + 60)
    assert kind == "effort" and "high to max" in text
    assert cc.cold_reason(clock, live=True, resuming=False, model="opus", effort="high", now=T0 + 60) is None
    assert cc.cold_reason(clock, live=True, resuming=False, model="opus", now=T0 + 60)[0] == "effort"
    assert cc.cold_reason(warm(), live=True, resuming=False, model="opus", effort="max", now=T0 + 60) is None
