"""Full hosted child tool. Local/headed paths remain explicitly blocked."""
from pathlib import Path
from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.tool_policy import ToolPolicy, CAPABILITIES
from litetui.agent_launcher import LaunchBlocked, validate_request, validate_capabilities
from litetui.agent_ancestry import require_root_launcher
from litetui.agent_capabilities import hosted_levels
from litetui.agent_tool_bridge import make_runner

SPEC = tool_schemas.load('spawn_agent')
# A full autonomous child may invoke any available tool. Declaring only network
# or workspace-write here would understate the delegated authority.
POLICY = ToolPolicy(frozenset(CAPABILITIES), 'Launch a full autonomous tool-using child')


def capture_launch(app, request):
    depth = require_root_launcher()
    profile = getattr(app, '_active_tool_profile', None) or app.settings.tool_policy_profile
    if profile != 'autonomous':
        raise LaunchBlocked('Full child tool currently requires autonomous parent policy; approval relay is not integrated')
    if hasattr(app.backend, 'app_server'):
        raise LaunchBlocked('Native app-server parent result delivery is not integrated')
    from litetui import hook_host
    hooks = hook_host.snapshot(app)
    if hooks.hooks or hooks.error:
        raise LaunchBlocked('Child-result prompt hook admission is not integrated')
    spec = validate_request(request, parent_profile=profile, depth=depth)
    if spec.backend != 'codex' or spec.headed or spec.workspace_mode != 'worktree':
        raise LaunchBlocked('Only Codex headless isolated worktree children are integrated')
    levels = hosted_levels(app, request)
    validate_capabilities(spec, levels)
    conversation = app.convo_id
    if (not conversation or app.store.convo_id != conversation or not app.store.owned
            or app.store.pending or app.store.loading):
        raise LaunchBlocked('Owned materialized parent conversation required')
    workspace = Path(spec.workspace).resolve()
    captured = dict(request)
    captured['workspace'] = str(workspace)
    # Home-owned sibling tree, never the caller's source checkout. Shared
    # registry enforces the default one-child budget across parent instances.
    root = Path.home() / '.litetui-agents'

    async def launch():
        from litetui.agent_preparation import await_preparation
        from litetui.agent_workspace import _git
        baseline = await await_preparation(
            lambda: _git(workspace, 'rev-parse', '--verify', 'HEAD^{commit}'))
        current_profile = getattr(app, '_active_tool_profile', None) or app.settings.tool_policy_profile
        if app.convo_id != conversation or current_profile != profile:
            raise LaunchBlocked('Parent conversation or authority changed before launch')
        from litetui.agent_registry import AgentRegistry
        from litetui.agent_inbox import AgentInbox
        from litetui.agent_receipts import ParentReceipts
        from litetui.agent_launch_service import launch_for_app
        return await launch_for_app(app, captured, registry=AgentRegistry(root/'registry.sqlite'),
            inbox=AgentInbox(root/'inbox.sqlite'), receipts=ParentReceipts(root/'receipts.sqlite'),
            parent=conversation, storage=root/'children', baseline=baseline,
            supported_levels=levels, limit=1)
    return launch


def _register(ctx):
    ctx.tool(SPEC, make_runner(ctx.app, launch=None,
             capture=lambda request: capture_launch(ctx.app, request)), policy=POLICY)


def _activate(app):
    if getattr(app, '_child_recovery_timer', None) is not None:
        return
    root = Path.home() / '.litetui-agents'
    restored = None

    def restore():
        nonlocal restored
        conversation = getattr(app, 'convo_id', None)
        if (not conversation or conversation == restored
                or getattr(app, '_gui_quitting', False)
                or not (root / 'registry.sqlite').is_file()):
            return
        from litetui.agent_registry import AgentRegistry
        from litetui.agent_inbox import AgentInbox
        from litetui.agent_receipts import ParentReceipts
        registry = AgentRegistry(root / 'registry.sqlite')
        inbox = AgentInbox(root / 'inbox.sqlite')
        receipts = ParentReceipts(root / 'receipts.sqlite')
        registry.reconcile(conversation, inbox=inbox)
        app._start_child_delivery(parent=conversation, registry=registry, inbox=inbox, receipts=receipts)
        restored = conversation

    # Conversation may materialize or resume after plugin activation. Observe
    # identity changes rather than pinning a transient startup UUID forever.
    last_error = None

    def guarded_restore():
        nonlocal last_error
        import sqlite3
        try:
            restore()
            last_error = None
        except (OSError, ValueError, sqlite3.Error) as exc:
            detail = str(exc)
            if detail != last_error:
                app._system(f'[child delivery recovery deferred: {detail}]')
                last_error = detail

    app._child_recovery_timer = app.set_interval(2.0, guarded_restore)


def _deactivate(app):
    """Stop only this plugin's observer, never children or delivery ownership.

    Explicit lifecycle seam for the reload coordinator; not yet registered as
    a manifest hook. Textual also stops App timers on ordinary App shutdown.
    A failed stop retains the handle so recovery cannot create a duplicate.
    """
    timer = getattr(app, '_child_recovery_timer', None)
    if timer is not None:
        timer.stop()
        app._child_recovery_timer = None


PLUGIN = PluginManifest(id='spawn_agent', register=_register, activate=_activate)
