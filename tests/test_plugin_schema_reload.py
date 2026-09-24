from copy import deepcopy
import pytest
from litetui.plugins import PluginRegistry
from litetui.tool_policy import NETWORK_READ_POLICY


def fixture():
    live = PluginRegistry()
    spec = {'type': 'function', 'function': {'name': 'demo', 'description': 'old',
            'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}}
    run = lambda args: args['text']
    live.add_tool('demo', spec, run, policy=NETWORK_READ_POLICY)
    live.add_command('host', ['/host'], lambda *args: None)
    live.activated.add('demo')
    live.status['demo'] = 'active'
    return live, spec, run


def test_metadata_refresh_preserves_handler_host_rows_and_live_generation():
    from litetui.plugin_schema_reload import stage_schema_refresh
    live, spec, run = fixture()
    updated = deepcopy(spec)
    updated['function']['description'] = 'new'
    candidate = stage_schema_refresh(live, {'demo': updated})
    assert candidate is not live
    assert candidate.dispatch_for('demo') is run
    assert candidate.tools[0].spec['function']['description'] == 'new'
    assert live.tools[0].spec['function']['description'] == 'old'
    assert candidate.commands['/host'] is live.commands['/host']
    candidate.activated.clear()
    assert live.activated == {'demo'}
    updated['function']['description'] = 'mutated later'
    assert candidate.tools[0].spec['function']['description'] == 'new'


@pytest.mark.parametrize('mutation', ['rename', 'type', 'required', 'extra_tool'])
def test_argument_contract_or_tool_inventory_changes_refuse(mutation):
    from litetui.plugin_schema_reload import stage_schema_refresh
    live, spec, run = fixture()
    updated = deepcopy(spec)
    replacements = {'demo': updated}
    if mutation == 'rename': updated['function']['name'] = 'other'
    if mutation == 'type': updated['function']['parameters']['properties']['text']['type'] = 'integer'
    if mutation == 'required': updated['function']['parameters']['required'] = []
    if mutation == 'extra_tool': replacements = {'other': updated}
    with pytest.raises(ValueError):
        stage_schema_refresh(live, replacements)
    assert live.dispatch_for('demo') is run
    assert live.tools[0].spec == spec
