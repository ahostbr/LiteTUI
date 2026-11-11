//! Exact source admission and retirement, separate from a stream's terminal token.
use super::{Observation, TerminalOutcome, Turn};
use crate::work_lifecycle::{self as life, Admission, Input, Mode, Payload};
use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    io,
};

// Observation boxes this complete update; boxing individual admission fields
// would add allocations without reducing the queued observation size.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Admitted {
        input: Input,
        admission: Admission,
        payload: Payload,
        suspended_submission: Option<String>,
    },
    Ended {
        operation: String,
        outcome: TerminalOutcome,
    },
    Retired {
        admission_id: String,
    },
    Settled {
        operation: String,
        outcome: TerminalOutcome,
    },
    LocalRefused {
        operation: String,
        reason: String,
    },
    LocalNotSent {
        reason: String,
    },
    NotStarted {
        operation: String,
        execution: String,
        outcome: TerminalOutcome,
    },
    Uncertain {
        input_id: String,
        reason: String,
    },
    Reconciled {
        input_id: String,
        recovered: bool,
    },
}
struct Pending {
    admission: Admission,
    ended: bool,
    stream_ended: bool,
    outcome: Option<String>,
}
#[derive(Default)]
pub(super) struct State {
    pending: BTreeMap<String, Pending>,
    pub(super) ended: BTreeMap<String, TerminalOutcome>,
    receipts: VecDeque<String>,
    operations: HashSet<String>,
    suspended: Option<String>,
    pub uncertain: bool,
    uncertain_inputs: HashSet<String>,
}
impl State {
    pub fn owns_work(&self) -> bool {
        !self.pending.is_empty() || self.suspended.is_some()
    }
    pub fn pending_operation(&self, operation: &str) -> bool {
        self.pending
            .values()
            .any(|p| p.admission.operation == operation)
    }
    pub fn reserve_operation(&mut self, operation: &str) -> io::Result<()> {
        if self.operations.len() >= 8192 || !self.operations.insert(operation.into()) {
            return Err(io::Error::other(
                "Repeated or excessive native operation identity",
            ));
        }
        Ok(())
    }
}
impl Turn {
    pub(super) fn work_current(
        &self,
        known: Option<life::Origin>,
    ) -> Option<life::CurrentExecution> {
        if self.rewrite_active {
            let action = self.action_identity().ok()?.0.to_owned();
            let origin = known?;
            if let Some(review) = &self.compact_input
                && self.rewrite_segments.is_empty()
                && (!self.compact_root_ended
                    || origin.execution != review.root_execution_id
                    || origin.operation != self.command_id)
            {
                return None;
            }
            Some(life::CurrentExecution {
                origin,
                action_id: Some(action),
            })
        } else if self.started && self.terminal.is_none() && !self.settled {
            Some(life::CurrentExecution {
                origin: known?,
                action_id: None,
            })
        } else {
            None
        }
    }
    pub(super) fn work_waits(&self) -> HashSet<String> {
        let mut ids: HashSet<String> = self
            .work
            .ended
            .keys()
            .map(|op| format!("operation:{op}"))
            .collect();
        ids.extend(
            self.work
                .pending
                .iter()
                .filter(|(_, p)| p.ended || p.stream_ended)
                .map(|(id, _)| format!("admission:{id}")),
        );
        ids.extend(
            self.work
                .pending
                .values()
                .filter(|p| !p.ended && !p.stream_ended)
                .map(|p| {
                    if p.admission.mode == Mode::ActionSegment {
                        format!("stream:{}", p.admission.origin.execution)
                    } else {
                        format!("stream:{}", p.admission.operation)
                    }
                }),
        );
        ids.extend(self.work.suspended.iter().map(|id| format!("local:{id}")));
        ids
    }
    pub(super) fn take_work_receipt(&mut self) -> Option<String> {
        self.work.receipts.pop_front()
    }
    pub(super) fn work_stream_ended(&mut self) {
        let execution = self
            .rewrite_active
            .then(|| self.rewrite_segments.last())
            .flatten();
        for pending in self.work.pending.values_mut() {
            if pending.admission.operation == self.command_id
                && (!self.rewrite_active || execution == Some(&pending.admission.origin.execution))
            {
                pending.stream_ended = true;
            }
        }
    }
    pub(super) fn work_observed_deadline(
        &self,
        observations: &[Observation],
        deadline: &mut Option<std::time::Instant>,
    ) {
        if self.closing || self.rewrite_active {
            return;
        }
        if observations.iter().any(|observation| match observation {
            Observation::WorkExecution(update) => matches!(update.as_ref(),
                Update::Ended {operation,..} | Update::Settled {operation,..} if operation == &self.command_id),
            _ => false,
        }) { *deadline = None; }
    }
    pub(super) fn work_terminal(&mut self) -> Option<Observation> {
        if self.rewrite_active || !self.work.pending_operation(&self.command_id) {
            return None;
        }
        let outcome = self.terminal.as_ref()?.clone();
        if self
            .work
            .ended
            .insert(self.command_id.clone(), outcome.clone())
            .is_some()
        {
            return None;
        }
        Some(Observation::WorkExecution(Box::new(Update::Ended {
            operation: self.command_id.clone(),
            outcome,
        })))
    }
    pub(super) fn accept_work(&mut self, effect: life::Effect) -> io::Result<Vec<Observation>> {
        let mut observations = Vec::new();
        let update = match effect {
            life::Effect::Admission {
                input,
                admission,
                payload,
                drain_only,
            } => {
                if self.work.pending.len() >= 256
                    || self.work.pending.contains_key(&admission.id)
                    || (self.work.uncertain && !drain_only)
                    || (self.closing && !drain_only)
                {
                    return Err(io::Error::other("Unowned or repeated Work admission"));
                }
                let future_seat = self
                    .runtime_policy
                    .as_ref()
                    .map(|policy| policy.facts.seat_profile)
                    .or_else(|| self.policy.as_ref().map(|launch| launch.profile));
                if Some(admission.seat) != future_seat {
                    return Err(io::Error::other(
                        "Work admission differs from the verified seat authority",
                    ));
                }
                let mut suspended_submission = None;
                match admission.mode {
                    Mode::NewTurn => {
                        if !self.ready
                            || self.rewrite_active
                            || !self.approvals.settled()
                            || !self.questions.settled()
                            || !self.aborts.settled()
                        {
                            return Err(io::Error::other(
                                "Source turn arrived before prior input settlement",
                            ));
                        }
                        if self.sent && !self.settled {
                            if self.terminal.is_some() && self.acknowledged {
                                if let std::collections::btree_map::Entry::Vacant(entry) =
                                    self.work.ended.entry(self.command_id.clone())
                                {
                                    let outcome = self.terminal.clone().unwrap();
                                    entry.insert(outcome.clone());
                                    observations.push(Observation::WorkExecution(Box::new(
                                        Update::Ended {
                                            operation: self.command_id.clone(),
                                            outcome,
                                        },
                                    )));
                                }
                            } else if !self.started
                                && !self.acknowledged
                                && self.terminal.is_none()
                                && self.work.suspended.is_none()
                            {
                                self.work.suspended = Some(self.command_id.clone());
                                suspended_submission = self.work.suspended.clone();
                            } else {
                                return Err(io::Error::other(
                                    "Source turn conflicts with an active execution",
                                ));
                            }
                        }
                        self.reset_operation(admission.operation.clone())?;
                        self.acknowledged = true; // Typed source admission is the acknowledgement.
                    }
                    Mode::InTurn => {
                        if !self.sent
                            || !self.started
                            || self.terminal.is_some()
                            || self.settled
                            || admission.operation != self.command_id
                        {
                            return Err(io::Error::other(
                                "Work boundary has no current native execution",
                            ));
                        }
                    }
                    Mode::ActionSegment => {
                        if !self.rewrite_active
                            || admission.operation != self.command_id
                            || self.action_identity().is_err()
                        {
                            return Err(io::Error::other(
                                "Work segment has no reviewed native action",
                            ));
                        }
                    }
                }
                self.work.pending.insert(
                    admission.id.clone(),
                    Pending {
                        admission: admission.clone(),
                        ended: false,
                        stream_ended: false,
                        outcome: None,
                    },
                );
                Update::Admitted {
                    input,
                    admission,
                    payload,
                    suspended_submission,
                }
            }
            life::Effect::Settled {
                input,
                admission,
                outcome,
            } => {
                let pending = self
                    .work
                    .pending
                    .get_mut(&admission.id)
                    .filter(|p| p.admission == admission && admission.input == input.id && !p.ended)
                    .ok_or_else(|| {
                        io::Error::other("Work retirement does not belong to a pending admission")
                    })?;
                pending.ended = true;
                pending.outcome = Some(outcome);
                if !self.closing {
                    self.work.receipts.push_back(admission.id);
                }
                return Ok(Vec::new());
            }
            life::Effect::Input {
                input,
                state: life::InputState::Uncertain,
                reason,
            } => {
                self.work.uncertain_inputs.insert(input.id.clone());
                self.work.uncertain = true;
                Update::Uncertain{input_id:input.id,reason:reason.unwrap_or_else(||"Source input delivery is uncertain; reopen to inspect before continuing.".into())}
            }
            life::Effect::Reconciled {
                input, recovered, ..
            } => {
                if !self.can_reconcile_work()
                    || (!recovered && !self.work.uncertain_inputs.contains(&input.id))
                {
                    return Err(io::Error::other(
                        "Work reconciliation does not resolve an idle held input",
                    ));
                }
                self.work.uncertain_inputs.remove(&input.id);
                self.work.uncertain = !self.work.uncertain_inputs.is_empty();
                Update::Reconciled {
                    input_id: input.id,
                    recovered,
                }
            }
            life::Effect::Producer {
                storage: Some(storage),
                ..
            } => {
                let mut failures = Vec::new();
                for (label, status) in [("task", storage.task), ("output", storage.output)] {
                    if let life::StorageStatus::Failed(reason) = status {
                        failures.push(format!("{label}: {reason}"));
                    }
                }
                return Ok(if !failures.is_empty() && !self.closing {
                    vec![Observation::Information(format!(
                        "Background persistence failed: {}",
                        failures.join("; ")
                    ))]
                } else {
                    Vec::new()
                });
            }
            _ => return Ok(Vec::new()),
        };
        if self.closing {
            Ok(Vec::new())
        } else {
            observations.push(Observation::WorkExecution(Box::new(update)));
            Ok(observations)
        }
    }
    pub(super) fn verify_work(&mut self, admission_id: &str) -> io::Result<Vec<Observation>> {
        if !self.work.pending.get(admission_id).is_some_and(|p| p.ended) {
            return Err(io::Error::other(
                "Work readback preceded its exact worker retirement",
            ));
        }
        let pending = self
            .work
            .pending
            .remove(admission_id)
            .expect("verified pending admission");
        let mut output = vec![Observation::WorkExecution(Box::new(Update::Retired {
            admission_id: admission_id.into(),
        }))];
        let operation = pending.admission.operation;
        if pending.admission.mode == Mode::ActionSegment
            && self.rewrite_active
            && operation == self.command_id
            && self.work.uncertain
            && !self
                .rewrite_executions
                .contains(&pending.admission.origin.execution)
            && matches!(pending.outcome.as_deref(), Some("cancelled" | "failed"))
        {
            let reason = "Source continuation worker retired before execution started; input delivery remains uncertain.".to_owned();
            let outcome = if pending.outcome.as_deref() == Some("cancelled") {
                TerminalOutcome::Cancelled(reason)
            } else {
                TerminalOutcome::Failed(reason)
            };
            output.push(Observation::WorkExecution(Box::new(Update::NotStarted {
                operation: operation.clone(),
                execution: pending.admission.origin.execution,
                outcome,
            })));
        }
        if operation == self.command_id
            && !self.rewrite_active
            && !self.started
            && self.terminal.is_none()
            && self.work.uncertain
            && matches!(pending.outcome.as_deref(), Some("cancelled" | "failed"))
        {
            // The source can create then retire a worker before its body starts.
            // Only its independent retirement readback permits a terminal result;
            // no synthetic stream start or successful completion is invented.
            let reason =
                "Source worker retired before execution started; input delivery remains uncertain."
                    .to_owned();
            let outcome = if pending.outcome.as_deref() == Some("cancelled") {
                TerminalOutcome::Cancelled(reason)
            } else {
                TerminalOutcome::Failed(reason)
            };
            self.terminal = Some(outcome.clone());
            self.work.ended.insert(operation.clone(), outcome.clone());
            output.push(Observation::WorkExecution(Box::new(Update::Ended {
                operation: operation.clone(),
                outcome,
            })));
        }
        if !self.work.pending_operation(&operation) {
            if operation == self.command_id && !self.rewrite_active && !self.can_settle() {
                return Ok(output);
            }
            if let Some(outcome) = self.work.ended.remove(&operation) {
                if operation == self.command_id {
                    self.settled = true;
                }
                output.push(Observation::WorkExecution(Box::new(Update::Settled {
                    operation,
                    outcome,
                })));
            } else if operation == self.command_id && self.can_settle() {
                output.push(self.settle());
            }
        }
        Ok(output)
    }
    pub(super) fn work_local_response(
        &mut self,
        event: &serde_json::Value,
    ) -> io::Result<Option<Observation>> {
        let Some(operation) = self.work.suspended.as_ref() else {
            return Ok(None);
        };
        if event["id"] != *operation {
            return Ok(None);
        }
        if event["ok"] != false {
            return Err(io::Error::other(
                "A local prompt raced source Work and was unexpectedly admitted",
            ));
        }
        let reason = event["error"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 16384)
            .ok_or_else(|| {
                io::Error::other("Missing refusal for the local prompt displaced by Work")
            })?
            .to_owned();
        let operation = self.work.suspended.take().unwrap();
        Ok(Some(Observation::WorkExecution(Box::new(
            Update::LocalRefused { operation, reason },
        ))))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_mode::{Expected, Profile};
    use serde_json::{Value, json};

    fn idle() -> Turn {
        Turn {
            ready: true,
            ..Turn::with_policy(Expected {
                enabled: true,
                profile: Profile::Autonomous,
                workspace: std::env::temp_dir(),
            })
        }
    }
    fn admission(operation: &str, id: &str, mode: Mode) -> (Input, Admission, Payload) {
        let input = Input {
            id: format!("input-{id}"),
            producer: format!("producer-{id}"),
            payload_sha256: "a".repeat(64),
            source: "task".into(),
            requested: Profile::Strict,
            session: "session".into(),
            revision: 1,
        };
        let a = Admission {
            id: id.into(),
            input: input.id.clone(),
            mode,
            operation: operation.into(),
            origin: life::Origin {
                session: input.session.clone(),
                operation: operation.into(),
                hook: None,
                source: "task".into(),
                execution: format!("execution-{id}"),
            },
            parent: None,
            boundary: 0,
            requested: Profile::Strict,
            effective: Profile::Strict,
            seat: Profile::Autonomous,
            provider: life::Provider::Host,
            delivery: None,
        };
        let payload = Payload {
            delivery: crate::work_lifecycle::Delivery::Append,
            content: "Exact tool result\u{0}\nwith policy suffix".into(),
            text: Some("Exact tool result\u{0}".into()),
            source: input.source.clone(),
            profile: input.requested,
        };
        (input, a, payload)
    }
    fn admit(turn: &mut Turn, op: &str, id: &str, mode: Mode) -> Vec<Observation> {
        let (input, admission, payload) = admission(op, id, mode);
        turn.accept_work(life::Effect::Admission {
            input,
            admission,
            payload,
            drain_only: false,
        })
        .unwrap()
    }
    fn source_retired(turn: &mut Turn, id: &str, outcome: &str) {
        let a = turn.work.pending[id].admission.clone();
        let (input, _, _) = admission(&a.operation, id, a.mode);
        assert!(
            turn.accept_work(life::Effect::Settled {
                input,
                admission: a,
                outcome: outcome.into()
            })
            .unwrap()
            .is_empty()
        );
    }
    fn stream(turn: &mut Turn) -> Observation {
        turn.accept(
            &json!({"type":"turn_start","model":"fixture"}),
            "claude",
            "fixture",
        )
        .unwrap();
        turn.accept(
            &json!({"type":"text_delta","text":"answer","card_index":0}),
            "claude",
            "fixture",
        )
        .unwrap();
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop","error":null}),
            "claude",
            "fixture",
        )
        .unwrap()
    }
    fn update(value: &Observation) -> &Update {
        match value {
            Observation::WorkExecution(value) => value,
            _ => panic!("expected Work update: {value:?}"),
        }
    }
    fn refusal(id: &str, ok: bool) -> Value {
        json!({"type":"response","id":id,"ok":ok,"error":"Captured background input owns execution"})
    }

