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
    Bootstrap(Value),
    Write(Value),
    Initialized(Snapshot),
    Restored {
        snapshot: Snapshot,
        history: crate::resume::History,
    },
    Control(Update),
    Queue(crate::restored_queue::Update),
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
}
struct Reconciliation {
    entry: crate::restored_queue::Entry,
    accepted: Option<bool>,
}
struct QueuePending {
    id: CommandId,
    applying: bool,
    cancelled: Option<String>,
    continued: Option<(String, String, bool)>,
    reconciled: Option<Reconciliation>,
    failure: Option<String>,
    succeeded: bool,
}
pub struct Management {
    wire: WireClient,
    control: Option<ControlPending>,
    queue: Option<QueuePending>,
    queue_menu: Option<crate::restored_queue::Snapshot>,
    continuation: Option<(String, String)>,
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
            control: None,
            queue: None,
            queue_menu: None,
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
    pub fn ready(&self) -> bool {
        self.phase == Phase::Active
            && !self.failed
            && self.control.is_none()
            && self.queue.is_none()
            && self.continuation.is_none()
    }
    pub fn operations(&self) -> &[String] {
        &self.operations
    }
    pub fn prompt(&mut self, id: &str) -> io::Result<()> {
        if !self.ready() || id.is_empty() || id.len() > 128 {
            return Err(fail("Management is not ready for a prompt"));
        }
        self.ensure_drained()?;
        self.menu = None;
        self.queue_menu = None;
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
                let command = json!({"type":"gui.state","id":id.to_value()});
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
                Ok(Step::Event(event))
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
            if kind == "turn_start" {
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
        use crate::restored_queue::{CANCEL, Intent, READ, RECONCILE};
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        let mut command = json!({"type":READ, "session_id":session});
        let mut reconciled = None;
        let cancelled = match intent {
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
            Intent::Cancel { revision, entry } => {
                let menu = self
                    .queue_menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
                if !menu.entries.iter().any(|e| e.id == entry && e.can_cancel) {
                    return Err(fail("Entry was not offered for cancellation"));
                }
                command["type"] = json!(CANCEL);
                command["snapshot"] = json!(menu.token);
                command["entry_id"] = json!(entry);
                Some(entry)
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
            applying: cancelled.is_some() || reconciled.is_some(),
            reconciled,
            cancelled,
            continued: None,
            failure: None,
            succeeded: false,
        });
        self.queue_menu = None;
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
            return if pending.cancelled.is_some()
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
                .any(|e| Some(&e.id) == pending.cancelled.as_ref())
        {
            return Err(fail("Runtime still holds the cancelled entry"));
        }
        if let Some((operation, entry, refused)) = &pending.continued {
            let result = &frame["result"];
            if result["operation_id"] != *operation
                || result["entry_id"] != *entry
                || result["continuation_outcome"] != if *refused { "refused" } else { "accepted" }
                || snapshot.entries.iter().any(|row| row.id == *entry) != *refused
            {
                return Err(fail(
                    "Continuation readback disagrees with its execution receipt",
                ));
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
                pending
                    .cancelled
                    .map(|_| "Cancellation saved. Queue read back; nothing replayed.".into())
            });
        if pending.continued.is_some() {
            snapshot.message = Some("Continuation settled; saved queue read back.".into());
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
        })
    }
    pub fn begin_continuation(
        &mut self,
        mut prepared: Continuation,
        id: &str,
    ) -> io::Result<Value> {
        self.prompt(id)?;
        self.continuation = Some((id.to_owned(), prepared.entry));
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
            "session_id":self.expected.as_ref().expect("active").0,"operation_id":operation,"entry_id":entry});
        self.queue = Some(QueuePending {
            id,
            applying: false,
            cancelled: None,
            continued: Some((operation, entry, refused)),
            reconciled: None,
            failure: None,
            succeeded: false,
        });
        Ok(command)
    }
}
