"""T705 — the terminal turn stop line is UI state, never transcript/RPC data."""

import ast
import inspect
import textwrap
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from litetui import app as app_mod
from litetui import convo_settings
from litetui.settings import Settings
from litetui.turnstats import format_turn_stop_line
from litetui.widgets import AssistantMessage


def _visible_stop_lines(app) -> list[str]:
    return [
        str(message.stop_line.content)
        for message in app.query(AssistantMessage)
        if message.stop_line.styles.display != "none"
    ]


def test_completed_turn_line_uses_whole_turn_elapsed_and_final_tps() -> None:
    """Elapsed starts at request/turn start; tok/s is the final generation rate."""
    line = format_turn_stop_line(
        started_at=100.0,
        final_tps=14.25,
        stopped=False,
        show_line=True,
        show_time=False,
        now_monotonic=136.2,
    )

    assert line == "Cooked for 36.2s · 14.2 tok/s"


def test_interrupted_turn_uses_stopped_verb() -> None:
    line = format_turn_stop_line(
        started_at=20.0,
        final_tps=8.0,
        stopped=True,
        show_line=True,
        show_time=False,
        now_monotonic=24.5,
    )

    assert line == "stopped after 4.5s · 8.0 tok/s"


def test_enabled_local_time_is_exact_12_hour_time() -> None:
    line = format_turn_stop_line(
        started_at=0.0,
        final_tps=1.0,
        stopped=False,
        show_line=True,
        show_time=True,
        now_monotonic=2.0,
        now_local=datetime(2026, 9, 13, 15, 39, tzinfo=UTC),
    )

    assert line == "Cooked for 2.0s · 1.0 tok/s · done 3:39 PM"


def test_visibility_off_returns_no_presentation_line() -> None:
    assert format_turn_stop_line(
        started_at=10.0,
        final_tps=3.0,
        stopped=False,
        show_line=False,
        show_time=True,
        now_monotonic=12.0,
        now_local=datetime(2026, 9, 13, 15, 39, tzinfo=UTC),
    ) is None


def test_assistant_owns_hidden_sibling_line_that_can_be_settled() -> None:
    """A sibling keeps display-only text out of answer markdown and transcripts."""
    message = AssistantMessage()

    assert message.stop_line.styles.display == "none"
    message.set_stop_line("Cooked for 2.0s · 1.0 tok/s")

    assert str(message.stop_line.content) == "Cooked for 2.0s · 1.0 tok/s"
    assert message.stop_line.styles.display == "block"
    assert list(message.compose())[-1] is message.stop_line


def test_stop_preferences_are_global_defaults_not_conversation_settings() -> None:
    settings = Settings()
    convo_fields = {field.name for field in fields(convo_settings.ConvoSettings)}

    assert settings.show_stop_line is True
    assert settings.show_stop_time is False
    assert {"show_stop_line", "show_stop_time"}.isdisjoint(convo_fields)
    assert {"show_stop_line", "show_stop_time"}.isdisjoint(convo_settings.BORN_FROM.values())


def test_rpc_mode_does_not_settle_tui_presentation() -> None:
    app = app_mod.LiteTUI(rpc=True)
    message = AssistantMessage()

    app._settle_turn_stop_line(
        message,
        started_at=0.0,
        final_tps=1.0,
        stopped=False,
    )

    assert message.stop_line.styles.display == "none"


def test_settlement_is_idempotent() -> None:
    app = app_mod.LiteTUI()
    app._turn_stop_line_settled = False
    app._scroll_down = lambda: None
    message = AssistantMessage()

    app._settle_turn_stop_line(
        message,
        started_at=0.0,
        final_tps=1.0,
        stopped=False,
    )
    first = str(message.stop_line.content)
    app._settle_turn_stop_line(
        message,
        started_at=0.0,
        final_tps=1.0,
        stopped=True,
    )

    assert str(message.stop_line.content) == first
    assert first.startswith("Cooked for ")


def test_force_stop_settles_before_cancelling_the_worker() -> None:
    source = textwrap.dedent(inspect.getsource(app_mod.LiteTUI._force_stop))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    settled = next(node.lineno for node in calls if node.func.attr == "_settle_turn_stop_line")
    cancelled = next(node.lineno for node in calls if node.func.attr == "cancel_group")
    assert settled < cancelled


def test_stream_settles_only_after_completion_gate_and_finalizers() -> None:
    """A retried/rejected draft is not a completed turn and gets no line."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.LiteTUI._stream)))
    plain_answer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "tool_acc"
    )
    calls = [
        node
        for node in ast.walk(plain_answer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    def line_of(name: str) -> int:
        return min(node.lineno for node in calls if node.func.attr == name)

    completion_line = next(
        node.lineno
        for node in calls
        if node.func.attr == "completion"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hook_host"
    )
    assert completion_line < line_of("finalize_turn") < line_of("_settle_turn_stop_line")


def test_eta_learning_keeps_a_per_request_clock() -> None:
    """Whole-turn display timing must not corrupt prompt-evaluation learning."""
    source = textwrap.dedent(inspect.getsource(app_mod.LiteTUI._stream))
    tree = ast.parse(source)
    learn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "learn"
    )

    assert "request_started_at = time.monotonic()" in source
    assert "self._elapsed.start(widget.body, started_at=turn_started_at)" in source
    assert isinstance(learn.args[1], ast.Name)
    assert learn.args[1].id == "request_started_at"


@pytest.mark.asyncio
async def test_completed_stream_shows_exactly_one_terminal_line() -> None:
    from test_chat_ready_call_sites import _app, _Backend, _ok_create, _seed, _settle

    app = _app(_Backend(ready=True))
    _seed(app)
    async with app.run_test(size=(100, 35)) as pilot:
        app.client.chat.completions.create = _ok_create("a normal answer")
        app._append({"role": "user", "content": "hello"})
        app._stream()
        await _settle(app, pilot)

        lines = _visible_stop_lines(app)
        assert len(lines) == 1
        assert lines[0].startswith("Cooked for ")


@pytest.mark.asyncio
async def test_interrupted_stream_shows_exactly_one_stopped_line() -> None:
    from test_wake_guard_abandoned_turn import _app, _seed, _settle, _stop_stream

    app = _app()
    _seed(app)
    async with app.run_test(size=(100, 35)) as pilot:
        app.client.chat.completions.create = lambda **kw: _stop_stream(app, **kw)
        app._append({"role": "user", "content": "start the long job"})
        app._stream()
        await _settle(app, pilot)

        lines = _visible_stop_lines(app)
        assert len(lines) == 1
        assert lines[0].startswith("stopped after ")
