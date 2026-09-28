from types import SimpleNamespace

import pytest

from litetui import tasks
from litetui.app import LiteTUI
from litetui.codex_app_server import AppServerTransport


def test_foreground_tool_result_cap_at_conversation_entry(tmp_path, monkeypatch):
    from litetui import paths

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    recorded = []
    app = SimpleNamespace(conversation=[], store=SimpleNamespace(record_msg=recorded.append))
    raw = "HEAD" + "x" * 2_400_000 + "TAIL"
    LiteTUI._append(app, {"role": "tool", "content": raw, "name": "grep"})
    visible = app.conversation[0]["content"]
    assert visible == recorded[0]["content"]
    assert len(visible) < tasks.TOOL_RESULT_CAP
    assert "HEAD" in visible and "TAIL" in visible and "chars cut" in visible
    log = next((tmp_path / "output" / "tasks").glob("tool-*.log"))
    assert log.as_posix() in visible
    assert log.read_text(encoding="utf-8") == raw

    LiteTUI._append(app, {"role": "tool", "content": "short", "name": "read"})
    assert app.conversation[1]["content"] == "short"
    assert len(list((tmp_path / "output" / "tasks").glob("tool-*.log"))) == 1


@pytest.mark.asyncio
async def test_codex_native_tool_result_cap(tmp_path, monkeypatch):
    from litetui import paths
    from litetui.codex_inventory import build_inventory

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    class Server:
        def __init__(self):
            self.replies = []

        async def send(self, message):
            self.replies.append(message)

    raw = "A" * 2_400_000

    async def execute(*args):
        return raw, True

    spec = {"type": "function", "function": {
        "name": "grep", "description": "test", "parameters": {"type": "object"},
    }}
    app = SimpleNamespace(
        _execute_tool=execute, _rpc=True, _stop_requested=False,
        _pending_tool_images=[], tools_enabled=True,
        plugins=SimpleNamespace(tool_specs=lambda: [spec], deferred_specs=lambda: []),
    )
    server = Server()
    transport = AppServerTransport(server, app)
    transport.registered_inventory = build_inventory([spec], app)[1]
    await transport._server_request({"id": 1, "method": "item/tool/call",
                                     "params": {"tool": "litetui_grep", "arguments": {}}})
    assert server.replies
    text = str(server.replies)
    log = next((tmp_path / "output" / "tasks").glob("tool-*.log"))
    assert len(text) < tasks.TOOL_RESULT_CAP
    assert log.as_posix() in text and log.read_text(encoding="utf-8") == raw
