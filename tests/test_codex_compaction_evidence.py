"""Reviewed Codex manual compaction requires native event identity and persisted full history."""
import asyncio
from types import SimpleNamespace

import pytest

from litetui.codex_app_server import AppServerTransport
from litetui.codex_compaction_evidence import VerifiedCompaction
from litetui.model_transport import ProviderError

pytestmark = pytest.mark.asyncio


class Server:
    def __init__(self, *, mode="paginated", fault=None):
        self.process = object()
        self.events = asyncio.Queue()
        self.requests = []
        self.replies = []
        self.mode = mode
        self.fault = fault

    async def start(self):
        pass

    async def send(self, message):
        self.replies.append(message)

    def emit(self, method, **fields):
        self.events.put_nowait({"method": method, "params": {"threadId": "thread", **fields}})

    async def request(self, method, params):
        self.requests.append((method, params))
        item = {"type": "contextCompaction", "id": "actual-native-item"}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/compact/start":
            self.emit("turn/started", turn={"id": "turn"})
            self.emit("item/started", turnId="turn", item=item)
            if self.fault == "unexpected-callback":
                self.events.put_nowait({"id": 91, "method": "item/tool/call", "params": {}})
            if self.fault == "extra-native-work":
                self.emit("item/started", turnId="turn", item={"type": "commandExecution", "id": "unexpected"})
            if self.fault == "overlap":
                self.emit("turn/started", turn={"id": "other"})
            if self.fault != "missing-completion":
                self.emit("item/completed", turnId="turn", item={**item, "id": (
                    "different" if self.fault == "changed-live-item" else item["id"])})
            self.emit("turn/completed", turn={"id": "turn", "status": "completed"})
            return {}
        if method == "turn/interrupt":
            return {}
        turn = {"id": "newer" if self.fault == "newer-turn" else "turn",
                "status": "interrupted" if self.fault == "interrupted" else "completed",
                "itemsView": "summary" if self.fault == "summary-only" else "full",
                "items": [] if self.fault == "missing-item" else [item]}
        if self.fault == "duplicate-item":
            turn["items"].append(item.copy())
        if self.fault == "synthetic-display-id":
            turn["items"][0] = {**item, "id": "compact:turn"}
        if method == "thread/read":
            return {"thread": {"id": "foreign" if self.fault == "foreign-thread" and any(
                m == "thread/compact/start" for m, _ in self.requests) else "thread",
                "status": {"type": "active" if self.fault == "active-before" else "idle"}, "historyMode": self.mode, "turns": [turn] if params["includeTurns"] else []}}
        if method == "thread/turns/list":
            assert self.mode == "paginated"
            assert params == {"threadId": "thread", "itemsView": "full", "sortDirection": "desc", "limit": 1}
            if self.fault == "process-change":
                self.process = object()
            return {"data": [turn]}
        raise AssertionError(method)


def transport(server):
    app = SimpleNamespace(conversation=[{"provider_metadata": {"app_server_thread_id": "thread"}}])
    client = AppServerTransport(server, app)
    client.thread_id, client.process = "thread", server.process
    return client


@pytest.mark.parametrize("mode", ["paginated", "legacy"])
async def test_receipt_requires_independent_full_native_readback(mode):
    server = Server(mode=mode)
    result = await transport(server).compact(expected_thread="thread", timeout=1)
    assert result == VerifiedCompaction("thread", "turn", "actual-native-item")
    assert [method for method, _params in server.requests] == ["thread/read", "thread/compact/start", "thread/read",
        "thread/turns/list" if mode == "paginated" else "thread/read"]


@pytest.mark.parametrize("fault", ["missing-completion", "changed-live-item", "overlap", "foreign-thread",
    "newer-turn", "summary-only", "interrupted", "missing-item", "duplicate-item", "synthetic-display-id",
    "unexpected-callback", "process-change", "extra-native-work"])
async def test_no_verified_receipt_on_unmatched_or_ambiguous_history(fault):
    server = Server(fault=fault)
    client = transport(server)
    with pytest.raises(ProviderError):
        await client.compact(expected_thread="thread", timeout=1)
    assert client.turn_id is None and not client.lock.locked()
    assert [name for name, _ in server.requests].count("thread/compact/start") == 1
    if fault == "unexpected-callback":
        assert len(server.replies) == 1 and server.replies[0]["id"] == 91 and "error" in server.replies[0]


async def test_remote_thread_must_be_idle_before_dispatch():
    server = Server(fault="active-before")
    with pytest.raises(ProviderError, match="idle native thread"):
        await transport(server).compact(expected_thread="thread", timeout=1)
    assert [name for name, _params in server.requests] == ["thread/read"]


async def test_foreign_review_is_refused_before_native_dispatch():
    server = Server()
    with pytest.raises(ProviderError, match="selected Codex thread changed"):
        await transport(server).compact(expected_thread="foreign", timeout=1)
    assert not server.requests


async def test_ordinary_cli_compaction_keeps_original_return_and_rpc_schedule():
    server = Server(fault="missing-completion")
    app = SimpleNamespace(conversation=[{"provider_metadata": {"app_server_thread_id": "thread"}}])
    assert await AppServerTransport(server, app).compact(timeout=1) is None
    assert [name for name, _ in server.requests] == ["thread/resume", "thread/compact/start"]
