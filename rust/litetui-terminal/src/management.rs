//! Owned-runtime negotiation and bounded startup facts. A snapshot is not a
//! stream barrier or an authority grant. Only a fresh idle session is admitted.
use crate::model_controls::{Choices, Intent, Kind, Update};
use crate::protocol::{CommandId, Delivery, Notice, WireClient};
use crate::stream::{ExecutionId, Origin};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

const GENERATION: u64 = 1;
// These producers call app._rpc_emit. Responses/errors use the direct RPC sink.
// Unknown producers remain fatal rather than being silently discarded.
const MIRRORED: &[&str] = &[
    "ready",
    "model_state",
    "usage",
    "turn_start",
    "turn_refused",
    "turn_end",
    "text_delta",
    "reasoning_delta",
    "native_text_snapshot",
    "native_activity",
    "native_usage",
    "native_event",
    "tool_approval_requested",
    "tool_approval_closed",
    "user_input_requested",
    "user_input_resolved",
    "submit_refused",
    "model_load_requested",
    "cache_warning",
    "tool_call",
    "generic_tool_requested",
    "generic_tool_returned",
];
const IDENTIFIED: &[&str] = &[
    "turn_start",
    "turn_refused",
    "turn_end",
    "text_delta",
    "reasoning_delta",
    "native_text_snapshot",
    "native_activity",
    "native_usage",
    "native_event",
    "generic_tool_requested",
    "generic_tool_returned",
];

fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text<'a>(value: &'a Value, key: &str, max: usize) -> io::Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn boolean(value: &Value, key: &str) -> io::Result<bool> {
    value
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn rows(value: &Value, key: &str) -> io::Result<usize> {
    value
        .get(key)
        .and_then(Value::as_array)
        .filter(|v| v.len() <= 10_000 && v.iter().all(Value::is_object))
        .map(Vec::len)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn names(value: &Value, key: &str) -> io::Result<Vec<String>> {
    let entries = value
        .get(key)
        .and_then(Value::as_array)
        .filter(|v| v.len() <= 256)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))?;
    let mut seen = HashSet::new();
    entries
        .iter()
        .map(|v| {
            let name = v
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 128)
                .ok_or_else(|| fail("Invalid management capability name"))?;
            if !seen.insert(name) {
                return Err(fail("Duplicate management capability"));
            }
            Ok(name.to_owned())
        })
        .collect()
}

