//! Work inspection, reviewed scheduler pause and exact owned task Stop.
use super::{CommandId, Management, Step, fail, text};
use crate::deferred_control as deferred;
use crate::work_controls as controls;
use crate::{work_protocol as protocol, work_view as view};
use serde_json::{Value, json};
use std::io;

#[derive(Default)]
pub(super) struct Control {
    pub pending: Option<Pending>,
    pub(super) snapshot: Option<protocol::Snapshot>,
    pub(super) page: Option<protocol::Page>,
    review: Option<protocol::PauseReview>,
    stop_review: Option<protocol::StopReview>,
    pub(super) generation: u64,
    deferred_review: Option<deferred::Review>,
    control_review: Option<controls::Review>,
    pub(super) manual_review: Option<crate::scheduled_transition::ManualReview>,
    context_review: Option<crate::context_edit::Review>,
    plugin_names: Vec<String>,
    plugin_review: Option<crate::plugin_reload::Review>,
    hook_list: Option<crate::hook_toggle::List>,
    hook_review: Option<crate::hook_toggle::Review>,
    hook_test: Option<crate::hook_toggle::TestReview>,
    hook_edit_snapshot: Option<crate::hook_edit::Snapshot>,
    hook_edit_review: Option<crate::hook_edit::Review>,
    hook_repair_review: Option<crate::hook_edit::RepairReview>,
    queue_review: Option<crate::midturn::Review>,
    pause_review: Option<crate::chat_pause::Review>,
    pause_origin: Option<crate::work_lifecycle::Origin>,
    dictation: Option<crate::dictation::State>,
    mark: Option<crate::mark::State>,
    queue_origin: Option<crate::work_lifecycle::Origin>,
    queue_policy: Option<(u64, crate::tool_mode::Profile)>,
}
pub(super) struct Pending {
    pub id: CommandId,
    generation: u64,
    request: view::Request,
    receipt: Option<protocol::PauseReceipt>,
    stop_receipt: Option<protocol::StopReceipt>,
    deferred_receipt: Option<deferred::ResultReceipt>,
    deferred_verified: bool,
    control_receipt: Option<controls::Receipt>,
    context_apply_id: Option<String>,
}
impl Management {
    pub(crate) fn bind_queue_policy(
        &mut self,
        policy: &crate::runtime_controls::Policy,
    ) -> io::Result<()> {
        self.work.queue_policy = Some((
            policy
                .generation
                .checked_add(1)
                .ok_or_else(|| fail("Runtime policy limit"))?,
            policy.facts.seat_profile,
        ));
        Ok(())
    }
    pub(super) fn invalidate_work_selection(&mut self) {
        self.work.plugin_names.clear();
        self.work.plugin_review = None;
        self.work.manual_review = None;
        self.work.queue_review = None;
        self.invalidate_execution_review();
        self.work.snapshot = None;
        self.work.page = None;
        self.work.review = None;
        self.work.stop_review = None;
        self.work.deferred_review = None;
        self.work.control_review = None;
        self.work.context_review = None;
        self.work.hook_list = None;
        self.work.hook_review = None;
        self.work.hook_test = None;
        self.work.hook_edit_snapshot = None;
        self.work.hook_edit_review = None;
        self.work.hook_repair_review = None;
    }
    fn extended_midturn(&self) -> bool {
        [crate::midturn::SEND_REVIEW, crate::midturn::SEND_APPLY]
            .iter()
            .all(|op| self.operations.iter().any(|v| v == op))
    }
    pub(crate) fn interrupt_apply_pending(&self) -> bool {
        self.work
            .pending
            .as_ref()
            .is_some_and(|p| matches!(&p.request, view::Request::Midturn(r) if r.interrupts()))
    }
    pub fn work_pending(&self) -> bool {
        self.work.pending.is_some() || self.execution_review_pending()
    }
    pub(super) fn context_instance(&self) -> io::Result<&str> {
        self.execution_ledger()
            .filter(|_| self.work_lifecycle_active() && !self.work_identity_transition_pending())
            .map(|ledger| ledger.capture().instance.as_str())
            .ok_or_else(|| fail("This inspection requires an active Work session"))
    }
    pub(super) fn work_identity(&self) -> io::Result<protocol::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Runtime is not initialized"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Runtime omitted Work ownership"))?;
        Ok(protocol::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
        })
    }
    pub(super) fn work_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && self
                .work
                .pending
                .as_ref()
                .is_some_and(|p| frame["id"] == p.id.to_value())
    }
    /// Ordinary streaming does not own the management lane. Transitions do.
    pub fn request_work(&mut self, intent: view::Intent) -> io::Result<Value> {
        if matches!(
            &intent.request,
            view::Request::ExecutionValidate(_) | view::Request::ExecutionApply(_)
        ) {
            return self.request_execution(intent);
        }
        if !self.ready() {
            return Err(fail(
                "Finish the current management operation before opening Work",
            ));
        }
        if intent.generation <= self.work.generation {
            return Err(fail("Stale Work request"));
        }
        let identity = self.work_identity()?;
        if let view::Request::ManualDecision { review, accept } = &intent.request {
            let retained = self
                .work
                .manual_review
                .take()
                .filter(|stored| stored == review.as_ref())
                .ok_or_else(|| fail("This manual job review expired; refresh Work"))?;
            if self.context_instance()? != retained.proposal.instance || self.schedule_pending() {
                return Err(fail(
                    "Manual job review no longer belongs to this Work session",
                ));
            }
            self.work.generation = intent.generation;
            return self.begin_manual_schedule(retained.proposal, *accept);
        }
        self.work.manual_review = None;
        let op = match &intent.request {
            view::Request::ManualReview { .. } => crate::scheduled_transition::MANUAL_REVIEW,
            view::Request::ManualDecision { .. } => unreachable!(),
            view::Request::ExecutionValidate(_) | view::Request::ExecutionApply(_) => {
                unreachable!()
            }
            view::Request::ControlValidate(_) => controls::VALIDATE,
            view::Request::ControlApply(_) => controls::APPLY,
            view::Request::DeferredValidate => deferred::VALIDATE,
            view::Request::DeferredApply(_) => deferred::DISCARD,
            view::Request::Image(request) => request.operation(),
            view::Request::Midturn(r) => {
                if self.extended_midturn() {
                    r.send_operation()
                } else {
                    r.operation()
                }
            }
            view::Request::ChatPause(r) => {
                if crate::chat_pause::OPERATIONS
                    .iter()
                    .any(|op| !self.operations.iter().any(|v| v == op))
                {
                    return Err(fail("Source lacks verified host chat-loop pause"));
                }
                r.operation()
            }
            view::Request::Dictation(_) => crate::dictation::OPERATION,
            view::Request::Mark(_) => crate::mark::OPERATION,
            view::Request::Context(r) => r.operation(),
            view::Request::Plugin(request) => {
                if !crate::plugin_reload::OPERATIONS
                    .iter()
                    .all(|op| self.operations.iter().any(|v| v == op))
                {
                    return Err(fail("Source lacks reviewed static description reload"));
                }
                request.operation()
            }
            view::Request::Hook(request) => request.operation(),
            view::Request::HookEdit(request) => {
                if crate::hook_edit::OPERATIONS
                    .iter()
                    .any(|op| !self.operations.iter().any(|v| v == op))
                {
                    return Err(fail("Source lacks verified lifecycle hook authoring"));
                }
                request.operation()
            }
            view::Request::ContextEdit(request) => {
                if request
                    .operations()
                    .iter()
                    .any(|op| !self.operations.iter().any(|v| v == op))
                {
                    return Err(fail("Python source lacks reviewed memory editing"));
                }
                request.operation()
            }
            view::Request::Snapshot => protocol::READ,
            view::Request::Page { .. } => protocol::PAGE,
            view::Request::Tail(_) => protocol::TAIL,
            view::Request::Calendar { .. } => protocol::CALENDAR,
            view::Request::PauseValidate => protocol::VALIDATE,
            view::Request::PauseApply(_) => protocol::APPLY,
            view::Request::StopValidate(_) => protocol::STOP_VALIDATE,
            view::Request::StopApply(_) => protocol::STOP_APPLY,
        };
        if matches!(
            &intent.request,
            view::Request::ControlValidate(_) | view::Request::ControlApply(_)
        ) && controls::OPERATIONS
            .iter()
            .any(|op| !self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks complete durable Work controls"));
        }
        if matches!(&intent.request, view::Request::Midturn(_))
            && [crate::midturn::REVIEW, crate::midturn::APPLY]
                .iter()
                .any(|op| !self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks reviewed composer queueing"));
        }
        if matches!(&intent.request, view::Request::DeferredApply(_))
            && !self.operations.iter().any(|v| v == deferred::READBACK)
        {
            return Err(fail("Runtime lacks deferred discard readback"));
        }
        if matches!(&intent.request, view::Request::Hook(_))
            && !self
                .operations
                .iter()
                .any(|v| v == "gui.hooks.native_readback")
        {
            return Err(fail("Source lacks verified lifecycle hook editing"));
        }
        if !self.operations.iter().any(|v| v == op)
            || matches!(&intent.request, view::Request::PauseApply(_))
                && !self.operations.iter().any(|v| v == protocol::READBACK)
            || matches!(&intent.request, view::Request::StopApply(_))
                && !self.operations.iter().any(|v| v == protocol::STOP_SETTLED)
        {
            return Err(fail(
                "Runtime lacks this Work operation; update Python source",
            ));
        }
        self.work.generation = intent.generation;
        let mut command = if matches!(&intent.request, view::Request::Snapshot) {
            self.invalidate_execution_review();
            self.work.snapshot = None;
            self.work.page = None;
            self.work.review = None;
            self.work.stop_review = None;
            self.work.deferred_review = None;
            self.work.control_review = None;
            self.work.context_review = None;
            identity.request()
        } else if matches!(
            &intent.request,
            view::Request::Image(_)
                | view::Request::Midturn(_)
                | view::Request::ChatPause(_)
                | view::Request::Dictation(_)
                | view::Request::Mark(_)
        ) {
            identity.request()
        } else {
            let snapshot = self
                .work
                .snapshot
                .as_ref()
                .filter(|s| s.identity == identity)
                .ok_or_else(|| fail("Refresh Work for the current conversation"))?;
            snapshot.request()
        };
        match &intent.request {
            view::Request::ManualDecision { .. } => unreachable!(),
            view::Request::ManualReview { section, row } => {
                let ledger = self
                    .execution_ledger()
                    .filter(|ledger| {
                        ledger.scheduled_transitions_active()
                            && ledger.capture().execution_receipt.is_some()
                    })
                    .ok_or_else(|| {
                        fail("Manual Run requires active scheduled and execution Work capabilities")
                    })?;
                let snapshot = self
                    .work
                    .snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Refresh Work before Run"))?;
                if snapshot.busy
                    || snapshot.paused
                    || snapshot.quitting
                    || row.raw["new_conversation"] != true
                    || !matches!(section, protocol::Section::Jobs | protocol::Section::Loops)
                    || !self.work.page.as_ref().is_some_and(|page| {
                        page.section == *section && page.rows.contains(row.as_ref())
                    })
                {
                    return Err(fail(
                        "Select the exact idle new-conversation job before Run",
                    ));
                }
                command = identity.request();
                command["version"] = json!(1);
                command["schedule_version"] = json!(1);
                command["instance"] = json!(ledger.capture().instance);
                command["inspection_instance"] = json!(snapshot.instance);
                command["snapshot"] = json!(snapshot.token);
                command["section"] = json!(section.key());
                command["target_id"] = json!(row.id);
                command["target_revision"] = json!(row.revision);
            }
            view::Request::ExecutionValidate(_) | view::Request::ExecutionApply(_) => {
                unreachable!()
            }
            view::Request::ControlValidate(edit) => {
                self.work.control_review = None;
                self.work.context_review = None;
                if self.work.snapshot.as_ref() != Some(&edit.snapshot) {
                    return Err(fail("Work edit changed captured snapshot"));
                }
                if !edit.action.is_goal()
                    && edit.action != controls::Action::JobCreate
                    && !self.work.page.as_ref().is_some_and(|p| {
                        p.rows.iter().any(|row| {
                            Some(&row.id) == edit.target_id.as_ref()
                                && Some(&row.revision) == edit.target_revision.as_ref()
                                && row.raw == edit.before
                        })
                    })
                {
                    return Err(fail("Select the exact captured job before editing"));
                }
                command = edit.request();
            }
            view::Request::ControlApply(review) => {
                let retained = self
                    .work
                    .control_review
                    .take()
                    .ok_or_else(|| fail("Review durable Work change again"))?;
                if &retained != review.as_ref()
                    || self.work.snapshot.as_ref() != Some(&retained.edit.snapshot)
                {
                    return Err(fail(
                        "Work change differs from retained review or current capture",
                    ));
                }
                command = retained.request();
            }
            view::Request::DeferredValidate => {
                self.work.deferred_review = None;
                let snapshot = self.work.snapshot.as_ref().expect("snapshot");
                let selected = snapshot
                    .deferred_job
                    .as_ref()
                    .ok_or_else(|| fail("Refresh Work and select a staged invocation"))?;
                command = selected.validate_request(
                    &crate::work_lifecycle::Identity {
                        session: identity.session.clone(),
                        agent: identity.agent.clone(),
                        backend: identity.backend.clone(),
                        model: identity.model.clone(),
                    },
                    &snapshot.instance,
                    &snapshot.token,
                )?;
            }
            view::Request::DeferredApply(review) => {
                let retained = self
                    .work
                    .deferred_review
                    .take()
                    .ok_or_else(|| fail("Review staged invocation discard again"))?;
                if &retained != review.as_ref()
                    || self.work.snapshot.as_ref().is_none_or(|s| {
                        s.token != retained.snapshot
                            || s.instance != retained.instance
                            || s.deferred_job.as_ref() != Some(&retained.selected)
                    })
                {
                    return Err(fail(
                        "Deferred discard differs from its exact review or current capture",
                    ));
                }
                command = retained.request();
            }
            view::Request::Plugin(request) => {
                use crate::plugin_reload::Request;
                match request.as_ref() {
                    Request::List => {
                        self.work.plugin_review = None;
                        self.work.plugin_names.clear();
                    }
                    Request::Review(n) => {
                        self.work.plugin_review = None;
                        if !self.work.plugin_names.contains(n) {
                            return Err(fail("Refresh and select a registered static description"));
                        }
                    }
                    Request::Apply(r) => {
                        if self.work.plugin_review.take().as_ref() != Some(r.as_ref()) {
                            return Err(fail("Review this exact plugin description again"));
                        }
                    }
                }
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::Hook(request) => {
                use crate::hook_toggle::Request;
                match request.as_ref() {
                    Request::List(_) => {
                        self.work.hook_test = None;
                        self.work.hook_list = None;
                        self.work.hook_review = None;
                    }
                    Request::Review {
                        scope,
                        revision,
                        row,
                    }
                    | Request::TestReview {
                        scope,
                        revision,
                        row,
                        ..
                    } => {
                        self.work.hook_test = None;
                        self.work.hook_review = None;
                        if !self.work.hook_list.as_ref().is_some_and(|l| {
                            &l.scope == scope && &l.revision == revision && l.rows.contains(row)
                        }) {
                            return Err(fail(
                                "Refresh and select the exact hook row before review",
                            ));
                        }
                    }
                    Request::Run(r) => {
                        if self.work.hook_test.take().as_ref() != Some(r) {
                            return Err(fail("Review this hook sample again"));
                        }
                    }
                    Request::Apply(r) => {
                        if self.work.hook_review.take().as_ref() != Some(r) {
                            return Err(fail("Review this exact hook change again"));
                        }
                    }
                }
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::HookEdit(request) => {
                use crate::hook_edit::Request;
                match request.as_ref() {
                    Request::Snapshot(_) => {
                        self.work.hook_edit_snapshot = None;
                        self.work.hook_edit_review = None;
                        self.work.hook_repair_review = None;
                    }
                    Request::Repair {
                        scope,
                        revision,
                        catalog,
                        ..
                    } => {
                        self.work.hook_repair_review = None;
                        if !self.work.hook_edit_snapshot.as_ref().is_some_and(|s| {
                            &s.scope == scope
                                && &s.revision == revision
                                && &s.catalog == catalog
                                && s.unreadable.is_some()
                        }) {
                            return Err(fail(
                                "Reload the unreadable hook document before reviewing its repair",
                            ));
                        }
                    }
                    Request::RepairApply(r) => {
                        if self.work.hook_repair_review.take().as_ref() != Some(r.as_ref()) {
                            return Err(fail("Review this exact hook repair again"));
                        }
                    }
                    Request::Review {
                        scope,
                        revision,
                        rows,
                        catalog,
                        before,
                        ..
                    } => {
                        self.work.hook_edit_review = None;
                        // The source stays authoritative on staleness; this only binds the request to
                        // the exact rows the person was shown.
                        if !self.work.hook_edit_snapshot.as_ref().is_some_and(|s| {
                            &s.scope == scope
                                && &s.revision == revision
                                && s.rows.len() == *rows
                                && &s.catalog == catalog
                                && before.as_ref().is_none_or(|b| s.rows.contains(b))
                        }) {
                            return Err(fail(
                                "Reload the hook scope and select the exact hook before review",
                            ));
                        }
                    }
                    Request::Apply(r) => {
                        if self.work.hook_edit_review.take().as_ref() != Some(r.as_ref()) {
                            return Err(fail("Review this exact hook change again"));
                        }
                    }
                }
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::ContextEdit(request) => {
                if let crate::context_edit::Request::Apply(r) = request.as_ref() {
                    if self.work.context_review.take().as_ref() != Some(r) {
                        return Err(fail("Review the current memory draft again"));
                    }
                } else {
                    self.work.context_review = None;
                }
                command = request.command(&identity, self.context_instance()?)?;
            }
            view::Request::ChatPause(request) => {
                if let crate::chat_pause::Request::Apply(r) = request.as_ref()
                    && self.work.pause_review.take().as_ref() != Some(r.as_ref())
                {
                    return Err(fail("Chat pause review is stale"));
                }
                let origin = if matches!(
                    request.as_ref(),
                    crate::chat_pause::Request::Review { active: true }
                ) {
                    Some(
                        self.known_work_origin()
                            .ok_or_else(|| fail("The foreground turn has no captured execution"))?,
                    )
                } else {
                    None
                };
                command = request.command(&identity, self.context_instance()?, origin.as_ref())?;
                self.work.pause_origin = origin;
            }
            view::Request::Midturn(request) => {
                if let crate::midturn::Request::Apply(r) = request.as_ref()
                    && self.work.queue_review.take().as_ref() != Some(r.as_ref())
                {
                    return Err(fail("Queue review is stale"));
                }
                let origin = self
                    .known_work_origin()
                    .ok_or_else(|| fail("The foreground turn has no captured execution"))?;
                command = request.command(&identity, self.context_instance()?, &origin)?;
                let (epoch, profile) = self
                    .work
                    .queue_policy
                    .ok_or_else(|| fail("Queueing needs confirmed runtime policy"))?;
                command["policy_epoch"] = json!(epoch);
                command["profile"] = json!(profile.as_str());
                self.work.queue_origin = Some(origin);
            }
            view::Request::Mark(request) => {
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::Dictation(request) => {
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::Image(request) => {
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::Context(request) => {
                command = request.command(&identity, self.context_instance()?);
            }
            view::Request::Snapshot => {}
            view::Request::Page { section, offset } => {
                let snapshot = self.work.snapshot.as_ref().expect("snapshot");
                if !snapshot.source(*section).available {
                    return Err(fail("Work source is unavailable"));
                }
                command["section"] = json!(section.key());
                command["offset"] = json!(offset);
                command["limit"] = json!(protocol::PAGE_SIZE);
            }
            view::Request::Tail(row) => {
                if !row.tail_available
                    || !self
                        .work
                        .page
                        .as_ref()
                        .is_some_and(|p| p.rows.contains(row))
                {
                    return Err(fail("Select a captured task with available output"));
                }
                command["task_id"] = json!(row.id);
                command["task_revision"] = json!(row.revision);
            }
            view::Request::Calendar {
                month,
                date,
                offset,
            } => {
                protocol::month_parts(month)?;
                command["month"] = json!(month);
                if let Some(date) = date {
                    command["date"] = json!(date);
                    command["offset"] = json!(offset);
                    command["limit"] = json!(protocol::PAGE_SIZE);
                } else if *offset != 0 {
                    return Err(fail("Calendar overview has no pagination"));
                }
            }
            view::Request::PauseValidate => {
                self.work.review = None;
                self.work.stop_review = None;
                if !self.work.snapshot.as_ref().expect("snapshot").can_pause {
                    return Err(fail(
                        "Future scheduled prompts cannot be paused in this state",
                    ));
                }
                command["action"] = json!("pause_schedules");
            }
            view::Request::PauseApply(review) => {
                let captured = self
                    .work
                    .review
                    .take()
                    .ok_or_else(|| fail("Review scheduler pause again"))?;
                if &captured != review.as_ref() {
                    return Err(fail("Scheduler pause differs from the reviewed action"));
                }
                command = captured.request();
            }
            view::Request::StopValidate(row) => {
                self.work.review = None;
                self.work.stop_review = None;
                if !row.can_stop()
                    || self.work.snapshot.as_ref().expect("snapshot").quitting
                    || !self.work.page.as_ref().is_some_and(|page| {
                        page.section == protocol::Section::Tasks && page.rows.contains(row)
                    })
                {
                    return Err(fail(
                        "Select the exact current owned running task before Stop",
                    ));
                }
                command["task_id"] = json!(row.id);
                command["task_revision"] = json!(row.revision);
            }
            view::Request::StopApply(review) => {
                let captured = self
                    .work
                    .stop_review
                    .take()
                    .ok_or_else(|| fail("Review Task Stop again"))?;
                if &captured != review.as_ref() {
                    return Err(fail("Task Stop differs from its source review"));
                }
                command = captured.request();
            }
        }
        command["type"] = json!(op);
        let mutating = match &intent.request {
            view::Request::Dictation(r) => !matches!(r.action, "review" | "status"),
            view::Request::Mark(r) => r.action != "status",
            view::Request::ChatPause(request) => {
                matches!(request.as_ref(), crate::chat_pause::Request::Apply(_))
            }
            view::Request::Midturn(request) => {
                matches!(request.as_ref(), crate::midturn::Request::Apply(_))
            }
            view::Request::Plugin(request) => {
                matches!(request.as_ref(), crate::plugin_reload::Request::Apply(_))
            }
            view::Request::Hook(request) => {
                matches!(request.as_ref(), crate::hook_toggle::Request::Apply(_))
            }
            view::Request::HookEdit(request) => request.applies(),
            view::Request::ContextEdit(request) => {
                matches!(request.as_ref(), crate::context_edit::Request::Apply(_))
            }
            view::Request::PauseApply(_)
            | view::Request::StopApply(_)
            | view::Request::DeferredApply(_)
            | view::Request::ControlApply(_) => true,
            _ => false,
        };
        let id = if mutating {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve Work request"))?;
        command["id"] = id.to_value();
        self.work.pending = Some(Pending {
            id,
            generation: intent.generation,
            request: intent.request,
            receipt: None,
            stop_receipt: None,
            deferred_receipt: None,
            deferred_verified: false,
            control_receipt: None,
            context_apply_id: None,
        });
        Ok(command)
    }
    pub(super) fn work_response(&mut self, frame: Value) -> io::Result<Step> {
        let mut pending = self.work.pending.take().expect("Work request");
        if frame["ok"] != true {
            let mut reason = text(&frame, "error", 4096)?.to_owned();
            if matches!(&pending.request, view::Request::ChatPause(_)) {
                self.work.pause_review = None;
                self.work.pause_origin = None;
                if matches!(&pending.request, view::Request::ChatPause(r) if matches!(r.as_ref(),crate::chat_pause::Request::Apply(_)))
                {
                    reason = format!("Chat pause outcome needs a fresh review. {reason}");
                }
            }
            if matches!(&pending.request, view::Request::Plugin(_)) {
                self.work.plugin_review = None;
                if pending.context_apply_id.is_some() {
                    reason = format!("Plugin metadata was published but readback failed. {reason}");
                }
            }
            if let view::Request::HookEdit(request) = &pending.request {
                self.work.hook_edit_review = None;
                self.work.hook_repair_review = None;
                if !matches!(
                    request.as_ref(),
                    crate::hook_edit::Request::Review { .. }
                        | crate::hook_edit::Request::Repair { .. }
                ) {
                    self.work.hook_edit_snapshot = None;
                }
                if pending.context_apply_id.is_some() {
                    reason = format!("Hook was published but separate readback failed: {reason}");
                }
            }
            if matches!(&pending.request, view::Request::Hook(_)) {
                self.work.hook_list = None;
                self.work.hook_test = None;
                self.work.hook_review = None;
                if pending.context_apply_id.is_some() {
                    reason = format!("Hook was published but separate readback failed: {reason}");
                }
            }
            self.work.review = None;
            self.work.stop_review = None;
            self.work.deferred_review = None;
            self.work.control_review = None;
            self.work.context_review = None;
            if matches!(pending.request, view::Request::ControlApply(_)) {
                return Err(fail(&format!(
                    "Durable Work save or readback failed; inspect saved state: {reason}"
                )));
            }
            if matches!(pending.request, view::Request::DeferredApply(_)) {
                return Err(fail(&format!(
                    "Deferred discard or readback failed; close and inspect: {reason}"
                )));
            }
            if matches!(pending.request, view::Request::StopApply(_)) {
                return Err(fail(&format!(
                    "Task Stop or retirement readback failed; close and inspect: {reason}"
                )));
            }
            if matches!(pending.request, view::Request::PauseApply(_)) {
                return Err(fail(&format!("Scheduler pause/readback failed: {reason}")));
            }
            return Ok(if self.closing {
                Step::Ignore
            } else {
                Step::Work(view::Update {
                    generation: pending.generation,
                    data: view::Data::Rejected(reason),
                })
            });
        }
        let value = &frame["result"];
        let data = match &pending.request {
            view::Request::ManualDecision { .. } => unreachable!(),
            view::Request::ManualReview { section, row } => {
                let snapshot = self
                    .work
                    .snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Manual review lost its Work inspection"))?;
                let ledger = self
                    .execution_ledger()
                    .ok_or_else(|| fail("Manual review lost its Work ledger"))?;
                let review = crate::scheduled_transition::ManualReview::parse(
                    value,
                    snapshot,
                    *section,
                    row,
                    &ledger.capture().instance,
                    ledger.capture().provider,
                    text(&frame, "id", 128)?,
                )?;
                self.work.manual_review = Some(review.clone());
                view::Data::ManualReview(Box::new(review))
            }
            view::Request::ChatPause(request) => {
                let identity = self.work_identity()?;
                let instance = self.context_instance()?;
                if matches!(request.as_ref(), crate::chat_pause::Request::Apply(_))
                    && pending.context_apply_id.is_none()
                {
                    let op = text(&frame, "id", 128)?.to_owned();
                    let mut command = request.readback(value, &identity, instance, &op)?;
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve chat pause readback"))?;
                    command["id"] = id.to_value();
                    pending.id = id;
                    pending.context_apply_id = Some(op);
                    self.work.pending = Some(pending);
                    return Ok(Step::Write(command));
                }
                let reply = request.reply(
                    value,
                    &identity,
                    instance,
                    self.work.pause_origin.as_ref(),
                    pending.context_apply_id.as_deref(),
                )?;
                self.work.pause_origin = None;
                self.work.pause_review = match &reply {
                    crate::chat_pause::Reply::Review(r) => Some(r.as_ref().clone()),
                    _ => None,
                };
                view::Data::ChatPause(Box::new(reply))
            }
            view::Request::Mark(request) => {
                let state = request.parse(
                    &self.work_identity()?,
                    self.context_instance()?,
                    value,
                    self.work.mark.as_ref(),
                )?;
                self.work.mark = if state.status == "closed" {
                    None
                } else {
                    Some(state.clone())
                };
                view::Data::Mark(Box::new(state))
            }
            view::Request::Dictation(request) => {
                let state = request.parse(
                    &self.work_identity()?,
                    self.context_instance()?,
                    value,
                    self.work.dictation.as_ref(),
                )?;
                self.work.dictation = if matches!(state.status.as_str(), "cancelled" | "applied") {
                    None
                } else {
                    Some(state.clone())
                };
                view::Data::Dictation(state)
            }
            view::Request::Midturn(request) => {
                let parent = self
                    .work
                    .queue_origin
                    .take()
                    .ok_or_else(|| fail("Queue review lost its bound foreground execution"))?;
                let policy = self
                    .work
                    .queue_policy
                    .take()
                    .ok_or_else(|| fail("Lost queued policy"))?;
                let provider = self
                    .execution_ledger()
                    .ok_or_else(|| fail("Queue lost its Work provider"))?
                    .capture()
                    .provider;
                let expected_native = match provider {
                    crate::work_lifecycle::Provider::Host => None,
                    crate::work_lifecycle::Provider::Codex => Some("codex"),
                    crate::work_lifecycle::Provider::Claude => Some("claude"),
                };
                if value["native"]["provider"].as_str() != expected_native {
                    return Err(fail(
                        "Queue native owner differs from its captured Work provider",
                    ));
                }
                let reply = request.parse(
                    &self.work_identity()?,
                    self.context_instance()?,
                    &parent,
                    text(&frame, "id", 128)?,
                    policy,
                    value,
                )?;
                match &reply {
                    crate::midturn::Reply::Review(r) => {
                        if let Some(ids) = &r.prior_inputs {
                            if !self.extended_midturn() {
                                return Err(fail("Source did not negotiate reviewed interruption"));
                            }
                            self.execution_ledger()
                                .ok_or_else(|| fail("Interrupt lost its Work ledger"))?
                                .verify_interrupt_order(ids)?;
                        }
                        self.work.queue_review = Some(r.as_ref().clone())
                    }
                    crate::midturn::Reply::Queued(r) => self
                        .execution_ledger()
                        .ok_or_else(|| fail("Queued text lost its Work ledger"))?
                        .verify_user_queue(r)?,
                }
                view::Data::Midturn(Box::new(reply))
            }
            view::Request::Image(crate::image::Request::Preview(review)) => {
                match crate::image::Thumbnail::parse(
                    value,
                    &self.work_identity()?,
                    self.context_instance()?,
                    review,
                ) {
                    Ok(thumbnail) => view::Data::ImagePreview(thumbnail),
                    Err(error) => view::Data::Rejected(error.to_string()),
                }
            }
            view::Request::Image(request) => match crate::image::parse(
                value,
                &self.work_identity()?,
                self.context_instance()?,
                request,
            ) {
                Ok(review) => view::Data::Image(review),
                Err(error) => view::Data::Rejected(error.to_string()),
            },
            view::Request::ExecutionValidate(_) | view::Request::ExecutionApply(_) => {
                unreachable!()
            }
            view::Request::Snapshot => {
                let snapshot = protocol::Snapshot::parse(value, &self.work_identity()?)?;
                self.require_deferred_snapshot(&snapshot)?;
                self.require_execution_snapshot(&snapshot)?;
                self.work.snapshot = Some(snapshot.clone());
                view::Data::Snapshot(Box::new(snapshot))
            }
            request => {
                let snapshot = self
                    .work
                    .snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Work capture was lost"))?;
                if snapshot.identity != self.work_identity()? {
                    return Err(fail("Work reply belongs to an old conversation"));
                }
                match request {
                    view::Request::Midturn(_)
                    | view::Request::ChatPause(_)
                    | view::Request::Dictation(_)
                    | view::Request::Mark(_) => unreachable!(),
                    view::Request::Plugin(request) => {
                        let instance = self.context_instance()?;
                        if matches!(request.as_ref(), crate::plugin_reload::Request::Apply(_))
                            && pending.context_apply_id.is_none()
                        {
                            let op = text(&frame, "id", 128)?.to_owned();
                            let mut command =
                                request.readback(value, &snapshot.identity, instance, &op)?;
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve plugin registry readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.context_apply_id = Some(op);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                        let reply = request.reply(
                            value,
                            &snapshot.identity,
                            instance,
                            pending.context_apply_id.as_deref(),
                        )?;
                        match &reply {
                            crate::plugin_reload::Reply::List(names) => {
                                self.work.plugin_names = names.clone()
                            }
                            crate::plugin_reload::Reply::Reviewed(review) => {
                                self.work.plugin_review = Some(review.clone())
                            }
                            crate::plugin_reload::Reply::Verified(_) => {
                                self.work.plugin_review = None
                            }
                        }
                        view::Data::Plugin(Box::new(reply))
                    }
                    view::Request::Hook(request) => {
                        let instance = self.context_instance()?;
                        if matches!(request.as_ref(), crate::hook_toggle::Request::Apply(_))
                            && pending.context_apply_id.is_none()
                        {
                            let op = text(&frame, "id", 128)?.to_string();
                            let mut command =
                                request.readback(value, &snapshot.identity, instance, &op)?;
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve hook readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.context_apply_id = Some(op);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                        let reply = request.reply(
                            value,
                            &snapshot.identity,
                            instance,
                            if matches!(request.as_ref(), crate::hook_toggle::Request::Run(_)) {
                                frame["id"].as_str()
                            } else {
                                pending.context_apply_id.as_deref()
                            },
                        )?;
                        match &reply {
                            crate::hook_toggle::Reply::List(l) => {
                                self.work.hook_list = Some(l.clone())
                            }
                            crate::hook_toggle::Reply::Review(r) => {
                                self.work.hook_review = Some(r.clone())
                            }
                            crate::hook_toggle::Reply::TestReview(r) => {
                                self.work.hook_test = Some(r.clone())
                            }
                            crate::hook_toggle::Reply::Tested(_) => self.work.hook_test = None,
                            crate::hook_toggle::Reply::Saved(_) => self.work.hook_list = None,
                        }
                        view::Data::Hook(Box::new(reply))
                    }
                    view::Request::HookEdit(request) => {
                        let instance = self.context_instance()?;
                        if request.applies() && pending.context_apply_id.is_none() {
                            let op = text(&frame, "id", 128)?.to_string();
                            let mut command =
                                request.readback(value, &snapshot.identity, instance, &op)?;
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve hook readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.context_apply_id = Some(op);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                        let reply = request.reply(
                            value,
                            &snapshot.identity,
                            instance,
                            pending.context_apply_id.as_deref(),
                        )?;
                        match &reply {
                            crate::hook_edit::Reply::Snapshot(s) => {
                                self.work.hook_edit_snapshot = Some(s.as_ref().clone())
                            }
                            crate::hook_edit::Reply::Review(r) => {
                                self.work.hook_edit_review = Some(r.as_ref().clone())
                            }
                            crate::hook_edit::Reply::RepairReview(r) => {
                                self.work.hook_repair_review = Some(r.as_ref().clone())
                            }
                            crate::hook_edit::Reply::Saved(_)
                            | crate::hook_edit::Reply::RepairSaved(_) => {
                                self.work.hook_edit_snapshot = None
                            }
                        }
                        view::Data::HookEdit(Box::new(reply))
                    }
                    view::Request::ContextEdit(request) => {
                        let instance = self.context_instance()?;
                        if matches!(request.as_ref(), crate::context_edit::Request::Apply(_))
                            && pending.context_apply_id.is_none()
                        {
                            let apply_id = text(&frame, "id", 128)?.to_string();
                            let mut command =
                                request.readback(value, &snapshot.identity, instance, &apply_id)?;
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve memory readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.context_apply_id = Some(apply_id);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                        let reply = request.reply(
                            value,
                            &snapshot.identity,
                            instance,
                            pending.context_apply_id.as_deref(),
                        )?;
                        if let crate::context_edit::Reply::Reviewed(review) = &reply {
                            self.work.context_review = Some(review.clone());
                        }
                        view::Data::ContextEdit(Box::new(reply))
                    }
                    view::Request::Image(_) => unreachable!(),
                    view::Request::Context(request) => match crate::context::Page::parse(
                        value,
                        &snapshot.identity,
                        self.context_instance()?,
                        request,
                    ) {
                        Ok(page) => view::Data::Context(Box::new(page)),
                        Err(error) => view::Data::Rejected(error.to_string()),
                    },
                    view::Request::ControlValidate(edit) => {
                        let review = controls::Review::parse(value, edit)?;
                        self.work.control_review = Some(review.clone());
                        view::Data::ControlReview(Box::new(review))
                    }
                    view::Request::ControlApply(review) => {
                        if let Some(receipt) = pending.control_receipt.as_ref() {
                            let readback = controls::Readback::parse(value, review, receipt)?;
                            self.invalidate_work_selection();
                            view::Data::ControlSaved {
                                action: review.edit.action,
                                row: readback.row,
                            }
                        } else {
                            let receipt = controls::Receipt::parse(value, review)?;
                            if self.closing {
                                return Ok(Step::Ignore);
                            }
                            let mut command = receipt.request(review);
                            command["type"] = json!(controls::READBACK);
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve durable Work readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.control_receipt = Some(receipt);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                    }
                    view::Request::DeferredValidate => {
                        let selected = snapshot
                            .deferred_job
                            .as_ref()
                            .ok_or_else(|| fail("Deferred Work evidence was lost"))?;
                        let identity = crate::work_lifecycle::Identity {
                            session: snapshot.identity.session.clone(),
                            agent: snapshot.identity.agent.clone(),
                            backend: snapshot.identity.backend.clone(),
                            model: snapshot.identity.model.clone(),
                        };
                        let review = deferred::Review::parse(
                            value,
                            &identity,
                            &snapshot.instance,
                            &snapshot.token,
                            selected,
                        )?;
                        self.work.deferred_review = Some(review.clone());
                        view::Data::DeferredReview(Box::new(review))
                    }
                    view::Request::DeferredApply(review) => {
                        if pending.deferred_verified {
                            let fresh = protocol::Snapshot::parse(value, &self.work_identity()?)?;
                            self.require_deferred_snapshot(&fresh)?;
                            if fresh.instance != review.instance || fresh.deferred_job.is_some() {
                                return Err(fail(
                                    "Discard readback retained staged invocation or changed epoch",
                                ));
                            }
                            self.work.snapshot = Some(fresh.clone());
                            self.work.page = None;
                            view::Data::DeferredDiscarded(Box::new(fresh))
                        } else {
                            let mut command =
                                if let Some(receipt) = pending.deferred_receipt.as_ref() {
                                    deferred::Readback::parse(value, receipt)?;
                                    pending.deferred_verified = true;
                                    let mut command = self.work_identity()?.request();
                                    command["type"] = json!(protocol::READ);
                                    command
                                } else {
                                    let operation = pending.id.to_value();
                                    let receipt = deferred::ResultReceipt::parse(
                                        value,
                                        review,
                                        operation.as_str().ok_or_else(|| {
                                            fail("Deferred operation is not text")
                                        })?,
                                    )?;
                                    match receipt.status {
                                        deferred::Status::Refused => {
                                            return Ok(Step::Work(view::Update {
                                                generation: pending.generation,
                                                data: view::Data::Rejected(receipt.reason.unwrap()),
                                            }));
                                        }
                                        deferred::Status::Uncertain => {
                                            return Err(fail(
                                                "Deferred discard is uncertain; close required",
                                            ));
                                        }
                                        deferred::Status::Discarded => {}
                                    }
                                    let command = receipt.readback_request()?;
                                    pending.deferred_receipt = Some(receipt);
                                    command
                                };
                            if self.closing {
                                return Ok(Step::Ignore);
                            }
                            let id = self.wire.register_read().map_err(|_| {
                                fail("Cannot reserve deferred discard verification")
                            })?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                    }
                    view::Request::Page { section, offset } => {
                        let page = protocol::Page::parse(
                            value,
                            snapshot,
                            *section,
                            *offset,
                            protocol::PAGE_SIZE,
                        )?;
                        self.work.page = Some(page.clone());
                        view::Data::Page(page)
                    }
                    view::Request::Tail(row) => {
                        view::Data::Tail(protocol::Tail::parse(value, snapshot, row)?)
                    }
                    view::Request::Calendar {
                        month,
                        date,
                        offset,
                    } => view::Data::Calendar(protocol::Calendar::parse(
                        value,
                        snapshot,
                        month,
                        date.as_deref(),
                        *offset,
                        protocol::PAGE_SIZE,
                    )?),
                    view::Request::PauseValidate => {
                        let review = protocol::PauseReview::parse(value, snapshot)?;
                        self.work.review = Some(review.clone());
                        view::Data::Review(Box::new(review))
                    }
                    view::Request::PauseApply(review) => {
                        if let Some(receipt) = &pending.receipt {
                            receipt.verify(value, review)?;
                            if !self.closing {
                                if let Some((_, paused)) = self.browser_authority.as_mut() {
                                    *paused = true;
                                }
                                if let Some(snapshot) = self.work.snapshot.as_mut() {
                                    snapshot.paused = true;
                                    snapshot.can_pause = false;
                                }
                            }
                            view::Data::Paused
                        } else {
                            let receipt = protocol::PauseReceipt::parse(value, review)?;
                            if self.closing {
                                return Ok(Step::Ignore);
                            }
                            let mut command = receipt.request(review);
                            command["type"] = json!(protocol::READBACK);
                            let id = self
                                .wire
                                .register_read()
                                .map_err(|_| fail("Cannot reserve scheduler pause readback"))?;
                            command["id"] = id.to_value();
                            pending.id = id;
                            pending.receipt = Some(receipt);
                            self.work.pending = Some(pending);
                            return Ok(Step::Write(command));
                        }
                    }
                    view::Request::StopValidate(row) => {
                        let review = protocol::StopReview::parse(value, snapshot, row)?;
                        self.work.stop_review = Some(review.clone());
                        view::Data::StopReview(Box::new(review))
                    }
                    view::Request::StopApply(review) => {
                        if let Some(receipt) = &pending.stop_receipt {
                            receipt.verify(value, review)?;
                            let row = receipt.row.clone();
                            if !self.closing {
                                self.work.snapshot = None;
                                self.work.page = None;
                            }
                            view::Data::Stopped(row)
                        } else {
                            match protocol::StopResult::parse(value, review)? {
                                protocol::StopResult::Refused(reason) => {
                                    view::Data::Rejected(reason)
                                }
                                protocol::StopResult::Uncertain(reason) => {
                                    return Err(fail(&format!(
                                        "Task Stop is uncertain; close and inspect: {reason}"
                                    )));
                                }
                                protocol::StopResult::Stopped(receipt) => {
                                    if self.closing {
                                        return Ok(Step::Ignore);
                                    }
                                    let mut command = receipt.request(review);
                                    command["type"] = json!(protocol::STOP_SETTLED);
                                    let id = self.wire.register_read().map_err(|_| {
                                        fail("Cannot reserve Task Stop retirement readback")
                                    })?;
                                    command["id"] = id.to_value();
                                    pending.id = id;
                                    pending.stop_receipt = Some(*receipt);
                                    self.work.pending = Some(pending);
                                    return Ok(Step::Write(command));
                                }
                            }
                        }
                    }
                    view::Request::Snapshot
                    | view::Request::ManualReview { .. }
                    | view::Request::ManualDecision { .. }
                    | view::Request::ExecutionValidate(_)
                    | view::Request::ExecutionApply(_) => unreachable!(),
                }
            }
        };
        if self.closing {
            self.invalidate_work_selection();
            Ok(Step::Ignore)
        } else {
            Ok(Step::Work(view::Update {
                generation: pending.generation,
                data,
            }))
        }
    }
}
