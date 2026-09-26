"""T992: the footer cache field on a Codex seat, the same field Claude's uses.

Ryan, 2026-09-26: "For the clawed back end, we display the caching and the
footer. Add that to light TUI as well. The same type of system."
"""
import time
from types import SimpleNamespace

from litetui import app as app_mod
from litetui import appsvc
from litetui import claude_cache as cc
from litetui.settings import Settings

T0 = time.time()


class FakeApp:
    ctx_label_text = app_mod.LiteTUI.ctx_label_text
    _append_tps_into = appsvc.append_tps_into

    def __init__(self, backend, last_usage=None, claude_cache=None, **settings):
        self.settings = Settings(**settings)
        self.backend = SimpleNamespace(name=backend)
        self.last_usage = last_usage
        self._claude_cache = claude_cache
        self.seat = None
        self.thinking_level = None
        self.convo_id = ""
        self.ctx_used = self.ctx_max = None
        self.tps = None


def app_server(cached, inp=1000):
    # codex_usage.NativeUsage: the last request's counters are latest_request_usage;
    # prompt_tokens/cached_tokens there are the TURN aggregate, not the last request.
    return {"prompt_tokens": 9999, "cached_tokens": 1,
            "latest_request_usage": {"inputTokens": inp, "cachedInputTokens": cached}}


def responses(cached, inp=1000):
    # model_transport._usage: one request, input_tokens_details.cached_tokens.
    # _record_usage stores every key, so latest_request_usage is present as None.
    return {"prompt_tokens": inp, "cached_tokens": cached, "latest_request_usage": None}


def footer(app):
    return app.ctx_label_text.plain


def test_codex_app_server_shows_last_request_hit_rate():
    assert "cache warm 83%" in footer(FakeApp("codex", app_server(830)))


def test_codex_responses_path_shows_hit_rate():
    assert "cache warm 25%" in footer(FakeApp("codex", responses(250)))


def test_codex_zero_cached_is_a_cold_readout_not_a_hidden_field():
    assert "cache cold 0%" in footer(FakeApp("codex", app_server(0)))


def test_app_server_without_per_request_input_is_absent_not_the_turn_aggregate():
    usage = {"prompt_tokens": 3000, "cached_tokens": 1000,
             "latest_request_usage": {"cachedInputTokens": 500}}
    assert "cache" not in footer(FakeApp("codex", usage))


def test_codex_unmeasured_is_absent_not_zero():
    assert "cache" not in footer(FakeApp("codex", None))
    assert "cache" not in footer(FakeApp("codex", {"prompt_tokens": 1000, "cached_tokens": None}))


def test_claude_readout_unchanged():
    clock = cc.CacheClock(segment_id="s")
    clock.read, clock.uncached, clock.used_at = 900, 100, T0
    expected = clock.label()[0]
    line = footer(FakeApp("claude", responses(250), claude_cache=clock))
    assert expected in line and "cache warm 25%" not in line


def test_setting_off_hides_it_for_both_backends():
    clock = cc.CacheClock(segment_id="s")
    clock.read, clock.used_at = 900, T0
    assert "cache" not in footer(FakeApp("codex", app_server(830), footer_show_cache=False))
    assert "cache" not in footer(FakeApp("claude", claude_cache=clock, footer_show_cache=False))


def test_other_backends_show_nothing():
    assert "cache" not in footer(FakeApp("lmstudio", responses(250)))