fn restored_input(value: &Value) -> io::Result<Option<usize>> {
    let Some(status) = value.get("restored_input") else {
        return Ok(None);
    };
    let held = status["held"]
        .as_u64()
        .filter(|count| *count <= 1000)
        .ok_or_else(|| fail("Invalid restored-input count"))? as usize;
    match status["policy"].as_str() {
        Some("hold") => Ok(Some(held)),
        Some("automatic") if held == 0 => Ok(None),
        _ => Err(fail("Invalid restored-input policy")),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub session: String,
    pub model: String,
    pub backend: String,
    pub profile: String,
    pub mode: String,
    pub writable: bool,
    pub held_restored_input: Option<usize>,
    pub schedules_paused: bool,
    pub conversations: usize,
    pub jobs: usize,
    pub tasks: usize,
}
impl Snapshot {
    fn parse(value: &Value) -> io::Result<Self> {
        if boolean(value, "busy")?
            || rows(value, "active_work")? != 0
            || rows(value, "pending_approvals")? != 0
            || rows(value, "pending_questions")? != 0
            || rows(value, "pending_model_loads")? != 0
        {
            return Err(fail(
                "Runtime startup state is not idle; no prompt was admitted",
            ));
        }
        let ownership = &value["ownership"];
        if ownership.get("data_version").and_then(Value::as_u64) != Some(1) {
            return Err(fail("Incompatible management data version"));
        }
        let mode = text(value, "mode", 32)?;
        if !matches!(mode, "plan" | "normal") {
            return Err(fail("Unknown management mode"));
        }
        Ok(Self {
            session: text(value, "session_id", 128)?.to_owned(),
            model: text(&value["model"], "model", 1024)?.to_owned(),
            backend: text(&value["model"], "backend", 64)?.to_owned(),
            profile: text(value, "tool_profile", 32)?.to_owned(),
            mode: mode.to_owned(),
            writable: boolean(ownership, "writable")?,
            held_restored_input: restored_input(value)?,
            schedules_paused: boolean(value, "schedules_paused")?,
            conversations: rows(value, "conversations")?,
            jobs: rows(value, "jobs")?,
            tasks: rows(value, "tasks")?,
        })
    }
}
#[derive(Debug)]
pub enum Step {
    Engine {
        update: crate::engine_controls::Update,
        policy: Option<Value>,
        state: Option<Snapshot>,
    },
    Bootstrap(Value),
    Write(Value),
    Initialized(Snapshot),
    Restored {
        snapshot: Snapshot,
        history: crate::resume::History,
    },
    Control(Update),
    Queue(crate::restored_queue::Update),
    Commands {
        update: crate::command_palette::Update,
        policy: Option<Value>,
    },
    Settings {
        update: crate::settings_view::Update,
        policy: Option<Value>,
    },
    Backend(crate::backend_controls::Update),
    BackendChanged(Box<crate::backend_controls::Changed>),
    Browser(crate::conversation_browser::Update),
    Opened(Box<crate::conversation_browser::Opened>),
    Event(Value),
    Ignore,
}
#[derive(Clone, Copy, PartialEq)]
enum Phase {
    AwaitReady,
    Hello,
    State,
    History,
    Active,
}

struct ControlPending {
    id: CommandId,
    kind: Kind,
    applying: bool,
    selection: Option<String>,
    mutation_id: Option<CommandId>,
    failure: Option<String>,
    succeeded: bool,
}
pub struct Continuation {
    pub command: Value,
    pub prompt: String,
    pub session: String,
    pub entry: String,
    record: crate::restored_queue::Entry,
}
struct Reconciliation {
    entry: crate::restored_queue::Entry,
    accepted: Option<bool>,
}
struct QueueClosure {
    entry: String,
    resolved: bool,
}
struct QueuePending {
    id: CommandId,
    applying: bool,
    closed: Option<QueueClosure>,
    continued: Option<(String, crate::restored_queue::Entry, bool)>,
    reconciled: Option<Reconciliation>,
    failure: Option<String>,
    succeeded: bool,
}
enum BrowserRequest {
    Preview {
        target: String,
        agent: String,
        snapshot: String,
    },
    Page {
        previous: Option<crate::conversation_browser::Page>,
        cursor: Option<String>,
    },
    Open {
        target: String,
        agent: String,
        mutation: CommandId,
        stage: u8,
        history: Option<crate::resume::History>,
        state: Option<Snapshot>,
        policy: Option<Value>,
        model: Option<String>,
    },
}
struct BrowserPending {
    id: CommandId,
    request: BrowserRequest,
}
struct BackendPending {
    id: CommandId,
    stage: u8,
    selection: Option<(String, String)>,
    catalog: Option<crate::backend_controls::Catalog>,
    mutation: Option<CommandId>,
    history: Option<crate::resume::History>,
    state: Option<Snapshot>,
    policy: Option<Value>,
    observed_model: bool,
}
struct SettingsEditPending {
    id: CommandId,
    stage: u8,
    selection: Option<(String, u64)>,
    review: Option<crate::settings_editor::Review>,
    outcome: Option<crate::settings_editor::Outcome>,
    snapshot: Option<crate::settings_editor::Snapshot>,
}
struct CommandPending {
    id: Option<CommandId>,
    stage: u8,
    raw: Option<String>,
    row: Option<String>,
    arguments: String,
    skills: bool,
    reviewed: Option<crate::prepared_skill::Prepared>,
}
struct EnginePending {
    id: Option<CommandId>,
    stage: u8,
    action: Option<crate::engine_controls::Action>,
    snapshot: Option<crate::engine_controls::Snapshot>,
    review: Option<crate::engine_controls::Review>,
    receipt: Option<crate::engine_controls::Receipt>,
    history: Option<crate::resume::History>,
    policy: Option<Value>,
    state: Option<Snapshot>,
    mutation: Option<CommandId>,
}
pub struct Management {
    closing: bool,
    engine: Option<EnginePending>,
    engine_instance: Option<(String, u64, crate::engine_controls::Admission)>,
    engine_snapshot: Option<crate::engine_controls::Snapshot>,
    engine_review: Option<(crate::engine_controls::Review, crate::resume::History)>,

    wire: WireClient,
    control: Option<ControlPending>,
    queue: Option<QueuePending>,
    queue_menu: Option<crate::restored_queue::Snapshot>,
    settings: Option<CommandId>,
    command: Option<CommandPending>,
    command_catalog: Option<crate::command_palette::Catalog>,
    command_prepared: Option<crate::prepared_skill::Prepared>,
    settings_edit: Option<SettingsEditPending>,
    settings_snapshot: Option<crate::settings_editor::Snapshot>,
    settings_review: Option<crate::settings_editor::Review>,
    backend: Option<BackendPending>,
    backend_catalog: Option<crate::backend_controls::Catalog>,
    browser: Option<BrowserPending>,
    browser_page: Option<crate::conversation_browser::Page>,
    browser_authority: Option<(String, bool)>,
    continuation: Option<(String, crate::restored_queue::Entry)>,
    menu: Option<Choices>,
    revision: u64,
    phase: Phase,
    expected: Option<(String, String, String, String)>,
    state_id: Option<CommandId>,
    resume: Option<String>,
    startup: Option<Snapshot>,
    history_id: Option<CommandId>,
    operations: Vec<String>,
    prompt: Option<String>,
    origin: Option<(ExecutionId, Origin)>,
    executions: HashSet<String>,
    failed: bool,
    mirrors: Vec<(Value, usize)>,
    mirror_bytes: usize,
}
impl Default for Management {
    fn default() -> Self {
        Self {
            wire: WireClient::new(
                GENERATION,
                2,
                MIRRORED
                    .iter()
                    .map(|name| ((*name).to_owned(), Delivery::Mirrored)),
            )
            .expect("static delivery policy"),
            closing: false,
            engine: None,
            engine_instance: None,
            engine_snapshot: None,
            engine_review: None,
            control: None,
            queue: None,
            queue_menu: None,
            settings: None,
            command: None,
            command_catalog: None,
            command_prepared: None,
            settings_edit: None,
            settings_snapshot: None,
            settings_review: None,
            backend: None,
            backend_catalog: None,
            browser: None,
            browser_page: None,
            browser_authority: None,
            continuation: None,
            menu: None,
            revision: 0,
            phase: Phase::AwaitReady,
            expected: None,
            state_id: None,
            resume: None,
            startup: None,
            history_id: None,
            operations: Vec::new(),
            prompt: None,
            origin: None,
            executions: HashSet::new(),
            failed: false,
            mirrors: Vec::new(),
            mirror_bytes: 0,
        }
    }
}
impl Management {
    pub fn new(resume: Option<String>) -> Self {
        Self {
            resume,
            ..Self::default()
        }
    }
    // Called only after Turn and the read-only ready-fact parser validate readiness.
    pub fn begin(&mut self, ready: &Value) -> io::Result<Value> {
        if self.failed || self.phase != Phase::AwaitReady {
            return Err(fail("Management startup already attempted"));
        }
        let result = self.begin_inner(ready);
        if result.is_err() {
            self.failed = true;
        }
        result
    }
    fn begin_inner(&mut self, ready: &Value) -> io::Result<Value> {
        if !ready
            .get("management_protocols")
            .and_then(Value::as_array)
            .is_some_and(|v| v.iter().any(|v| v.as_u64() == Some(1)))
        {
            return Err(fail(
                "Runtime lacks management protocol 1; update the Python source",
            ));
        }
        let capabilities = names(ready, "capabilities")?;
        if !["hello", "state"]
            .iter()
            .all(|s| capabilities.iter().any(|v| v == s))
        {
            return Err(fail("Runtime lacks hello/state management capabilities"));
        }
        if ready.get("management_instance").is_some() {
            let instance = text(ready, "management_instance", 64)?.to_owned();
            if instance.len() != 64
                || !instance
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || ready["desired_model"] != ready["model"]
            {
                return Err(fail("Invalid engine startup identity"));
            }
            self.engine_instance = Some((
                instance,
                ready["engine_generation"]
                    .as_u64()
                    .ok_or_else(|| fail("Invalid engine generation"))?,
                crate::engine_controls::Admission::parse(&ready["admission"])?,
            ));
        }
        self.expected = Some((
            text(ready, "conversation_id", 128)?.to_owned(),
            text(ready, "backend", 64)?.to_owned(),
            text(ready, "model", 1024)?.to_owned(),
            text(ready, "tool_profile", 32)?.to_owned(),
        ));
        if self
            .resume
            .as_deref()
            .is_some_and(|id| ready["conversation_id"] != id)
        {
            return Err(fail("Runtime did not resume the requested conversation"));
        }
        self.phase = Phase::Hello;
        self.wire
            .begin_hello()
            .map_err(|_| fail("Cannot begin management negotiation"))
    }
    pub fn begin_close(&mut self) {
        self.closing = true;
        self.engine_review = None;
        self.engine_snapshot = None;
    }
    pub fn ready(&self) -> bool {
        self.phase == Phase::Active
            && !self.closing
            && !self.failed
            && self.engine.is_none()
            && self.command.is_none()
            && self.settings.is_none()
            && self.settings_edit.is_none()
            && self.backend.is_none()
            && self.browser.is_none()
            && self.control.is_none()
            && self.queue.is_none()
            && self.continuation.is_none()
    }
    pub fn operations(&self) -> &[String] {
        &self.operations
    }
    pub fn prompt(&mut self, id: &str) -> io::Result<()> {
        if !self.ready()
            || self
                .engine_instance
                .as_ref()
                .is_some_and(|v| v.2 != crate::engine_controls::Admission::PromptReady)
            || id.is_empty()
            || id.len() > 128
        {
            return Err(fail("Management is not ready for a prompt"));
        }
        self.ensure_drained()?;
        self.menu = None;
        self.queue_menu = None;
        self.backend_catalog = None;
        self.browser_page = None;
        self.prompt = Some(id.to_owned());
        self.origin = None;
        Ok(())
    }
    /// Raw/envelope pairing is checked without deduplicating selected events.
    /// Cross-thread interleaving may reorder pairs; identical events still each
    /// consume one raw occurrence. An unmatched suffix can never be clean EOF.
    pub fn has_unpaired(&self) -> bool {
        !self.mirrors.is_empty()
    }
    pub fn ensure_drained(&self) -> io::Result<()> {
        if self.mirrors.is_empty() {
            Ok(())
        } else {
            Err(fail("Runtime omitted an expected GUI event envelope"))
        }
    }
    pub fn accept(&mut self, frame: Value) -> io::Result<Step> {
        if self.failed {
            return Err(fail("Management connection is uncertain"));
        }
        let result = self.accept_inner(frame);
        if result.is_err() {
            self.failed = true;
        }
        result
    }
    fn accept_inner(&mut self, frame: Value) -> io::Result<Step> {
        match self.wire.ingest(GENERATION, frame) {
            Notice::LegacyEvent(value)
                if self.phase == Phase::AwaitReady && value["type"] == "ready" =>
            {
                Ok(Step::Bootstrap(value))
            }
            Notice::Hello(frame) if self.phase == Phase::Hello => {
                let result = &frame["result"];
                let capabilities = names(result, "capabilities")?;
                let operations = names(result, "operations")?;
                if !["hello", "state"]
                    .iter()
                    .all(|name| capabilities.iter().any(|v| v == name))
                    || !["gui.hello", "gui.state"]
                        .iter()
                        .all(|name| operations.iter().any(|v| v == name))
                {
                    return Err(fail("Negotiated runtime lacks hello/state capabilities"));
                }
                self.operations = operations;
                let id = self
                    .wire
                    .register_read()
                    .map_err(|_| fail("Cannot reserve state read"))?;
                let command =
                    json!({"type":"gui.state","id":id.to_value(),"include_conversations":false});
                self.state_id = Some(id);
                self.phase = Phase::State;
                Ok(Step::Write(command))
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if id == self.state_id && self.phase == Phase::State => {
                if frame["ok"] != true {
                    return Err(fail("Runtime rejected startup state read"));
                }
                self.ensure_drained()?;
                let snapshot = Snapshot::parse(&frame["result"])?;
                let (session, backend, model, profile) = self.expected.as_ref().expect("begun");
                if (
                    &snapshot.session,
                    &snapshot.backend,
                    &snapshot.model,
                    &snapshot.profile,
                ) != (session, backend, model, profile)
                {
                    return Err(fail(
                        "Runtime state differs from validated startup identity/policy",
                    ));
                }
                self.browser_authority = frame["result"]["ownership"]["agent_id"]
                    .as_str()
                    .filter(|id| !id.is_empty() && id.len() <= 128)
                    .map(|id| (id.to_owned(), snapshot.schedules_paused));
                self.state_id = None;
                if let Some(session) = &self.resume {
                    if snapshot.held_restored_input.is_none() {
                        return Err(fail(
                            "Resume requires verified held-input policy; update Python source",
                        ));
                    }
                    if !snapshot.writable
                        || !self
                            .operations
                            .iter()
                            .any(|op| op == "gui.conversations.read")
                    {
                        return Err(fail(
                            "Resume requires writable owned state and transcript read support",
                        ));
                    }
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve saved transcript read"))?;
                    let command = json!({"type":"gui.conversations.read","id":id.to_value(),"session_id":session});
                    self.history_id = Some(id);
                    self.startup = Some(snapshot);
                    self.phase = Phase::History;
                    return Ok(Step::Write(command));
                }
                self.phase = Phase::Active;
                Ok(Step::Initialized(snapshot))
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self.phase == Phase::History && id == self.history_id => {
                if frame["ok"] != true {
                    return Err(fail("Runtime rejected saved transcript read"));
                }
                self.ensure_drained()?;
                let history = crate::resume::History::parse(
                    &frame["result"],
                    self.resume.as_deref().expect("resume"),
                )?;
                self.history_id = None;
                self.phase = Phase::Active;
                Ok(Step::Restored {
                    snapshot: self.startup.take().expect("startup"),
                    history,
                })
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .engine
                .as_ref()
                .is_some_and(|p| p.id.as_ref() == id.as_ref()) =>
            {
                self.engine_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .control
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.control_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .queue
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.queue_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .browser
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.browser_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .backend
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.backend_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .command
                .as_ref()
                .is_some_and(|p| p.id.as_ref() == id.as_ref()) =>
            {
                self.command_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .settings_edit
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.settings_edit_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if id == self.settings && self.settings.is_some() => self.settings_response(frame),
            Notice::Response {
                matched: false,
                frame,
                ..
            } => Ok(Step::Event(frame)),
            Notice::DirectError(frame) => Ok(Step::Event(frame)),
            Notice::IgnoredMirror(value) => {
                let bytes = serde_json::to_vec(&value).map_err(io::Error::other)?.len();
                if self.mirrors.len() >= 64
                    || self.mirror_bytes.saturating_add(bytes) > 4 * 1024 * 1024
                {
                    return Err(fail("Unpaired runtime event capacity exceeded"));
                }
                self.mirror_bytes += bytes;
                self.mirrors.push((value, bytes));
                Ok(Step::Ignore)
            }
            Notice::Event { frame, event } if self.phase == Phase::Active => {
                let index = self
                    .mirrors
                    .iter()
                    .position(|(raw, _)| raw == &event)
                    .ok_or_else(|| fail("GUI event has no matching legacy occurrence"))?;
                let (_, bytes) = self.mirrors.remove(index);
                self.mirror_bytes -= bytes;
                self.validate_event(&frame, &event)?;
                if self
                    .browser
                    .as_ref()
                    .is_some_and(|pending| matches!(pending.request, BrowserRequest::Open { .. }))
                    || self
                        .backend
                        .as_ref()
                        .is_some_and(|pending| pending.mutation.is_some())
                {
                    Ok(Step::Ignore)
                } else {
                    Ok(Step::Event(event))
                }
            }
            Notice::Fault { reason, .. } => {
                Err(fail(&format!("Management protocol fault: {reason:?}")))
            }
            _ => Err(fail(
                "Unexpected event during management startup; state continuity is uncertain",
            )),
        }
    }
    fn validate_event(&mut self, frame: &Value, event: &Value) -> io::Result<()> {
        let (session, backend, model, _) = self.expected.as_ref().expect("begun");
        if let Some(pending) = self.engine.as_ref()
            && let Some(mutation) = &pending.mutation
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Engine lifecycle event lacks emission identity"))?;
            if frame["session_id"] != *session
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !matches!(event["type"].as_str(), Some("model_state" | "usage"))
            {
                return Err(fail("Unexpected event during owned engine lifecycle"));
            }
            if event["type"] == "model_state"
                && (event["backend"] != *backend || event["model"] != *model)
            {
                return Err(fail("Engine event changed desired authority"));
            }
            return Ok(());
        }
        if let Some(pending) = self.backend.as_mut()
            && let Some(mutation) = &pending.mutation
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Backend transition lacks emission identity"))?;
            let (target, target_model) = pending.selection.as_ref().expect("selection");
            let kind = event["type"].as_str();
            if frame["session_id"] != *session
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !matches!(kind, Some("model_state" | "usage"))
            {
                return Err(fail("Unexpected event during owned backend transition"));
            }
            if kind == Some("model_state") {
                if event["backend"] != *target || event["model"] != *target_model {
                    return Err(fail("Backend transition event changed the explicit target"));
                }
                pending.observed_model = true;
            }
            return Ok(());
        }
        if let Some(BrowserPending {
            request:
                BrowserRequest::Open {
                    target,
                    mutation,
                    model: transition_model,
                    ..
                },
            ..
        }) = self.browser.as_mut()
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Conversation transition lacks emission identity"))?;
            let event_session = frame["session_id"].as_str();
            let kind = event["type"].as_str();
            if !matches!(kind, Some("model_state" | "usage"))
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !(event_session == Some(session.as_str())
                    || event_session == Some(target.as_str()))
            {
                return Err(fail("Unexpected event during owned conversation opening"));
            }
            if kind == Some("model_state") {
                if event_session != Some(target.as_str()) || event["backend"] != *backend {
                    return Err(fail(
                        "Conversation transition changed backend or session unexpectedly",
                    ));
                }
                *transition_model = Some(text(event, "model", 1024)?.to_owned());
            }
            return Ok(());
        }
        if frame.get("session_id").and_then(Value::as_str) != Some(session.as_str()) {
            return Err(fail("Event belongs to another conversation"));
        }
        crate::stream::required_uuid4(frame, "event_id")
            .map_err(|_| fail("GUI event lacks a canonical emission identity"))?;
        let kind = event["type"].as_str().expect("wire validated");
        if kind == "model_state" && (event["backend"] != *backend || event["model"] != *model) {
            let owned_change = event["backend"] == *backend
                && self.control.as_ref().is_some_and(|pending| {
                    pending.kind == Kind::Model
                        && pending.selection.as_deref() == event["model"].as_str()
                        && pending
                            .mutation_id
                            .as_ref()
                            .is_some_and(|id| frame["operation_id"] == id.to_value())
                });
            if !owned_change {
                return Err(fail(
                    "Runtime model changed outside an owned management operation",
                ));
            }
        }
        if IDENTIFIED.contains(&kind) {
            let (execution, origin, _) = crate::stream::origin(frame, 4096)
                .map_err(|_| fail("Runtime turn lacks a valid captured execution origin"))?;
            if origin.operation.as_ref().and_then(Value::as_str) != self.prompt.as_deref()
                || self.prompt.is_none()
            {
                return Err(fail("Runtime event belongs to another prompt operation"));
            }
            if matches!(kind, "turn_start" | "turn_refused") {
                if self.origin.is_some()
                    || self.executions.len() >= 1000
                    || !self.executions.insert(execution.as_str().to_owned())
                {
                    return Err(fail("Duplicate or excessive runtime execution identity"));
                }
                self.origin = Some((execution, origin));
            } else if self.origin.as_ref() != Some(&(execution, origin)) {
                return Err(fail("Runtime execution origin changed or has no start"));
            }
        }
        Ok(())
    }
}

impl Management {
    pub fn backend(&self) -> Option<&str> {
        self.expected.as_ref().map(|v| v.1.as_str())
    }
    pub fn model(&self) -> Option<&str> {
        self.expected.as_ref().map(|v| v.2.as_str())
    }
    pub fn request_control(&mut self, intent: Intent) -> io::Result<Value> {
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        let (kind, selection) = match intent {
            Intent::Open(kind) => (kind, None),
            Intent::Select { revision, value } => {
                let menu = self
                    .menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Choices changed; reopen the selector"))?;
                if !menu.rows.iter().any(|row| row.value == value) {
                    return Err(fail("Choice was not offered by the current runtime"));
                }
                (menu.kind, Some(value))
            }
        };
        let operation = if selection.is_some() {
            kind.apply()
        } else {
            kind.read()
        };
        if !self.operations.iter().any(|v| v == operation)
            || !self.operations.iter().any(|v| v == kind.read())
        {
            return Err(fail("The connected runtime does not support this control"));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve model control"))?;
        let mut command = json!({"type":operation,"id":id.to_value()});
        if let Some(value) = &selection {
            command[if kind == Kind::Model { "slug" } else { "level" }] =
                Value::String(value.clone());
        } else if kind == Kind::Thinking {
            let (_, backend, model, _) = self.expected.as_ref().expect("active");
            command["backend"] = json!(backend);
            command["model"] = json!(model);
        }
        self.control = Some(ControlPending {
            id: id.clone(),
            kind,
            applying: selection.is_some(),
            mutation_id: selection.as_ref().map(|_| id),
            selection,
            failure: None,
            succeeded: false,
        });
        self.menu = None;
        self.queue_menu = None;
        self.browser_page = None;
        Ok(command)
    }
    fn control_response(&mut self, frame: Value) -> io::Result<Step> {
        let mut pending = self.control.take().expect("matched control");
        let success = frame["ok"] == true;
        let error = if success {
            None
        } else {
            Some(text(&frame, "error", 4096)?.to_owned())
        };
        if pending.applying {
            pending.applying = false;
            pending.failure = error;
            pending.succeeded = success;
            let id = self
                .wire
                .register_request()
                .map_err(|_| fail("Cannot reserve selection readback"))?;
            let mut command = json!({"type":pending.kind.read(),"id":id.to_value()});
            if pending.kind == Kind::Thinking {
                let (_, backend, model, _) = self.expected.as_ref().expect("active");
                command["backend"] = json!(backend);
                command["model"] = json!(model);
            }
            pending.id = id;
            self.control = Some(pending);
            return Ok(Step::Write(command));
        }
        if let Some(error) = error {
            return if pending.selection.is_some() {
                Err(fail(&format!(
                    "Cannot verify runtime after selection: {error}"
                )))
            } else {
                Ok(Step::Control(Update::Rejected(error)))
            };
        }
        self.ensure_drained()?;
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| fail("Choice revision exhausted"))?;
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        let mut menu = Choices::parse(pending.kind, &frame["result"], self.revision, session)?;
        if menu.backend != *backend {
            return Err(fail("Control readback changed the backend unexpectedly"));
        }
        if menu.model != *model
            && !(pending.kind == Kind::Model
                && pending.selection.as_deref() == Some(menu.model.as_str()))
        {
            return Err(fail("Control readback changed the model unexpectedly"));
        }
        if pending.succeeded && pending.selection.as_deref() != Some(menu.current.as_str()) {
            return Err(fail("Runtime did not retain the requested selection"));
        }
        if let Some(error) = pending.failure {
            menu.message = Some(format!(
                "Request failed: {error}. Current setting read back from runtime."
            ));
        } else if pending.selection.is_some() {
            menu.message = Some(format!("Current setting: {}", menu.current));
        }
        if menu.model != *model {
            self.expected.as_mut().expect("active").2 = menu.model.clone();
            self.origin = None;
            self.prompt = None;
        }
        self.menu = Some(menu.clone());
        Ok(Step::Control(Update::Choices(menu)))
    }
}

impl Management {
    pub fn request_queue(&mut self, intent: crate::restored_queue::Intent) -> io::Result<Value> {
        use crate::restored_queue::{CANCEL, Intent, READ, RECONCILE, RESOLVE};
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        let mut command = json!({"type":READ, "session_id":session});
        let mut reconciled = None;
        let resolving = matches!(intent, Intent::Resolve { .. });
        let closed = match intent {
            Intent::Open => None,
            Intent::Continue { .. } => {
                return Err(fail("Continuation must reserve an execution command"));
            }
            Intent::Reconcile { revision, entry } => {
                let menu = self
                    .queue_menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
                let row = menu
                    .entries
                    .iter()
                    .find(|e| e.id == entry && e.can_reconcile)
                    .ok_or_else(|| fail("Entry was not offered for history reconciliation"))?;
                command["type"] = json!(RECONCILE);
                command["snapshot"] = json!(menu.token);
                command["entry_id"] = json!(entry);
                reconciled = Some(Reconciliation {
                    entry: row.clone(),
                    accepted: None,
                });
                None
            }
            Intent::Cancel { revision, entry } | Intent::Resolve { revision, entry } => {
                let menu = self
                    .queue_menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
                if !menu.entries.iter().any(|e| {
                    e.id == entry
                        && if resolving {
                            e.can_resolve
                        } else {
                            e.can_cancel
                        }
                }) {
                    return Err(fail(
                        "Entry was not offered for the requested recovery action",
                    ));
                }
                command["type"] = json!(if resolving { RESOLVE } else { CANCEL });
                command["snapshot"] = json!(menu.token);
                command["entry_id"] = json!(entry);
                Some(QueueClosure {
                    entry,
                    resolved: resolving,
                })
            }
        };
        if !self.operations.iter().any(|v| v == READ)
            || !self
                .operations
                .iter()
                .any(|v| Some(v.as_str()) == command["type"].as_str())
        {
            return Err(fail(
                "The connected runtime does not support held-input controls",
            ));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve queue control"))?;
        command["id"] = id.to_value();
        self.queue = Some(QueuePending {
            id,
            applying: closed.is_some() || reconciled.is_some(),
            reconciled,
            closed,
            continued: None,
            failure: None,
            succeeded: false,
        });
        self.queue_menu = None;
        self.browser_page = None;
        self.menu = None;
        Ok(command)
    }
    fn queue_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::restored_queue::{READ, Snapshot, Update};
        let mut pending = self.queue.take().expect("matched queue request");
        let success = frame["ok"] == true;
        let error = if success {
            None
        } else {
            Some(text(&frame, "error", 4096)?.to_owned())
        };
        if pending.applying {
            if let Some(closed) = &pending.closed
                && closed.resolved
                && success
            {
                let result = &frame["result"];
                if result["resolved"] != closed.entry
                    || result["session_id"] != self.expected.as_ref().expect("active").0
                    || result["replayed"] != false
                    || result["effects_verified"] != false
                {
                    return Err(fail(
                        "Resolution receipt changed identity or claimed provider effects",
                    ));
                }
            }
            if let Some(check) = &mut pending.reconciled
                && success
            {
                let result = &frame["result"];
                if result["entry_id"] != check.entry.id
                    || result["session_id"] != self.expected.as_ref().expect("active").0
                {
                    return Err(fail("History reconciliation receipt changed identity"));
                }
                check.accepted = Some(
                    result["reconciled"]
                        .as_bool()
                        .ok_or_else(|| fail("Missing history reconciliation outcome"))?,
                );
            }
            pending.applying = false;
            pending.failure = error;
            pending.succeeded = success;
            let id = self
                .wire
                .register_request()
                .map_err(|_| fail("Cannot reserve queue readback"))?;
            let command = json!({"type":READ,"id":id.to_value(),"session_id":self.expected.as_ref().expect("active").0});
            pending.id = id;
            self.queue = Some(pending);
            return Ok(Step::Write(command));
        }
        if let Some(error) = error {
            return if pending.closed.is_some()
                || pending.continued.is_some()
                || pending.reconciled.is_some()
            {
                Err(fail(&format!(
                    "Cannot verify saved queue after mutation: {error}"
                )))
            } else {
                Ok(Step::Queue(Update::Rejected(error)))
            };
        }
        self.ensure_drained()?;
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| fail("Queue revision exhausted"))?;
        let mut snapshot = Snapshot::parse(&frame["result"], self.revision)?;
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        if (&snapshot.session, &snapshot.backend, &snapshot.model) != (session, backend, model) {
            return Err(fail("Queue readback changed runtime identity"));
        }
        if pending.succeeded
            && snapshot
                .entries
                .iter()
                .any(|e| Some(&e.id) == pending.closed.as_ref().map(|c| &c.entry))
        {
            return Err(fail("Runtime still holds the closed entry"));
        }
        if let Some((operation, entry, refused)) = &pending.continued {
            let result = &frame["result"];
            if result["operation_id"] != *operation
                || result["entry_id"] != entry.id
                || result["continuation_outcome"] != if *refused { "refused" } else { "accepted" }
            {
                return Err(fail(
                    "Continuation readback disagrees with its execution receipt",
                ));
            }
            let held = snapshot.entries.iter().find(|row| row.id == entry.id);
            if entry.provider == "claude" {
                let state = result["continuation_state"].as_str();
                if result["continuation_provider"] != "claude"
                    || !matches!(state, Some("prepared" | "uncertain" | "terminal"))
                    || (*refused && state != Some("prepared"))
                    || held.is_some() != (state != Some("terminal"))
                {
                    return Err(fail(
                        "Claude continuation readback lacks consistent delivery evidence",
                    ));
                }
                if let Some(row) = held
                    && (Some(row.state.as_str()) != state
                        || row.provider != entry.provider
                        || row.content != entry.content
                        || row.source != entry.source
                        || row.profile != entry.profile
                        || row.segment != entry.segment
                        || row.workspace != entry.workspace
                        || (!entry.thread.is_empty() && row.thread != entry.thread))
                {
                    return Err(fail("Held Claude delivery changed after continuation"));
                }
            } else if held.is_some() != *refused {
                return Err(fail("Continuation readback disagrees with held input"));
            }
        }
        if let Some(check) = &pending.reconciled {
            let held = snapshot.entries.iter().find(|e| e.id == check.entry.id);
            match check.accepted {
                Some(true) if held.is_none() => {
                    snapshot.recovered = Some(Box::new(check.entry.clone()))
                }
                Some(false) if held == Some(&check.entry) => {}
                None => {}
                _ => {
                    return Err(fail(
                        "Saved queue contradicts provider-history reconciliation",
                    ));
                }
            }
        }
        snapshot.message = pending
            .failure
            .map(|e| format!("Request failed: {e}. Saved queue read back."))
            .or_else(|| {
                pending.closed.map(|closed| {
                    if closed.resolved {
                        "Marked resolved without replay; provider effects remain unverified.".into()
                    } else {
                        "Cancellation saved. Queue read back; nothing replayed.".into()
                    }
                })
            });
        if let Some((_, entry, _)) = &pending.continued {
            snapshot.message = Some(match snapshot.entries.iter().find(|row| row.id == entry.id) {
                Some(row) if row.provider == "claude" && row.state == "prepared" =>
                    "Continuation settled; input remains unsent and held. Saved queue read back.".into(),
                Some(row) if row.provider == "claude" && row.state == "uncertain" =>
                    "Continuation settled; delivery is uncertain and held without replay. Saved queue read back.".into(),
                _ => "Continuation settled; saved queue read back.".into(),
            });
        }
        if let Some(check) = pending.reconciled
            && let Some(accepted) = check.accepted
        {
            snapshot.message = Some(
                if accepted {
                    "Provider history confirmed acceptance. Recorded locally; nothing resent."
                } else {
                    "No matching acceptance found. Input remains held; nothing resent."
                }
                .into(),
            );
        }
        self.queue_menu = Some(snapshot.clone());
        Ok(Step::Queue(Update::Snapshot(snapshot)))
    }
}

impl Management {
    pub fn prepare_continuation(&self, revision: u64, entry: &str) -> io::Result<Continuation> {
        use crate::restored_queue::{CONTINUE, SETTLED};
        if !self.ready() {
            return Err(fail("Runtime is not ready for continuation"));
        }
        self.ensure_drained()?;
        if ![CONTINUE, SETTLED]
            .iter()
            .all(|op| self.operations.iter().any(|value| value == op))
        {
            return Err(fail(
                "Runtime lacks continuation/settlement support; update Python source",
            ));
        }
        let menu = self
            .queue_menu
            .as_ref()
            .filter(|m| m.revision == revision)
            .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
        let row = menu
            .entries
            .iter()
            .find(|row| row.id == entry && row.can_continue)
            .ok_or_else(|| fail("Entry was not offered for continuation"))?;
        Ok(Continuation {
            command: json!({"type":CONTINUE,"session_id":menu.session,"entry_id":row.id,"snapshot":menu.token}),
            prompt: row.content.clone(),
            session: menu.session.clone(),
            entry: row.id.clone(),
            record: row.clone(),
        })
    }
    pub fn begin_continuation(
        &mut self,
        mut prepared: Continuation,
        id: &str,
    ) -> io::Result<Value> {
        self.prompt(id)?;
        self.continuation = Some((id.to_owned(), prepared.record));
        prepared.command["id"] = json!(id);
        Ok(prepared.command)
    }
    pub fn needs_continuation_readback(&self) -> bool {
        self.continuation.is_some()
    }
    pub fn read_continuation(&mut self, refused: bool) -> io::Result<Value> {
        let (operation, entry) = self
            .continuation
            .take()
            .ok_or_else(|| fail("No continuation to settle"))?;
        if self.queue.is_some() || self.control.is_some() {
            return Err(fail("Concurrent continuation readback"));
        }
        self.ensure_drained()?;
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve continuation readback"))?;
        let command = json!({"type":crate::restored_queue::SETTLED,"id":id.to_value(),
            "session_id":self.expected.as_ref().expect("active").0,"operation_id":operation,"entry_id":entry.id});
        self.queue = Some(QueuePending {
            id,
            applying: false,
            closed: None,
            continued: Some((operation, entry, refused)),
            reconciled: None,
            failure: None,
            succeeded: false,
        });
        Ok(command)
    }
}

impl Management {
    pub fn request_browser(
        &mut self,
        intent: crate::conversation_browser::Intent,
    ) -> io::Result<Value> {
        use crate::conversation_browser::{Intent, OPEN, PAGE, PREVIEW};
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail(
                "Runtime omitted owned-agent browser authority; update Python source",
            ));
        }

        if ![
            PAGE,
            PREVIEW,
            OPEN,
            "gui.state",
            crate::restored_queue::READ,
        ]
        .iter()
        .all(|op| self.operations.iter().any(|value| value == op))
        {
            return Err(fail(
                "Connected runtime does not support the conversation browser",
            ));
        }
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        let (operation, request, mut command) = match intent {
            Intent::Refresh => (
                PAGE,
                BrowserRequest::Page {
                    previous: None,
                    cursor: None,
                },
                json!({"session_id":session,"cursor":null}),
            ),
            Intent::Page { revision, cursor } => {
                let page = self
                    .browser_page
                    .as_ref()
                    .filter(|page| page.revision == revision)
                    .ok_or_else(|| fail("Conversation page changed; refresh the browser"))?;
                if page.previous.as_deref() != Some(cursor.as_str())
                    && page.next.as_deref() != Some(cursor.as_str())
                {
                    return Err(fail("Conversation page cursor was not offered"));
                }
                (
                    PAGE,
                    BrowserRequest::Page {
                        previous: Some(page.clone()),
                        cursor: Some(cursor.clone()),
                    },
                    json!({"session_id":session,"cursor":cursor}),
                )
            }
            Intent::Open {
                revision,
                session: target,
            } => {
                let page = self
                    .browser_page
                    .as_ref()
                    .ok_or_else(|| fail("Refresh the conversation browser before opening"))?;
                page.selection(revision, &target)?;
                (
                    PREVIEW,
                    BrowserRequest::Preview {
                        target: target.clone(),
                        agent: page.agent.clone(),
                        snapshot: page.snapshot.clone(),
                    },
                    json!({"session_id":target,"from_session_id":session,"snapshot":page.snapshot}),
                )
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve conversation operation"))?;
        command["type"] = json!(operation);
        command["id"] = id.to_value();
        self.browser = Some(BrowserPending { id, request });
        self.menu = None;
        self.queue_menu = None;
        Ok(command)
    }
    fn browser_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::conversation_browser::{Opened, Page, Update};
        let mut pending = self.browser.take().expect("matched browser");
        self.ensure_drained()?;
        let success = frame["ok"] == true;
        match &mut pending.request {
            BrowserRequest::Preview {
                target,
                agent,
                snapshot,
            } => {
                if !success {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(
                        text(&frame, "error", 4096)?.to_owned(),
                    )));
                }
                let result = &frame["result"];
                let current = &self.expected.as_ref().expect("active").0;
                if result["session_id"] != *target
                    || result["from_session_id"] != *current
                    || result["agent_id"] != *agent
                    || result["snapshot"] != *snapshot
                    || result["read_only"] != false
                {
                    return Err(fail(
                        "Conversation preview changed selection or owned authority",
                    ));
                }
                if let Err(error) = crate::resume::History::parse(result, target) {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(format!(
                        "Conversation cannot be displayed: {error}. Current conversation remains open."
                    ))));
                }
                let id = self
                    .wire
                    .register_request()
                    .map_err(|_| fail("Cannot reserve verified conversation open"))?;
                let command = json!({"type":crate::conversation_browser::OPEN,"id":id.to_value(),"session_id":target,
                    "from_session_id":current,"snapshot":snapshot});
                self.browser = Some(BrowserPending {
                    id: id.clone(),
                    request: BrowserRequest::Open {
                        target: target.clone(),
                        agent: agent.clone(),
                        mutation: id,
                        stage: 0,
                        history: None,
                        state: None,
                        policy: None,
                        model: None,
                    },
                });
                Ok(Step::Write(command))
            }
            BrowserRequest::Page { previous, cursor } => {
                if !success {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(
                        text(&frame, "error", 4096)?.to_owned(),
                    )));
                }
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Conversation revision exhausted"))?;
                let page = Page::parse(
                    &frame["result"],
                    self.revision,
                    &self.expected.as_ref().expect("active").0,
                )?;
                if self
                    .browser_authority
                    .as_ref()
                    .map(|value| value.0.as_str())
                    != Some(page.agent.as_str())
                {
                    return Err(fail("Conversation page changed owned agent"));
                }
                if let (Some(previous), Some(cursor)) = (previous, cursor) {
                    page.follows(previous, cursor)?;
                }
                self.browser_page = Some(page.clone());
                Ok(Step::Browser(Update::Page(page)))
            }
            BrowserRequest::Open {
                target,
                agent,
                stage,
                history,
                state,
                policy,
                model: transition_model,
                ..
            } => {
                if !success {
                    return Err(fail(&format!(
                        "Conversation open/readback failed; session is uncertain: {}",
                        text(&frame, "error", 4096)?
                    )));
                }
                let result = &frame["result"];
                match *stage {
                    0 => {
                        *history = Some(crate::conversation_browser::connected_history(
                            result, target,
                        )?);
                    }
                    1 => {
                        let snapshot = Snapshot::parse(result)?;
                        let (_, backend, model, profile) = self.expected.as_ref().expect("active");
                        if snapshot.session != *target
                            || snapshot.backend != *backend
                            || snapshot.profile != *profile
                            || !snapshot.writable
                            || snapshot.held_restored_input.is_none()
                            || self.browser_authority.as_ref().map(|value| value.1)
                                != Some(snapshot.schedules_paused)
                            || result["ownership"]["agent_id"] != *agent
                            || (snapshot.model != *model
                                && transition_model.as_ref() != Some(&snapshot.model))
                            || transition_model
                                .as_ref()
                                .is_some_and(|value| value != &snapshot.model)
                        {
                            return Err(fail(
                                "Opened conversation state contradicts its owned identity, policy, or model transition",
                            ));
                        }
                        *policy = Some(result["tool_policy"].clone());
                        *state = Some(snapshot);
                    }
                    2 => {
                        self.revision = self
                            .revision
                            .checked_add(1)
                            .ok_or_else(|| fail("Conversation revision exhausted"))?;
                        let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
                        let snapshot = state.take().expect("state read");
                        if queue.session != snapshot.session
                            || queue.backend != snapshot.backend
                            || queue.model != snapshot.model
                            || Some(queue.entries.len()) != snapshot.held_restored_input
                        {
                            return Err(fail(
                                "Opened conversation held-input readback contradicts state",
                            ));
                        }
                        self.engine_instance = None;
                        self.engine_snapshot = None;
                        self.engine_review = None;
                        self.expected = Some((
                            snapshot.session.clone(),
                            snapshot.backend.clone(),
                            snapshot.model.clone(),
                            snapshot.profile.clone(),
                        ));
                        self.prompt = None;
                        self.origin = None;
                        self.menu = None;
                        self.browser_page = None;
                        self.queue_menu = Some(queue.clone());
                        return Ok(Step::Opened(Box::new(Opened {
                            snapshot,
                            history: history.take().expect("connected history"),
                            queue,
                            policy: policy.take().expect("policy read"),
                        })));
                    }
                    _ => return Err(fail("Invalid conversation opening phase")),
                }
                *stage += 1;
                let id = self
                    .wire
                    .register_read()
                    .map_err(|_| fail("Cannot reserve conversation readback"))?;
                let command = if *stage == 1 {
                    json!({"type":"gui.state","id":id.to_value(),"include_conversations":false})
                } else {
                    json!({"type":crate::restored_queue::READ,"id":id.to_value(),"session_id":target})
                };
                pending.id = id;
                self.browser = Some(pending);
                Ok(Step::Write(command))
            }
        }
    }
}

