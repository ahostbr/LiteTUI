"""A delta is not a token: the tok/s readouts under speculative decoding.

RYAN, 2026-09-18 12:4x, watching the ninfer seat: "idk why toks r so low ...
it was getting 150toks last night ... it seems to get like a burst at 150ish at
the start of a response then slow down ... and then that wall at 152 at the
end ... weird af".

Measured on the live server (--spec mtp --draft-tokens 3): SSE deltas arrive at
~62/s whatever the text, because one delta is one VERIFY STEP carrying every
accepted draft token; usage.completion_tokens said 245 tok/s on predictable
text and 150 on unpredictable. The footer's LIVE rate and the thinking header
both counted deltas (~55), the footer's SETTLED rate used the server's count
(152) - three numbers, one of them right. The 150 "burst at the start" was the
previous turn's settled figure still standing until the first live tick.
"""
import time
from types import SimpleNamespace

import pytest

from litetui import app as app_mod, paths
from litetui.settings import Settings
from litetui.turnstats import TpsState
from litetui.widgets import AssistantMessage, ThinkingBlock

from tests.test_card_summary import _app, _Resp, _Stream, _Chunk


class TestTpsState:
    def test_batched_deltas_are_estimated_from_characters(self):
        t = TpsState()
        t.tick(now=0.0)                                   # t0
        for i in range(10):                               # 10 deltas x 16 chars
            t.tick(now=0.1 * (i + 1), chars=16)
        assert t.n == 10
        assert t.tokens_estimate == 40, "16-char deltas are ~4 tokens each"

    def test_one_token_per_delta_keeps_the_exact_count(self):
        t = TpsState()
        t.tick(now=0.0)
        for i in range(10):                               # short deltas: the count wins
            t.tick(now=0.1 * (i + 1), chars=3)
        assert t.tokens_estimate == 10

    def test_live_rate_uses_the_estimate(self):
        t = TpsState()
        t.tick(now=0.0)
        rate = None
        for i in range(10):
            rate = t.tick(now=0.1 * (i + 1), chars=16) or rate
        assert rate == pytest.approx(40 / 1.0)

    def test_reasoning_share_is_split_by_characters(self):
        t = TpsState()
        t.tick(now=0.0)
        t.tick(now=0.5, reasoning=True, chars=300)
        t.tick(now=1.0, chars=100)
        assert t.reasoning_estimate == 75
        assert t.split_reasoning(100) == 75
        assert TpsState().split_reasoning(100) is None, "no trace, no share"


class TestThinkingBlock:
    """The bare double from test_thinking_timer: a constructed block without
    a composed app, so append()'s widget writes are stood in for."""

    def _block(self):
        b = ThinkingBlock.__new__(ThinkingBlock)
        b._t0 = time.monotonic() - 1.0
        b._toks = 0
        b._chars = 0
        b._settled = None
        b._buffer = ""
        b._frozen = None
        b._marker = "▾"
        b._set_header = lambda text: setattr(b, "header", text)
        return b

    @staticmethod
    def _feed(b, deltas, chars_each):
        for _ in range(deltas):                 # what append() counts, minus its widget writes
            b._buffer += "x" * chars_each
            b._toks += 1
            b._chars += chars_each

    def test_frozen_count_is_the_estimate_not_the_delta_count(self):
        b = self._block()
        self._feed(b, 5, 20)                    # 5 deltas, ~25 tokens
        b.freeze_header()
        assert b._frozen[1] == 25

    def test_settle_after_freeze_redoes_the_readout(self):
        b = self._block()
        self._feed(b, 5, 20)
        b.freeze_header()
        b.settle(60)
        assert b._frozen[1] == 60
        assert "60 tok" in b.header

    def test_settle_before_freeze_is_kept_for_the_freeze(self):
        """A thinking-only round: usage lands before _thinking_done freezes."""
        b = self._block()
        self._feed(b, 5, 20)
        b.settle(60)
        b.freeze_header()
        assert b._frozen[1] == 60

    def test_settle_with_nothing_changes_nothing(self):
        b = self._block()
        self._feed(b, 1, 4)
        b.freeze_header()
        before = b._frozen
        b.settle(None); b.settle(0)
        assert b._frozen == before


class _UsageChunk:
    """The engine's final chunk: no delta, the round's usage."""
    def __init__(self, completion_tokens):
        self.choices = [type("C", (), {"delta": SimpleNamespace(
            content=None, reasoning_content=None, reasoning=None, tool_calls=None),
            "finish_reason": "stop"})()]
        self.usage = SimpleNamespace(prompt_tokens=40, completion_tokens=completion_tokens,
                                     total_tokens=40 + completion_tokens)


@pytest.mark.asyncio
async def test_the_thinking_header_settles_on_the_servers_count(monkeypatch, tmp_path):
    """Five 20-char reasoning deltas (5 deltas, ~25 tokens by chars), then the
    server says the round was 60 tokens: the header must say 60, not 5."""
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path)
    app = _app()
    app.settings = Settings(tools_enabled=False, autocompact_enabled=False,
                            wake_after_compact=False, clear_screen_after_compact=False)
    app.model_id = "fixture"
    app.available_models = ["fixture"]
    app._system = lambda *a, **k: None

    async def ready():
        pass
    app._ensure_chat_ready = ready

    chunks = [_Chunk(reasoning_content="x" * 20) for _ in range(5)] + [_UsageChunk(60)]
    rounds = iter([_Stream(chunks)])

    async def create(**kw):
        if kw.get("purpose", "turn") != "turn":
            return _Resp("one line")
        return next(rounds)
    monkeypatch.setattr(app_mod.model_transport, "for_app",
                        lambda _a: SimpleNamespace(create=create))

    async with app.run_test(size=(100, 40)) as pilot:
        app._append({"role": "user", "content": "go"})
        app._stream()
        for _ in range(200):
            await pilot.pause()
            if not app._chat_running():
                break
        for _ in range(10):
            await pilot.pause()
        card = list(app.query(AssistantMessage))[-1]
        assert card.thinking is not None
        assert card.thinking._frozen[1] == 60, card.thinking._frozen
        assert "60 tok" in card.thinking.query_one("ThinkingHeader").content
