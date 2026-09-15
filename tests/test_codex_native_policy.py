from types import SimpleNamespace as NS

import pytest

from litetui.codex_native_policy import NativePolicy


def host(**overrides):
    calls = []

    async def authorize(name, args, policy, **kwargs):
        calls.append((name, args, policy))

    values = {
        "tools_enabled": True,
        "settings": NS(tools_disabled=[]),
        "_authorize_action": authorize,
        "_stop_requested": False,
        "plugins": NS(policy_for=lambda name: None),
    }
    values.update(overrides)
    return NS(**values), calls


@pytest.mark.asyncio
async def test_native_policy_uses_captured_host_workspace(tmp_path, monkeypatch):
    project = tmp_path / "selected project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    checked = []

    async def authorize(name, args, policy, **kwargs):
        checked.append(kwargs["workspace"])

    app, _ = host(_hook_workspace=project, _authorize_action=authorize)
    assert await NativePolicy(app).handle({"hook_event_name": "PreToolUse",
                                          "tool_name": "apply_patch"}) == {}
    assert checked == [project.resolve()]


@pytest.mark.asyncio
async def test_native_shell_uses_shared_policy_and_disabled_alias():
    app, calls = host()
    policy = NativePolicy(app)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_use_id": "one",
        "tool_input": {"command": "echo hello"},
    }
    assert await policy.handle(event) == {}
    assert calls[0][0] == "bash"
    app.settings.tools_disabled = ["bash"]
    denied = await policy.handle(dict(event, tool_use_id="two"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tools_off_blocks_native_and_children_before_execution():
    app, calls = host(tools_enabled=False)
    for name in ("Bash", "apply_patch", "spawn_agent", "mcp__server__tool"):
        response = await NativePolicy(app).handle(
            {"hook_event_name": "PreToolUse", "tool_name": name}
        )
        assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []


@pytest.mark.asyncio
async def test_dynamic_host_tools_keep_their_existing_authorization_door():
    app, calls = host()
    response = await NativePolicy(app).handle(
        {"hook_event_name": "PreToolUse", "tool_name": "litetui_read"}
    )
    assert response == {} and calls == []


@pytest.mark.asyncio
async def test_hook_denial_is_recoverable_and_post_receives_captured_context(
    monkeypatch,
):
    calls = []

    async def dispatch(app, event, data, **kwargs):
        calls.append((event, data, kwargs))
        return NS(allowed=event != "tool_before", reason="Synthetic refusal")

    monkeypatch.setattr("litetui.codex_native_policy.hook_host.dispatch", dispatch)
    app, _ = host(hook_config=object())
    denied = await NativePolicy(app).handle(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "x"}
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not app._stop_requested
    assert calls[0][0] == "tool_before"


@pytest.mark.asyncio
async def test_after_hook_uses_native_exit_status_and_original_context_once(
    monkeypatch,
):
    calls = []

    async def dispatch(app, event, data, **kwargs):
        calls.append((event, data, kwargs))
        return NS(allowed=True, reason="")

    monkeypatch.setattr("litetui.codex_native_policy.hook_host.dispatch", dispatch)
    app, _ = host(hook_config=object(), convo_id="original")
    policy = NativePolicy(app)
    await policy.handle(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "x",
            "session_id": "s",
            "turn_id": "t",
            "tool_input": {"command": "exit 1"},
        }
    )
    app.convo_id = "changed"
    await policy.handle(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "x",
            "session_id": "s",
            "turn_id": "t",
            "tool_response": "output without status",
        }
    )
    assert len(calls) == 1
    await policy.completed(
        {
            "threadId": "s",
            "turnId": "t",
            "item": {
                "id": "x",
                "status": "completed",
                "exitCode": 1,
                "aggregatedOutput": "failed",
            },
        }
    )
    await policy.finish()
    assert len(calls) == 2
    assert calls[-1][1]["ok"] is False
    assert calls[-1][2]["captured"]["conversation_id"] == "original"