impl Management {
    pub fn request_backend(
        &mut self,
        intent: crate::backend_controls::Intent,
    ) -> io::Result<Value> {
        use crate::backend_controls::{Intent, LIST, SET};
        if !self.ready() {
            return Err(fail("Wait for the current runtime operation"));
        }
        self.ensure_drained()?;
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        if self.browser_authority.is_none()
            || ![
                LIST,
                SET,
                "gui.state",
                "gui.conversations.read",
                crate::restored_queue::READ,
            ]
            .iter()
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail(
                "Runtime lacks owned backend transition support; update Python source",
            ));
        }
        let (stage, selection, catalog, mut command) = match intent {
            Intent::Read => (0, None, None, json!({"type":LIST,"session_id":session})),
            Intent::Select {
                revision,
                name,
                model,
            } => {
                let catalog = self
                    .backend_catalog
                    .as_ref()
                    .ok_or_else(|| fail("Refresh the backend catalog before selecting"))?;
                catalog.selection(revision, &name, &model)?;
                (
                    1,
                    Some((name, model)),
                    Some(catalog.clone()),
                    json!({"type":"gui.conversations.read","session_id":session,"max_response_bytes":1048576}),
                )
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve backend operation"))?;
        command["id"] = id.to_value();
        self.backend = Some(BackendPending {
            id,
            stage,
            selection,
            catalog,
            mutation: None,
            history: None,
            state: None,
            policy: None,
            observed_model: false,
        });
        self.menu = None;
        self.browser_page = None;
        self.queue_menu = None;
        Ok(command)
    }
    fn backend_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::backend_controls::{Catalog, Changed, LIST, SET, Update};
        self.ensure_drained()?;
        let mut pending = self.backend.take().expect("matched backend");
        let (session, old_backend, old_model, profile) =
            self.expected.as_ref().expect("active").clone();
        let (agent, schedules) = self
            .browser_authority
            .as_ref()
            .expect("owned agent")
            .clone();
        if frame["ok"] != true {
            if pending.stage <= 1 {
                self.backend_catalog = None;
                return Ok(Step::Backend(Update::Rejected(
                    text(&frame, "error", 4096)?.into(),
                )));
            }
            return Err(fail(
                "Backend mutation/readback failed; runtime identity is uncertain",
            ));
        }
        let result = &frame["result"];
        if pending.stage == 0 {
            self.revision = self
                .revision
                .checked_add(1)
                .ok_or_else(|| fail("Backend revision exhausted"))?;
            let catalog = Catalog::parse(result, self.revision, &session, &agent)?;
            if catalog.selected != old_backend || catalog.model != old_model {
                return Err(fail("Backend catalog changed active runtime identity"));
            }
            self.backend_catalog = Some(catalog.clone());
            return Ok(Step::Backend(Update::Catalog(catalog)));
        }
        let (name, model) = pending.selection.as_ref().expect("target").clone();
        match pending.stage {
            1 => match crate::backend_controls::history(result, &session) {
                Ok(history) => pending.history = Some(history),
                Err(error) => {
                    self.backend_catalog = None;
                    return Ok(Step::Backend(Update::Rejected(format!(
                        "Cannot verify preserved conversation: {error}"
                    ))));
                }
            },
            2 => {
                let catalog = pending.catalog.as_ref().expect("catalog");
                if result["version"].as_u64() != Some(1)
                    || result["kind"] != "backend_transition"
                    || result["session_id"] != session
                    || result["agent_id"] != agent
                    || result["snapshot"] != catalog.snapshot
                {
                    return Err(fail("Backend transition receipt changed owned selection"));
                }
                match result["status"].as_str() {
                    Some("refused")
                        if result["mutated"] == false
                            && result["close_required"] == false
                            && result["backend"] == old_backend
                            && result["model"] == old_model
                            && !pending.observed_model =>
                    {
                        self.backend_catalog = None;
                        return Ok(Step::Backend(Update::Rejected(
                            text(result, "error", 4096)?.into(),
                        )));
                    }
                    Some("connected")
                        if result["mutated"] == true
                            && result["close_required"] == false
                            && result["backend"] == name
                            && result["model"] == model
                            && result["error"].is_null()
                            && pending.observed_model => {}
                    _ => {
                        return Err(fail(
                            "Backend transition failed or contradicted its receipt; connection closed",
                        ));
                    }
                }
            }
            3 => {
                let snapshot = Snapshot::parse(result)?;
                if snapshot.session != session
                    || snapshot.backend != name
                    || snapshot.model != model
                    || snapshot.profile != profile
                    || (!snapshot.writable
                        && pending
                            .history
                            .as_ref()
                            .is_none_or(|history| !history.messages.is_empty()))
                    || snapshot.held_restored_input != Some(0)
                    || snapshot.schedules_paused != schedules
                    || result["ownership"]["agent_id"] != agent
                {
                    return Err(fail(
                        "Backend state readback contradicts target, ownership, or held-input policy",
                    ));
                }
                pending.policy = Some(result["tool_policy"].clone());
                pending.state = Some(snapshot);
            }
            4 => {
                let history = crate::backend_controls::history(result, &session)?;
                if Some(&history) != pending.history.as_ref() {
                    return Err(fail("Backend transition changed the saved conversation"));
                }
            }
            5 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Backend revision exhausted"))?;
                let catalog = Catalog::parse(result, self.revision, &session, &agent)?;
                if catalog.selected != name
                    || catalog.model != model
                    || catalog.configured != name
                    || catalog.requested_native
                        != pending
                            .catalog
                            .as_ref()
                            .expect("original catalog")
                            .requested_native
                {
                    return Err(fail("Backend catalog readback contradicts selected target"));
                }
                pending.catalog = Some(catalog);
            }
            6 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Backend revision exhausted"))?;
                let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
                if queue.session != session
                    || queue.backend != name
                    || queue.model != model
                    || !queue.entries.is_empty()
                {
                    return Err(fail(
                        "Backend held-input readback contradicts verified state",
                    ));
                }
                let snapshot = pending.state.take().expect("state");
                let catalog = pending.catalog.take().expect("catalog");
                self.engine_instance = None;
                self.engine_snapshot = None;
                self.engine_review = None;
                self.expected = Some((session, name, model, profile));
                self.prompt = None;
                self.origin = None;
                self.menu = None;
                self.browser_page = None;
                self.backend_catalog = Some(catalog.clone());
                self.queue_menu = Some(queue.clone());
                return Ok(Step::BackendChanged(Box::new(Changed {
                    snapshot,
                    catalog,
                    history: pending.history.take().expect("history"),
                    queue,
                    policy: pending.policy.take().expect("policy"),
                })));
            }
            _ => return Err(fail("Invalid backend transition phase")),
        }
        pending.stage += 1;
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve backend readback"))?;
        let mut command = match pending.stage {
            2 => {
                pending.mutation = Some(id.clone());
                json!({"type":SET,"version":1,"session_id":session,"agent_id":agent,"snapshot":pending.catalog.as_ref().expect("catalog").snapshot,"name":name,"model":model})
            }
            3 => json!({"type":"gui.state","include_conversations":false}),
            4 => {
                json!({"type":"gui.conversations.read","session_id":session,"max_response_bytes":1048576})
            }
            5 => json!({"type":LIST,"session_id":session}),
            6 => json!({"type":crate::restored_queue::READ,"session_id":session}),
            _ => return Err(fail("Invalid backend readback operation")),
        };
        command["id"] = id.to_value();
        pending.id = id;
        self.backend = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    pub fn request_settings(&mut self, intent: crate::settings_view::Intent) -> io::Result<Value> {
        if let crate::settings_view::Intent::Editor(intent) = intent {
            return self.request_settings_edit(intent);
        }
        if !self.ready() {
            return Err(fail(
                "Wait for the runtime operation before reading settings",
            ));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail(
                "Runtime omitted owned settings authority; update Python source",
            ));
        }
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve settings read"))?;
        let command = json!({"type":"gui.state","id":id.to_value(),"include_conversations":false});
        self.settings = Some(id);
        Ok(command)
    }
    fn settings_response(&mut self, frame: Value) -> io::Result<Step> {
        self.settings = None;
        self.ensure_drained()?;
        if frame["ok"] != true {
            return Ok(Step::Settings {
                update: crate::settings_view::Update::Rejected(text(&frame, "error", 4096)?.into()),
                policy: None,
            });
        }
        let result = &frame["result"];
        let state = Snapshot::parse(result)?;
        let (session, backend, model, profile) = self.expected.as_ref().expect("active");
        let (agent, schedules) = self.browser_authority.as_ref().expect("authority");
        if (&state.session, &state.backend, &state.model, &state.profile)
            != (session, backend, model, profile)
            || state.held_restored_input.is_none()
            || state.schedules_paused != *schedules
            || result["ownership"]["agent_id"] != *agent
        {
            return Err(fail(
                "Settings read changed owned runtime identity or policy",
            ));
        }
        let update = match crate::settings_view::Snapshot::parse(result, session) {
            Ok(snapshot) => crate::settings_view::Update::Snapshot(snapshot),
            Err(error) => crate::settings_view::Update::Rejected(error.to_string()),
        };
        Ok(Step::Settings {
            update,
            policy: Some(result["tool_policy"].clone()),
        })
    }
}

