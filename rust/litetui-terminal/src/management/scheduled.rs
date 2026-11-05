//! A source proposal reserves one identity handoff, never a synthetic Turn.
use super::{CommandId, Management, Snapshot, Step, fail};
use crate::{conversation_actions as actions, scheduled_transition as schedule};
use serde_json::{Value, json};
use std::io;

#[derive(Default)]
pub(super) struct Control {
    pub pending: Option<Pending>,
}
pub(super) struct Pending {
    pub id: Option<CommandId>,
    proposal: schedule::Proposal,
    phase: Phase,
    request: schedule::DecisionRequest,
    decision: Option<schedule::Decision>,
    before: Option<(Snapshot, Value)>,
    current: Option<actions::Snapshot>,
    state: Option<(Snapshot, Value)>,
    history: Option<crate::resume::History>,
    queue: Option<crate::restored_queue::Snapshot>,
}
#[derive(Clone, Copy, PartialEq)]
enum Phase {
    Frontend,
    BeforeCurrent,
    Before,
    Decision,
    Readback,
    Current,
    State,
    History,
    Queue,
    Catalog,
}
impl Management {
    pub fn scheduled_shutdown_identity(&self, command: &Value) -> Option<Value> {
        self.scheduled
            .pending
            .as_ref()
            .filter(|p| {
                p.phase == Phase::Decision
                    && p.request == schedule::DecisionRequest::Accept
                    && command["type"] == schedule::ACCEPT
                    && p.id
                        .as_ref()
                        .is_some_and(|id| command["id"] == id.to_value())
            })
            .map(|p| p.proposal.transition.wire.clone())
    }
    pub fn schedule_pending(&self) -> bool {
        self.scheduled.pending.is_some()
    }
    pub(super) fn schedule_identity_pending(&self) -> bool {
        self.scheduled.pending.as_ref().is_some_and(|p| {
            p.request == schedule::DecisionRequest::Accept && p.phase != Phase::Frontend
        })
    }
    pub(super) fn propose_schedule(&mut self, proposal: schedule::Proposal) -> io::Result<Step> {
        if self.scheduled.pending.is_some() {
            return Err(fail(
                "Repeated scheduled proposal before decision retirement",
            ));
        }
        let ready = self.ready();
        let id = proposal.id.clone();
        self.work.manual_review = None;
        self.reserve_schedule(proposal);
        if self.closing {
            self.scheduled.pending = None;
            return Ok(Step::Ignore);
        }
        if !ready {
            return self.decide_schedule(&id, false, false).map(Step::Write);
        }
        Ok(Step::ScheduledProposal(id))
    }
    pub(super) fn begin_manual_schedule(
        &mut self,
        proposal: schedule::Proposal,
        accept: bool,
    ) -> io::Result<Value> {
        if self.schedule_pending() || self.closing {
            return Err(fail("Another conversation handoff owns this decision"));
        }
        let id = proposal.id.clone();
        self.reserve_schedule(proposal);
        self.decide_schedule(&id, accept, true)
    }
    fn reserve_schedule(&mut self, proposal: schedule::Proposal) {
        self.scheduled.pending = Some(Pending {
            id: None,
            proposal,
            phase: Phase::Frontend,
            request: schedule::DecisionRequest::Decline,
            decision: None,
            before: None,
            current: None,
            state: None,
            history: None,
            queue: None,
        });
    }
    /// The frontend's decision is evidence of current UI quiescence only.
    /// The supervisor independently rechecks actual Turn and management owners.
    pub fn decide_schedule(
        &mut self,
        id: &str,
        frontend_idle: bool,
        turn_idle: bool,
    ) -> io::Result<Value> {
        let mut pending = self
            .scheduled
            .pending
            .take()
            .ok_or_else(|| fail("No scheduled proposal awaits a decision"))?;
        if pending.phase != Phase::Frontend || pending.proposal.id != id || self.closing {
            return Err(fail("Scheduled decision changed its exact proposal"));
        }
        let accept = frontend_idle && turn_idle && self.ready();
        let command = if accept {
            pending.phase = Phase::BeforeCurrent;
            pending.request = schedule::DecisionRequest::Accept;
            let old = &pending.proposal.identity;
            actions::Identity {
                session: old.session.clone(),
                agent: old.agent.clone(),
                backend: old.backend.clone(),
                model: old.model.clone(),
            }
            .command(actions::SNAPSHOT)
        } else {
            pending.phase = Phase::Decision;
            pending.proposal.decline_request(
                "Current native editor, submitted input, or owned work is not idle",
            )?
        };
        self.schedule_send(pending, command)
    }
    fn schedule_send(&mut self, mut pending: Pending, mut command: Value) -> io::Result<Value> {
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve scheduled verification"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.scheduled.pending = Some(pending);
        Ok(command)
    }
    pub(super) fn schedule_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && self
                .scheduled
                .pending
                .as_ref()
                .and_then(|p| p.id.as_ref())
                .is_some_and(|id| frame["id"] == id.to_value())
    }
    pub(super) fn schedule_response(&mut self, frame: Value) -> io::Result<Step> {
        let mut pending = self
            .scheduled
            .pending
            .take()
            .expect("matched scheduled response");
        if frame["ok"] != true {
            if self.closing {
                // Quit can cancel the staged invocation before this readback.
                // Drain only the matched request; the supervisor still requires
                // the exact conversation and Work retirement proofs at shutdown.
                return Ok(Step::Ignore);
            }
            return Err(fail(
                "Scheduled handoff response failed; ownership cannot be inferred",
            ));
        }
        let result = &frame["result"];
        let target = pending.proposal.transition.target_identity();
        let identity = actions::Identity {
            session: target.session.clone(),
            agent: target.agent.clone(),
            backend: target.backend.clone(),
            model: target.model.clone(),
        };
        match pending.phase {
            Phase::BeforeCurrent => {
                let old = &pending.proposal.identity;
                let identity = actions::Identity {
                    session: old.session.clone(),
                    agent: old.agent.clone(),
                    backend: old.backend.clone(),
                    model: old.model.clone(),
                };
                pending.current = Some(actions::Snapshot::parse(result, &identity)?);
            }
            Phase::Before => {
                let old = &pending.proposal.identity;
                let selected = actions::Identity {
                    session: old.session.clone(),
                    agent: old.agent.clone(),
                    backend: old.backend.clone(),
                    model: old.model.clone(),
                };
                let state = self.verify_conversation_state(
                    result,
                    &selected,
                    pending.current.take().is_some_and(|s| s.pending),
                    None,
                )?;
                if result["busy"] == true
                    || [
                        "active_work",
                        "pending_approvals",
                        "pending_questions",
                        "pending_model_loads",
                    ]
                    .iter()
                    .any(|k| result[k].as_array().is_some_and(|v| !v.is_empty()))
                {
                    pending.request = schedule::DecisionRequest::Decline;
                }
                pending.before = Some((state, result["tool_policy"].clone()));
            }
            Phase::Decision => {
                let operation = pending.id.as_ref().unwrap().to_value();
                let decision = schedule::Decision::parse_with_runtime(
                    result,
                    &pending.proposal,
                    operation
                        .as_str()
                        .ok_or_else(|| fail("Scheduled operation is not text"))?,
                    pending.request,
                    self.historical_runtime_binding(),
                )?;
                if decision.status == schedule::Status::Uncertain {
                    return Err(fail("Scheduled transition is uncertain; close required"));
                }
                if decision.status == schedule::Status::Staged {
                    self.work_transition_receipt(decision.transition.as_ref().unwrap(), true)?;
                }
                pending.decision = Some(decision);
            }
            Phase::Readback => {
                let decision = pending.decision.as_ref().unwrap();
                schedule::Readback::parse(result, decision)?;
                if decision.status != schedule::Status::Staged {
                    return Ok(if self.closing {
                        Step::Ignore
                    } else {
                        Step::ScheduledHeld {
                            reason: decision
                                .reason
                                .clone()
                                .unwrap_or_else(|| "Scheduled transition held".into()),
                            shutdown_identity: (pending.request
                                == schedule::DecisionRequest::Accept)
                                .then(|| pending.proposal.transition.wire.clone()),
                        }
                    });
                }
            }
            Phase::Current => {
                let current = actions::Snapshot::parse(result, &identity)?;
                if current.pending {
                    return Err(fail("Scheduled New was not materialized"));
                }
                pending.current = Some(current);
            }
            Phase::State => {
                let state = self.verify_conversation_state(
                    result,
                    &identity,
                    false,
                    pending.before.as_ref(),
                )?;
                pending.state = Some((state, result["tool_policy"].clone()));
            }
            Phase::History => {
                pending.history = Some(self.verify_new_conversation_history(result, &identity)?);
            }
            Phase::Queue => {
                pending.queue = Some(self.verify_empty_conversation_queue(result, &identity)?);
            }
            Phase::Catalog => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Conversation revision exhausted"))?;
                let page = crate::conversation_browser::Page::parse(
                    result,
                    self.revision,
                    &identity.session,
                )?;
                let current = pending.current.take().unwrap();
                if page.agent != identity.agent
                    || page
                        .entries
                        .iter()
                        .any(|e| e.current && e.title != current.title)
                {
                    return Err(fail("Scheduled catalog changed current ownership/title"));
                }
                if self.closing {
                    return Ok(Step::Ignore);
                }
                let (state, policy) = pending.state.take().unwrap();
                let queue = pending.queue.take().unwrap();
                self.expect_scheduled_capture(
                    pending
                        .decision
                        .as_ref()
                        .unwrap()
                        .deferred
                        .clone()
                        .ok_or_else(|| fail("Scheduled readback lost its deferred invocation"))?,
                )?;
                self.install_verified_conversation(&state, &queue, &page);
                return Ok(Step::ScheduledConversationChanged(Box::new(
                    actions::Changed {
                        action: actions::Action::New,
                        snapshot: state,
                        history: pending.history.take().unwrap(),
                        queue,
                        policy,
                        current,
                        page,
                    },
                )));
            }
            Phase::Frontend => {
                return Err(fail("Scheduled response arrived before frontend decision"));
            }
        }
        if self.closing {
            return Ok(Step::Ignore);
        }
        pending.phase = match pending.phase {
            Phase::BeforeCurrent => Phase::Before,
            Phase::Before => Phase::Decision,
            Phase::Decision => Phase::Readback,
            Phase::Readback => Phase::Current,
            Phase::Current => Phase::State,
            Phase::State => Phase::History,
            Phase::History => Phase::Queue,
            Phase::Queue => Phase::Catalog,
            _ => return Err(fail("Invalid scheduled handoff continuation")),
        };
        let command = match pending.phase {
            Phase::Decision if pending.request == schedule::DecisionRequest::Accept => {
                pending.proposal.accept_request()
            }
            Phase::Decision => pending
                .proposal
                .decline_request("Source state acquired work before acceptance")?,
            Phase::Readback => pending.decision.as_ref().unwrap().readback_request()?,
            Phase::Current => identity.command(actions::SNAPSHOT),
            Phase::Before | Phase::State => {
                json!({"type":"gui.state","include_conversations":false})
            }
            Phase::History => {
                self.history_command(&identity.session, &identity.backend, &identity.model)?
            }
            Phase::Queue => {
                json!({"type":crate::restored_queue::READ,"session_id":identity.session})
            }
            Phase::Catalog => {
                json!({"type":crate::conversation_browser::PAGE,"session_id":identity.session,"cursor":null})
            }
            _ => return Err(fail("Invalid scheduled handoff request")),
        };
        self.schedule_send(pending, command).map(Step::Write)
    }
    /// Check target state against immutable host constraints while the old Work
    /// epoch is retired. Only the matching new capture/activation adopts it.
    pub(super) fn verify_target_profile(
        &self,
        state: &Snapshot,
        policy: &Value,
        previous_profile: &str,
    ) -> io::Result<()> {
        let Some(previous) = self.retired_runtime_policy() else {
            if state.profile != previous_profile {
                return Err(fail("Target changed the legacy launch profile"));
            }
            return Ok(());
        };
        let expected = crate::tool_mode::Expected {
            enabled: previous.facts.tools_enabled,
            profile: previous.facts.seat_profile,
            workspace: previous.facts.cwd.clone().into(),
        };
        expected.validate_mode(policy, crate::tool_mode::ValidationMode::Target)?;
        if policy["tool_profile"] != state.profile
            || policy["tool_profile_requested"].as_str()
                != previous
                    .facts
                    .requested_cli_profile
                    .map(crate::tool_mode::Profile::as_str)
        {
            return Err(fail(
                "Target profile contradicts its state or retained launch choice",
            ));
        }
        Ok(())
    }
    pub(super) fn verify_conversation_state(
        &self,
        result: &Value,
        identity: &actions::Identity,
        allow_pending: bool,
        before: Option<&(Snapshot, Value)>,
    ) -> io::Result<Snapshot> {
        let state = Snapshot::parse(result)?;
        let previous_profile = &self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Missing conversation authority"))?
            .3;
        let changed_session = before.is_some_and(|(old, _)| old.session != identity.session);
        if changed_session {
            self.verify_target_profile(&state, &result["tool_policy"], previous_profile)?;
        } else if state.profile != *previous_profile {
            return Err(fail("Conversation state changed the current profile"));
        }
        if state.session != identity.session
            || state.backend != identity.backend
            || state.model != identity.model
            || !(state.writable || allow_pending)
            || state.held_restored_input != Some(0)
            || Some(state.schedules_paused) != self.browser_authority.as_ref().map(|v| v.1)
            || result["ownership"]["agent_id"] != identity.agent
        {
            return Err(fail(
                "Conversation state changed identity, authority or holds",
            ));
        }
        if !(changed_session && self.retired_runtime_policy().is_some())
            && before.is_some_and(|(old, policy)| {
                old.mode != state.mode || policy != &result["tool_policy"]
            })
        {
            return Err(fail("Conversation transition changed tool policy"));
        }
        Ok(state)
    }
    pub(super) fn verify_new_conversation_history(
        &self,
        result: &Value,
        identity: &actions::Identity,
    ) -> io::Result<crate::resume::History> {
        if serde_json::to_vec(result)?.len() > 1048576 {
            return Err(fail(
                "Conversation history exceeded requested response budget",
            ));
        }
        let history = self.parse_history(
            result,
            &identity.session,
            &identity.backend,
            &identity.model,
        )?;
        let fresh = history.descriptor.as_ref().map_or_else(
            || {
                result["messages"].as_array().is_some_and(|rows| {
                    rows.iter().all(|m| {
                        m["role"] == "system"
                            && m.as_object().is_some_and(|o| {
                                o.keys().all(|k| matches!(k.as_str(), "role" | "content"))
                            })
                    })
                })
            },
            |d| d.fresh_system_context,
        );
        if !fresh {
            return Err(fail("New conversation contains non-system history"));
        }
        Ok(history)
    }
    pub(super) fn verify_empty_conversation_queue(
        &mut self,
        result: &Value,
        identity: &actions::Identity,
    ) -> io::Result<crate::restored_queue::Snapshot> {
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| fail("Conversation revision exhausted"))?;
        let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
        if queue.session != identity.session
            || queue.backend != identity.backend
            || queue.model != identity.model
            || !queue.entries.is_empty()
        {
            return Err(fail("Conversation has changed or nonempty held input"));
        }
        Ok(queue)
    }
    pub(super) fn install_verified_conversation(
        &mut self,
        state: &Snapshot,
        queue: &crate::restored_queue::Snapshot,
        page: &crate::conversation_browser::Page,
    ) {
        self.expected = Some((
            state.session.clone(),
            state.backend.clone(),
            state.model.clone(),
            state.profile.clone(),
        ));
        self.origin = None;
        self.prompt = None;
        self.menu = None;
        self.command_catalog = None;
        self.command_prepared = None;
        self.backend_catalog = None;
        self.settings_snapshot = None;
        self.settings_review = None;
        self.conversation_snapshot = None;
        self.conversation_review = None;
        self.browser_preview = None;
        self.history_snapshot = None;
        self.history_page = None;
        self.history_target = None;
        self.compact.invalidate();
        self.rewrite_cache = None;
        self.rewrite_review = None;
        self.invalidate_work_selection();
        self.browser_page = Some(page.clone());
        self.queue_menu = Some(queue.clone());
    }
}
