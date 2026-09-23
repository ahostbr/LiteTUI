"""The shared task store must not expose another conversation's live work."""
from types import SimpleNamespace

from litetui import rpc, tasks
from litetui.app import LiteTUI
from litetui.plugins.tasks_plugin import _cmd_tasks
from litetui.task_screens import bg_text


def test_live_panel_only_shows_current_conversation():
    own = tasks.new_task("bash", {"command": "own job"}, "our-convo")
    other = tasks.new_task("bash", {"command": "foreign job"}, "other-convo")
    app = SimpleNamespace(bg_tasks={own.id: own, other.id: other},
                          convo_id="our-convo", codex_native_activity={}, backend=None)

    _, visible = tasks.live_for_app(app)
    assert [row.id for row in visible] == [own.id]
    assert own.id in bg_text(app)
    assert other.id not in bg_text(app)
    assert [row.id for row in tasks.host_tasks_for_app(app)] == [own.id]


def test_other_conversation_cannot_list_tail_or_kill_task(monkeypatch):
    own = tasks.new_task("bash", {"command": "own job"}, "our-convo")
    other = tasks.new_task("bash", {"command": "foreign job"}, "other-convo")
    app = LiteTUI()
    app.convo_id = "our-convo"
    app.bg_tasks = {own.id: own, other.id: other}
    said = []
    app._system = said.append
    app.notify = lambda *args, **kwargs: None
    responses = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kwargs: responses.append(kwargs))

    _cmd_tasks(app, "tasks", "list")
    assert own.id in said[-1] and other.id not in said[-1]
    _cmd_tasks(app, "tasks", "tail " + other.id)
    assert "no such task" in said[-1]
    _cmd_tasks(app, "tasks", "kill " + other.id)
    assert other.state == tasks.RUNNING
    assert "no task" in said[-1]

    rpc._handle_tasks(app, "tasks.list", {}, 1)
    assert [row["id"] for row in responses[-1]["result"]] == [own.id]
    rpc._handle_tasks(app, "tasks.tail", {"task_id": other.id}, 2)
    assert responses[-1]["ok"] is False
    rpc._handle_tasks(app, "tasks.kill", {"task_id": other.id}, 3)
    assert responses[-1]["ok"] is False
    assert other.state == tasks.RUNNING
