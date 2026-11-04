//! One reviewed launch with independent publication and Stop obligations.
use super::{CommandId, Management, Step, fail};
use crate::{work_execution_controls as execution, work_lifecycle as lifecycle, work_view as view};
use serde_json::Value;
use std::io;

#[derive(Default)]
pub(super) struct Control {
    epoch: Option<Epoch>,
    validation: Option<Validation>,
    review: Option<execution::Review>,
    invocation: Option<Invocation>,
    retired_shutdown: Option<Value>,
}
struct Epoch {
    identity: lifecycle::Identity,
    instance: String,
    provider: lifecycle::Provider,
    active: bool,
}
struct Validation {
    id: CommandId,
    generation: u64,
    edit: execution::Edit,
}
struct Pending {
    id: CommandId,
    readback: bool,
}
struct Invocation {
    generation: u64,
    review: execution::Review,
    apply_id: String,
    launch: Option<Pending>,
    receipt: Option<execution::Receipt>,
    launch_readback: Option<execution::Readback>,
    stop: Option<Pending>,
    stop_id: Option<String>,
    stop_receipt: Option<execution::StopReceipt>,
    stop_readback: Option<execution::Readback>,
    released: bool,
    waiting_announced: bool,
    stop_handed_off: bool,
}
fn rpc_id(id: &CommandId) -> io::Result<String> {
    id.to_value()
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| fail("Execution RPC identity must be text"))
}
impl Management {
    pub(super) fn execution_review_pending(&self) -> bool {
        self.execution.validation.is_some()
    }
    pub(super) fn require_execution_snapshot(
        &self,
        snapshot: &crate::work_protocol::Snapshot,
    ) -> io::Result<()> {
        if self.execution.epoch.is_some() != snapshot.execution_receipt.is_some() {
            return Err(fail("Work snapshot changed negotiated execution evidence"));
        }
        Ok(())
    }
    pub(super) fn invalidate_execution_review(&mut self) {
        self.execution.review = None;
    }
    pub fn execution_launch_pending(&self) -> bool {
        self.execution.invocation.as_ref().is_some_and(|p| {
            p.launch.is_some() || p.launch_readback.is_some() && !p.released && p.stop_id.is_none()
        })
    }
    pub fn execution_stop_pending(&self) -> bool {
        self.execution
            .invocation
            .as_ref()
            .is_some_and(|p| p.stop.is_some())
    }
    pub(super) fn execution_barrier(&self) -> bool {
        self.execution.validation.is_some()
            || self
                .execution
                .invocation
                .as_ref()
                .is_some_and(|p| !p.released)
    }
    pub(super) fn execution_capture(&mut self, capture: &lifecycle::Capture) -> io::Result<()> {
        if capture.execution_receipt.is_none() {
            if self.execution.epoch.is_some() {
                return Err(fail("Execution capability disappeared during recapture"));
            }
            return Ok(());
        }
        if self.execution.epoch.is_some() && self.execution.retired_shutdown.is_none() {
            return Err(fail("Execution recapture lacks exact prior shutdown"));
        }
        self.execution.epoch = Some(Epoch {
            identity: capture.identity.clone(),
            instance: capture.instance.clone(),
            provider: capture.provider,
            active: false,
        });
        self.execution.validation = None;
        self.execution.review = None;
        self.execution.invocation = None;
        // Keep the exact old proof until this new epoch actually activates.
        Ok(())
    }
    pub(super) fn execution_activated(&mut self) {
        if let Some(epoch) = self.execution.epoch.as_mut() {
            epoch.active = true;
        }
        self.execution.retired_shutdown = None;
    }
    pub(super) fn execution_response_pending(&self, frame: &Value) -> bool {
        if !matches!(frame["type"].as_str(), Some("response" | "error")) {
            return false;
        }
        self.execution
            .validation
            .as_ref()
            .is_some_and(|p| frame["id"] == p.id.to_value())
            || self.execution.invocation.as_ref().is_some_and(|p| {
                [&p.launch, &p.stop]
                    .iter()
                    .any(|v| v.as_ref().is_some_and(|v| frame["id"] == v.id.to_value()))
            })
    }
    pub(super) fn request_execution(&mut self, intent: view::Intent) -> io::Result<Value> {
        if !self.ready() {
            return Err(fail(
                "Finish the current management operation before reviewing execution",
            ));
        }
        if intent.generation <= self.work.generation {
            return Err(fail("Stale Work execution request"));
        }
        let epoch = self
            .execution
            .epoch
            .as_ref()
            .filter(|e| e.active)
            .ok_or_else(|| fail("Runtime lacks activated reviewed Work execution"))?;
        let mut command = match &intent.request {
            view::Request::ExecutionValidate(edit) => {
                if self.work.snapshot.as_ref() != Some(&edit.snapshot) {
                    return Err(fail("Execution selection changed Work snapshot"));
                }
                if edit.action == execution::Action::JobRun
                    && !self.work.page.as_ref().is_some_and(|p| {
                        p.rows.iter().any(|r| {
                            Some(&r.id) == edit.selected_id.as_ref()
                                && r.revision == edit.target_revision
                                && r.raw == edit.before
                        })
                    })
                {
                    return Err(fail("Select the exact captured job before Run"));
                }
                edit.validate_request(&epoch.instance)?
            }
            view::Request::ExecutionApply(review) => {
                let retained = self
                    .execution
                    .review
                    .take()
                    .ok_or_else(|| fail("Review execution again before confirming"))?;
                if &retained != review.as_ref()
                    || self.work.snapshot.as_ref() != Some(&retained.edit.snapshot)
                {
                    return Err(fail("Execution differs from its exact retained review"));
                }
                retained.request()
            }
            _ => return Err(fail("Not an explicit execution request")),
        };
        let applying = matches!(&intent.request, view::Request::ExecutionApply(_));
        let id = if applying {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve execution request"))?;
        command["id"] = id.to_value();
        self.work.generation = intent.generation;
        match intent.request {
            view::Request::ExecutionValidate(edit) => {
                self.execution.review = None;
                self.execution.validation = Some(Validation {
                    id,
                    generation: intent.generation,
                    edit: *edit,
                });
            }
            view::Request::ExecutionApply(review) => {
                let apply_id = rpc_id(&id)?;
                self.execution.invocation = Some(Invocation {
                    generation: intent.generation,
                    review: *review,
                    apply_id,
                    launch: Some(Pending {
                        id,
                        readback: false,
                    }),
                    receipt: None,
                    launch_readback: None,
                    stop: None,
                    stop_id: None,
                    stop_receipt: None,
                    stop_readback: None,
                    released: false,
                    waiting_announced: false,
                    stop_handed_off: false,
                });
            }
            _ => unreachable!(),
        }
        Ok(command)
    }
    pub fn request_execution_stop(&mut self, action_id: &str) -> io::Result<Value> {
        if self.closing {
            return Err(fail("Runtime is closing; no new Stop request"));
        }
        let p = self
            .execution
            .invocation
            .as_ref()
            .filter(|p| p.review.action_id == action_id)
            .ok_or_else(|| fail("Stop has no matching pending invocation"))?;
        if p.stop_id.is_some() {
            return Err(fail(
                "This invocation already has its exact Stop obligation",
            ));
        }
        let mut command = p.review.stop_request(&p.apply_id)?;
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve invocation Stop"))?;
        command["id"] = id.to_value();
        let p = self.execution.invocation.as_mut().expect("invocation");
        p.released = false;
        p.stop_id = Some(rpc_id(&id)?);
        p.stop = Some(Pending {
            id,
            readback: false,
        });
        Ok(command)
    }
    pub(super) fn execution_response(&mut self, frame: Value) -> io::Result<Step> {
        if self
            .execution
            .validation
            .as_ref()
            .is_some_and(|p| frame["id"] == p.id.to_value())
        {
            let p = self.execution.validation.take().expect("validation");
            if self.closing {
                return Ok(Step::Ignore);
            }
            let data = if frame["ok"] == true {
                let epoch = self
                    .execution
                    .epoch
                    .as_ref()
                    .filter(|e| e.active)
                    .ok_or_else(|| fail("Execution review lost its epoch"))?;
                let review = execution::Review::parse(
                    &frame["result"],
                    &p.edit,
                    &epoch.instance,
                    epoch.provider,
                )?;
                self.execution.review = Some(review.clone());
                view::Data::ExecutionReview(Box::new(review))
            } else {
                view::Data::Rejected(super::text(&frame, "error", 4096)?.into())
            };
            return Ok(Step::Work(view::Update {
                generation: p.generation,
                data,
            }));
        }
        let mut p = self
            .execution
            .invocation
            .take()
            .expect("matched invocation");
        let result = self.execution_response_inner(&mut p, frame);
        self.execution.invocation = Some(p);
        result
    }
    fn execution_response_inner(&mut self, p: &mut Invocation, frame: Value) -> io::Result<Step> {
        let is_stop = p
            .stop
            .as_ref()
            .is_some_and(|v| frame["id"] == v.id.to_value());
        let pending = if is_stop {
            p.stop.take()
        } else {
            p.launch.take()
        }
        .ok_or_else(|| fail("Execution reply has no exact pending scope"))?;
        if frame["ok"] != true {
            if self.closing {
                return Ok(Step::Ignore);
            }
            return Err(fail(&format!(
                "Execution or scoped readback failed; no retry: {}",
                super::text(&frame, "error", 4096)?
            )));
        }
        let value = &frame["result"];
        if !pending.readback {
            let mut command = if is_stop {
                let receipt = execution::StopReceipt::parse(
                    value,
                    &p.review,
                    &p.apply_id,
                    p.stop_id.as_deref().expect("Stop RPC"),
                )?;
                let command = receipt.readback_request()?;
                p.stop_receipt = Some(receipt);
                command
            } else {
                let receipt = execution::Receipt::parse(value, &p.review, &p.apply_id)?;
                if let Some(document) = &receipt.document {
                    let ledger = self
                        .execution_ledger()
                        .ok_or_else(|| fail("Execution result lost Work ledger"))?;
                    ledger.verify_execution_document(document)?;
                    ledger.verify_execution_lifecycle(document, &receipt.lifecycle)?;
                }
                let command = receipt.readback_request()?;
                p.receipt = Some(receipt);
                command
            };
            if value["close_required"] == true && !self.closing {
                return Err(fail("Execution effects are uncertain; close without retry"));
            }
            if self.closing {
                return Ok(Step::Ignore);
            }
            let id = self
                .wire
                .register_read()
                .map_err(|_| fail("Cannot reserve execution readback"))?;
            command["id"] = id.to_value();
            let next = Some(Pending { id, readback: true });
            if is_stop {
                p.stop = next;
            } else {
                p.launch = next;
            }
            return Ok(Step::Write(command));
        }
        let readback = if is_stop {
            execution::Readback::parse_stop(
                value,
                &p.review,
                p.stop_receipt.as_ref().expect("Stop result"),
            )?
        } else {
            execution::Readback::parse_launch(value, p.receipt.as_ref().expect("launch result"))?
        };
        if let Some(document) = &readback.document {
            let ledger = self
                .execution_ledger()
                .ok_or_else(|| fail("Execution readback lost Work ledger"))?;
            ledger.verify_execution_document(document)?;
            ledger.verify_execution_lifecycle(document, &readback.lifecycle)?;
        }
        if is_stop {
            p.stop_readback = Some(readback);
        } else {
            p.launch_readback = Some(readback);
        }
        self.finish_execution_if_ready(p)
    }
    fn finish_execution_if_ready(&mut self, p: &mut Invocation) -> io::Result<Step> {
        if self.closing || p.released {
            return Ok(Step::Ignore);
        }
        // A verified admitted Stop owns cancellation immediately. The independent
        // launch publication may still be pending and must not delay actual abort.
        let stop = if !p.stop_handed_off
            && p.stop_receipt
                .as_ref()
                .is_some_and(|t| t.status == "admitted")
        {
            p.stop_readback
                .as_ref()
                .and_then(|r| r.lifecycle.admission.clone())
        } else {
            None
        };
        if stop.is_some() {
            p.stop_handed_off = true;
        }
        if p.launch_readback.is_none() || p.stop_id.is_some() && p.stop_readback.is_none() {
            return Ok(match stop {
                Some(admission) => Step::WorkLaunchStop {
                    action_id: p.review.action_id.clone(),
                    generation: p.generation,
                    admission: Box::new(admission),
                },
                None => Step::Ignore,
            });
        }
        if p.stop_id.is_none()
            && let Some(document) = p.receipt.as_ref().and_then(|r| r.document.as_ref())
        {
            let ledger = self
                .execution_ledger()
                .ok_or_else(|| fail("Pending launch lost its Work ledger"))?;
            if ledger.execution_admission(document)?.is_none() {
                if p.waiting_announced {
                    return Ok(Step::Ignore);
                }
                p.waiting_announced = true;
                return Ok(Step::WorkLaunchPending {action_id:p.review.action_id.clone(), generation:p.generation,
                    reason:"Launch recorded; waiting for source admission. Esc reviews Stop; nothing is replayed.".into()});
            }
        }
        p.released = true;
        let receipt = p.receipt.as_ref().expect("launch result");
        let status = receipt.wire["status"]
            .as_str()
            .expect("parsed status")
            .to_owned();
        let mut reason = receipt.wire["reason"].as_str().map(str::to_owned);
        if let Some(t) = &p.stop_receipt {
            reason = Some(format!(
                "Stop verified: {}. {}",
                t.wire["disposition"].as_str().expect("parsed disposition"),
                reason.as_deref().unwrap_or("")
            ));
        }
        Ok(Step::WorkLaunch {
            action_id: p.review.action_id.clone(),
            update: view::Update {
                generation: p.generation,
                data: view::Data::ExecutionVerified {
                    action: p.review.action,
                    status,
                    reason,
                },
            },
            stop,
            stop_verified: p.stop_readback.is_some(),
        })
    }
    pub fn execution_after_work(&mut self) -> io::Result<Step> {
        let Some(mut p) = self.execution.invocation.take() else {
            return Ok(Step::Ignore);
        };
        let result = self.finish_execution_if_ready(&mut p);
        self.execution.invocation = Some(p);
        result
    }
    pub fn verify_execution_shutdown(&mut self, value: &Value) -> io::Result<()> {
        let Some(epoch) = self.execution.epoch.as_ref() else {
            return if value.is_null() {
                Ok(())
            } else {
                Err(fail("Unexpected execution shutdown proof"))
            };
        };
        if !epoch.active {
            if self.execution.retired_shutdown.as_ref() == Some(value) {
                return Ok(());
            }
            // Capture alone is inert. Quit may win before activation was sent
            // or published, in which case the source owns no execution epoch.
            if self.closing
                && self.execution.retired_shutdown.is_none()
                && self.execution.invocation.is_none()
                && value.is_null()
            {
                return Ok(());
            }
            return Err(fail("Execution shutdown changed an unactivated epoch"));
        }
        let proof = execution::Shutdown::parse(value)?;
        if let Some(p) = self.execution.invocation.as_ref() {
            let stops = p.stop_receipt.iter().cloned().collect::<Vec<_>>();
            proof.verify_invocation(
                &p.review,
                &p.apply_id,
                p.receipt.as_ref(),
                &stops,
                p.stop_id.as_deref(),
            )?;
            if p.launch_readback.is_some() && value["launch_readback_published"] != true {
                return Err(fail(
                    "Execution shutdown erased observed launch readback publication",
                ));
            }
            if let Some(readback) = &p.stop_readback {
                let id = &readback.wire["stop_id"];
                if !value["stop_readbacks_published"]
                    .as_array()
                    .is_some_and(|ids| ids.contains(id))
                {
                    return Err(fail(
                        "Execution shutdown erased observed Stop readback publication",
                    ));
                }
            }
        } else {
            proof.verify_idle(&epoch.identity, &epoch.instance)?;
        }
        if epoch.active {
            let ledger = self
                .execution_ledger()
                .ok_or_else(|| fail("Active execution epoch lost Work ledger"))?;
            ledger.verify_execution_shutdown(
                &proof,
                self.execution.invocation.as_ref().map(|p| &p.review),
            )?;
        }
        self.execution.retired_shutdown = Some(value.clone());
        if let Some(epoch) = self.execution.epoch.as_mut() {
            epoch.active = false;
        }
        Ok(())
    }
}
