from types import SimpleNamespace

import pytest

from litetui import tasks
from litetui.codex_tasks import update
from litetui.task_screens import BackgroundProcessesBody, bg_text


def event(kind, item="item", turn="turn"):
    return {"type": kind, "threadId": "thread", "turnId": turn, "id": item, "name": "command"}


def test_provider_rows_have_no_process_ownership_and_settle_independently():
    app = SimpleNamespace(bg_tasks={}, backend=SimpleNamespace(name="codex"), convo_id="convo")
    update(app, event("tool_call", "a"))
    update(app, event("tool_call", "b"))
    update(app, event("tool_call", "a"))
    _, rows = tasks.live_for_app(app)
    assert len(rows) == 2
    assert all(not hasattr(row, "proc") and not hasattr(row, "owner_pid") for row in rows)
    assert app.bg_tasks == {}
    assert "Codex-owned operation" in bg_text(app)
    assert "Stop Codex turn" in bg_text(app)
    update(app, event("tool_result", "a"))
    assert [row.item_id for row in tasks.live_for_app(app)[1]] == ["b"]
    app.convo_id = "other"
    assert not tasks.live_for_app(app)[1]


def test_tasks_command_lists_native_activity_without_routing_it_to_host_kill():
    from litetui.plugins.tasks_plugin import _cmd_tasks

    notes, killed = [], []
    app = SimpleNamespace(bg_tasks={}, backend=SimpleNamespace(name="codex"), convo_id="convo",
                          _system=notes.append, _kill_background=killed.append)
    update(app, event("tool_call"))
    _cmd_tasks(app, "tasks", "list")
    assert "Codex-owned operation" in notes[-1]
    row = tasks.live_for_app(app)[1][0]
    _cmd_tasks(app, "tasks", "kill " + row.id)
    assert not killed and "whole turn" in notes[-1]
    app.convo_id = "convo"
    app.backend.name = "lmstudio"
    assert not tasks.live_for_app(app)[1]


@pytest.mark.asyncio
async def test_native_mapper_drives_shared_panel_and_explicit_whole_turn_stop():
    from test_codex_tool_ui import Host

    from litetui.codex_tool_ui import CodexToolUI

    app = Host()
    app.backend = SimpleNamespace(name="codex")
    app.convo_id = "convo"
    stopped = []
    app.action_stop_turn = lambda: stopped.append(True)
    async with app.run_test(size=(100, 45)) as pilot:
        ui = CodexToolUI(app, thread_id="thread", turn_id="turn")
        await ui.item({"type": "commandExecution", "id": "a", "command": "echo"})
        await ui.item({"type": "commandExecution", "id": "b", "command": "echo"})
        body = BackgroundProcessesBody()
        await app.query_one("#chat-log").mount(body)
        await pilot.pause()
        assert len(body.rows()) == 2
        await pilot.click(".lt-stop-codex")
        assert stopped == [True]
        ui.finish()
        assert tasks.live_for_app(app) == ([], [])
        assert not app.running