    #[test]
    fn source_operation_adopted_once_and_only_readback_releases_completion() {
        let mut turn = idle();
        let result = admit(&mut turn, "source-one", "a", Mode::NewTurn);
        assert!(
            matches!(update(&result[0]),Update::Admitted{payload,..} if payload.text.as_deref()==Some("Exact tool result\u{0}"))
        );
        assert_eq!(turn.command_id, "source-one");
        assert!(turn.acknowledged);
        assert_eq!(turn.policy.as_ref().unwrap().profile, Profile::Autonomous);
        assert!(
            matches!(update(&stream(&mut turn)),Update::Ended{operation,..} if operation=="source-one")
        );
        assert!(!turn.can_manage());
        assert!(!turn.settled);
        source_retired(&mut turn, "a", "completed");
        assert_eq!(turn.take_work_receipt().as_deref(), Some("a"));
        assert!(!turn.can_manage());
        let out = turn.verify_work("a").unwrap();
        assert!(matches!(update(&out[0]),Update::Retired{admission_id} if admission_id=="a"));
        assert!(
            matches!(update(&out[1]),Update::Settled{operation,outcome:TerminalOutcome::Complete} if operation=="source-one")
        );
        assert!(turn.can_manage());
        assert!(turn.work_waits().is_empty());
        assert!(turn.verify_work("a").is_err());
    }
    #[test]
    fn reviewed_seat_is_used_for_future_work_without_rewriting_admitted_work() {
        use crate::runtime_controls::{Binding, Facts, Policy};
        let mut turn = idle();
        admit(&mut turn, "old", "a", Mode::NewTurn);
        let recorded = turn.work.pending["a"].admission.clone();
        let facts = Facts::parse(&json!({
            "tools_enabled":true, "settings_profile":"interactive", "chosen_profile":"interactive",
            "seat_profile":"interactive", "effective_profile":"interactive", "requested_cli_profile":null,
            "plan_mode":false, "mcp_enabled":false, "approval_route":"host",
            "cwd":std::env::temp_dir(), "provider":"host", "inventory":"host_request"
        }), life::Provider::Host).unwrap();
        turn.runtime_policy = Some(Policy {
            binding: Binding {
                mcp_lifecycle: false,
                identity: life::Identity {
                    session: "session".into(),
                    agent: "agent".into(),
                    backend: "custom".into(),
                    model: "fixture".into(),
                },
                work_instance: "a".repeat(64),
                provider: life::Provider::Host,
                tool_selection: false,
            },
            generation: 1,
            facts,
        });
        stream(&mut turn);
        assert_eq!(turn.work.pending["a"].admission, recorded);
        source_retired(&mut turn, "a", "completed");
        turn.verify_work("a").unwrap();
        let (input, mut next, payload) = admission("new", "b", Mode::NewTurn);
        assert!(
            turn.accept_work(life::Effect::Admission {
                input: input.clone(),
                admission: next.clone(),
                payload: payload.clone(),
                drain_only: false
            })
            .is_err()
        );
        next.seat = Profile::Interactive;
        assert!(
            turn.accept_work(life::Effect::Admission {
                input,
                admission: next,
                payload,
                drain_only: false
            })
            .is_ok()
        );
    }

