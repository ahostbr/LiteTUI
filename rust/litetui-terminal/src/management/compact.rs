//! Reviewed current-message compact and independent post-turn readback.
use super::*;
use crate::conversation_compact as r;

#[derive(Default)]
pub(super) struct Control {
    pub(super) pending: Option<Pending>,
    cache: Option<Cache>,
    review: Option<Prepared>,
    attempt: Option<Attempt>,
    generation: u64,
    protocol: bool,
    pub(super) chain: Option<Chain>,
}
impl Control {
    pub(super) fn idle(&self) -> bool {
        self.pending.is_none() && self.attempt.is_none() && self.chain.is_none()
    }
    pub(super) fn invalidate(&mut self) {
        self.cache = None;
        self.review = None;
    }
}

#[derive(Clone)]
pub struct Prepared {
    pub key: r::RequestKey,
    pub review: r::Review,
    before: Snapshot,
    policy: Value,
}
#[derive(Debug, PartialEq)]
pub struct Changed {
    pub key: r::RequestKey,
    pub admission: r::Admission,
    pub review: r::Review,
    pub settlement: r::Settlement,
    pub snapshot: Snapshot,
    pub history: crate::resume::History,
    pub queue: crate::restored_queue::Snapshot,
    pub policy: Value,
}
pub(super) struct Cache {
    snapshot: r::Snapshot,
    before: Snapshot,
    policy: Value,
}
pub(super) struct Attempt {
    prepared: Prepared,
    operation: String,
}
pub(super) enum Stage {
    Read,
    ReadState(r::Snapshot),
    Validate {
        before: Cache,
        hint: String,
    },
    Settled {
        attempt: Box<Attempt>,
        admission: r::Admission,
    },
    State {
        attempt: Box<Attempt>,
        admission: r::Admission,
        settled: r::Settlement,
    },
    History {
        admission: r::Admission,
        review: r::Review,
        settled: r::Settlement,
        state: Snapshot,
        policy: Value,
    },
    Page {
        admission: r::Admission,
        review: r::Review,
        settled: r::Settlement,
        state: Snapshot,
        policy: Value,
        history: crate::resume::History,
    },
    Queue {
        admission: r::Admission,
        review: r::Review,
        settled: r::Settlement,
        state: Snapshot,
        policy: Value,
        history: crate::resume::History,
    },
}
pub(super) struct Pending {
    pub(super) id: CommandId,
    key: r::RequestKey,
    stage: Stage,
}
pub(super) use super::action::Chain;

