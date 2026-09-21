"""Tool-call output must not be attributed to the short thinking phase."""
import json
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings
from litetui.turnstats import TpsState
from litetui.widgets import AssistantMessage
from tests.test_card_summary import _TC, _app, _Chunk, _Resp, _Stream
from tests.test_tps_under_spec_decoding import _UsageChunk


def test_first_payload_counts_even_when_it_starts_the_clock():
    stats = TpsState()
    assert stats.tick(now=1.0, reasoning=True, chars=84) is None
    assert stats.reasoning_chars == stats.chars == 84
    assert stats.reasoning_estimate == 21
    stats.tick(now=1.2, chars=36)
    stats.tick(now=16.0, chars=9656)
    assert stats.split_reasoning(2610) == 22
    stats.start()
    assert stats.chars == stats.reasoning_chars == 0


@pytest.mark.parametrize('answer', ['', 'Writing the source.'])
@pytest.mark.asyncio
async def test_real_stream_includes_fragmented_tool_arguments_in_thinking_share(
    monkeypatch, tmp_path, answer,
):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path)
    app = _app()
    app.settings = Settings(tools_enabled=False, autocompact_enabled=False,
                            wake_after_compact=False, clear_screen_after_compact=False)
    app.model_id = 'fixture'
    app.available_models = ['fixture']
    app._system = lambda *a, **k: None

    async def ready():
        pass

    async def execute(name, args):
        return 'ok', True

    app._ensure_chat_ready = ready
    app._execute_tool = execute
    args = json.dumps({'text': 'x' * 3900})
    chunks = [_Chunk(reasoning_content='r' * 40), _Chunk(reasoning_content='r' * 40)]
    if answer:
        chunks.append(_Chunk(content=answer))
    chunks.extend([
        _Chunk(tool_calls=[_TC(0, id='c1', name='pro')]),
        _Chunk(tool_calls=[_TC(0, name='be', arguments=args[:100])]),
        _Chunk(tool_calls=[_TC(0, arguments=args[100:])]),
        _UsageChunk(1000),
    ])
    rounds = iter([_Stream(chunks), _Stream([_Chunk(content='Done')])])

    async def create(**kw):
        if kw.get('purpose', 'turn') != 'turn':
            return _Resp('summary')
        return next(rounds)

    monkeypatch.setattr(app_mod.model_transport, 'for_app',
                        lambda _a: SimpleNamespace(create=create))
    async with app.run_test(size=(100, 40)) as pilot:
        app._append({'role': 'user', 'content': 'go'})
        app._stream()
        for _ in range(200):
            await pilot.pause()
            if not app._chat_running():
                break
        assert not app._chat_running()
        blocks = [c.thinking for c in app.query(AssistantMessage) if c.thinking]
        assert len(blocks) == 1
        expected = round(1000 * 80 / (80 + len(answer) + len('probe') + len(args)))
        assert blocks[0]._frozen[1] == expected, blocks[0]._frozen
        assert expected < 25, 'tool generation must not inflate thinking tokens'
