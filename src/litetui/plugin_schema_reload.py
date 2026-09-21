"""Narrow static-schema metadata staging; no code reload or registry swap.

Preserves handlers, gates, policies, host rows and resource owners. Only the
function-level description may change; argument contracts require restart.
This deliberately does not claim general plugin handler hot reload.
"""
from copy import copy, deepcopy
from dataclasses import replace
from litetui.plugin_reload import _schema_problem
from litetui.plugin_reload_state import transfer_session_state


def stage_schema_refresh(live, replacements):
    by_name = {entry.name: entry for entry in live.tools}
    prepared = {}
    for name, schema in replacements.items():
        if name not in by_name:
            raise ValueError(f'{name}: new tool requires restart and authority review')
        problem = _schema_problem(schema)
        if problem:
            raise ValueError(f'{name}: {problem}')
        before, after = deepcopy(by_name[name].spec), deepcopy(schema)
        before['function'].pop('description', None)
        after['function'].pop('description', None)
        if before != after:
            raise ValueError(f'{name}: executable schema contract changed; restart required')
        prepared[name] = deepcopy(schema)
    # Structural copy retains immutable registration entries/callables, but no
    # mutable registry table container or session activation set is shared.
    candidate = copy(live)
    for attr in ('dynamic', 'palette_rows', 'prompt_sections', 'observers', 'turn_finalizers'):
        setattr(candidate, attr, list(getattr(live, attr)))
    candidate.commands = dict(live.commands)
    candidate.status = dict(live.status)
    candidate.tools = [replace(entry, spec=prepared.get(entry.name, deepcopy(entry.spec)))
                       for entry in live.tools]
    candidate._tool_by_name = {entry.name: entry for entry in candidate.tools}
    transfer_session_state(live, candidate)
    return candidate
