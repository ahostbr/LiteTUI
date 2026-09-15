"""T707 — tool calls are one reusable fold: live open, completed collapsed."""

import ast
import inspect
import textwrap

import pytest

from litetui import app as app_mod
from litetui.widgets import AssistantMessage, ToolMessage


def _plain(value) -> str:
    return getattr(value, "plain", str(value))


def _app():
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    return app


@pytest.mark.asyncio
async def test_running_tool_card_starts_expanded_with_header_and_full_body() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_args('{"command": "git status --short"}')
        await pilot.pause()

        assert card.expanded is True
        assert "bash" in _plain(card.header.content)
        assert "git status --short" in _plain(card.header.content)
        assert "…" in _plain(card.header.content)
        assert '"command": "git status --short"' in _plain(card.body.content)


@pytest.mark.asyncio
async def test_completed_tool_collapses_and_header_keeps_elapsed_and_result_size(
    monkeypatch,
) -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("read")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_args('{"path": "README.md"}')
        # `widgets.time` is Python's shared time module, so a test-wide patch
        # would also freeze asyncio/Textual's clock and deadlock the pilot.
        # Freeze only the synchronous product seam that captures `_took`.
        with monkeypatch.context() as clock:
            clock.setattr(
                "litetui.widgets.time.monotonic", lambda: card._t0 + 2.0
            )
            card.set_result("first line\nsecond line", True)
        await pilot.pause()

        header = _plain(card.header.content)
        assert card.expanded is False
        assert "read" in header and "README.md" in header
        assert "2.0s" in header
        assert "22 chars" in header
        assert card.scroll.styles.display == "none"


@pytest.mark.asyncio
async def test_expanding_completed_tool_reveals_full_arguments_and_result() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        args = '{"command": "printf one && printf two"}'
        result = "line\n" * 30
        card.set_args(args)
        card.set_result(result, True)
        card.set_expanded(True)
        await pilot.pause()

        body = _plain(card.body.content)
        assert card.expanded is True
        assert card.scroll.styles.display == "block"
        assert args in body
        assert result in body
        assert "more lines" not in body


@pytest.mark.asyncio
async def test_running_tool_cannot_be_collapsed() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()

        card.header.on_click()
        assert card.expanded is True


@pytest.mark.asyncio
async def test_clickable_header_uses_same_expanded_state_transition() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_result("ok", True)
        await pilot.pause()

        card.header.on_click()
        assert card.expanded is True
        card.header.on_click()
        assert card.expanded is False


@pytest.mark.asyncio
async def test_running_tick_updates_elapsed_without_collapsing(monkeypatch) -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        # Freeze only the synchronous tick: wall-clock scheduling may cross
        # a formatting boundary, and freezing across await would stall Textual.
        with monkeypatch.context() as clock:
            clock.setattr("litetui.widgets.time.monotonic", lambda: card._t0 + 3.3)
            card._tick()
            assert card.expanded is True
            assert "3.3s" in _plain(card.header.content)
            assert "…" in _plain(card.header.content)


@pytest.mark.asyncio
async def test_argument_summary_is_single_line_and_short() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("write")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_args('{\n  "path": "' + "x" * 180 + '"\n}')
        await pilot.pause()

        header = _plain(card.header.content)
        assert "\n" not in header
        assert "..." in header
        assert len(header) < 180


def test_assistant_stop_line_remains_the_bubbles_last_child() -> None:
    bubble = AssistantMessage()

    assert list(bubble.compose())[-1] is bubble.stop_line


def test_tool_widget_reuses_foldblock_instead_of_a_second_toggle() -> None:
    assert issubclass(ToolMessage, app_mod.FoldBlock)


def test_ui_fold_does_not_change_transcript_or_rpc_emission() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ToolMessage)))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "_append" not in called
    assert "_rpc_emit" not in called


@pytest.mark.asyncio
async def test_compaction_card_uses_the_same_collapsed_tool_widget() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = app_mod.CompactionCard("plan", "prompt")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        tool = ToolMessage("write")
        card.add_tool(tool)
        await pilot.pause()
        tool.set_result("ok", True)
        await pilot.pause()

        assert tool.expanded is False
        assert tool in list(card.query(ToolMessage))


@pytest.mark.asyncio
async def test_error_result_keeps_failure_style_when_collapsed() -> None:
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        card = ToolMessage("bash")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_result("permission denied", False)
        await pilot.pause()

        assert card.expanded is False
        assert "error" in _plain(card.header.content).lower()
        assert "permission denied" in _plain(card.body.content)
