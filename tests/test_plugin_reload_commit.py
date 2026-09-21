from types import SimpleNamespace
from unittest.mock import Mock
from litetui.plugins import PluginRegistry
from litetui.plugin_reload_state import ActivitySnapshot


def test_commit_refuses_stale_generation_without_touching_current():
    from litetui.plugin_reload_commit import commit_metadata_candidate
    old, current, candidate = PluginRegistry(), PluginRegistry(), PluginRegistry()
    app = SimpleNamespace(plugins=current, backend=SimpleNamespace())
    result = commit_metadata_candidate(app, old, candidate, activity=lambda: ActivitySnapshot())
    assert result.status == 'deferred'
    assert app.plugins is current


def test_native_candidate_refuses_without_rethread():
    from litetui.plugin_reload_commit import commit_metadata_candidate
    live, candidate = PluginRegistry(), PluginRegistry()
    app = SimpleNamespace(plugins=live, backend=SimpleNamespace(app_server=Mock()))
    result = commit_metadata_candidate(app, live, candidate, activity=lambda: ActivitySnapshot())
    assert result.status == 'restart-required'
    assert app.plugins is live
    app.backend.app_server.assert_not_called()


def test_busy_candidate_does_not_commit_and_idle_transfers_latest_state():
    from litetui.plugin_reload_commit import commit_metadata_candidate
    live, candidate = PluginRegistry(), PluginRegistry()
    app = SimpleNamespace(plugins=live, backend=SimpleNamespace())
    result = commit_metadata_candidate(app, live, candidate, activity=lambda: ActivitySnapshot(tool_active=True))
    assert result.status == 'deferred'
    assert app.plugins is live
    live.activated.add('recent')
    result = commit_metadata_candidate(app, live, candidate, activity=lambda: ActivitySnapshot())
    assert result.status == 'reloaded'
    assert app.plugins is candidate
    assert candidate.activated == {'recent'}


def test_commit_cannot_smuggle_new_handler_or_command():
    from litetui.plugin_reload_commit import commit_metadata_candidate
    from litetui.plugin_schema_reload import stage_schema_refresh
    from litetui.tool_policy import NETWORK_READ_POLICY
    from dataclasses import replace
    live = PluginRegistry()
    live.add_tool('owner', {'type': 'function', 'function': {'name': 'demo', 'parameters':
                  {'type': 'object', 'properties': {}}}}, lambda args: 'old', policy=NETWORK_READ_POLICY)
    candidate = stage_schema_refresh(live, {})
    candidate.tools[0] = replace(candidate.tools[0], run=lambda args: 'new')
    app = SimpleNamespace(plugins=live, backend=SimpleNamespace())
    result = commit_metadata_candidate(app, live, candidate, activity=lambda: ActivitySnapshot())
    assert result.status == 'failed'
    assert app.plugins is live
    candidate = stage_schema_refresh(live, {})
    candidate.add_command('new', ['/new'], lambda *args: None)
    assert commit_metadata_candidate(app, live, candidate, activity=lambda: ActivitySnapshot()).status == 'failed'
    assert app.plugins is live


def test_unreadable_activity_preserves_live_generation():
    from litetui.plugin_reload_commit import commit_metadata_candidate
    live = PluginRegistry()
    app = SimpleNamespace(plugins=live, backend=SimpleNamespace())
    result = commit_metadata_candidate(app, live, PluginRegistry(), activity=lambda: None)
    assert result.status == 'failed'
    assert app.plugins is live
