from pathlib import Path

import pytest
from textual.app import App

from litetui.app import LiteTUI
from litetui.tool_approval import (
    ALWAYS,
    DENIED,
    ONCE,
    ToolApproval,
    ToolApprovalScreen,
)
from litetui.textfmt import tool_denied
from litetui.tool_policy import INTERACTIVE, SHELL_POLICY, approval_preview, evaluate


def _decision(tmp_path):
    return evaluate(
        INTERACTIVE,
        SHELL_POLICY,
        {"command": "git status"},
        Path(tmp_path),
    )


def _probe_app(monkeypatch):
    """A LiteTUI with one shell-authority tool and NO standing rules.

    `settings_mod.save` is stubbed in every wiring test: the real one writes the
    developer's own settings.json, so an unpatched run of this suite would
    rewrite live configuration as a side effect of a test.
    """
    tui = LiteTUI()
    tui._connect = lambda: None
    tui.settings.tool_always_allow = []
    tui.settings.tool_deny = []
    saved = []
    monkeypatch.setattr("litetui.app.settings_mod.save", lambda s, root=None: saved.append(s))
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
    return tui, calls, saved


async def _run_tool(tui, pilot, args, answer):
    """Drive one _execute_tool call.  Returns (was_prompted, worker_result)."""
    worker = tui.run_worker(tui._execute_tool("approval_probe", args), group="probe")
    for _ in range(20):
        await pilot.pause()
        if isinstance(tui.screen, ToolApprovalScreen) or worker.is_finished:
            break
    prompted = isinstance(tui.screen, ToolApprovalScreen)
    if prompted:
        tui.screen.dismiss(answer)
    for _ in range(20):
        await pilot.pause()
        if worker.is_finished:
            break
    assert worker.is_finished, "the tool worker never resumed"
    return prompted, worker.result


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


def test_denial_is_falsy_and_both_approvals_are_truthy():
    """The contract that lets `if not answer:` stay correct after the tri-state.

    This is the ONE test standing between the codebase and the string-valued
    refactor: with "deny"/"once"/"always" as plain strings every assertion
    about the allow path still passes while the Deny button starts running the
    tool.  A dead `ToolApproval.__bool__` fails here and nowhere else.
    """
    assert not DENIED
    assert ONCE
    assert ALWAYS
    assert not ToolApproval(approved=False, remember=True)
    # remember is orthogonal to approved -- it must not leak into truthiness.
    assert ONCE.remember is False
    assert ALWAYS.remember is True


@pytest.mark.asyncio
async def test_allow_button_returns_once(tmp_path):
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
    assert result == [ONCE]


@pytest.mark.asyncio
async def test_always_button_returns_always(tmp_path):
    app = App()
    result = []
    async with app.run_test(size=(100, 35)) as pilot:
        app.push_screen(
            ToolApprovalScreen("powershell", {"command": "git status"}, _decision(tmp_path)),
            result.append,
        )
        await pilot.pause()
        await pilot.click("#tool-approval-always")
        await pilot.pause()
    assert result == [ALWAYS]


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
    assert result == [DENIED]


@pytest.mark.asyncio
async def test_deny_button_denies(tmp_path):
    app = App()
    result = []
    async with app.run_test(size=(100, 35)) as pilot:
        app.push_screen(
            ToolApprovalScreen("bash", {"command": "echo hi"}, _decision(tmp_path)),
            result.append,
        )
        await pilot.pause()
        await pilot.click("#tool-approval-deny")
        await pilot.pause()
    assert result == [DENIED]


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
        tui.screen.dismiss(ONCE)
        for _ in range(10):
            await pilot.pause()
            if worker.is_finished:
                break
        assert worker.is_finished
        assert worker.result == ("ran", True)
    assert calls == [{}]


