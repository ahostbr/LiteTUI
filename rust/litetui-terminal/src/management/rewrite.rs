//! Reviewed current-message rewrite and independent post-turn readback.
use super::*;
use crate::conversation_rewrite as r;

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
    Read(r::Action),
    ReadState(r::Snapshot),
    Validate {
        before: Cache,
        action: r::Action,
        text: String,
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
        settled: r::Settlement,
        state: Snapshot,
        policy: Value,
    },
    Page {
        admission: r::Admission,
        state: Snapshot,
        policy: Value,
        history: crate::resume::History,
    },
    Queue {
        admission: r::Admission,
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
    fn rewrite_selection(&self, key: &r::RequestKey) -> io::Result<()> {
        key.selection.verify()?;
        if !self.ready()
            || !self.history_projection
            || key.generation == 0
            || key.generation <= self.rewrite_generation
        {
            return Err(fail(
                "Edit/retry needs an idle runtime and fresh action generation",
            ));
        }
        self.ensure_drained()?;
        if !r::OPERATIONS
            .iter()
            .chain([&crate::restored_queue::READ])
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks reviewed edit/retry support"));
        }
        let selected = &key.selection;
        if selected.identity != self.history_active_identity()?
            || selected.history_key.generation != self.history_generation
        {
            return Err(fail(
                "Selected current history has changed; refresh before edit/retry",
            ));
        }
        let descriptor = self
            .history_snapshot
            .as_ref()
            .ok_or_else(|| fail("Read current history before edit/retry"))?;
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
            || !page.fragments.iter().any(|f| {
                f.role == "user"
                    && f.kind == crate::history::Kind::User
                    && f.message_index == selected.message_index
                    && f.message_sha256 == selected.message_sha256
            })
        {
            return Err(fail(
                "Edit/retry selection is not a current displayed user message",
            ));
        }
        Ok(())
    }
    pub fn rewrite_negotiated(&self) -> bool {
        self.rewrite_protocol
    }
    pub fn request_rewrite(&mut self, intent: r::Intent) -> io::Result<Value> {
        let key = intent.key().clone();
        self.rewrite_selection(&key)?;
        let (command, stage) = match intent {
            r::Intent::Read { action, .. } => {
                self.rewrite_cache = None;
                self.rewrite_review = None;
                (key.selection.command(r::SNAPSHOT), Stage::Read(action))
            }
            r::Intent::Validate {
                snapshot,
                action,
                message,
                ..
            } => {
                self.rewrite_review = None;
                let cached = self
                    .rewrite_cache
                    .take()
                    .filter(|c| {
                        c.snapshot.token == snapshot && c.snapshot.selection == key.selection
                    })
                    .ok_or_else(|| {
                        fail("Edit/retry source selection is stale; reopen its review")
                    })?;
                if !cached.snapshot.eligibility(action).eligible {
                    return Err(fail("Selected action was not offered by the source"));
                }
                let text = if action == r::Action::Retry {
                    cached.snapshot.original_text.clone()
                } else {
                    message
                };
                r::validate_text(&text)?;
                (
                    cached.snapshot.request(action, &text),
                    Stage::Validate {
                        before: cached,
                        action,
                        text,
                    },
                )
            }
            r::Intent::Apply { .. } => {
                return Err(fail("Edit/retry Apply must reserve an execution operation"));
            }
        };
        let result = self.rewrite_read(key.clone(), stage, command)?;
        self.rewrite_generation = key.generation;
        self.rewrite_protocol = true;
        Ok(result)
    }
    pub fn prepare_rewrite(&self, intent: &r::Intent) -> io::Result<Prepared> {
        self.rewrite_selection(intent.key())?;
        let r::Intent::Apply { key, review } = intent else {
            return Err(fail("Expected explicit Apply review"));
        };
        let prepared = self
            .rewrite_review
            .as_ref()
            .filter(|p| p.review.token == *review && p.key.selection == key.selection)
            .ok_or_else(|| fail("Confirmed edit/retry review is stale"))?;
        if prepared.before.held_restored_input != Some(0) || !prepared.before.writable {
            return Err(fail(
                "Edit/retry cannot execute with held input or without ownership",
            ));
        }
        let mut prepared = prepared.clone();
        prepared.key = key.clone();
        Ok(prepared)
    }
    pub fn begin_rewrite(&mut self, prepared: Prepared, operation: &str) -> io::Result<Value> {
        self.rewrite_selection(&prepared.key)?;
        if self.rewrite_review.as_ref().is_none_or(|p| {
            p.review != prepared.review || p.key.selection != prepared.key.selection
        }) {
            return Err(fail("Edit/retry review changed before dispatch"));
        }
        self.prompt(operation)?;
        let mut command = prepared.review.request();
        command["id"] = json!(operation);
        self.rewrite_generation = prepared.key.generation;
        self.rewrite_cache = None;
        self.rewrite_review = None;
        let mut chain = Chain::new(
            super::action::Kind::Rewrite,
            prepared.review.action_id.clone(),
            operation.into(),
        );
        chain.rewind_thread = prepared
            .review
            .native_rewind
            .as_ref()
            .map(|plan| plan.thread_id.clone());
        self.rewrite_chain = Some(chain);
        self.rewrite_attempt = Some(Attempt {
            prepared,
            operation: operation.into(),
        });
        Ok(command)
    }
    /// Called only after Turn's question ledger validates its correlated answer ACK.
    /// A source cancellation may already have consumed the same pending closure.
    pub(crate) fn acknowledge_rewrite_question(&mut self, id: &str) {
        if let Some(chain) = &mut self.rewrite_chain {
            chain.acknowledge_question(id);
        }
    }
    pub fn needs_rewrite_readback(&self) -> bool {
        self.rewrite_attempt.is_some()
    }
    pub fn read_rewrite(&mut self, admission: r::Admission) -> io::Result<Value> {
        self.ensure_drained()?;
        let attempt = self
            .rewrite_attempt
            .take()
            .ok_or_else(|| fail("No edit/retry execution to settle"))?;
        if admission.operation_id != attempt.operation
            || admission.action_id != attempt.prepared.review.action_id
            || admission.outcome == r::AdmissionOutcome::Failed
            || admission.close_required
        {
            return Err(fail(
                "Edit/retry admission is uncertain or belongs to another action",
            ));
        }
        let command = attempt.prepared.review.settled_request(&attempt.operation);
        self.rewrite_read(
            attempt.prepared.key.clone(),
            Stage::Settled {
                attempt: Box::new(attempt),
                admission,
            },
            command,
        )
    }
    fn rewrite_read(
        &mut self,
        key: r::RequestKey,
        stage: Stage,
        mut command: Value,
    ) -> io::Result<Value> {
        if self.rewrite_pending.is_some() {
            return Err(fail("Concurrent edit/retry read"));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve edit/retry read"))?;
        command["id"] = id.to_value();
        self.rewrite_pending = Some(Pending { id, key, stage });
        Ok(command)
    }
    pub(super) fn rewrite_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && (self
                .rewrite_pending
                .as_ref()
                .is_some_and(|p| frame["id"] == p.id.to_value())
                || self
                    .rewrite_attempt
                    .as_ref()
                    .is_some_and(|a| frame["id"] == a.operation))
    }
    fn rewrite_state(
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
            return Err(fail("Edit/retry state changed owned execution authority"));
        }
        if !value["tool_policy"].is_object() {
            return Err(fail("Edit/retry state omitted tool policy"));
        }
        Ok((state, value["tool_policy"].clone()))
    }
    pub(super) fn rewrite_response(&mut self, frame: Value) -> io::Result<Step> {
        let pending = self
            .rewrite_pending
            .take()
            .expect("matched edit/retry read");
        self.ensure_drained()?;
        if self.closing {
            return Ok(Step::Ignore);
        }
        let Pending { key, stage, .. } = pending;
        if key.selection.identity != self.history_active_identity()? {
            return Err(fail("Edit/retry owner changed during a read"));
        }
        if frame["ok"] != true {
            if matches!(
                stage,
                Stage::Read(_) | Stage::ReadState(_) | Stage::Validate { .. }
            ) {
                self.rewrite_cache = None;
                self.rewrite_review = None;
                return Ok(Step::Rewrite(r::Update::Rejected {
                    key,
                    reason: text(&frame, "error", 4096)?.into(),
                }));
            }
            return Err(fail(
                "Edit/retry settlement/readback failed; reopen to inspect saved effects",
            ));
        }
        let value = &frame["result"];
        let (next, command) = match stage {
            Stage::Read(_action) => {
                let snapshot = r::Snapshot::parse(value, &key.selection)?;
                (
                    Stage::ReadState(snapshot),
                    json!({"type":"gui.state","include_conversations":false}),
                )
            }
            Stage::ReadState(snapshot) => {
                let (before, policy) = self.rewrite_state(value, &key.selection)?;
                self.rewrite_cache = Some(Cache {
                    snapshot: snapshot.clone(),
                    before,
                    policy,
                });
                return Ok(Step::Rewrite(r::Update::Snapshot {
                    key,
                    snapshot: Box::new(snapshot),
                }));
            }
            Stage::Validate {
                before,
                action,
                text,
            } => {
                let review = r::Review::parse(value, before.snapshot, action, &text)?;
                if review.native_rewind.is_some()
                    != self.runtime_policy().is_some_and(|policy| {
                        policy.binding.provider == crate::work_lifecycle::Provider::Codex
                    })
                {
                    return Err(fail(
                        "Rewrite review changed the active native provider route",
                    ));
                }
                self.rewrite_review = Some(Prepared {
                    key: key.clone(),
                    review: review.clone(),
                    before: before.before,
                    policy: before.policy,
                });
                return Ok(Step::Rewrite(r::Update::Review {
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
                    .rewrite_chain
                    .as_ref()
                    .ok_or_else(|| fail("Missing rewrite execution chain"))?;
                if settled.native_turn != chain.rewind_turn
                    || settled.execution_segments != chain.segments
                    || settled.last_execution_id != chain.last_execution
                    || !chain.ended
                    || !chain.maintenance.is_empty()
                    || !chain.inputs.is_empty()
                {
                    return Err(fail(
                        "Settled rewrite does not prove every observed execution retired",
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
                let (state, policy) = self.rewrite_state(value, &key.selection)?;
                if state.held_restored_input != Some(0)
                    || state.mode != attempt.prepared.before.mode
                    || state.schedules_paused != attempt.prepared.before.schedules_paused
                    || policy != attempt.prepared.policy
                {
                    return Err(fail(
                        "Edit/retry changed execution policy, scheduling or held input",
                    ));
                }
                let selected = &key.selection.identity;
                let command =
                    self.history_command(&selected.session, &selected.backend, &selected.model)?;
                (
                    Stage::History {
                        admission,
                        settled,
                        state,
                        policy,
                    },
                    command,
                )
            }
            Stage::History {
                admission,
                settled,
                state,
                policy,
            } => {
                let descriptor = crate::history::Snapshot::parse(value, &key.selection.identity)?;
                if descriptor != settled.history {
                    return Err(fail(
                        "Independent current history contradicts settled edit/retry",
                    ));
                }
                let command = descriptor.page_request(&crate::history::PageRequest::Anchor(
                    crate::history::Anchor::Tail,
                ))?;
                (
                    Stage::Page {
                        admission,
                        state,
                        policy,
                        history: crate::resume::History::projected(descriptor),
                    },
                    command,
                )
            }
            Stage::Page {
                admission,
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
                        state,
                        policy,
                        history,
                    },
                    command,
                )
            }
            Stage::Queue {
                admission,
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
                        "Edit/retry queue readback changed owner or held input",
                    ));
                }
                self.history_snapshot = history.descriptor.as_deref().cloned();
                self.history_page = history.page.as_deref().cloned();
                self.history_target = None;
                self.queue_menu = Some(queue.clone());
                self.prompt = None;
                self.origin = None;
                self.rewrite_chain = None;
                return Ok(Step::RewriteSettled(Box::new(Changed {
                    key,
                    admission,
                    snapshot: state,
                    history,
                    queue,
                    policy,
                })));
            }
        };
        Ok(Step::Write(self.rewrite_read(key, next, command)?))
    }
}

impl Management {
    pub(super) fn validate_rewrite_event(
        &mut self,
        frame: &Value,
        event: &Value,
    ) -> io::Result<()> {
        self.rewrite_chain.as_mut().expect("active rewrite").verify(
            frame,
            event,
            &mut self.executions,
            &mut self.origin,
        )
    }
}
