//! The one rule the paid real-provider acceptance run uses to abort on an observation.
//! Shared with the controlled compaction scenarios, which prove (without a provider) that a
//! tools-on Claude compaction with wake never trips it.
use litetui_terminal::live::Observation;

/// Why the acceptance run must stop on `event`, or `None` when the frontend should simply
/// observe it. Everything not named here (snapshots, rewrite/compaction progress, approvals)
/// is ordinary and is validated by `Demo::observe_live` itself. `WorkExecution` admission is
/// ordinary too: a compaction wake turn is admitted that way (found by the dry run).
pub fn abort_reason(event: &Observation) -> Option<&'static str> {
    match event {
        Observation::Failed(_)
        | Observation::Cancelled(_)
        | Observation::Refused(_)
        | Observation::ApprovalStopped(_)
        | Observation::ImageRefused { .. } => Some("the turn or action stopped"),
        Observation::Question { .. } => Some("an unexpected question"),
        Observation::ScheduledProposal(_)
        | Observation::ScheduledHeld(_)
        | Observation::ScheduledLocalNotSent { .. }
        | Observation::ScheduledConversationChanged(_)
        | Observation::WorkLaunchStop { .. }
        | Observation::WorkLaunchPending { .. }
        | Observation::WorkLaunchFinished { .. }
        | Observation::BackendChanged(_)
        | Observation::Browser(_)
        | Observation::ConversationChanged(_)
        | Observation::ConversationUnverified(_)
        | Observation::Opened(_)
        | Observation::Engine(_)
        | Observation::EngineAdmission(_) => Some("an action this run never requested"),
        _ => None,
    }
}