@pytest.mark.asyncio
async def test_allow_once_does_not_stop_the_next_prompt(monkeypatch):
    """"Once" means once.  The second identical call must ask again."""
    tui, calls, saved = _probe_app(monkeypatch)
    async with tui.run_test(size=(100, 35)) as pilot:
        first, result = await _run_tool(tui, pilot, {"command": "git status"}, ONCE)
        second, _ = await _run_tool(tui, pilot, {"command": "git status"}, DENIED)
    assert first is True
    assert second is True, "'allow once' silenced a later prompt"
    assert result == ("ran", True)
    assert tui.settings.tool_always_allow == [], "'once' wrote a standing rule"
    assert saved == [], "'once' persisted settings"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_always_allow_persists_and_silences_the_next_identical_call(monkeypatch):
    tui, calls, saved = _probe_app(monkeypatch)
    async with tui.run_test(size=(100, 35)) as pilot:
        first, _ = await _run_tool(tui, pilot, {"command": "git status"}, ALWAYS)
        second, result = await _run_tool(tui, pilot, {"command": "git log"}, DENIED)
    assert first is True
    assert second is False, "the standing rule did not silence the second prompt"
    assert result == ("ran", True)
    # Hardcoded, NOT rule_key(...): asserting against the function under test
    # would pass through any change to the key format, including a change to
    # the name-only key that drops the escalation guard entirely.
    assert tui.settings.tool_always_allow == ["approval_probe:process_execution"]
    assert saved and saved[0] is tui.settings, "the rule was never persisted"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_standing_rule_does_not_cover_wider_authority(monkeypatch):
    """The escalation guard, end to end through the real call site.

    "Always allow" on `git status` grants process_execution.  A destructive
    command classifies as destructive_irreversible TOO, which is a different
    key, so it must prompt again -- otherwise one approval of "run a command"
    silently authorises `rm -rf` on some later call.
    """
    tui, calls, _ = _probe_app(monkeypatch)
    async with tui.run_test(size=(100, 35)) as pilot:
        await _run_tool(tui, pilot, {"command": "git status"}, ALWAYS)
        escalated, result = await _run_tool(
            tui, pilot, {"command": "rm -rf ./build"}, DENIED
        )
    assert escalated is True, "a wider authority reused the narrower rule"
    # Compared against the RENDERER, not a frozen literal: the refusal text now
    # lives in prompts/tool-denied.md and is meant to be edited. A hardcoded
    # copy here would fail on every legitimate wording change while still not
    # noticing the one thing that matters — that the tool was refused.
    assert result == (tool_denied("by-user", name="approval_probe"), False)
    assert "approval_probe" in result[0] and "{" not in result[0]
    assert len(calls) == 1, "the destructive call ran without its own approval"


@pytest.mark.asyncio
async def test_a_deny_rule_refuses_without_a_prompt(monkeypatch):
    tui, calls, _ = _probe_app(monkeypatch)
    tui.settings.tool_deny = ["approval_probe:process_execution"]
    # Both rules present: deny must win rather than the later one winning.
    tui.settings.tool_always_allow = ["approval_probe:process_execution"]
    async with tui.run_test(size=(100, 35)) as pilot:
        prompted, result = await _run_tool(tui, pilot, {"command": "git status"}, ONCE)
    assert prompted is False
    assert result[1] is False
    assert "standing rule" in result[0]
    assert calls == [], "a denied tool ran"


@pytest.mark.asyncio
async def test_a_failed_save_still_honours_the_approval(monkeypatch):
    """Honest degradation: the call the human approved must not fail on an
    unwritable settings file, and the rule still holds for this session."""
    tui, calls, _ = _probe_app(monkeypatch)
    def _boom(s, root=None):
        raise OSError("read-only settings.json")
    monkeypatch.setattr("litetui.app.settings_mod.save", _boom)
    async with tui.run_test(size=(100, 35)) as pilot:
        _, result = await _run_tool(tui, pilot, {"command": "git status"}, ALWAYS)
    assert result == ("ran", True)
    assert tui.settings.tool_always_allow == ["approval_probe:process_execution"]
    assert calls == [{"command": "git status"}]
