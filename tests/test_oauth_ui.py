import json

import httpx
import pytest

from litetui import app as app_mod
from litetui import model_transport as mt
from litetui import oauth_backend, settings, tool_policy
from litetui.plugins.model_switch import _switch_backend


@pytest.mark.asyncio
async def test_remote_compaction_uses_tool_door_and_preserves_transcript(
    tmp_path, monkeypatch
):
    cfg = settings.Settings(
        backend="codex",
        backend_chosen=True,
        mcp_enabled=False,
        skills_enabled=False,
        compact_keep_recent=2,
        wake_after_compact=False,
        clear_screen_after_compact=False,
    )
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(oauth_backend, "read_credentials", lambda p: None)
    monkeypatch.setattr(
        mt, "read_credentials", lambda *a: mt.Credentials("test", "account")
    )

    async def listing(self):
        from litetui.llm_backend import ModelRow

        self.models = {
            "gpt-test": {
                "context_window": 100000,
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
            }
        }
        return [ModelRow(key="gpt-test", path=None, source="OAuth", loaded=True)]

    monkeypatch.setattr(oauth_backend.OAuthBackend, "list_models", listing)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            events = [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "call_id": "call_write",
                        "name": "write",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": json.dumps(
                        {
                            "path": str(tmp_path / "handoff.md"),
                            "content": "durable fact",
                        }
                    ),
                },
                {"type": "response.completed", "response": {"usage": {}, "output": []}},
            ]
        else:
            events = [
                {
                    "type": "response.output_text.delta",
                    "delta": "Remember the durable fact.",
                },
                {"type": "response.completed", "response": {"usage": {}, "output": []}},
            ]
        return httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )

    monkeypatch.setattr(
        mt,
        "for_app",
        lambda app: mt.OAuthTransport(
            "codex", http_transport=httpx.MockTransport(handler)
        ),
    )
    app = app_mod.LiteTUI()
    async with app.run_test(size=(120, 38)) as pilot:
        await pilot.pause()
        app.conversation = [
            {"role": "system", "content": "LiteTUI identity"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]
        app._active_tool_profile = tool_policy.AUTONOMOUS
        writes = []

        def write(args):
            assert args["path"] == str(tmp_path / "handoff.md")
            (tmp_path / "handoff.md").write_text(args["content"])
            writes.append(args)
            return "written"

        app._dispatch_for = lambda name: write if name == "write" else None
        # Policy remains the real host policy; allow this fixture's temp path.
        monkeypatch.setattr(
            app.plugins, "policy_for", lambda name: tool_policy.READ_POLICY
        )
        app._compact()
        for _ in range(20):
            await pilot.pause(0.1)
            if not app._chat_running():
                break
        assert len(writes) == 1
        assert (tmp_path / "handoff.md").read_text() == "durable fact"
        assert len(requests) == 2
        assert any(
            i.get("type") == "function_call_output" and i["output"] == "written"
            for i in requests[1]["input"]
        )
        assert app.conversation[0]["content"] == "LiteTUI identity"
        assert "durable fact" in app.conversation[1]["content"]
        assert app.conversation[-1]["content"] == "recent answer"
        app.tools_enabled = False
        _, allowed = await app._execute_tool(
            "write", {"path": str(tmp_path / "handoff.md"), "content": "must not run"}
        )
        assert not allowed and len(writes) == 1
        app.tools_enabled = True
        app.settings.tools_disabled = ["write"]
        _, allowed = await app._execute_tool(
            "write", {"path": str(tmp_path / "handoff.md"), "content": "must not run"}
        )
        assert not allowed and len(writes) == 1
        before = list(app.conversation)
        app.connect = lambda: None
        _switch_backend(app, "lmstudio")
        assert app.conversation == before
        _switch_backend(app, "codex")
        assert app.conversation == before


def test_switch_refused_midturn_without_changing_history():
    from types import SimpleNamespace

    from litetui.plugins.model_switch import _switch_backend

    notes = []
    app = SimpleNamespace(
        _chat_running=lambda: True,
        system_message=notes.append,
        backend=SimpleNamespace(name="codex"),
        conversation=[{"role": "user", "content": "keep"}],
    )
    _switch_backend(app, "lmstudio")
    assert app.backend.name == "codex"
    assert app.conversation == [{"role": "user", "content": "keep"}]
    assert notes