impl Management {
    fn compact_selection(&self, key: &r::RequestKey) -> io::Result<()> {
        key.selection.verify()?;
        if !self.ready()
            || !self.history_projection
            || key.generation == 0
            || key.generation <= self.compact.generation
        {
            return Err(fail(
                "Compaction needs an idle runtime and fresh action generation",
            ));
        }
        self.ensure_drained()?;
        if !r::OPERATIONS
            .iter()
            .chain([&crate::restored_queue::READ])
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks reviewed compaction support"));
        }
        let selected = &key.selection;
        if selected.identity != self.history_active_identity()?
            || selected.history_key.generation != self.history_generation
        {
            return Err(fail(
                "Selected current history has changed; refresh before compaction",
            ));
        }
        let descriptor = self
            .history_snapshot
            .as_ref()
            .ok_or_else(|| fail("Read current history before compaction"))?;
        let page = self
            .history_page
            .as_ref()
            .ok_or_else(|| fail("No current history page is displayed"))?;
        if descriptor.identity != selected.identity
            || descriptor.token != selected.history_snapshot
            || descriptor.context_sha256 != selected.context_sha256
            || descriptor.counts.messages != selected.messages
            || descriptor.read_only
            || descriptor.pending
            || !descriptor.materialized
            || page.snapshot != descriptor.token
            || page.identity != descriptor.identity
        {
            return Err(fail(
                "Compaction selection is not fresh current saved history",
            ));
        }
        Ok(())
    }
    pub fn compact_negotiated(&self) -> bool {
        self.compact.protocol
    }
    pub fn request_compact(&mut self, intent: r::Intent) -> io::Result<Value> {
        let key = intent.key().clone();
        self.compact_selection(&key)?;
        let (command, stage) = match intent {
            r::Intent::Read { .. } => {
                self.compact.cache = None;
                self.compact.review = None;
                (key.selection.command(r::SNAPSHOT), Stage::Read)
            }
            r::Intent::Validate { snapshot, hint, .. } => {
                self.compact.review = None;
                let cached = self
                    .compact
                    .cache
                    .take()
                    .filter(|c| {
                        c.snapshot.token == snapshot && c.snapshot.selection == key.selection
                    })
                    .ok_or_else(|| {
                        fail("Compaction source snapshot is stale; reopen its review")
                    })?;
                if !cached.snapshot.eligible {
                    return Err(fail("Compaction was not offered by the source"));
                }
                r::validate_hint(&hint)?;
                (
                    cached.snapshot.request(&hint),
                    Stage::Validate {
                        before: cached,
                        hint,
                    },
                )
            }
            r::Intent::Apply { .. } => {
                return Err(fail("Compaction Apply must reserve an execution operation"));
            }
        };
        let result = self.compact_read(key.clone(), stage, command)?;
        self.compact.generation = key.generation;
        self.compact.protocol = true;
        Ok(result)
    }
    pub fn prepare_compact(&self, intent: &r::Intent) -> io::Result<Prepared> {
        self.compact_selection(intent.key())?;
        let r::Intent::Apply { key, review } = intent else {
            return Err(fail("Expected explicit Apply review"));
        };
        let prepared = self
            .compact
            .review
            .as_ref()
            .filter(|p| p.review.token == *review && p.key.selection == key.selection)
            .ok_or_else(|| fail("Confirmed compaction review is stale"))?;
        if prepared.before.held_restored_input != Some(0) || !prepared.before.writable {
            return Err(fail(
                "Compaction cannot execute with held input or without ownership",
            ));
        }
        let mut prepared = prepared.clone();
        prepared.key = key.clone();
        Ok(prepared)
    }
    pub fn begin_compact(&mut self, prepared: Prepared, operation: &str) -> io::Result<Value> {
        self.compact_selection(&prepared.key)?;
        if self.compact.review.as_ref().is_none_or(|p| {
            p.review != prepared.review || p.key.selection != prepared.key.selection
        }) {
            return Err(fail("Compaction review changed before dispatch"));
        }
        self.prompt(operation)?;
        let mut command = prepared.review.request();
        command["id"] = json!(operation);
        self.compact.generation = prepared.key.generation;
        self.compact.cache = None;
        self.compact.review = None;
        self.compact.chain = Some(Chain::new(
            super::action::Kind::Compact,
            prepared.review.action_id.clone(),
            operation.into(),
        ));
        self.compact
            .chain
            .as_mut()
            .expect("reserved chain")
            .bind_root(Some(&prepared.review.root_execution_id))?;
        self.compact
            .chain
            .as_mut()
            .expect("reserved chain")
            .permit_wake(prepared.review.before.config.wake_after_compact);
        self.compact.attempt = Some(Attempt {
            prepared,
            operation: operation.into(),
        });
        Ok(command)
    }
    /// Called only after Turn's question ledger validates its correlated answer ACK.
    /// A source cancellation may already have consumed the same pending closure.
    pub(crate) fn acknowledge_compact_question(&mut self, id: &str) {
        if let Some(chain) = &mut self.compact.chain {
            chain.acknowledge_question(id);
        }
    }
    pub fn needs_compact_readback(&self) -> bool {
        self.compact.attempt.is_some()
    }
    pub fn read_compact(&mut self, admission: r::Admission) -> io::Result<Value> {
        self.ensure_drained()?;
        let attempt = self
            .compact
            .attempt
            .take()
            .ok_or_else(|| fail("No compaction execution to settle"))?;
        if admission.operation_id != attempt.operation
            || admission.action_id != attempt.prepared.review.action_id
            || admission.status == r::AdmissionStatus::Failed
            || admission.close_required
        {
            return Err(fail(
                "Compaction admission is uncertain or belongs to another action",
            ));
        }
        let command = attempt.prepared.review.settled_request(&attempt.operation);
        self.compact_read(
            attempt.prepared.key.clone(),
            Stage::Settled {
                attempt: Box::new(attempt),
                admission,
            },
            command,
        )
    }
    fn compact_read(
        &mut self,
        key: r::RequestKey,
        stage: Stage,
        mut command: Value,
    ) -> io::Result<Value> {
        if self.compact.pending.is_some() {
            return Err(fail("Concurrent compaction read"));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve compaction read"))?;
        command["id"] = id.to_value();
        self.compact.pending = Some(Pending { id, key, stage });
        Ok(command)
    }
    pub(super) fn compact_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && (self
                .compact
                .pending
                .as_ref()
                .is_some_and(|p| frame["id"] == p.id.to_value())
                || self
                    .compact
                    .attempt
                    .as_ref()
                    .is_some_and(|a| frame["id"] == a.operation))
    }
    fn compact_state(
        &self,
        value: &Value,
        selection: &r::Selection,
    ) -> io::Result<(Snapshot, Value)> {
        let state = Snapshot::parse(value)?;
        let selected = &selection.identity;
        if state.session != selected.session
            || state.backend != selected.backend
            || state.model != selected.model
            || state.profile
                != self
                    .expected
                    .as_ref()
                    .ok_or_else(|| fail("No active profile"))?
                    .3
            || !state.writable
            || value["ownership"]["agent_id"] != selected.agent
        {
            return Err(fail("Compaction state changed owned execution authority"));
        }
        if !value["tool_policy"].is_object() {
            return Err(fail("Compaction state omitted tool policy"));
        }
        Ok((state, value["tool_policy"].clone()))
    }
    pub(super) fn compact_response(&mut self, frame: Value) -> io::Result<Step> {
        let pending = self
            .compact
            .pending
            .take()
            .expect("matched compaction read");
        self.ensure_drained()?;
        if self.closing {
            return Ok(Step::Ignore);
        }
        let Pending { key, stage, .. } = pending;
        if key.selection.identity != self.history_active_identity()? {
            return Err(fail("Compaction owner changed during a read"));
        }
        if frame["ok"] != true {
            if matches!(
                stage,
                Stage::Read | Stage::ReadState(_) | Stage::Validate { .. }
            ) {
                self.compact.cache = None;
                self.compact.review = None;
                return Ok(Step::Compact(r::Update::Rejected {
                    key,
                    reason: text(&frame, "error", 4096)?.into(),
                }));
            }
            if let Stage::Settled { attempt, .. } = &stage
                && matches!(attempt.prepared.review.before.plan, r::Plan::Claude(_))
            {
                let reason = text(&frame, "error", 4096)?;
                if reason.starts_with("Claude compaction stopped during ") {
                    return Err(fail(&crate::live::terminal_text(reason)));
                }
            }
            return Err(fail(
                "Compaction settlement/readback failed; reopen to inspect saved effects",
            ));
        }
        let value = &frame["result"];
        let (next, command) = match stage {
            Stage::Read => {
                let snapshot = r::Snapshot::parse(value, &key.selection)?;
                (
                    Stage::ReadState(snapshot),
                    json!({"type":"gui.state","include_conversations":false}),
                )
            }
            Stage::ReadState(snapshot) => {
                let (before, policy) = self.compact_state(value, &key.selection)?;
                self.compact.cache = Some(Cache {
                    snapshot: snapshot.clone(),
                    before,
                    policy,
                });
                return Ok(Step::Compact(r::Update::Snapshot {
                    key,
                    snapshot: Box::new(snapshot),
                }));
            }
            Stage::Validate { before, hint } => {
                let review = r::Review::parse(value, before.snapshot, &hint)?;
                self.compact.review = Some(Prepared {
                    key: key.clone(),
                    review: review.clone(),
                    before: before.before,
                    policy: before.policy,
                });
                return Ok(Step::Compact(r::Update::Review {
                    key,
                    review: Box::new(review),
                }));
            }
            Stage::Settled { attempt, admission } => {
                let settled = r::Settlement::parse(
                    value,
                    &admission,
                    &attempt.prepared.review,
                    &attempt.operation,
                )?;
                let chain = self
                    .compact
                    .chain
                    .as_ref()
                    .ok_or_else(|| fail("Missing compact execution chain"))?;
                if settled.execution_segments != chain.segments
                    || settled.last_execution_id != chain.last_execution
                    || !chain.ended
                    || !chain.maintenance.is_empty()
                    || !chain.inputs.is_empty()
                    || (admission.status == r::AdmissionStatus::Accepted
                        && (!chain.root_finished
                            || chain.root_execution.as_deref()
                                != Some(attempt.prepared.review.root_execution_id.as_str())))
                    || (admission.status == r::AdmissionStatus::Refused
                        && chain.root_execution.is_some())
                {
                    return Err(fail(
                        "Settled compact does not prove every observed execution retired",
                    ));
                }
                (
                    Stage::State {
                        attempt,
                        admission,
                        settled,
                    },
                    json!({"type":"gui.state","include_conversations":false}),
                )
            }
            Stage::State {
                attempt,
                admission,
                settled,
            } => {
                let (state, policy) = self.compact_state(value, &key.selection)?;
                if state.held_restored_input != Some(0)
                    || state.mode != attempt.prepared.before.mode
                    || state.schedules_paused != attempt.prepared.before.schedules_paused
                    || policy != attempt.prepared.policy
                {
                    return Err(fail(
                        "Compaction changed execution policy, scheduling or held input",
                    ));
                }
                let selected = &key.selection.identity;
                let command =
                    self.history_command(&selected.session, &selected.backend, &selected.model)?;
                (
                    Stage::History {
                        admission,
                        review: attempt.prepared.review,
                        settled,
                        state,
                        policy,
                    },
                    command,
                )
            }
            Stage::History {
                admission,
                review,
                settled,
                state,
                policy,
            } => {
                let descriptor = crate::history::Snapshot::parse(value, &key.selection.identity)?;
                if descriptor != settled.history {
                    return Err(fail(
                        "Independent current history contradicts settled compaction",
                    ));
                }
                let command = descriptor.page_request(&crate::history::PageRequest::Anchor(
                    crate::history::Anchor::Tail,
                ))?;
                (
                    Stage::Page {
                        admission,
                        review,
                        settled,
                        state,
                        policy,
                        history: crate::resume::History::projected(descriptor),
                    },
                    command,
                )
            }
            Stage::Page {
                admission,
                review,
                settled,
                state,
                policy,
                mut history,
            } => {
                let page = crate::history::Page::parse(
                    value,
                    history.descriptor.as_ref().expect("descriptor"),
                )?;
                page.accepts(&crate::history::PageRequest::Anchor(
                    crate::history::Anchor::Tail,
                ))?;
                history.page = Some(Box::new(page));
                let command = json!({"type":crate::restored_queue::READ,"session_id":key.selection.identity.session});
                (
                    Stage::Queue {
                        admission,
                        review,
                        settled,
                        state,
                        policy,
                        history,
                    },
                    command,
                )
            }
            Stage::Queue {
                admission,
                review,
                settled,
                state,
                policy,
                history,
            } => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Queue revision exhausted"))?;
                let queue = crate::restored_queue::Snapshot::parse(value, self.revision)?;
                if queue.session != state.session
                    || queue.backend != state.backend
                    || queue.model != state.model
                    || !queue.entries.is_empty()
                {
                    return Err(fail(
                        "Compaction queue readback changed owner or held input",
                    ));
                }
                self.history_snapshot = history.descriptor.as_deref().cloned();
                self.history_page = history.page.as_deref().cloned();
                self.history_target = None;
                self.queue_menu = Some(queue.clone());
                self.prompt = None;
                self.origin = None;
                self.compact.chain = None;
                return Ok(Step::CompactSettled(Box::new(Changed {
                    key,
                    admission,
                    review,
                    settlement: settled,
                    snapshot: state,
                    history,
                    queue,
                    policy,
                })));
            }
        };
        Ok(Step::Write(self.compact_read(key, next, command)?))
    }
}

impl Management {
    pub(super) fn validate_compact_event(
        &mut self,
        frame: &Value,
        event: &Value,
    ) -> io::Result<()> {
        self.compact.chain.as_mut().expect("active compact").verify(
            frame,
            event,
            &mut self.executions,
            &mut self.origin,
        )
    }
}
