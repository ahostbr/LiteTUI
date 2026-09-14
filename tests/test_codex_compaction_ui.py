import pytest
from test_codex_app_server import Server
from test_codex_tool_ui import Host

from litetui.codex_app_server import AppServerTransport
from litetui.model_transport import ProviderError
from litetui.widgets import CompactionCard


@pytest.mark.asyncio
async def test_compaction_refuses_mismatched_resume_before_mutating_thread_or_transcript():
    from copy import deepcopy

    class WrongThreadServer(Server):
        async def request(self, method, params):
            self.requests.append((method, params))
            assert method == "thread/resume"
            return {"thread": {"id": "different-thread"}}

    app = Host()
    app.conversation = [{"role": "assistant", "content": "preserved", "provider_metadata": {
        "provider": "codex", "app_server_thread_id": "thread-1",
    }}]
    before = deepcopy(app.conversation)
    server = WrongThreadServer()
    transport = AppServerTransport(server, app)
    with pytest.raises(ProviderError, match="different thread"):
        await transport.compact()
    assert server.requests == [("thread/resume", {"threadId": "thread-1"})]
    assert transport.thread_id is None and transport.process is None
    assert app.conversation == before and not app.events


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_manual_compaction_uses_shared_card_rpc_status_and_saved_trace(stopped):
    class CompactServer(Server):
        async def request(self, method, params):
            if method == "thread/compact/start":
                await self.events.put(
                    {
                        "method": "turn/started",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "compact-turn"},
                        },
                    }
                )
                if not stopped:
                    await self.done("completed")
                return {}
            if method == "turn/interrupt":
                self.requests.append((method, params))
                await self.done("interrupted")
                return {}
            return await super().request(method, params)

        async def done(self, status):
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "compact-turn", "status": status},
                    },
                }
            )

    app = Host()
    app.conversation = [
        {
            "role": "assistant",
            "content": "preserved",
            "provider_metadata": {
                "provider": "codex",
                "app_server_thread_id": "thread-1",
            },
        }
    ]
    app._edit = lambda *args: None
    app._stop_requested = stopped
    server = CompactServer()
    async with app.run_test():
        transport = AppServerTransport(server, app)
        if stopped:
            with pytest.raises(ProviderError, match="did not complete"):
                await transport.compact()
        else:
            await transport.compact()
        assert len(app.query(CompactionCard)) == 1
        assert app._compact_card is None
        starts = [e for e in app.events if e["type"] == "compaction_start"]
        ends = [e for e in app.events if e["type"] == "compaction_end"]
        assert len(starts) == len(ends) == 1
        assert not starts[0]["automatic"]
        assert starts[0]["id"] == ends[0]["id"]
        assert ends[0]["outcome"] == ("cancelled" if stopped else "success")
        assert not ends[0]["will_resume"]
        assert app.conversation[0]["content"] == "preserved"
        trace = app.conversation[0]["provider_metadata"]["display_trace"]["items"]
        assert len(trace) == 1 and trace[0]["kind"] == "contextCompaction"
        assert trace[0]["state"] == ("interrupted" if stopped else "completed")
