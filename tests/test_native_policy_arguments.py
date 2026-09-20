from types import SimpleNamespace
import pytest
from litetui.codex_native_policy import NativePolicy


@pytest.mark.asyncio
async def test_native_exec_cmd_reaches_host_as_command(monkeypatch):
    captured = []
    async def authorize(name, args, policy, **kwargs):
        captured.append((name, args))
        return None
    app = SimpleNamespace(tools_enabled=True, settings=SimpleNamespace(tools_disabled=[]),
        plugins=SimpleNamespace(policy_for=lambda name: None), _authorize_action=authorize)
    import litetui.codex_native_policy as mod
    monkeypatch.setattr(mod, 'workspace', lambda app: '.')
    await NativePolicy(app).handle({'hook_event_name': 'PreToolUse', 'tool_name': 'exec_command',
                                   'tool_input': {'cmd': 'echo native'}})
    assert captured[0][1]['command'] == 'echo native'
