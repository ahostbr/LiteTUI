//! Owned-runtime negotiation and bounded startup facts. A snapshot is not a
//! stream barrier or an authority grant. Only a fresh idle session is admitted.
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

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub session: String,
    pub model: String,
    pub backend: String,
    pub profile: String,
    pub mode: String,
    pub writable: bool,
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
    Event(Value),
    Ignore,
}
#[derive(Clone, Copy, PartialEq)]
enum Phase {
    AwaitReady,
    Hello,
    State,
    Active,
}

pub struct Management {
    wire: WireClient,
    phase: Phase,
    expected: Option<(String, String, String, String)>,
    state_id: Option<CommandId>,
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
            phase: Phase::AwaitReady,
            expected: None,
            state_id: None,
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
        self.phase = Phase::Hello;
        self.wire
            .begin_hello()
            .map_err(|_| fail("Cannot begin management negotiation"))
    }
    pub fn ready(&self) -> bool {
        self.phase == Phase::Active && !self.failed
    }
    pub fn operations(&self) -> &[String] {
        &self.operations
    }
    pub fn prompt(&mut self, id: &str) -> io::Result<()> {
        if !self.ready() || id.is_empty() || id.len() > 128 {
            return Err(fail("Management is not ready for a prompt"));
        }
        self.ensure_drained()?;
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
                self.phase = Phase::Active;
                Ok(Step::Initialized(snapshot))
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
            return Err(fail(
                "Runtime model changed outside an owned management operation",
            ));
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
