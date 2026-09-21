from types import SimpleNamespace
import pytest
from litetui.plugins.spawn_agent_plugin import SPEC, POLICY, _register, capture_launch
from litetui.agent_launcher import LaunchBlocked
from litetui.tool_policy import CAPABILITIES


def test_public_registration_and_conservative_policy():
    from litetui.plugins import PLUGIN_LOAD_ORDER
    assert 'litetui.plugins.spawn_agent_plugin' in PLUGIN_LOAD_ORDER
    calls = []
    _register(SimpleNamespace(app=object(), tool=lambda *a, **kw: calls.append((a, kw))))
    assert calls[0][0][0]['function']['name'] == 'spawn_agent'
    assert POLICY.capabilities == CAPABILITIES
    assert SPEC['function']['parameters']['additionalProperties'] is False


@pytest.mark.parametrize('profile', ['scheduled', 'interactive'])
def test_non_autonomous_parent_refused_before_any_storage(profile):
    app = SimpleNamespace(settings=SimpleNamespace(tool_policy_profile=profile))
    with pytest.raises(LaunchBlocked, match='autonomous'):
        capture_launch(app, {})


def test_child_depth_prevents_tool_launch(monkeypatch):
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '1')
    with pytest.raises(LaunchBlocked, match='depth'):
        capture_launch(None, {})


def test_native_parent_not_promised_delivery():
    app = SimpleNamespace(settings=SimpleNamespace(tool_policy_profile='autonomous'),
                          backend=SimpleNamespace(app_server=object()))
    with pytest.raises(LaunchBlocked, match='Native'):
        capture_launch(app, {})


def test_activation_restores_delivery_when_parent_conversation_changes(tmp_path, monkeypatch):
    from pathlib import Path
    from litetui.agent_registry import AgentRegistry
    from litetui.plugins.spawn_agent_plugin import _activate
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    AgentRegistry(tmp_path / '.litetui-agents' / 'registry.sqlite')
    callbacks, parents = [], []
    app = SimpleNamespace(convo_id='first', set_interval=lambda seconds, cb: callbacks.append(cb),
                          _start_child_delivery=lambda **kwargs: parents.append(kwargs['parent']))
    _activate(app)
    callbacks[0]()
    callbacks[0]()
    app.convo_id = 'second'
    callbacks[0]()
    assert parents == ['first', 'second']
    app._gui_quitting = True
    app.convo_id = 'third'
    callbacks[0]()
    assert parents == ['first', 'second']