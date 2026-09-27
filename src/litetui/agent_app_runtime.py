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
        from litetui.agent_launcher import delegated_profile
        if profile is not None and delegated_profile(profile) != spec.tool_profile:
            raise LaunchBlocked('Parent authority changed during child preparation')

    def notify(event):
        # Only durable transfer here, never UI delivery or inference while the
        # parent might be in a tool round. The timer owns idle application.
        receipts.replay_from_inbox(parent, inbox=inbox, registry=registry)

    # T1049-B: the child's CONFIRMs come back to THIS parent (plan §5, gate
    # d47235da): Ryan's own parent asks him with no deadline, a locked parent its
    # spawner. The child waits as long as its parent will.
    from litetui import approval_relay, seat_authority
    owner = seat_authority.confirm_route(app) == "own"
    approval_timeout = 0 if owner else int(approval_relay.timeout_s(app)) + 60
    return await run_prepared_child(spec, process, registry=registry, inbox=inbox,
        parent=parent, child_id=child_id, workspace=workspace, data_root=data_root,
        branch=branch, evidence=evidence, supported_levels=supported_levels,
        notify=notify, limit=limit, timeout=timeout, parent_conversation=conversation,
        prepare=prepare, before_start=before_start,
        on_approval=app.approve_for_child, approval_timeout=approval_timeout)
