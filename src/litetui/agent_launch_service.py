"""Composed hosted launch service, callable only from trusted App runtime."""
from uuid import uuid4
from litetui.agent_ancestry import require_root_launcher
from litetui.agent_launcher import LaunchBlocked, validate_request, validate_capabilities
from litetui.agent_preparation import prepare_child
from litetui.agent_app_runtime import run_for_app
from litetui.agent_supervisor import AgentProcess


async def launch_for_app(app, request, *, registry, inbox, receipts, parent,
                         storage, baseline, supported_levels, limit=1,
                         timeout=300, process_factory=AgentProcess):
    depth = require_root_launcher()
    profile = getattr(app, '_active_tool_profile', None) or app.settings.tool_policy_profile
    spec = validate_request(request, parent_profile=profile, depth=depth)
    if spec.headed or spec.backend != 'codex' or spec.workspace_mode != 'worktree':
        raise LaunchBlocked('Only hosted headless isolated worktree launch is integrated')
    validate_capabilities(spec, supported_levels)
    child_id = uuid4().hex
    def prepare():
        return prepare_child(spec, storage=storage, child_id=child_id,
                             baseline=baseline, supported_levels=supported_levels)
    completion = await run_for_app(app, spec, process_factory(), registry=registry,
        inbox=inbox, receipts=receipts, parent=parent, child_id=child_id,
        workspace=None, data_root=None, branch=None, evidence=[],
        supported_levels=supported_levels, limit=limit, timeout=timeout, prepare=prepare)
    return {'child_id': child_id, 'completion_id': completion,
            'result': inbox.get(parent, completion)}