    #[test]
    fn readback_cannot_consume_an_unretired_admission() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        assert!(turn.verify_work("a").is_err());
        assert!(turn.work.pending.contains_key("a"));
        assert!(turn.verify_work("foreign").is_err());
        stream(&mut turn);
        source_retired(&mut turn, "a", "completed");
        assert!(turn.verify_work("a").is_ok());
    }
    #[test]
    fn multiple_boundary_inputs_wait_for_every_exact_retirement() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        turn.accept(
            &json!({"type":"turn_start","model":"fixture"}),
            "claude",
            "fixture",
        )
        .unwrap();
        admit(&mut turn, "source-one", "b", Mode::InTurn);
        admit(&mut turn, "source-one", "c", Mode::InTurn);
        turn.accept(
            &json!({"type":"text_delta","text":"answer"}),
            "claude",
            "fixture",
        )
        .unwrap();
        let end = turn
            .accept(
                &json!({"type":"turn_end","stopReason":"stop"}),
                "claude",
                "fixture",
            )
            .unwrap();
        assert!(matches!(update(&end), Update::Ended { .. }));
        for id in ["b", "a"] {
            source_retired(&mut turn, id, "completed");
            assert_eq!(turn.verify_work(id).unwrap().len(), 1);
            assert!(!turn.can_manage());
        }
        source_retired(&mut turn, "c", "completed");
        assert_eq!(turn.verify_work("c").unwrap().len(), 2);
        assert!(turn.can_manage());
    }
    #[test]
    fn previous_retirement_does_not_settle_or_reset_next_source_turn() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        stream(&mut turn);
        source_retired(&mut turn, "a", "completed");
        admit(&mut turn, "source-two", "b", Mode::NewTurn);
        assert!(turn.work_waits().contains("operation:source-one"));
        let out = turn.verify_work("a").unwrap();
        assert!(matches!(update(&out[1]),Update::Settled{operation,..} if operation=="source-one"));
        assert_eq!(turn.command_id, "source-two");
        assert!(!turn.settled);
        assert!(!turn.work_waits().contains("operation:source-one"));
        stream(&mut turn);
        source_retired(&mut turn, "b", "completed");
        turn.verify_work("b").unwrap();
        assert!(turn.can_manage());
    }
    #[test]
    fn reserved_local_prompt_can_only_receive_its_exact_refusal() {
        let mut turn = idle();
        let local = turn.submit("human draft").unwrap()["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let out = admit(&mut turn, "source-one", "a", Mode::NewTurn);
        assert!(
            matches!(update(&out[0]),Update::Admitted{suspended_submission:Some(id),..} if id==&local)
        );
        assert!(
            turn.work_local_response(&refusal("foreign", false))
                .unwrap()
                .is_none()
        );
        assert!(turn.work_local_response(&refusal(&local, true)).is_err());
        let out = turn
            .accept(&refusal(&local, false), "claude", "fixture")
            .unwrap();
        assert!(matches!(update(&out),Update::LocalRefused{operation,..} if operation==&local));
        assert_eq!(turn.command_id, "source-one");
        assert!(!turn.work_waits().contains(&format!("local:{local}")));
        stream(&mut turn);
        source_retired(&mut turn, "a", "completed");
        turn.verify_work("a").unwrap();
        assert!(turn.can_manage());
    }
    #[test]
    fn retired_source_identity_cannot_alias_a_new_source_or_user_operation() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        stream(&mut turn);
        source_retired(&mut turn, "a", "completed");
        turn.verify_work("a").unwrap();
        let (input, admission, payload) = admission("source-one", "b", Mode::NewTurn);
        assert!(
            turn.accept_work(life::Effect::Admission {
                input,
                admission,
                payload,
                drain_only: false
            })
            .is_err()
        );
        let user = turn.submit("fresh prompt").unwrap();
        assert_ne!(user["id"], "source-one");
    }
    #[test]
    fn prestart_refusal_waits_for_source_retirement_without_success() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        let out = turn
            .accept(
                &json!({"type":"turn_refused","provider":"claude","reason":"policy refused"}),
                "claude",
                "fixture",
            )
            .unwrap();
        assert!(matches!(
            update(&out),
            Update::Ended {
                outcome: TerminalOutcome::Refused(_),
                ..
            }
        ));
        source_retired(&mut turn, "a", "completed");
        assert!(matches!(
            update(&turn.verify_work("a").unwrap()[1]),
            Update::Settled {
                outcome: TerminalOutcome::Refused(_),
                ..
            }
        ));
        assert!(turn.can_manage());
    }
    #[test]
    fn cancelled_before_body_requires_readback_and_keeps_uncertainty_latched() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        let (input, _, _) = admission("source-one", "a", Mode::NewTurn);
        turn.accept_work(life::Effect::Input {
            input,
            state: life::InputState::Uncertain,
            reason: Some("Worker binding failed".into()),
        })
        .unwrap();
        source_retired(&mut turn, "a", "cancelled");
        assert!(turn.terminal.is_none());
        let out = turn.verify_work("a").unwrap();
        assert_eq!(out.len(), 3);
        assert!(matches!(
            update(&out[1]),
            Update::Ended {
                outcome: TerminalOutcome::Cancelled(_),
                ..
            }
        ));
        assert!(matches!(
            update(&out[2]),
            Update::Settled {
                outcome: TerminalOutcome::Cancelled(_),
                ..
            }
        ));
        assert!(!turn.started);
        assert!(turn.settled);
        assert!(!turn.can_manage());
        assert!(turn.submit("must not replay uncertain input").is_err());
        assert!(turn.work_waits().is_empty());
    }
    #[test]
    fn stop_receipt_after_worker_readback_settles_only_current_operation() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        turn.accept(
            &json!({"type":"turn_start","model":"fixture"}),
            "claude",
            "fixture",
        )
        .unwrap();
        let stop = turn.abort("source-one").unwrap().unwrap();
        turn.accept(
            &json!({"type":"turn_end","stopReason":"cancelled"}),
            "claude",
            "fixture",
        )
        .unwrap();
        source_retired(&mut turn, "a", "cancelled");
        assert_eq!(turn.verify_work("a").unwrap().len(), 1);
        assert!(!turn.can_manage());
        let out=turn.accept(&json!({"type":"response","id":stop["id"],"ok":true,"result":{"stopped":true,"cancelled_asks":[]}}),"claude","fixture").unwrap();
        assert!(matches!(out, Observation::Cancelled(_)));
        assert!(turn.can_manage());
        assert!(turn.work_waits().is_empty());
    }
    #[test]
    fn buffered_prequit_admission_is_drained_without_publication_or_readback() {
        let mut turn = idle();
        turn.begin_close();
        let (input, admission, payload) = admission("source-one", "a", Mode::NewTurn);
        assert!(
            turn.accept_work(life::Effect::Admission {
                input,
                admission,
                payload,
                drain_only: true
            })
            .unwrap()
            .is_empty()
        );
        source_retired(&mut turn, "a", "cancelled");
        assert!(turn.take_work_receipt().is_none());
        assert!(!turn.can_manage());
    }
    #[test]
    fn in_turn_and_action_inputs_require_their_existing_execution() {
        let mut turn = idle();
        for mode in [Mode::InTurn, Mode::ActionSegment] {
            let (input, admission, payload) = admission("foreign", "a", mode);
            assert!(
                turn.accept_work(life::Effect::Admission {
                    input,
                    admission,
                    payload,
                    drain_only: false
                })
                .is_err()
            );
        }
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        turn.accept(
            &json!({"type":"turn_start","model":"fixture"}),
            "claude",
            "fixture",
        )
        .unwrap();
        let (_, admission, _) = admission("source-one", "a", Mode::NewTurn);
        assert!(turn.work_current(Some(admission.origin)).is_some());
        assert!(turn.work_current(None).is_none());
        let (input, admission, payload) = self::admission("foreign", "b", Mode::InTurn);
        assert!(
            turn.accept_work(life::Effect::Admission {
                input,
                admission,
                payload,
                drain_only: false
            })
            .is_err()
        );
    }
    #[test]
    fn persistence_failure_is_visible_and_never_a_completion() {
        let mut turn = idle();
        // Producer storage has independent typed parsing coverage. Exercise the
        // exact publication branch without requiring a stream or fake success.
        let record = life::Producer {
            id: "p".into(),
            kind: life::ProducerKind::HostTask,
            object: "task".into(),
            session: "session".into(),
            owner: "owner".into(),
            owner_created: None,
            generation: 1,
            action: None,
            operation: None,
            execution: None,
        };
        let out = turn
            .accept_work(life::Effect::Producer {
                producer: record,
                state: life::ProducerState::Retired,
                worker: None,
                storage: Some(life::StorageProof {
                    task: life::StorageStatus::Failed("disk full".into()),
                    output: life::StorageStatus::Verified,
                }),
            })
            .unwrap();
        assert!(matches!(&out[0],Observation::Information(reason) if reason.contains("disk full")));
        assert!(!turn.sent);
        assert!(turn.terminal.is_none());
    }
    #[test]
    fn exact_work_deadlines_survive_unrelated_read_and_retirement() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        let mut foreground = None; // A prior management read completed.
        assert!(turn.work_waits().contains("stream:source-one"));
        let end = stream(&mut turn);
        turn.work_observed_deadline(&[end], &mut foreground);
        assert!(foreground.is_none());
        assert!(!turn.work_waits().contains("stream:source-one"));
        assert!(turn.work_waits().contains("admission:a"));
        source_retired(&mut turn, "a", "completed");
        admit(&mut turn, "source-two", "b", Mode::NewTurn);
        let active = std::time::Instant::now() + std::time::Duration::from_secs(30);
        foreground = Some(active);
        let old = turn.verify_work("a").unwrap();
        turn.work_observed_deadline(&old, &mut foreground);
        assert_eq!(foreground, Some(active));
        assert!(turn.work_waits().contains("stream:source-two"));
    }
    #[test]
    fn verified_prestart_cancellation_clears_only_its_own_foreground_deadline() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "a", Mode::NewTurn);
        turn.work.uncertain = true;
        source_retired(&mut turn, "a", "cancelled");
        let mut deadline = Some(std::time::Instant::now() + std::time::Duration::from_secs(30));
        let out = turn.verify_work("a").unwrap();
        turn.work_observed_deadline(&out, &mut deadline);
        assert!(deadline.is_none());
        assert!(turn.work_waits().is_empty());
        assert!(!turn.can_manage());
    }
    #[test]
    fn action_retirement_deadline_binds_ended_execution_across_next_segment() {
        let mut turn = idle();
        turn.rewrite_active = true;
        turn.command_id = "action".into();
        let (_, a, _) = admission("action", "a", Mode::ActionSegment);
        let (_, b, _) = admission("action", "b", Mode::ActionSegment);
        turn.work.pending.insert(
            "a".into(),
            Pending {
                admission: a,
                ended: false,
                stream_ended: false,
                outcome: None,
            },
        );
        turn.work.pending.insert(
            "b".into(),
            Pending {
                admission: b,
                ended: false,
                stream_ended: false,
                outcome: None,
            },
        );
        turn.rewrite_segments.push("execution-a".into());
        turn.work_stream_ended();
        turn.rewrite_segments.push("execution-b".into());
        assert!(turn.work_waits().contains("admission:a"));
        assert!(!turn.work_waits().contains("stream:execution-a"));
        assert!(turn.work_waits().contains("stream:execution-b"));
        source_retired(&mut turn, "a", "completed");
        turn.verify_work("a").unwrap();
        assert!(!turn.work_waits().contains("admission:a"));
        assert!(turn.work_waits().contains("stream:execution-b"));
    }

    fn reconciled_input(input: Input, recovered: bool) -> life::Effect {
        let reconciliation = life::Reconciliation {
            operation: "explicit-check".into(),
            provider: life::Provider::Codex,
            delivery: "native-delivery".into(),
            thread: "thread".into(),
            turn: "native-turn".into(),
            provider_history_sha256: "b".repeat(64),
            journal_instance: "c".repeat(64),
            journal_sha256: "d".repeat(64),
            payload_sha256: input.payload_sha256.clone(),
        };
        life::Effect::Reconciled {
            input,
            reconciliation,
            recovered,
        }
    }
    #[test]
    fn verified_history_clears_only_its_exact_input_barrier() {
        let mut turn = idle();
        let (one, _, _) = admission("unused", "one", Mode::NewTurn);
        let (two, _, _) = admission("unused", "two", Mode::NewTurn);
        for input in [&one, &two] {
            turn.accept_work(life::Effect::Input {
                input: input.clone(),
                state: life::InputState::Uncertain,
                reason: Some("unknown send".into()),
            })
            .unwrap();
        }
        assert!(turn.can_reconcile_work());
        assert!(!turn.can_engine());
        turn.accept_work(reconciled_input(one, false)).unwrap();
        assert!(!turn.can_engine());
        turn.accept_work(reconciled_input(two.clone(), false))
            .unwrap();
        assert!(turn.can_engine());
        assert!(turn.accept_work(reconciled_input(two, false)).is_err());
        assert!(turn.work.pending.is_empty());
        assert!(!turn.sent);
    }
    #[test]
    fn recovered_history_never_supplies_worker_or_execution_authority() {
        let mut turn = idle();
        let (input, _, _) = admission("historical", "old", Mode::NewTurn);
        let updates = turn.accept_work(reconciled_input(input, true)).unwrap();
        assert!(matches!(
            update(&updates[0]),
            Update::Reconciled {
                recovered: true,
                ..
            }
        ));
        assert!(turn.can_manage());
        assert!(!turn.started && !turn.sent);
        assert!(turn.work.pending.is_empty());
    }
    #[test]
    fn reconciliation_cannot_release_pending_worker_or_closing_runtime() {
        let mut turn = idle();
        admit(&mut turn, "source", "active", Mode::NewTurn);
        let (input, _, _) = admission("historical", "old", Mode::NewTurn);
        assert!(!turn.can_reconcile_work());
        assert!(
            turn.accept_work(reconciled_input(input.clone(), true))
                .is_err()
        );
        let mut closed = idle();
        closed.begin_close();
        assert!(!closed.can_reconcile_work());
        assert!(closed.accept_work(reconciled_input(input, true)).is_err());
    }
    #[test]
    fn reviewed_mcp_enable_installs_only_within_the_same_negotiated_epoch() {
        use crate::runtime_controls::{Binding, Facts, Policy};
        let mut turn = idle();
        let facts = json!({"tools_enabled":true,"settings_profile":"autonomous","chosen_profile":"autonomous",
            "seat_profile":"autonomous","effective_profile":"autonomous","requested_cli_profile":"autonomous",
            "plan_mode":false,"mcp_enabled":false,"approval_route":"host","cwd":std::env::temp_dir(),
            "provider":"host","inventory":"host_request"});
        let initial = Policy {
            binding: Binding {
                mcp_lifecycle: true,
                tool_selection: false,
                identity: life::Identity {
                    session: "session".into(),
                    agent: "agent".into(),
                    backend: "custom".into(),
                    model: "fixture".into(),
                },
                work_instance: "a".repeat(64),
                provider: life::Provider::Host,
            },
            generation: 0,
            facts: Facts::parse(&facts, life::Provider::Host).unwrap(),
        };
        turn.install_runtime_policy(initial.clone()).unwrap();
        let mut enabled = initial.clone();
        enabled.generation = 1;
        let mut f = facts;
        f["mcp_enabled"] = json!(true);
        enabled.facts = Facts::parse_mcp(&f, life::Provider::Host, true).unwrap();
        turn.install_runtime_policy(enabled.clone()).unwrap();
        assert!(turn.runtime_policy.as_ref().unwrap().facts.mcp_enabled);
        assert!(turn.install_runtime_policy(enabled.clone()).is_err());
        let mut target = enabled.clone();
        target.binding.work_instance = "b".repeat(64);
        target.generation = 0;
        assert!(turn.install_runtime_policy(target).is_err());
        let mut unsupported = enabled;
        unsupported.binding.mcp_lifecycle = false;
        unsupported.facts = initial.facts.clone();
        unsupported.generation = 0;
        assert!(turn.install_runtime_policy(unsupported).is_err());
        let mut startup = idle();
        let mut bad = initial;
        bad.facts = Facts::parse_mcp(&f, life::Provider::Host, true).unwrap();
        assert!(startup.install_runtime_policy(bad).is_err());
    }
    #[test]
    fn pre_admission_refusal_cannot_release_an_admitted_work_execution() {
        let mut turn = idle();
        admit(&mut turn, "source-one", "admission-one", Mode::NewTurn);
        let refusal = json!({"type":"submit_refused","phase":"pre_admission",
            "reason":"admission","detail":"unowned refusal"});
        assert!(turn.accept(&refusal, "custom", "fixture").is_err());
        assert!(turn.work.pending.contains_key("admission-one"));
        assert!(!turn.can_manage());
        assert!(turn.verify_work("foreign-admission").is_err());
        assert!(turn.work.pending.contains_key("admission-one"));
    }
}
