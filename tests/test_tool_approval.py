from pathlib import Path

import pytest
from textual.app import App

from litetui.app import LiteTUI
from litetui.tool_approval import ToolApprovalScreen
from litetui.tool_policy import INTERACTIVE, SHELL_POLICY, approval_preview, evaluate


def _decision(tmp_path):
    return evaluate(
        INTERACTIVE,
        SHELL_POLICY,
        {"command": "git status"},
        Path(tmp_path),
    )


def test_preview_redacts_secret_fields_and_bounds_large_values():
    out = approval_preview(
        {
            "api_key": "do-not-show",
            "password": "also-hidden",
            "command": "x" * 900,
        }
    )
    assert "do-not-show" not in out
    assert "also-hidden" not in out
    assert out.count("[redacted]") == 2
    assert "more chars" in out


@pytest.mark.asyncio
async def test_allow_button_returns_true_once(tmp_path):
    app = App()
    result = []
    async with app.run_test(size=(100, 35)) as pilot:
        app.push_screen(
            ToolApprovalScreen("powershell", {"command": "git status"}, _decision(tmp_path)),
            result.append,
        )
        await pilot.pause()
        assert isinstance(app.screen, ToolApprovalScreen)
        await pilot.click("#tool-approval-allow")
        await pilot.pause()
    assert result == [True]


@pytest.mark.asyncio
async def test_escape_denies(tmp_path):
    app = App()
    result = []
    async with app.run_test(size=(100, 35)) as pilot:
        app.push_screen(
            ToolApprovalScreen("bash", {"command": "echo hi"}, _decision(tmp_path)),
            result.append,
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert result == [False]


@pytest.mark.asyncio
async def test_real_worker_waits_for_the_modal_before_execution():
    tui = LiteTUI()
    tui._connect = lambda: None
    calls = []
    tui.plugins.add_tool(
        "test",
        {
            "type": "function",
            "function": {
                "name": "approval_probe",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda args: calls.append(args) or "ran",
        policy=SHELL_POLICY,
    )
    async with tui.run_test(size=(100, 35)) as pilot:
        worker = tui.run_worker(
            tui._execute_tool("approval_probe", {}),
            group="approval-probe",
        )
        for _ in range(10):
            await pilot.pause()
            if isinstance(tui.screen, ToolApprovalScreen):
                break
        assert isinstance(tui.screen, ToolApprovalScreen)
        assert calls == [], "the sensitive tool ran before the host decided"
        # Button routing is covered above; dismiss directly here so this arm
        # isolates push_screen_wait's worker suspension/resumption contract.
        tui.screen.dismiss(True)
        for _ in range(10):
            await pilot.pause()
            if worker.is_finished:
                break
        assert worker.is_finished
        assert worker.result == ("ran", True)
    assert calls == [{}]
