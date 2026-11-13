"""native_tasks_command: Python's own /tasks, run as typed, answering in the chat log."""
from __future__ import annotations

import pytest

from litetui import native_tasks_command as op


def cmd(arguments, command="/tasks", **extra):
    return {"type": op.OPERATION, "id": "x", "version": 1, "command": command,
            "arguments": arguments, **extra}


@pytest.mark.asyncio
async def test_each_verb_writes_the_handlers_own_line_and_answers_nothing_else(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)):
        said = []
        app._system = lambda text, *a, **k: said.append(text)
        for arguments, line in (("", "no background tasks"), ("list", "no background tasks"),
                                ("kill nope", "no task nope"),
                                ("bogus", "usage: /tasks [list | kill <id> | tail <id>]")):
            said.clear()
            assert op.dispatch(app, cmd(arguments)) == {"version": 1, "ran": True}
            assert said == [line], arguments


@pytest.mark.asyncio
async def test_glassbox_runs_pythons_handler_and_its_usage_line(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)):
        said = []
        app.system_message = lambda text, *a, **k: said.append(text)
        app._system = app.system_message
        assert op.dispatch(app, cmd("bogus", command="/glassbox")) == {"version": 1, "ran": True}
        assert said == ["[glassbox] usage: /glassbox [start|stop|status]"]


@pytest.mark.asyncio
async def test_engine_runs_pythons_handler_which_names_the_backend_to_pick_first(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)):
        said = []
        app.system_message = lambda text, *a, **k: said.append(text)
        assert op.dispatch(app, cmd("status", command="/engine")) == {"version": 1, "ran": True}
        assert len(said) == 1 and said[0].startswith("/engine drives the")


@pytest.mark.parametrize("bad", [
    {"version": 2}, {"arguments": 3}, {"arguments": "a\0b"}, {"arguments": "x" * 4097}, {"extra": 1},
    {"command": "/settings"},
])
def test_a_malformed_request_is_refused_before_the_handler_runs(bad):
    class App:
        def _system(self, text):
            raise AssertionError("handler ran")

    with pytest.raises(ValueError):
        op.dispatch(App(), {**cmd("list"), **bad})
