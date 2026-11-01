//! Work inspection, reviewed scheduler pause and exact owned task Stop.
use super::{CommandId, Management, Step, fail, text};
use crate::{work_protocol as protocol, work_view as view};
use serde_json::{Value, json};
use std::io;

#[derive(Default)]
pub(super) struct Control {
    pub pending: Option<Pending>,
    snapshot: Option<protocol::Snapshot>,
    page: Option<protocol::Page>,
    review: Option<protocol::PauseReview>,
    stop_review: Option<protocol::StopReview>,
    generation: u64,
}
pub(super) struct Pending {
    pub id: CommandId,
    generation: u64,
    request: view::Request,
    receipt: Option<protocol::PauseReceipt>,
    stop_receipt: Option<protocol::StopReceipt>,
}
impl Management {
    pub fn work_pending(&self) -> bool {
        self.work.pending.is_some()
    }
    fn work_identity(&self) -> io::Result<protocol::Identity> {
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
        if !self.ready() {
            return Err(fail(
                "Finish the current management operation before opening Work",
            ));
        }
        if intent.generation <= self.work.generation {
            return Err(fail("Stale Work request"));
        }
        let identity = self.work_identity()?;
        let op = match &intent.request {
            view::Request::Snapshot => protocol::READ,
            view::Request::Page { .. } => protocol::PAGE,
            view::Request::Tail(_) => protocol::TAIL,
            view::Request::Calendar { .. } => protocol::CALENDAR,
            view::Request::PauseValidate => protocol::VALIDATE,
            view::Request::PauseApply(_) => protocol::APPLY,
            view::Request::StopValidate(_) => protocol::STOP_VALIDATE,
            view::Request::StopApply(_) => protocol::STOP_APPLY,
        };
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
            self.work.snapshot = None;
            self.work.page = None;
            self.work.review = None;
            self.work.stop_review = None;
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
        let id = if matches!(
            &intent.request,
            view::Request::PauseApply(_) | view::Request::StopApply(_)
        ) {
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
        });
        Ok(command)
    }
    pub(super) fn work_response(&mut self, frame: Value) -> io::Result<Step> {
        let mut pending = self.work.pending.take().expect("Work request");
        if frame["ok"] != true {
            let reason = text(&frame, "error", 4096)?.to_owned();
            self.work.review = None;
            self.work.stop_review = None;
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
            view::Request::Snapshot => {
                let snapshot = protocol::Snapshot::parse(value, &self.work_identity()?)?;
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
                    view::Request::Snapshot => unreachable!(),
                }
            }
        };
        if self.closing {
            self.work.snapshot = None;
            self.work.page = None;
            self.work.review = None;
            self.work.stop_review = None;
            Ok(Step::Ignore)
        } else {
            Ok(Step::Work(view::Update {
                generation: pending.generation,
                data,
            }))
        }
    }
}
