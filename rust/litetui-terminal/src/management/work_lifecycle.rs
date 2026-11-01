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
    active: bool,
    rebind: bool,
    transition_closing: bool,
    pub pending: Option<Pending>,
    startup: Option<Box<Step>>,
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
        self.work_lifecycle.rebind
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
            if proof.is_some() || recapture.is_some() || self.work_lifecycle.transition_closing {
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
        let snapshot: Option<&Snapshot> = match &step {
            Step::Initialized(snapshot) | Step::Restored { snapshot, .. } => Some(snapshot),
            Step::Theme {
                startup: Some((snapshot, _)),
                ..
            } => Some(snapshot),
            Step::Control(super::Update::Choices(_))
            | Step::BackendChanged(_)
            | Step::Opened(_)
            | Step::ConversationChanged(_)
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
        let mut command = self.lifecycle_identity()?.request(lifecycle::CAPTURE);
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
            return Err(fail(
                "Work lifecycle request failed; ownership is uncertain",
            ));
        }
        let result = &frame["result"];
        match pending.phase {
            PendingPhase::Capture => {
                self.ensure_drained()?;
                let capture = lifecycle::Capture::parse(result, &self.lifecycle_identity()?)?;
                if self.work_lifecycle.rebind
                    && self
                        .work_lifecycle
                        .ledger
                        .as_ref()
                        .is_some_and(|old| old.capture().instance == capture.instance)
                {
                    return Err(fail("Work recapture reused the retired epoch"));
                }
                let seat = self
                    .work_lifecycle
                    .enabled
                    .expect("explicit lifecycle opt-in");
                if Profile::parse(&self.expected.as_ref().expect("startup").3)? != seat {
                    return Err(fail(
                        "Work lifecycle launch profile differs from verified startup",
                    ));
                }
                let mut command = capture.activation_request();
                self.work_lifecycle.ledger = Some(lifecycle::Ledger::new(capture, seat));
                if self.closing {
                    self.work_lifecycle.startup = None;
                    return Ok(Step::Ignore);
                }
                let id = self
                    .wire
                    .register_request()
                    .map_err(|_| fail("Cannot reserve Work activation"))?;
                command["id"] = id.to_value();
                self.work_lifecycle.pending = Some(Pending {
                    id,
                    phase: PendingPhase::Activate,
                });
                Ok(Step::Write(command))
            }
            PendingPhase::Activate => {
                self.ensure_drained()?;
                let ledger = self
                    .work_lifecycle
                    .ledger
                    .as_mut()
                    .expect("captured lifecycle");
                ledger.activate(result)?;
                self.work_lifecycle.active = true;
                self.work_lifecycle.rebind = false;
                self.work_lifecycle.transition_closing = false;
                let startup = self
                    .work_lifecycle
                    .startup
                    .take()
                    .ok_or_else(|| fail("Work activation lost deferred startup"))?;
                if self.closing {
                    ledger.begin_close();
                    Ok(Step::Ignore)
                } else {
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
        } else if value.is_null() {
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
