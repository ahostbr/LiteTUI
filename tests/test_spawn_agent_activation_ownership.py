from types import SimpleNamespace
from unittest.mock import Mock
from litetui.plugins import spawn_agent_plugin as plugin


def test_activation_retains_one_timer_per_app():
    timer = Mock()
    app = SimpleNamespace(set_interval=Mock(return_value=timer))
    plugin._activate(app)
    plugin._activate(app)
    assert app.set_interval.call_count == 1
    assert app._child_recovery_timer is timer


def test_deactivate_stops_only_owned_recovery_timer():
    timer = Mock()
    app = SimpleNamespace(set_interval=Mock(return_value=timer), _stop_child_delivery=Mock())
    plugin._activate(app)
    plugin._deactivate(app)
    plugin._deactivate(app)
    timer.stop.assert_called_once()
    app._stop_child_delivery.assert_not_called()
    assert app._child_recovery_timer is None
    plugin._activate(app)
    assert app.set_interval.call_count == 2


def test_failed_timer_stop_retains_ownership_for_retry():
    import pytest
    timer = Mock()
    timer.stop.side_effect = RuntimeError('stop failed')
    app = SimpleNamespace(set_interval=Mock(return_value=timer))
    plugin._activate(app)
    with pytest.raises(RuntimeError, match='stop failed'):
        plugin._deactivate(app)
    assert app._child_recovery_timer is timer
    plugin._activate(app)
    assert app.set_interval.call_count == 1
