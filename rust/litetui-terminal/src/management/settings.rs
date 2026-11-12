//! One publication/readback state machine for the two reviewed settings contracts.
use super::{CommandId, Management, Snapshot as State, Step, fail, text};
use crate::{
    compaction_settings as compact, settings_editor as budget, settings_form as form,
    settings_view as view,
};
use serde_json::{Value, json};
use std::io;

#[derive(Clone, Copy)]
enum Route {
    Budget,
    Compaction(u64),
}
impl Route {
    fn read(self) -> &'static str {
        match self {
            Self::Budget => budget::READ,
            Self::Compaction(_) => compact::READ,
        }
    }
    fn validate(self) -> &'static str {
        match self {
            Self::Budget => budget::VALIDATE,
            Self::Compaction(_) => compact::VALIDATE,
        }
    }
    fn save(self) -> &'static str {
        match self {
            Self::Budget => budget::SAVE,
            Self::Compaction(_) => compact::SAVE,
        }
    }
    fn rejected(self, reason: String) -> view::Update {
        match self {
            Self::Budget => view::Update::Editor(budget::Update::Rejected(reason)),
            Self::Compaction(generation) => {
                view::Update::Compaction(compact::Update::Rejected { generation, reason })
            }
        }
    }
    fn read_command(self, session: &str, agent: &str, backend: &str, model: &str) -> Value {
        match self {
            Self::Budget => json!({"type":self.read(),"version":1,"session_id":session}),
            Self::Compaction(_) => {
                json!({"type":self.read(),"version":1,"session_id":session,"agent_id":agent,"backend":backend,"model":model})
            }
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Read,
    Validate,
    Save,
    Readback,
    State,
}

pub(super) enum Snapshot {
    Budget(Box<budget::Snapshot>),
    Compaction(Box<compact::Snapshot>),
}
impl Snapshot {
    fn parse(
        route: Route,
        value: &Value,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        match route {
            Route::Budget => Ok(Self::Budget(Box::new(budget::Snapshot::parse(
                value, session, agent, backend, model,
            )?))),
            Route::Compaction(_) => Ok(Self::Compaction(Box::new(compact::Snapshot::parse(
                value, session, agent, backend, model,
            )?))),
        }
    }
    fn request(&self, route: Route, token: &str, field: &str, value: &Value) -> io::Result<Value> {
        match (self, route) {
            (Self::Budget(s), Route::Budget) if s.token == token => s.request(
                field,
                value
                    .as_u64()
                    .ok_or_else(|| fail("Invalid output budget"))?,
            ),
            (Self::Compaction(s), Route::Compaction(_)) if s.token == token => {
                s.request(field, value)
            }
            _ => Err(fail(
                "Refresh the selected settings editor before validating",
            )),
        }
    }
    fn update(&self, route: Route) -> io::Result<view::Update> {
        match (self, route) {
            (Self::Budget(s), Route::Budget) => Ok(view::Update::Editor(budget::Update::Snapshot(
                (**s).clone(),
            ))),
            (Self::Compaction(s), Route::Compaction(generation)) => {
                Ok(view::Update::Compaction(compact::Update::Snapshot {
                    generation,
                    snapshot: s.clone(),
                }))
            }
            _ => Err(fail("Settings read belongs to another editor")),
        }
    }
    fn verify_state(&self, value: &Value) -> io::Result<()> {
        let valid = match self {
            Self::Budget(s) => budget::FIELDS
                .iter()
                .enumerate()
                .all(|(i, f)| value["settings"]["effective"][*f] == s.budgets[i].effective),
            Self::Compaction(s) => compact::FIELDS.iter().all(|f| {
                s.effective(f)
                    .is_ok_and(|v| &value["settings"]["effective"][*f] == v)
            }),
        };
        if valid {
            Ok(())
        } else {
            Err(fail(
                "Settings effective values disagree with runtime state",
            ))
        }
    }
}
pub(super) enum Review {
    Budget(Box<budget::Review>),
    Compaction(Box<compact::Review>),
}
impl Review {
    fn parse(
        value: &Value,
        snapshot: &Snapshot,
        field: &str,
        selected: &Value,
    ) -> io::Result<Self> {
        match snapshot {
            Snapshot::Budget(s) => Ok(Self::Budget(Box::new(budget::Review::parse(
                value,
                s,
                field,
                selected
                    .as_u64()
                    .ok_or_else(|| fail("Invalid output budget"))?,
            )?))),
            Snapshot::Compaction(s) => Ok(Self::Compaction(Box::new(compact::Review::parse(
                value, s, field, selected,
            )?))),
        }
    }
    fn request(
        &self,
        route: Route,
        token: &str,
        reviewed: Option<&compact::Review>,
    ) -> io::Result<Value> {
        match (self, route) {
            (Self::Budget(r), Route::Budget) if r.token == token && reviewed.is_none() => {
                r.request()
            }
            (Self::Compaction(r), Route::Compaction(_))
                if r.token == token && reviewed == Some(r.as_ref()) =>
            {
                r.request()
            }
            _ => Err(fail(
                "Validate this exact settings edit again before saving",
            )),
        }
    }
    fn update(&self, route: Route) -> io::Result<view::Update> {
        match (self, route) {
            (Self::Budget(r), Route::Budget) => {
                Ok(view::Update::Editor(budget::Update::Review((**r).clone())))
            }
            (Self::Compaction(r), Route::Compaction(generation)) => {
                Ok(view::Update::Compaction(compact::Update::Review {
                    generation,
                    review: r.clone(),
                }))
            }
            _ => Err(fail("Settings review belongs to another editor")),
        }
    }
}
enum Outcome {
    Budget(Box<budget::Outcome>),
    Compaction(Box<compact::Outcome>),
}
impl Outcome {
    fn parse(value: &Value, review: &Review) -> io::Result<Result<Self, String>> {
        match review {
            Review::Budget(r) => {
                Ok(budget::Outcome::parse(value, r)?.map(|o| Self::Budget(Box::new(o))))
            }
            Review::Compaction(r) => {
                Ok(compact::Outcome::parse(value, r)?.map(|o| Self::Compaction(Box::new(o))))
            }
        }
    }
    fn verify(&self, after: &Snapshot, review: &Review) -> io::Result<()> {
        match (self, after, review) {
            (Self::Budget(o), Snapshot::Budget(s), Review::Budget(r)) => o.verify(s, r),
            (Self::Compaction(o), Snapshot::Compaction(s), Review::Compaction(r)) => o.verify(s, r),
            _ => Err(fail("Settings readback belongs to another editor")),
        }
    }
    fn update(
        self,
        snapshot: &Snapshot,
        route: Route,
        inspection: view::Snapshot,
    ) -> io::Result<view::Update> {
        match (self, snapshot, route) {
            (Self::Budget(outcome), Snapshot::Budget(snapshot), Route::Budget) => {
                Ok(view::Update::Edited {
                    update: budget::Update::Saved {
                        snapshot: (**snapshot).clone(),
                        outcome,
                    },
                    inspection,
                })
            }
            (
                Self::Compaction(outcome),
                Snapshot::Compaction(snapshot),
                Route::Compaction(generation),
            ) => Ok(view::Update::CompactionEdited {
                update: compact::Update::Saved {
                    generation,
                    outcome,
                    snapshot: snapshot.clone(),
                },
                inspection,
            }),
            _ => Err(fail("Settings result belongs to another editor")),
        }
    }
}
pub(super) struct Pending {
    pub(super) id: CommandId,
    route: Route,
    phase: Phase,
    selection: Option<(String, Value)>,
    review: Option<Review>,
    outcome: Option<Outcome>,
    snapshot: Option<Snapshot>,
}
enum Action {
    Read,
    Validate {
        token: String,
        field: String,
        value: Value,
    },
    Save {
        token: String,
        reviewed: Option<Box<compact::Review>>,
    },
}
impl Management {
    pub(super) fn compaction_settings_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && self.settings_edit.as_ref().is_some_and(|p| {
                matches!(p.route, Route::Compaction(_))
                    && p.phase != Phase::State
                    && frame["id"] == p.id.to_value()
            })
    }
    pub(super) fn request_settings_editor(&mut self, intent: view::Intent) -> io::Result<Value> {
        let (route, action) = match intent {
            view::Intent::Editor(budget::Intent::Read) => (Route::Budget, Action::Read),
            view::Intent::Editor(budget::Intent::Validate {
                snapshot,
                field,
                value,
            }) => (
                Route::Budget,
                Action::Validate {
                    token: snapshot,
                    field,
                    value: json!(value),
                },
            ),
            view::Intent::Editor(budget::Intent::Save { review }) => (
                Route::Budget,
                Action::Save {
                    token: review,
                    reviewed: None,
                },
            ),
            view::Intent::Compaction(compact::Intent::Read { generation }) => {
                (Route::Compaction(generation), Action::Read)
            }
            view::Intent::Compaction(compact::Intent::Validate {
                generation,
                snapshot,
                field,
                value,
            }) => (
                Route::Compaction(generation),
                Action::Validate {
                    token: snapshot,
                    field,
                    value,
                },
            ),
            view::Intent::Compaction(compact::Intent::Save { generation, review }) => (
                Route::Compaction(generation),
                Action::Save {
                    token: review.token.clone(),
                    reviewed: Some(review),
                },
            ),
            view::Intent::Read
            | view::Intent::RuntimeStatus
            | view::Intent::Mcp(_)
            | view::Intent::McpLifecycle(_)
            | view::Intent::ModelPreferences(_)
            | view::Intent::Voice(_)
            | view::Intent::Presentation(_)
            | view::Intent::Connection(_)
            | view::Intent::Agent(_)
            | view::Intent::Glassbox(_)
            | view::Intent::Monitor(_)
            | view::Intent::ClaudeReset(_)
            | view::Intent::Sidecar(_)
            | view::Intent::ModelLifecycle(_)
            | view::Intent::Form(_) => {
                return Err(fail("Expected a reviewed settings editor operation"));
            }
        };
        if !self.ready() {
            return Err(fail("Finish current work before editing settings"));
        }
        self.ensure_drained()?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Runtime omitted owned settings authority"))?;
        if let Route::Compaction(generation) = route {
            if generation <= self.settings_generation {
                return Err(fail("Compaction preferences request is stale"));
            }
            self.settings_generation = generation;
            if ![compact::READ, compact::VALIDATE, compact::SAVE]
                .iter()
                .all(|op| self.operations.iter().any(|v| v == op))
            {
                return Err(fail(
                    "Runtime lacks reviewed compaction preferences; update Python source",
                ));
            }
        }
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        let (phase, mut command, selection, review) = match action {
            Action::Read => {
                self.settings_snapshot = None;
                self.settings_review = None;
                (
                    Phase::Read,
                    route.read_command(session, agent, backend, model),
                    None,
                    None,
                )
            }
            Action::Validate {
                token,
                field,
                value,
            } => {
                self.settings_review = None;
                let current = self
                    .settings_snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Refresh settings authority before validating"))?;
                let mut command = current.request(route, &token, &field, &value)?;
                command["type"] = json!(route.validate());
                (Phase::Validate, command, Some((field, value)), None)
            }
            Action::Save { token, reviewed } => {
                let current = self
                    .settings_review
                    .take()
                    .ok_or_else(|| fail("Validate the edit again before saving"))?;
                let mut command = current.request(route, &token, reviewed.as_deref())?;
                command["type"] = json!(route.save());
                (Phase::Save, command, None, Some(current))
            }
        };
        let id = if phase == Phase::Save {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve settings operation"))?;
        command["id"] = id.to_value();
        self.settings_edit = Some(Pending {
            id,
            route,
            phase,
            selection,
            review,
            outcome: None,
            snapshot: None,
        });
        Ok(command)
    }
    fn settings_closed(&mut self, route: Route) -> Step {
        self.settings_snapshot = None;
        self.settings_review = None;
        Step::Settings {
            update: route.rejected(
                "Settings response consumed during shutdown; reopen to verify saved preferences."
                    .into(),
            ),
            policy: None,
        }
    }
    pub(super) fn settings_edit_response(&mut self, frame: Value) -> io::Result<Step> {
        self.ensure_drained()?;
        let mut pending = self.settings_edit.take().expect("pending settings edit");
        let route = pending.route;
        let publish = |update, policy| Step::Settings { update, policy };
        if frame["ok"] != true {
            self.settings_review = None;
            self.settings_snapshot = None;
            let reason = text(&frame, "error", 4096)?;
            if matches!(pending.phase, Phase::Save | Phase::Readback | Phase::State) {
                return Err(fail(&format!("Settings save/readback failed: {reason}")));
            }
            return Ok(publish(route.rejected(reason.into()), None));
        }
        let value = &frame["result"];
        let (session, backend, model, profile) = self.expected.as_ref().expect("active");
        let (agent, schedules) = self.browser_authority.as_ref().expect("owned");
        match pending.phase {
            Phase::Read => {
                let snapshot = Snapshot::parse(route, value, session, agent, backend, model)?;
                if self.closing {
                    return Ok(self.settings_closed(route));
                }
                let update = snapshot.update(route)?;
                self.settings_snapshot = Some(snapshot);
                return Ok(publish(update, None));
            }
            Phase::Validate => {
                let (field, selected) = pending.selection.as_ref().expect("selection");
                let snapshot = self
                    .settings_snapshot
                    .as_ref()
                    .expect("validated selection");
                let review = Review::parse(value, snapshot, field, selected)?;
                if self.closing {
                    return Ok(self.settings_closed(route));
                }
                let update = review.update(route)?;
                self.settings_review = Some(review);
                return Ok(publish(update, None));
            }
            Phase::Save => {
                match Outcome::parse(value, pending.review.as_ref().expect("confirmed edit"))? {
                    Err(reason) => {
                        self.settings_snapshot = None;
                        return Ok(publish(route.rejected(reason), None));
                    }
                    Ok(outcome) => pending.outcome = Some(outcome),
                }
                pending.phase = Phase::Readback;
            }
            Phase::Readback => {
                let snapshot = Snapshot::parse(route, value, session, agent, backend, model)?;
                pending
                    .outcome
                    .as_ref()
                    .expect("receipt")
                    .verify(&snapshot, pending.review.as_ref().expect("review"))?;
                pending.snapshot = Some(snapshot);
                pending.phase = Phase::State;
            }
            Phase::State => {
                let state = State::parse(value)?;
                if (&state.session, &state.backend, &state.model, &state.profile)
                    != (session, backend, model, profile)
                    || state.held_restored_input != Some(0)
                    || state.schedules_paused != *schedules
                    || value["ownership"]["agent_id"] != *agent
                {
                    return Err(fail(
                        "Settings save readback changed runtime identity or policy",
                    ));
                }
                let snapshot = pending.snapshot.expect("independent settings snapshot");
                snapshot.verify_state(value)?;
                let inspection = view::Snapshot::parse(value, session)?;
                if self.closing {
                    return Ok(self.settings_closed(route));
                }
                let update = pending
                    .outcome
                    .expect("saved receipt")
                    .update(&snapshot, route, inspection)?;
                self.settings_snapshot = Some(snapshot);
                return Ok(publish(update, Some(value["tool_policy"].clone())));
            }
        }
        if self.closing {
            return Ok(self.settings_closed(route));
        }
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve settings readback"))?;
        let mut command = if pending.phase == Phase::Readback {
            route.read_command(session, agent, backend, model)
        } else {
            json!({"type":"gui.state","include_conversations":false})
        };
        command["id"] = id.to_value();
        pending.id = id;
        self.settings_edit = Some(pending);
        Ok(Step::Write(command))
    }
}

/// `/settings`: one read, or one save followed by a runtime-state readback that
/// proves the save left the session, backend, model, agent and policy as they were.
pub(super) struct FormPending {
    pub(super) id: CommandId,
    save: bool,
    saved: Option<form::Update>,
}
impl Management {
    pub(super) fn request_settings_form(&mut self, intent: form::Intent) -> io::Result<Value> {
        if !self.ready() {
            return Err(fail("Finish current work before opening settings"));
        }
        self.ensure_drained()?;
        if ![form::READ, form::SAVE]
            .iter()
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail(
                "Runtime lacks the native settings dialog; update Python source",
            ));
        }
        if self.browser_authority.is_none() {
            return Err(fail("Runtime omitted owned settings authority"));
        }
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        let save = matches!(intent, form::Intent::Save { .. });
        let (mut command, id) = match intent {
            form::Intent::Read => (
                json!({"type":form::READ,"version":1,"session_id":session}),
                self.wire.register_read(),
            ),
            form::Intent::Save {
                form: token,
                values,
            } => (
                json!({"type":form::SAVE,"version":1,"session_id":session,"form":token,"values":values}),
                self.wire.register_request(),
            ),
        };
        let id = id.map_err(|_| fail("Cannot reserve settings operation"))?;
        command["id"] = id.to_value();
        self.settings_form = Some(FormPending {
            id,
            save,
            saved: None,
        });
        Ok(command)
    }
    pub(super) fn settings_form_response(&mut self, frame: Value) -> io::Result<Step> {
        self.ensure_drained()?;
        let mut pending = self.settings_form.take().expect("pending settings form");
        let publish = |update, policy| Step::Settings {
            update: view::Update::Form(update),
            policy,
        };
        if self.closing {
            return Ok(publish(
                form::Update::Rejected(
                    "Settings response consumed during shutdown; reopen to verify saved settings."
                        .into(),
                ),
                None,
            ));
        }
        if frame["ok"] != true {
            let reason = text(&frame, "error", 4096)?;
            if pending.saved.is_some() {
                return Err(fail(&format!("Settings save readback failed: {reason}")));
            }
            return Ok(publish(form::Update::Rejected(reason.into()), None));
        }
        let value = &frame["result"];
        let (session, backend, model, profile) = self.expected.as_ref().expect("active");
        let (agent, schedules) = self.browser_authority.as_ref().expect("owned");
        if let Some(saved) = pending.saved.take() {
            let state = State::parse(value)?;
            if (&state.session, &state.backend, &state.model, &state.profile)
                != (session, backend, model, profile)
                || state.held_restored_input != Some(0)
                || state.schedules_paused != *schedules
                || value["ownership"]["agent_id"] != *agent
            {
                return Err(fail(
                    "Settings save changed runtime identity or policy; reconnect required",
                ));
            }
            let saved = match saved {
                form::Update::Saved {
                    status,
                    message,
                    form,
                    ..
                } => form::Update::Saved {
                    status,
                    message,
                    form,
                    presentation: Some(state.presentation.clone()),
                },
                other => other,
            };
            return Ok(publish(saved, Some(value["tool_policy"].clone())));
        }
        if !pending.save {
            let read = form::Form::parse(value, "settings_form", session, agent, backend, model)?;
            return Ok(publish(form::Update::Form(Box::new(read)), None));
        }
        let saved = form::Update::saved(value, session, agent, backend, model)?;
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve settings readback"))?;
        let mut command = json!({"type":"gui.state","include_conversations":false});
        command["id"] = id.to_value();
        self.settings_form = Some(FormPending {
            id,
            save: true,
            saved: Some(saved),
        });
        Ok(Step::Write(command))
    }
}