impl Management {
    fn request_settings_edit(
        &mut self,
        intent: crate::settings_editor::Intent,
    ) -> io::Result<Value> {
        use crate::settings_editor::{Intent, READ, SAVE, VALIDATE};
        if !self.ready() {
            return Err(fail("Finish current work before editing settings"));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail("Runtime omitted owned settings authority"));
        }
        let session = &self.expected.as_ref().expect("active").0;
        let (stage, mut command, selection, review) = match intent {
            Intent::Read => {
                self.settings_snapshot = None;
                self.settings_review = None;
                (
                    0,
                    json!({"type":READ,"version":1,"session_id":session}),
                    None,
                    None,
                )
            }
            Intent::Validate {
                snapshot,
                field,
                value,
            } => {
                self.settings_review = None;
                let current = self
                    .settings_snapshot
                    .as_ref()
                    .filter(|s| s.token == snapshot)
                    .ok_or_else(|| fail("Refresh settings authority before validating"))?;
                let mut command = current.request(&field, value)?;
                command["type"] = json!(VALIDATE);
                (1, command, Some((field, value)), None)
            }
            Intent::Save { review } => {
                let current = self
                    .settings_review
                    .take()
                    .filter(|r| r.token == review)
                    .ok_or_else(|| fail("Validate the edit again before saving"))?;
                let mut command = current.request()?;
                command["type"] = json!(SAVE);
                (2, command, None, Some(current))
            }
        };
        let id = if stage == 2 {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve settings operation"))?;
        command["id"] = id.to_value();
        self.settings_edit = Some(SettingsEditPending {
            id,
            stage,
            selection,
            review,
            outcome: None,
            snapshot: None,
        });
        Ok(command)
    }
    fn settings_edit_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::settings_editor::{Outcome, READ, Review, Snapshot as EditSnapshot, Update};
        self.ensure_drained()?;
        let mut pending = self.settings_edit.take().expect("pending settings edit");
        let publish = |update, policy| Step::Settings {
            update: crate::settings_view::Update::Editor(update),
            policy,
        };
        if frame["ok"] != true {
            self.settings_review = None;
            self.settings_snapshot = None;
            let reason = text(&frame, "error", 4096)?;
            if pending.stage >= 2 {
                return Err(fail(&format!("Settings save/readback failed: {reason}")));
            }
            return Ok(publish(Update::Rejected(reason.into()), None));
        }
        let value = &frame["result"];
        let (session, backend, model, profile) = self.expected.as_ref().expect("active");
        let (agent, schedules) = self.browser_authority.as_ref().expect("owned");
        match pending.stage {
            0 => {
                let snapshot = EditSnapshot::parse(value, session, agent, backend, model)?;
                self.settings_snapshot = Some(snapshot.clone());
                return Ok(publish(Update::Snapshot(snapshot), None));
            }
            1 => {
                let (field, value_number) = pending.selection.as_ref().expect("selection");
                let snapshot = self
                    .settings_snapshot
                    .as_ref()
                    .expect("validated selection");
                let review = Review::parse(value, snapshot, field, *value_number)?;
                self.settings_review = Some(review.clone());
                return Ok(publish(Update::Review(review), None));
            }
            2 => {
                let review = pending.review.as_ref().expect("confirmed edit");
                match Outcome::parse(value, review)? {
                    Err(reason) => {
                        self.settings_snapshot = None;
                        return Ok(publish(Update::Rejected(reason), None));
                    }
                    Ok(outcome) => pending.outcome = Some(outcome),
                }
                pending.stage = 3;
            }
            3 => {
                let snapshot = EditSnapshot::parse(value, session, agent, backend, model)?;
                pending
                    .outcome
                    .as_ref()
                    .expect("receipt")
                    .verify(&snapshot, pending.review.as_ref().expect("review"))?;
                pending.snapshot = Some(snapshot);
                pending.stage = 4;
            }
            4 => {
                let state = Snapshot::parse(value)?;
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
                for (i, field) in crate::settings_editor::FIELDS.iter().enumerate() {
                    if value["settings"]["effective"][*field] != snapshot.budgets[i].effective {
                        return Err(fail(
                            "Settings effective values disagree with runtime state",
                        ));
                    }
                }
                self.settings_snapshot = Some(snapshot.clone());
                return Ok(Step::Settings {
                    update: crate::settings_view::Update::Edited {
                        update: Update::Saved {
                            snapshot,
                            outcome: Box::new(pending.outcome.expect("saved receipt")),
                        },
                        inspection: crate::settings_view::Snapshot::parse(value, session)?,
                    },
                    policy: Some(value["tool_policy"].clone()),
                });
            }
            _ => return Err(fail("Invalid settings edit stage")),
        }
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve settings readback"))?;
        let command = if pending.stage == 3 {
            json!({"type":READ,"id":id.to_value(),"version":1,"session_id":session})
        } else {
            json!({"type":"gui.state","id":id.to_value(),"include_conversations":false})
        };
        pending.id = id;
        self.settings_edit = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    pub fn request_command(&mut self, intent: crate::command_palette::Intent) -> io::Result<Value> {
        use crate::command_palette::{Intent, LIST, PREPARE, RESOLVE};
        if !self.ready() {
            return Err(fail("Finish current work before opening commands"));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail("Runtime omitted owned command authority"));
        }
        let session = &self.expected.as_ref().expect("active").0;
        let mut pending = CommandPending {
            id: None,
            stage: 0,
            raw: None,
            row: None,
            arguments: String::new(),
            skills: false,
            reviewed: None,
        };
        let mut command = match intent {
            Intent::Read { skills } => {
                pending.skills = skills;
                self.command_prepared = None;
                json!({"type":LIST,"version":1,"session_id":session})
            }
            Intent::Raw(raw) => {
                if raw.len() > 65536 || !raw.trim_start().starts_with('/') {
                    return Err(fail("Enter a slash command within 64 KiB"));
                }
                pending.raw = Some(raw);
                self.command_prepared = None;
                json!({"type":LIST,"version":1,"session_id":session})
            }
            Intent::Resolve { snapshot, row } => {
                let c = self
                    .command_catalog
                    .as_ref()
                    .filter(|c| c.snapshot == snapshot)
                    .ok_or_else(|| fail("Refresh command catalog before selecting"))?;
                let entry = c
                    .entries
                    .iter()
                    .find(|e| e.id == row)
                    .ok_or_else(|| fail("Command row is absent"))?;
                if entry.action.is_none() {
                    return Err(fail(&entry.unavailable));
                }
                pending.stage = 1;
                pending.row = Some(row.clone());
                let mut v = c.request();
                v["type"] = json!(RESOLVE);
                v["row_id"] = json!(row);
                v["arguments"] = json!("");
                v
            }
            Intent::Prepare {
                snapshot,
                row,
                arguments,
            } => {
                let c = self
                    .command_catalog
                    .as_ref()
                    .filter(|c| c.snapshot == snapshot)
                    .ok_or_else(|| fail("Refresh skill catalog before preparing"))?;
                if !c.entries.iter().any(|e| e.id == row && e.kind == "skill")
                    || arguments.len() > 65536
                {
                    return Err(fail("Invalid skill row or argument budget"));
                }
                pending.stage = 2;
                pending.row = Some(row.clone());
                pending.arguments = arguments.clone();
                let mut v = c.request();
                v["type"] = json!(PREPARE);
                v["row_id"] = json!(row);
                v["arguments"] = json!(arguments);
                v["backend"] = json!(c.backend);
                v["model"] = json!(c.model);
                v
            }
            Intent::Submit { digest } => {
                let p = self
                    .command_prepared
                    .as_ref()
                    .filter(|p| p.digest == digest)
                    .ok_or_else(|| {
                        fail("Prepare and review this exact skill again before submitting")
                    })?;
                pending.stage = 3;
                pending.row = Some(p.row.clone());
                pending.arguments = p.arguments.clone();
                pending.reviewed = Some(p.clone());
                json!({"type":PREPARE,"version":1,"session_id":p.session,"agent_id":p.agent,"backend":p.backend,"model":p.model,"snapshot":p.snapshot,"row_id":p.row,"arguments":p.arguments})
            }
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve command read"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.command = Some(pending);
        Ok(command)
    }
    fn command_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::command_palette::{Action, Catalog, PREPARE, RESOLVE, Route, Update};
        let publish = |update| Step::Commands {
            update,
            policy: None,
        };
        self.ensure_drained()?;
        let mut pending = self.command.take().expect("command pending");
        if frame["ok"] != true {
            return Ok(publish(Update::Rejected(
                text(&frame, "error", 4096)?.into(),
            )));
        }
        let v = &frame["result"];
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        let (agent, _) = self.browser_authority.as_ref().expect("authority");
        let mut command = match pending.stage {
            0 => {
                let c = Catalog::parse(v, session, agent, backend, model)?;
                self.command_catalog = Some(c.clone());
                if let Some(raw) = &pending.raw {
                    pending.stage = 1;
                    let mut command = c.request();
                    command["type"] = json!(RESOLVE);
                    command["command"] = json!(raw);
                    command
                } else {
                    return Ok(publish(Update::Catalog {
                        catalog: c,
                        skills: pending.skills,
                    }));
                }
            }
            1 => {
                let c = self.command_catalog.as_ref().expect("catalog");
                let r = Route::parse(
                    v,
                    c,
                    pending.row.as_deref(),
                    pending.raw.as_deref(),
                    &pending.arguments,
                )?;
                if r.action == Action::SkillPrepare {
                    pending.row = Some(r.row.clone());
                    pending.arguments = r.arguments.clone();
                    pending.stage = 2;
                    let mut command = c.request();
                    command["type"] = json!(PREPARE);
                    command["row_id"] = json!(r.row);
                    command["arguments"] = json!(r.arguments);
                    command["backend"] = json!(c.backend);
                    command["model"] = json!(c.model);
                    command
                } else {
                    return Ok(publish(Update::Route {
                        catalog: c.clone(),
                        route: r,
                    }));
                }
            }
            2 | 3 => {
                let c = self.command_catalog.as_ref().expect("catalog");
                let prepared = crate::prepared_skill::Prepared::parse(
                    v,
                    c,
                    pending.row.as_deref().expect("skillrow"),
                    &pending.arguments,
                )?;
                if let Err(error) = crate::live_chat::display_units(&prepared.prompt) {
                    return Ok(publish(Update::Rejected(error.to_string())));
                }
                if pending.stage == 3 && pending.reviewed.as_ref() != Some(&prepared) {
                    return Ok(publish(Update::Rejected("Skill file or prepared payload changed; prepare and review again before submitting".into())));
                }
                pending.stage = if pending.stage == 3 { 5 } else { 4 };
                pending.reviewed = Some(prepared);
                json!({"type":"gui.state","include_conversations":false})
            }
            4 | 5 => {
                let state = Snapshot::parse(v)?;
                let (_, _, _, profile) = self.expected.as_ref().expect("active");
                let (_, schedules) = self.browser_authority.as_ref().expect("authority");
                if (&state.session, &state.backend, &state.model, &state.profile)
                    != (session, backend, model, profile)
                    || state.held_restored_input.is_none()
                    || state.schedules_paused != *schedules
                    || v["ownership"]["agent_id"] != *agent
                {
                    return Err(fail("Prepared skill state readback changed owned runtime"));
                }
                let prepared = pending.reviewed.expect("verified preparation");
                let update = if pending.stage == 5 {
                    self.command_prepared = None;
                    Update::Verified(Box::new(prepared))
                } else {
                    self.command_prepared = Some(prepared.clone());
                    Update::Prepared {
                        catalog: Box::new(self.command_catalog.as_ref().expect("catalog").clone()),
                        prepared: Box::new(prepared),
                    }
                };
                return Ok(Step::Commands {
                    update,
                    policy: Some(v["tool_policy"].clone()),
                });
            }
            _ => return Err(fail("Invalid command operation stage")),
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve command resolution"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.command = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    fn engine_identity(&self) -> io::Result<crate::engine_controls::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Runtime has not negotiated"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Engine controls require owned agent identity"))?;
        let (instance, _, _) = self.engine_instance.as_ref().ok_or_else(|| {
            fail("Engine controls require startup ownership; update source or relaunch after changing backend/conversation")
        })?;
        Ok(crate::engine_controls::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
            instance: instance.clone(),
        })
    }
    pub fn engine_timeout(&self) -> Option<std::time::Duration> {
        self.engine
            .as_ref()
            .and_then(|p| p.review.as_ref())
            .map(|r| std::time::Duration::from_millis(r.timeout_ms))
    }
    pub fn request_engine(&mut self, intent: crate::engine_controls::Intent) -> io::Result<Value> {
        use crate::engine_controls::{INSPECT, Intent, OPERATIONS};
        if !self.ready() {
            return Err(fail("Wait for the current runtime operation"));
        }
        self.ensure_drained()?;
        let identity = self.engine_identity()?;
        if !OPERATIONS
            .iter()
            .chain(
                [
                    "gui.state",
                    "gui.conversations.read",
                    crate::restored_queue::READ,
                ]
                .iter(),
            )
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks complete owned engine controls"));
        }
        let mut pending = EnginePending {
            id: None,
            stage: 0,
            action: None,
            snapshot: None,
            review: None,
            receipt: None,
            history: None,
            policy: None,
            state: None,
            mutation: None,
        };
        let mut command = match intent {
            Intent::Read => {
                self.engine_review = None;
                identity.command(INSPECT)
            }
            Intent::Validate { snapshot, action } => {
                let inspected = self
                    .engine_snapshot
                    .as_ref()
                    .filter(|s| s.token == snapshot)
                    .ok_or_else(|| fail("Refresh engine inspection before selecting"))?
                    .clone();
                inspected.request(action)?;
                pending.stage = 1;
                pending.action = Some(action);
                pending.snapshot = Some(inspected);
                json!({"type":"gui.state","include_conversations":false})
            }
            Intent::Execute { review } => {
                let (reviewed, history) = self
                    .engine_review
                    .as_ref()
                    .filter(|(r, _)| r.token == review)
                    .ok_or_else(|| fail("Review the source engine action again"))?
                    .clone();
                let command = reviewed.request()?;
                pending.stage = 5;
                pending.action = Some(reviewed.action);
                pending.snapshot = Some(reviewed.snapshot.clone());
                pending.review = Some(reviewed);
                pending.history = Some(history);
                self.engine_review = None;
                command
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve engine operation"))?;
        command["id"] = id.to_value();
        pending.id = Some(id.clone());
        if pending.stage == 5 {
            pending.mutation = Some(id);
        }
        self.engine = Some(pending);
        self.menu = None;
        self.queue_menu = None;
        self.browser_page = None;
        self.backend_catalog = None;
        self.command_prepared = None;
        Ok(command)
    }
    fn engine_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::engine_controls::{Receipt, Review, Snapshot as EngineSnapshot, Update};
        self.ensure_drained()?;
        let mut pending = self.engine.take().expect("matched engine");
        if frame["ok"] != true {
            if pending.stage <= 4 {
                self.engine_snapshot = None;
                self.engine_review = None;
                return Ok(Step::Engine {
                    update: Update::Rejected(text(&frame, "error", 4096)?.into()),
                    policy: None,
                    state: None,
                });
            }
            return Err(fail("Engine action readback failed; outcome is unproven"));
        }
        let result = &frame["result"];
        let identity = self.engine_identity()?;
        let (session, backend, model, profile) = self.expected.as_ref().expect("active").clone();
        let (agent, schedules) = self.browser_authority.as_ref().expect("owned").clone();
        match pending.stage {
            0 => {
                let snapshot = EngineSnapshot::parse(result, &identity)?;
                let (_, generation, admission) = self.engine_instance.as_ref().expect("engine");
                if snapshot.generation != *generation || snapshot.admission != *admission {
                    return Err(fail(
                        "Engine inspection changed admission without an owned action",
                    ));
                }
                self.engine_snapshot = Some(snapshot.clone());
                return Ok(Step::Engine {
                    policy: Some(snapshot.policy.clone()),
                    update: Update::Snapshot(Box::new(snapshot)),
                    state: None,
                });
            }
            1 | 7 => {
                if pending.stage == 1
                    && (result["busy"] == true
                        || [
                            "active_work",
                            "pending_approvals",
                            "pending_questions",
                            "pending_model_loads",
                        ]
                        .iter()
                        .any(|key| result[key].as_array().is_some_and(|v| !v.is_empty())))
                {
                    return Ok(Step::Engine {
                        update: Update::Rejected(
                            "Finish source work before managing its engine".into(),
                        ),
                        policy: None,
                        state: None,
                    });
                }
                let state = Snapshot::parse(result)?;
                if pending.stage == 1 && state.held_restored_input.is_some_and(|n| n > 0) {
                    return Ok(Step::Engine {
                        update: Update::Rejected(
                            "Resolve held input before managing its engine".into(),
                        ),
                        policy: None,
                        state: None,
                    });
                }

                if state.session != session
                    || state.backend != backend
                    || state.model != model
                    || state.profile != profile
                    || state.schedules_paused != schedules
                    || state.held_restored_input != Some(0)
                    || result["ownership"]["agent_id"] != agent
                {
                    return Err(fail(
                        "Engine state changed owned authority or pending input",
                    ));
                }
                if result["tool_policy"] != pending.snapshot.as_ref().expect("inspected").policy {
                    return Err(fail("Engine state changed tool policy"));
                }
                pending.policy = Some(result["tool_policy"].clone());
                pending.state = Some(state);
            }
            2 => match crate::backend_controls::history(result, &session) {
                Ok(history) => pending.history = Some(history),
                Err(error) => {
                    return Ok(Step::Engine {
                        update: Update::Rejected(format!(
                            "Cannot review engine action with this history: {error}"
                        )),
                        policy: None,
                        state: None,
                    });
                }
            },
            3 | 9 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Engine revision exhausted"))?;
                let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
                if queue.session != session
                    || queue.backend != backend
                    || queue.model != model
                    || !queue.entries.is_empty()
                {
                    return Err(fail(
                        "Engine action requires unchanged empty held-input queue",
                    ));
                }
                if pending.stage == 9 {
                    let receipt = pending.receipt.take().expect("receipt");
                    let (_, generation, admission) = self.engine_instance.as_mut().expect("engine");
                    *generation = receipt.generation;
                    *admission = receipt.admission;
                    self.engine_snapshot = None;
                    self.engine_review = None;
                    self.queue_menu = Some(queue);
                    return Ok(Step::Engine {
                        update: Update::Settled {
                            admission: receipt.admission,
                            message: receipt.message(),
                        },
                        policy: pending.policy,
                        state: pending.state,
                    });
                }
            }
            4 => {
                let review = Review::parse(
                    result,
                    pending.snapshot.take().expect("snapshot"),
                    pending.action.expect("action"),
                )?;
                self.engine_review =
                    Some((review.clone(), pending.history.take().expect("history")));
                return Ok(Step::Engine {
                    update: Update::Review(Box::new(review)),
                    policy: pending.policy,
                    state: None,
                });
            }
            5 => {
                pending.receipt = Some(Receipt::parse(
                    result,
                    pending.review.as_ref().expect("review").clone(),
                )?);
            }
            6 => {
                pending
                    .receipt
                    .as_ref()
                    .expect("receipt")
                    .verify_readback(result)?;
            }
            8 => {
                let history = crate::backend_controls::history(result, &session)?;
                if Some(&history) != pending.history.as_ref() {
                    return Err(fail("Engine lifecycle changed saved conversation"));
                }
            }
            _ => return Err(fail("Unknown engine operation stage")),
        }
        if self.closing {
            return Ok(Step::Engine{update:Update::Rejected("Engine response consumed during shutdown; no action success or prompt was published".into()),policy:None,state:None});
        }
        pending.stage += 1;
        let mut command = match pending.stage {
            2 | 8 => {
                json!({"type":"gui.conversations.read","session_id":session,"max_response_bytes":1048576})
            }
            3 | 9 => json!({"type":crate::restored_queue::READ,"session_id":session}),
            4 => pending
                .snapshot
                .as_ref()
                .expect("snapshot")
                .request(pending.action.expect("action"))?,
            6 => pending
                .receipt
                .as_ref()
                .expect("receipt")
                .readback_request(),
            7 => json!({"type":"gui.state","include_conversations":false}),
            _ => return Err(fail("Invalid engine continuation")),
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve engine readback"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.engine = Some(pending);
        Ok(Step::Write(command))
    }
}
