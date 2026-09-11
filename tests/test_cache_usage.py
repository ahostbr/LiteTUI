"""T624: offline completed-event usage survives mapping and app consumption."""
import json
from types import SimpleNamespace

import httpx
import pytest
from test_oauth_transport import auth_file

from litetui import model_transport as mt
from litetui.app import LiteTUI


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("details", [None, {"cached_tokens": 10000, "cache_write_tokens": 2000, "other_tokens": 7}])
async def test_cache_details_reach_app_record(tmp_path, streamed, details):
    raw = {"input_tokens": 12000, "output_tokens": 100}
    if details is not None:
        raw["input_tokens_details"] = details
    event = {"type": "response.completed", "response": {"usage": raw, "output": []}}
    transport = mt.OAuthTransport("codex", credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="data: " + json.dumps(event) + "\n\n")))
    result = await transport.create(model="gpt-test", messages=[], stream=streamed)
    if streamed:
        chunks = [chunk async for chunk in result]
        usage = chunks[-1].usage
    else:
        usage = result.usage
    assert getattr(usage, "cached_tokens", None) == (details["cached_tokens"] if details else None)
    app = SimpleNamespace(ctx_used=12100)
    LiteTUI._record_usage(app, usage)
    assert app.last_usage["cached_tokens"] == (details["cached_tokens"] if details else None)
    assert app.last_usage["cache_write_tokens"] == (details["cache_write_tokens"] if details else None)
    assert app.last_usage["input_tokens_details"] == details
    assert usage.total_tokens == app.ctx_used == 12100


@pytest.mark.asyncio
async def test_stream_consumer_records_usage_without_subtracting_cache():
    from test_deny_stops_the_turn import _app, _Chunk, _ToolCallStream, _turn
    usage = mt._usage({"input_tokens": 12000, "output_tokens": 100,
                       "input_tokens_details": {"cached_tokens": 10000}}, "codex")
    a = _app(None)

    async def create(**kwargs):
        stream = _ToolCallStream(0)
        chunk = _Chunk(content="done")
        chunk.usage = usage
        stream._chunks = [chunk]
        return stream

    a._create = create
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)
        assert a.last_usage["cached_tokens"] == 10000
        assert a.ctx_used == 12100


def test_usage_details_never_keep_content():
    usage = mt._usage({"input_tokens": 10, "output_tokens": 2,
                       "input_tokens_details": {"cached_tokens": 0, "text": "secret", "flag": True},
                       "output_tokens_details": {"reasoning_tokens": 2}}, "codex")
    assert usage.cached_tokens == 0
    assert usage.cache_write_tokens is None
    assert usage.input_tokens_details == {"cached_tokens": 0}
    assert usage.usage_details["output_tokens_details"] == {"reasoning_tokens": 2}
