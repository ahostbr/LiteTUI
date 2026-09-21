"""Trusted prepared-launch App bridge; public spawn tool remains separate."""
from litetui.agent_launcher import LaunchBlocked
from litetui.agent_runtime import run_prepared_child


async def run_for_app(app, spec, process, *, registry, inbox, receipts, parent,
                      child_id, workspace, data_root, branch, evidence,
                      supported_levels, limit=1, timeout=300, prepare=None):
    from litetui.agent_ancestry import require_root_launcher
    require_root_launcher()
    # Capture before the first await. Never route a returning child to the chat
    # selected later; the launch record is the source of truth after restart.
    conversation = app.convo_id
    if (not conversation or app.store.convo_id != conversation
            or not app.store.owned or app.store.pending or app.store.loading):
        raise LaunchBlocked('Parent must own a materialized conversation before spawning')
    if getattr(app, '_gui_quitting', False):
        raise LaunchBlocked('Parent is quitting')
    app._start_child_delivery(parent=parent, registry=registry, inbox=inbox, receipts=receipts)

    def before_start():
        if (getattr(app, '_gui_quitting', False) or app.convo_id != conversation
                or app.store.convo_id != conversation or not app.store.owned
                or app.store.pending or app.store.loading):
            raise LaunchBlocked('Parent ownership changed during child preparation')
        settings = getattr(app, 'settings', None)
        profile = getattr(app, '_active_tool_profile', None) or getattr(settings, 'tool_policy_profile', None)
        if profile is not None and profile != spec.tool_profile:
            raise LaunchBlocked('Parent authority changed during child preparation')

    def notify(event):
        # Only durable transfer here, never UI delivery or inference while the
        # parent might be in a tool round. The timer owns idle application.
        receipts.replay_from_inbox(parent, inbox=inbox, registry=registry)

    return await run_prepared_child(spec, process, registry=registry, inbox=inbox,
        parent=parent, child_id=child_id, workspace=workspace, data_root=data_root,
        branch=branch, evidence=evidence, supported_levels=supported_levels,
        notify=notify, limit=limit, timeout=timeout, parent_conversation=conversation,
        prepare=prepare, before_start=before_start)
