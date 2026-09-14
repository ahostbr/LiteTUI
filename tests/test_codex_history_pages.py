from types import SimpleNamespace

import pytest

from litetui.codex_history import hydrate, read
from litetui.model_transport import ProviderError


@pytest.mark.asyncio
async def test_paginated_reader_fetches_every_full_page_in_native_order():
    requests = []

    async def request(method, params):
        requests.append((method, params))
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {"thread": {"id": "thread", "historyMode": "paginated", "turns": []}}
        assert params["itemsView"] == "full" and params["sortDirection"] == "asc"
        if "cursor" not in params:
            return {"data": [{"id": "one", "items": [], "itemsView": "full"}], "nextCursor": "page2"}
        assert params["cursor"] == "page2"
        return {"data": [{"id": "two", "items": [{"id": "answer", "type": "agentMessage"}]}]}

    result = await read(SimpleNamespace(request=request), "thread")
    assert [turn["id"] for turn in result["turns"]] == ["one", "two"]
    assert result["turns"][1]["items"][0]["id"] == "answer"
    assert [method for method, _ in requests] == ["thread/read", "thread/turns/list", "thread/turns/list"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_page", [
    {"data": [{"id": "one", "items": [], "itemsView": "summary"}]},
    {"data": [{"id": "one", "itemsView": "full"}]},
    {"data": None},
    {"data": [], "nextCursor": "repeated"},
])
async def test_invalid_or_repeating_pages_fail_without_returning_partial_history(bad_page):
    async def request(method, params):
        return bad_page

    with pytest.raises(ProviderError):
        await hydrate(SimpleNamespace(request=request), {"id": "thread", "historyMode": "paginated"})


@pytest.mark.asyncio
async def test_legacy_read_uses_full_read_and_validates_identity_again():
    calls = []

    async def request(method, params):
        calls.append(params)
        return {"thread": {"id": "other" if params["includeTurns"] else "thread"}}

    with pytest.raises(ProviderError, match="different thread"):
        await read(SimpleNamespace(request=request), "thread")
    assert [params["includeTurns"] for params in calls] == [False, True]
