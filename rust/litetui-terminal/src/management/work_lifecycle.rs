//! Capture/activation, full-envelope routing and exact background lifetime receipts.
use super::{CommandId, Management, Snapshot, Step, fail};
use crate::{tool_mode::Profile, work_lifecycle as lifecycle};
use serde_json::Value;
use std::{collections::HashSet, io};

#[derive(Default)]
pub(super) struct Control {
    advertised: bool,
    enabled: Option<Profile>,
    supported: bool,
    scheduled: Option<crate::scheduled_transition::Capabilities>,
    execution: Option<crate::work_execution_controls::Capabilities>,
    expected_deferred: Option<lifecycle::DeferredJob>,
    active: bool,
    rebind: bool,
    transition_closing: bool,
    retired_shutdown: Option<Value>,
    pub pending: Option<Pending>,
    startup: Option<Box<Step>>,
    deferred_activation: Option<Value>,
    ledger: Option<lifecycle::Ledger>,
    operations: HashSet<String>,
    announced: Option<lifecycle::Origin>,
    reconciliation: Option<(Value, Box<lifecycle::Effect>)>,
    verified_reconciliation: Option<Box<lifecycle::Effect>>,
}
pub(super) struct Pending {
    pub id: CommandId,
    phase: PendingPhase,
}
enum PendingPhase {
    Capture,
    Activate,
    Settled(String),
}
impl Management {
    /// Opt in only after the supervisor can consume typed lifecycle effects.
    pub fn enable_work_lifecycle(&mut self, launch_profile: Profile) -> io::Result<()> {
        if self.phase != super::Phase::AwaitReady || self.work_lifecycle.enabled.is_some() {
            return Err(fail(
                "Enable Work lifecycle once, before management startup",
            ));
        }
        self.work_lifecycle.enabled = Some(launch_profile);
        Ok(())
    }
    pub(super) fn expect_scheduled_capture(
        &mut self,
        deferred: lifecycle::DeferredJob,
    ) -> io::Result<()> {
        if self
            .work_lifecycle
            .expected_deferred
            .replace(deferred)
            .is_some()
        {
            return Err(fail("Repeated scheduled capture expectation"));
        }
        Ok(())
    }
    pub(super) fn require_deferred_snapshot(
        &self,
        snapshot: &crate::work_protocol::Snapshot,
    ) -> io::Result<()> {
        if let Some(capabilities) = self.work_lifecycle.scheduled.as_ref() {
            snapshot.require_deferred(capabilities)?;
        }
        Ok(())
    }
    /// Last already-validated stream origin; the Turn decides whether it is active.
    pub fn known_work_origin(&self) -> Option<lifecycle::Origin> {
        let (execution, origin) = if let Some((execution, origin)) = self.origin.as_ref() {
            (execution.as_str(), origin)
        } else {
            self.compact.chain.as_ref()?.work_predecessor()?
        };
        Some(lifecycle::Origin {
            session: origin.session.clone(),
            operation: origin.operation.as_ref()?.as_str()?.to_owned(),
            hook: origin.hook.clone(),
            source: origin.source.clone()?,
            execution: execution.to_owned(),
        })
    }
    pub(super) fn execution_ledger(&self) -> Option<&lifecycle::Ledger> {
        self.work_lifecycle.ledger.as_ref()
    }
    pub(super) fn install_future_work_seat(
        &mut self,
        policy: &crate::runtime_controls::Policy,
    ) -> io::Result<()> {
        self.work_lifecycle
            .ledger
            .as_mut()
            .ok_or_else(|| fail("Runtime choice lost captured Work ledger"))?
            .install_runtime_policy(policy)
    }
    pub fn work_lifecycle_active(&self) -> bool {
        self.work_lifecycle.active
    }
    pub fn work_lifecycle_pending(&self) -> bool {
        self.work_lifecycle.pending.is_some()
    }
    pub(super) fn work_lifecycle_starting(&self) -> bool {
        self.work_lifecycle.startup.is_some() || self.work_lifecycle.rebind
    }
    /// The supervisor must distinguish an owned epoch barrier from runtime Quit.
    pub fn work_identity_transition_pending(&self) -> bool {
        if self.work_lifecycle.enabled.is_none() {
            return false;
        }
        self.schedule_identity_pending()
            || self.work_lifecycle.rebind
            || self.work_lifecycle.transition_closing
            || self.control.as_ref().is_some_and(|p| {
                p.applying && p.kind == super::Kind::Model && p.selection.as_deref() != self.model()
            })
            || self.backend.as_ref().is_some_and(|p| p.stage == 2)
            || self
                .browser
                .as_ref()
                .is_some_and(|p| matches!(p.request, super::BrowserRequest::Open { stage: 0, .. }))
            || self.conversation.as_ref().is_some_and(|p| {
                p.stage == 5 && p.action == crate::conversation_actions::Action::New
            })
    }
    pub(super) fn require_work_identity_rebind(&self) -> io::Result<()> {
        if self.work_lifecycle.enabled.is_some() && !self.work_lifecycle.rebind {
            return Err(fail("Changed identity lacks verified Work retirement"));
        }
        Ok(())
    }
    /// Called only after the matching mutation's ordinary receipt is validated.
    pub(super) fn work_transition_receipt(
        &mut self,
        value: &Value,
        changed: bool,
    ) -> io::Result<()> {
        let proof = value.get("work_shutdown");
        let recapture = value.get("work_recapture_required");
        if self.work_lifecycle.enabled.is_none() || !changed {
            if proof.is_some()
                || recapture.is_some()
                || self.work_lifecycle.transition_closing
                || [
                    "runtime_controls_shutdown",
                    "runtime_controls_historical_shutdown",
                    "work_execution_shutdown",
                ]
                .iter()
                .any(|key| value.get(*key).is_some())
            {
                return Err(fail("Unexpected Work identity retirement receipt"));
            }
            return Ok(());
        }
        if !self.work_lifecycle.active
            || self.work_lifecycle.rebind
            || !self.work_lifecycle.transition_closing
            || recapture != Some(&Value::Bool(true))
        {
            return Err(fail(
                "Identity mutation lacks the exact captured Work retirement barrier",
            ));
        }
        let ledger = self
            .work_lifecycle
            .ledger
            .as_mut()
            .expect("active lifecycle");
        ledger.begin_close();
        ledger.verify_shutdown(
            proof.ok_or_else(|| fail("Identity mutation omitted Work shutdown proof"))?,
        )?;
        self.verify_execution_shutdown(&value["work_execution_shutdown"])?;
        self.verify_runtime_shutdown(value)?;
        self.work_lifecycle.retired_shutdown = proof.cloned();
        self.work_lifecycle.active = false;
        self.work_lifecycle.rebind = true;
        self.work_lifecycle.announced = None;
        Ok(())
    }
    pub(super) fn work_lifecycle_advertisement(&mut self, ready: &Value) -> io::Result<()> {
        self.work_lifecycle.advertised = match ready.get("work_lifecycle_version") {
            None | Some(Value::Null) => false,
            Some(value) if value.as_u64() == Some(1) => true,
            _ => return Err(fail("Unsupported Work lifecycle version")),
        };
        Ok(())
    }
    pub(super) fn configure_work_lifecycle(&mut self, operations: &[String]) -> io::Result<()> {
        let count = [lifecycle::CAPTURE, lifecycle::ACTIVATE, lifecycle::SETTLED]
            .iter()
            .filter(|operation| operations.iter().any(|v| v == **operation))
            .count();
        if count != 3
            && (count != 0
                || self.work_lifecycle.advertised
                || self.work_lifecycle.enabled.is_some())
        {
            return Err(fail(
                "Work lifecycle negotiation omitted a required operation",
            ));
        }
        self.work_lifecycle.supported = count == 3;
        self.work_lifecycle.scheduled =
            crate::scheduled_transition::Capabilities::negotiate(operations)?;
        self.work_lifecycle.execution =
            crate::work_execution_controls::Capabilities::negotiate(operations)?;
        self.configure_runtime_controls(operations)?;
        Ok(())
    }
    fn lifecycle_identity(&self) -> io::Result<lifecycle::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Work lifecycle requires initialized runtime identity"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Work lifecycle requires owned agent identity"))?;
        Ok(lifecycle::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
        })
    }
    pub(super) fn work_startup_gate(&mut self, step: Step) -> io::Result<Step> {
        // An offline managed engine can inspect settings, but activating Work may
        // release deferred input. Keep the source activation barrier until the first
        // independently verified engine connection.
        if self
            .engine_instance
            .as_ref()
            .is_some_and(|(_, _, admission)| {
                *admission != crate::engine_controls::Admission::PromptReady
            })
        {
            return Ok(step);
        }
        let snapshot: Option<&Snapshot> = match &step {
            Step::Initialized(snapshot) | Step::Restored { snapshot, .. } => Some(snapshot),
            Step::Engine {
                update:
                    crate::engine_controls::Update::Settled {
                        admission: crate::engine_controls::Admission::PromptReady,
                        ..
                    },
                state,
                ..
            } => state.as_ref(),
            Step::Theme {
                startup: Some((snapshot, _)),
                ..
            } => Some(snapshot),
            Step::Control(super::Update::Choices(_))
            | Step::BackendChanged(_)
            | Step::Opened(_)
            | Step::ConversationChanged(_)
            | Step::ScheduledConversationChanged(_)
                if self.work_lifecycle.rebind =>
            {
                None
            }
            _ => return Ok(step),
        };
        if self.work_lifecycle.enabled.is_none()
            || !self.work_lifecycle.supported
            || self.work_lifecycle.active
        {
            return Ok(step);
        }
        if self.closing {
            return Ok(Step::Ignore);
        }
        if self.work_lifecycle.pending.is_some()
            || self.work_lifecycle.startup.is_some()
            || self.work_lifecycle.ledger.is_some() && !self.work_lifecycle.rebind
        {
            return Err(fail("Work lifecycle capture was already attempted"));
        }
        if snapshot.is_some_and(|snapshot| snapshot.held_restored_input.is_none()) {
            return Err(fail(
                "Work lifecycle requires explicit restored-input hold policy",
            ));
        }
        self.ensure_drained()?;
        let identity = self.lifecycle_identity()?;
        let mut command = match self.work_lifecycle.execution.as_ref() {
            Some(capabilities) => capabilities.capture_request(&identity),
            None => identity.request(lifecycle::CAPTURE),
        };
        self.runtime_capture_request(&mut command);
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve Work capture"))?;
        command["id"] = id.to_value();
        self.work_lifecycle.startup = Some(Box::new(step));
        self.work_lifecycle.pending = Some(Pending {
            id,
            phase: PendingPhase::Capture,
        });
        Ok(Step::Write(command))
    }
    pub(super) fn work_lifecycle_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && self
                .work_lifecycle
                .pending
                .as_ref()
                .is_some_and(|p| frame["id"] == p.id.to_value())
    }
    pub(super) fn work_lifecycle_response(&mut self, frame: Value) -> io::Result<Step> {
        let pending = self
            .work_lifecycle
            .pending
            .take()
            .expect("Work lifecycle request");
        if frame["ok"] != true {
            if self.closing
                && matches!(
                    pending.phase,
                    PendingPhase::Capture | PendingPhase::Activate
                )
            {
                // A reserved capture/activation may be refused after Quit wins.
                // Retain the old verified epoch and final shutdown obligations;
                // never publish startup or schedule another activation here.
                self.work_lifecycle.startup = None;
                return Ok(Step::Ignore);
            }
            return Err(fail(
                "Work lifecycle request failed; ownership is uncertain",
            ));
        }
        let result = &frame["result"];
        match pending.phase {
            PendingPhase::Capture => {
                self.ensure_drained()?;
                let base_capture = self.runtime_capture(result, &self.lifecycle_identity()?)?;
                let capture = lifecycle::Capture::parse_execution(
                    &base_capture,
                    &self.lifecycle_identity()?,
                    self.work_lifecycle.execution.as_ref(),
                )?;
                if self.retired_runtime_policy().is_some() {
                    let model_target = self.take_model_target_state();
                    let (state, reported) = match self.work_lifecycle.startup.as_deref() {
                        Some(Step::Opened(target)) => (&target.snapshot, &target.policy),
                        Some(Step::BackendChanged(target)) => (&target.snapshot, &target.policy),
                        Some(
                            Step::ScheduledConversationChanged(target)
                            | Step::ConversationChanged(target),
                        ) => (&target.snapshot, &target.policy),
                        Some(Step::Control(super::Update::Choices(_))) => model_target
                            .as_ref()
                            .map(|(state, reported)| (state, reported))
                            .ok_or_else(|| {
                                fail("Model recapture omitted independent target state")
                            })?,
                        _ => {
                            return Err(fail(
                                "Runtime control recapture lacks a verified identity transition",
                            ));
                        }
                    };
                    self.verify_target_runtime_capture(state, reported)?;
                }
                if let Some(expected) = self.work_lifecycle.expected_deferred.as_ref()
                    && (capture.deferred_jobs.as_slice() != std::slice::from_ref(expected)
                        || !capture.recovered.is_empty())
                {
                    return Err(fail(
                        "Scheduled capture changed the exact staged invocation or inherited input",
                    ));
                }
                if self.work_lifecycle.rebind
                    && self
                        .work_lifecycle
                        .ledger
                        .as_ref()
                        .is_some_and(|old| old.capture().instance == capture.instance)
                {
                    return Err(fail("Work recapture reused the retired epoch"));
                }
                let seat = self.runtime_capture_seat().unwrap_or_else(|| {
                    self.work_lifecycle
                        .enabled
                        .expect("explicit lifecycle opt-in")
                });
                if self.runtime_capture_seat().is_none()
                    && Profile::parse(&self.expected.as_ref().expect("startup").3)? != seat
                {
                    return Err(fail(
                        "Work lifecycle launch profile differs from verified startup",
                    ));
                }
                let command = match self.work_lifecycle.execution.as_ref() {
                    Some(capabilities) => capture.activation_request_execution(
                        self.work_lifecycle.scheduled.as_ref(),
                        capabilities,
                    ),
                    None => match self.work_lifecycle.scheduled.as_ref() {
                        Some(capabilities) => capture.activation_request_scheduled(capabilities),
                        None => capture.activation_request(),
                    },
                };
                self.execution_capture(&capture)?;
                let mut ledger = lifecycle::Ledger::new(capture, seat);
                if let Some(capabilities) = self.work_lifecycle.scheduled.as_ref() {
                    ledger.request_scheduled_transitions(capabilities)?;
                }
                self.work_lifecycle.ledger = Some(ledger);
                if self.closing {
                    self.work_lifecycle.startup = None;
                    return Ok(Step::Ignore);
                }
                if self.historical_runtime_pending() {
                    self.work_lifecycle.deferred_activation = Some(command);
                    return Ok(Step::Ignore);
                }
                self.work_activation_command(command).map(Step::Write)
            }
            PendingPhase::Activate => {
                self.ensure_drained()?;
                let base_activation = self.runtime_activation(result)?;
                let ledger = self
                    .work_lifecycle
                    .ledger
                    .as_mut()
                    .expect("captured lifecycle");
                ledger.activate(&base_activation)?;
                self.work_lifecycle.active = true;
                self.work_lifecycle.retired_shutdown = None;
                self.work_lifecycle.expected_deferred = None;
                self.work_lifecycle.rebind = false;
                self.work_lifecycle.transition_closing = false;
                let startup = self
                    .work_lifecycle
                    .startup
                    .take()
                    .ok_or_else(|| fail("Work activation lost deferred startup"))?;
                if self.closing {
                    ledger.begin_close();
                    self.execution_activated();
                    Ok(Step::Ignore)
                } else {
                    self.execution_activated();
                    Ok(*startup)
                }
            }
            PendingPhase::Settled(admission_id) => {
                if result["admission"]["admission_id"] != admission_id {
                    return Err(fail(
                        "Work settlement readback belongs to another admission",
                    ));
                }
                self.work_lifecycle
                    .ledger
                    .as_ref()
                    .expect("active lifecycle")
                    .verify_settled(result)?;
                if self.closing {
                    Ok(Step::Ignore)
                } else {
                    Ok(Step::WorkLifecycleVerified { admission_id })
                }
            }
        }
    }
    fn work_activation_command(&mut self, mut command: Value) -> io::Result<Value> {
        self.runtime_activation_request(&mut command);
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve Work activation"))?;
        command["id"] = id.to_value();
        self.work_lifecycle.pending = Some(Pending {
            id,
            phase: PendingPhase::Activate,
        });
        Ok(command)
    }
    pub fn take_deferred_work_activation(&mut self) -> io::Result<Option<Value>> {
        if self.closing || self.historical_runtime_pending() {
            return Ok(None);
        }
        let command = self.work_lifecycle.deferred_activation.take();
        command
            .map(|value| self.work_activation_command(value))
            .transpose()
    }
    pub(super) fn close_work_lifecycle(&mut self) {
        self.work_lifecycle.verified_reconciliation = None;
        if self.work_lifecycle.active
            && let Some(ledger) = &mut self.work_lifecycle.ledger
        {
            ledger.begin_close();
        }
    }
    pub(super) fn record_work_operation(&mut self, id: &str) -> io::Result<()> {
        if self.work_lifecycle.enabled.is_some()
            && (self.work_lifecycle.operations.len() >= 8192
                || !self.work_lifecycle.operations.insert(id.to_owned()))
        {
            return Err(fail(
                "Repeated or excessive user operation in captured Work lifetime",
            ));
        }
        self.work_lifecycle.announced = None;
        Ok(())
    }
    pub(super) fn work_lifecycle_event(
        &mut self,
        frame: &Value,
        authority: Option<Option<&lifecycle::CurrentExecution>>,
    ) -> io::Result<Step> {
        if !self.work_lifecycle.active {
            return Err(fail("Work event arrived before captured activation"));
        }
        if frame["event"]["type"] == "work_reconciled" {
            self.validate_work_reconciliation(frame)?;
        }
        let retiring_identity = self.work_identity_transition_pending();
        let identity = self.lifecycle_identity()?;
        let ledger = self
            .work_lifecycle
            .ledger
            .as_mut()
            .expect("active lifecycle");
        if ledger.capture().identity != identity {
            return Err(fail(
                "Work capture identity changed without verified retirement and rebind",
            ));
        }
        if frame["event"]["type"] == "work_admission" && authority.is_none() {
            return Err(fail("Work admission lacks actual Turn execution authority"));
        }
        let effect = ledger.observe(frame, authority.flatten())?;
        if matches!(&effect, lifecycle::Effect::Admission { .. }) && self.runtime_gate_held() {
            return Err(fail("Work admission overtook runtime control publication"));
        }
        if matches!(effect, lifecycle::Effect::Reconciled { .. }) {
            self.work_lifecycle.reconciliation = Some((frame["event"].clone(), Box::new(effect)));
            // A typed event alone is not the independent queue readback.
            return Ok(Step::Ignore);
        }
        if matches!(effect, lifecycle::Effect::Closing) && retiring_identity {
            self.work_lifecycle.transition_closing = true;
        }
        if let lifecycle::Effect::Admission { admission, .. } = &effect
            && admission.mode == lifecycle::Mode::NewTurn
        {
            if self.work_lifecycle.operations.len() >= 8192
                || !self
                    .work_lifecycle
                    .operations
                    .insert(admission.operation.clone())
            {
                return Err(fail(
                    "Source Work operation aliases an earlier user or source operation",
                ));
            }
            // Correlation only: the supervisor must independently adopt this typed effect.
            // Do not gate captured admission on a concurrent read or mutate its cache.
            self.prompt = Some(admission.operation.clone());
            self.origin = None;
            self.work_lifecycle.announced = Some(admission.origin.clone());
        }
        if let lifecycle::Effect::ScheduledProposal { proposal } = effect {
            return self.propose_schedule(*proposal);
        }
        Ok(Step::WorkLifecycle(Box::new(effect)))
    }
    fn validate_work_reconciliation(&self, frame: &Value) -> io::Result<()> {
        let pending = self
            .queue
            .as_ref()
            .filter(|p| p.applying)
            .ok_or_else(|| fail("Unsolicited Work history reconciliation"))?;
        let selected = &pending
            .reconciled
            .as_ref()
            .ok_or_else(|| fail("Work history receipt lacks explicit reconciliation"))?
            .entry;
        let proof = &frame["event"]["reconciliation"];
        if self.work_lifecycle.reconciliation.is_some()
            || self.work_lifecycle.verified_reconciliation.is_some()
            || proof["operation_id"] != pending.id.to_value()
            || proof["provider"] != selected.provider
            || proof["delivery_id"] != selected.id
            || proof["thread_id"] != selected.thread
            || proof["turn_id"] != selected.turn
        {
            return Err(fail(
                "Work history receipt differs from selected reconciliation",
            ));
        }
        Ok(())
    }
    pub(super) fn work_reconciliation_echo(
        &self,
        result: &Value,
        accepted: Option<bool>,
    ) -> io::Result<()> {
        match (
            &self.work_lifecycle.reconciliation,
            result.get("work_reconciliation"),
        ) {
            (Some((event, _)), Some(echo)) if accepted == Some(true) && event == echo => Ok(()),
            (None, None) => Ok(()),
            _ => Err(fail(
                "Work reconciliation response differs from its exact event",
            )),
        }
    }
    pub(super) fn mark_work_reconciliation_readback(&self, command: &mut Value) {
        if let Some((event, _)) = &self.work_lifecycle.reconciliation {
            command["work_reconciliation"] = serde_json::json!({
                "instance":event["instance"], "input_id":event["input"]["input_id"],
                "operation_id":event["reconciliation"]["operation_id"],
                "delivery_id":event["reconciliation"]["delivery_id"]
            });
        }
    }
    pub(super) fn verify_work_reconciliation_readback(&self, result: &Value) -> io::Result<()> {
        let Some((event, _)) = &self.work_lifecycle.reconciliation else {
            return if result.get("work_reconciliation_readback").is_none() {
                Ok(())
            } else {
                Err(fail("Unsolicited Work reconciliation readback"))
            };
        };
        let echo = &result["work_reconciliation_readback"];
        let ledger = echo["ledger_sha256"]
            .as_str()
            .filter(|hash| {
                hash.len() == 64
                    && hash
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            .ok_or_else(|| fail("Work reconciliation readback omitted durable ledger hash"))?;
        let proof = &event["reconciliation"];
        let expected = serde_json::json!({
            "kind":"work_reconciliation_readback", "version":1,
            "instance":event["instance"], "input_id":event["input"]["input_id"],
            "operation_id":proof["operation_id"], "delivery_id":proof["delivery_id"],
            "journal_instance":proof["journal_instance"], "journal_sha256":proof["journal_sha256"],
            "payload_sha256":proof["payload_sha256"], "history_sha256":event["history_sha256"],
            "event_ledger_sha256":event["ledger_sha256"], "ledger_sha256":ledger,
            "snapshot":result["snapshot"], "verified":true
        });
        if echo != &expected {
            return Err(fail(
                "Work reconciliation readback changed exact durable evidence",
            ));
        }
        Ok(())
    }
    pub(super) fn discard_work_reconciliation(&mut self) {
        self.work_lifecycle.reconciliation = None;
        self.work_lifecycle.verified_reconciliation = None;
    }
    pub(super) fn finish_work_reconciliation(&mut self, accepted: Option<bool>) -> io::Result<()> {
        if let Some((_, effect)) = self.work_lifecycle.reconciliation.take() {
            if accepted != Some(true) || self.work_lifecycle.verified_reconciliation.is_some() {
                return Err(fail(
                    "Work reconciliation lacks successful independent queue readback",
                ));
            }
            self.work_lifecycle.verified_reconciliation = Some(effect);
        }
        Ok(())
    }
    /// Published only after the selected entry is absent from an independent queue read.
    pub fn take_verified_work_reconciliation(&mut self) -> Option<lifecycle::Effect> {
        self.work_lifecycle
            .verified_reconciliation
            .take()
            .map(|effect| *effect)
    }
    pub(super) fn validate_work_stream_origin(&self, frame: &Value) -> io::Result<()> {
        if let Some(origin) = &self.work_lifecycle.announced {
            let expected = origin.value();
            for key in [
                "session_id",
                "operation_id",
                "hook_id",
                "source",
                "execution_id",
            ] {
                if frame.get(key) != expected.get(key) {
                    return Err(fail(
                        "Work stream differs from its preannounced execution origin",
                    ));
                }
            }
        }
        Ok(())
    }
    pub fn request_work_settled(&mut self, admission_id: &str) -> io::Result<Value> {
        if self.failed
            || self.closing
            || !self.work_lifecycle.active
            || self.work_lifecycle.pending.is_some()
        {
            return Err(fail("Work settlement read is unavailable"));
        }
        let mut command = self
            .work_lifecycle
            .ledger
            .as_ref()
            .expect("active lifecycle")
            .settled_request(admission_id)?;
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve Work settlement read"))?;
        command["id"] = id.to_value();
        self.work_lifecycle.pending = Some(Pending {
            id,
            phase: PendingPhase::Settled(admission_id.to_owned()),
        });
        Ok(command)
    }
    pub fn verify_work_shutdown(&mut self, value: &Value) -> io::Result<()> {
        let result = if self.work_lifecycle.active {
            self.work_lifecycle
                .ledger
                .as_mut()
                .expect("active lifecycle")
                .verify_shutdown(value)
        } else if value.is_null()
            || self.work_lifecycle.rebind
                && self.work_lifecycle.retired_shutdown.as_ref() == Some(value)
        {
            // Quit may have joined the old epoch before its transition response
            // arrived. Reuse only that exact already-verified retirement receipt.
            Ok(())
        } else {
            Err(fail("Uncaptured runtime supplied Work shutdown authority"))
        };
        if result.is_err() {
            self.failed = true;
        }
        result
    }
}
