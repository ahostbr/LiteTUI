//! Approval display and command correlation; Python alone owns policy/authority.
//! ACK and closure are distinct facts. Neither proves execution or a saved rule.
use serde_json::{Value, json};
use std::{collections::VecDeque, io, io::Write};

const MAX_REQUESTS: usize = 100;
const MAX_PENDING: usize = 16;
const MAX_TEXT: usize = 16384;

#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub id: String,
    pub tool: String,
    pub input: String,
    pub profile: String,
    pub why: String,
    pub timeout_s: Option<f64>,
    pub remember_scope: Option<Value>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Requested(Request),
    Closed {
        id: String,
        outcome: Option<String>,
        reason: String,
    },
    Acknowledged {
        id: String,
        accepted: bool,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub struct Decision {
    pub id: String,
    pub allow: bool,
    pub remember: bool,
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn string(event: &Value, field: &str, max: usize) -> io::Result<String> {
    event[field]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .map(str::to_owned)
        .ok_or_else(|| fail("Invalid approval text field"))
}
fn id(event: &Value) -> io::Result<String> {
    let value = string(event, "id", 128)?;
    if value
        .chars()
        .any(|ch| ch.is_control() || ch.is_whitespace())
    {
        return Err(fail("Invalid approval request ID"));
    }
    Ok(value)
}
// Stop serialization at the display limit, including pretty indentation.
// Checking after to_string_pretty permits frame-bounded JSON to amplify ~100x.
#[derive(Default)]
struct LimitedJson(Vec<u8>);
impl Write for LimitedJson {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_TEXT {
            return Err(fail("Approval JSON display limit exceeded"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn input_text(value: &Value) -> io::Result<String> {
    let mut output = LimitedJson::default();
    serde_json::to_writer_pretty(&mut output, value)?;
    String::from_utf8(output.0).map_err(io::Error::other)
}
fn remember_scope(event: &Value) -> io::Result<Option<Value>> {
    let Some(scope) = event.get("remember_scope") else {
        return Ok(None);
    };
    let object = scope
        .as_object()
        .ok_or_else(|| fail("Invalid remembered approval scope"))?;
    if object.len() != 7
        || scope["version"] != 1
        || scope["scope"] != "conversation"
        || scope["tool"] != event["tool"]
        || event.get("rewrite_prewrite").is_some()
    {
        return Err(fail("Invalid remembered approval authority"));
    }
    let caps = scope["capabilities"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 32)
        .ok_or_else(|| fail("Invalid approval capabilities"))?;
    let caps = caps
        .iter()
        .map(|v| {
            v.as_str()
                .filter(|v| !v.is_empty() && v.len() <= 128)
                .ok_or_else(|| fail("Invalid approval capability"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    if caps.windows(2).any(|v| v[0] >= v[1])
        || scope["rule"] != format!("{}:{}", string(event, "tool", 256)?, caps.join(","))
    {
        return Err(fail("Approval rule differs from its exact capabilities"));
    }
    let session = string(scope, "conversation_id", 36)?;
    if session.len() != 36 || !session.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err(fail("Invalid standing approval conversation"));
    }
    let digest = string(scope, "snapshot", 64)?;
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(fail("Invalid standing approval snapshot"));
    }
    Ok(Some(scope.clone()))
}
impl Request {
    fn parse(event: &Value) -> io::Result<Self> {
        let timeout_s = match event.get("timeout_s") {
            Some(Value::Null) => None,
            Some(value) => Some(
                value
                    .as_f64()
                    .filter(|v| v.is_finite() && *v > 0.0)
                    .ok_or_else(|| fail("Invalid approval timeout"))?,
            ),
            None => return Err(fail("Missing approval timeout")),
        };
        if !event["input"].is_object() {
            return Err(fail("Approval input must be an object"));
        }
        let request = Self {
            id: id(event)?,
            tool: string(event, "tool", 256)?,
            input: input_text(&event["input"])?,
            profile: string(event, "profile", 128)?,
            why: string(event, "why", 4096)?,
            timeout_s,
            remember_scope: remember_scope(event)?,
        };
        if request.text_bytes() > MAX_TEXT {
            return Err(fail("Approval request exceeds display byte limit"));
        }
        // Same escaped-control/LF accounting as the bounded transcript. A modal
        // cannot bypass layout-work limits through thousands of short lines.
        crate::live_chat::display_units(&request.display_text())?;
        Ok(request)
    }
    pub fn text_bytes(&self) -> usize {
        self.id.len()
            + self.tool.len()
            + self.input.len()
            + self.profile.len()
            + self.why.len()
            + self
                .remember_scope
                .as_ref()
                .map_or(0, |s| s.to_string().len())
    }
    pub fn display_text(&self) -> String {
        format!(
            "Request: {}\nTool: {}\nProfile: {}\nWhy: {}\nInput:\n{}",
            self.id, self.tool, self.profile, self.why, self.input
        ) + &self.remember_scope.as_ref().map_or(String::new(), |s| format!(
            "\n\nAlways allow: {}\nConversation: {}\nOnly these capabilities: {}\nA wider request asks again. Saved rule applies to this conversation; failed persistence is session-only.",
            s["tool"].as_str().unwrap_or_default(), s["conversation_id"].as_str().unwrap_or_default(), s["capabilities"]))
    }
}
struct Record {
    id: String,
    closed: bool,
    prewrite: Option<Value>,
    remember_scope: Option<Value>,
    remember: bool,
    command: Option<(String, bool)>,
    acknowledged: bool,
    accepted: bool,
}
#[derive(Default)]
pub struct Ledger {
    records: Vec<Record>,
}
impl Ledger {
    pub fn request(&mut self, event: &Value) -> io::Result<Event> {
        let request = Request::parse(event)?;
        if self.records.len() >= MAX_REQUESTS
            || self.records.iter().filter(|r| !r.closed).count() >= MAX_PENDING
        {
            return Err(fail("Approval lifecycle limit exceeded"));
        }
        if self.records.iter().any(|r| r.id == request.id) {
            return Err(fail("Reused approval request ID"));
        }
        self.records.push(Record {
            id: request.id.clone(),
            closed: false,
            prewrite: event.get("rewrite_prewrite").cloned(),
            remember_scope: request.remember_scope.clone(),
            remember: false,
            command: None,
            acknowledged: false,
            accepted: false,
        });
        Ok(Event::Requested(request))
    }
    pub fn close(&mut self, event: &Value) -> io::Result<Event> {
        let id = id(event)?;
        let outcome = match event.get("outcome") {
            Some(Value::Null) => None,
            Some(Value::String(value))
                if matches!(
                    value.as_str(),
                    "approved"
                        | "denied"
                        | "timeout"
                        | "cancelled"
                        | "revoked"
                        | "audit-error"
                        | "error"
                ) =>
            {
                Some(value.clone())
            }
            _ => return Err(fail("Invalid approval closure outcome")),
        };
        let mut reason = string(event, "reason", 32)?;
        if !matches!(
            reason.as_str(),
            "answered" | "timeout" | "cancelled" | "revoked" | "audit-error" | "error"
        ) {
            return Err(fail("Invalid approval closure reason"));
        }
        let record = self
            .records
            .iter_mut()
            .find(|r| r.id == id && !r.closed)
            .ok_or_else(|| fail("Unknown or already closed approval"))?;
        if record.prewrite.as_ref() != event.get("rewrite_prewrite") {
            return Err(fail("Approval closure changed its reviewed action"));
        }
        if record.prewrite.is_some()
            && let Some(decision @ ("approved" | "denied")) = outcome.as_deref()
            && (!record.accepted
                || record.command.as_ref().map(|item| item.1) != Some(decision == "approved"))
        {
            return Err(fail(
                "Prewrite approval closed without its accepted host decision",
            ));
        }
        if record.remember && reason == "answered" && outcome.as_deref() == Some("approved") {
            let receipt = &event["remembered"];
            if receipt.as_object().map(|v| v.len()) != Some(3)
                || receipt.get("scope") != record.remember_scope.as_ref()
                || receipt["saved"].as_bool().is_none()
                || receipt["session_only"].as_bool() != receipt["saved"].as_bool().map(|v| !v)
            {
                return Err(fail("Remembered approval lacks exact saved-rule readback"));
            }
            reason.push_str(if receipt["saved"] == true {
                "; exact standing rule saved and independently verified"
            } else {
                "; standing rule is SESSION ONLY, saved rule was not verified"
            });
        } else if event.get("remembered").is_some() {
            return Err(fail("Unrequested remembered approval receipt"));
        }
        record.closed = true;
        Ok(Event::Closed {
            id,
            outcome,
            reason,
        })
    }
    /// A queued decision may race source closure. Drop that stale intent, never
    /// retarget it to the next request or retry it. Other invalid use fails.
    pub fn decide(&mut self, decision: Decision) -> io::Result<Option<Value>> {
        let (index, record) = self
            .records
            .iter_mut()
            .enumerate()
            .find(|(_, r)| r.id == decision.id)
            .ok_or_else(|| fail("Decision targets unknown approval"))?;
        if record.closed {
            return Ok(None);
        }
        if record.command.is_some() {
            return Err(fail("Approval decision already sent"));
        }
        let mut command = format!("rust-approval-decision-{index}");
        if command == record.id {
            command.push_str("-command");
        }
        if decision.remember
            && (!decision.allow || record.remember_scope.is_none() || record.prewrite.is_some())
        {
            return Err(fail("Request does not authorize a standing rule"));
        }
        record.remember = decision.remember;
        record.command = Some((command.clone(), decision.allow));
        let mut value = json!({"type":"approve", "id":command, "approval_id":decision.id,
                               "allow":decision.allow, "remember":decision.remember});
        if decision.remember {
            value["remember_scope"] = record.remember_scope.clone().expect("validated scope");
        }
        if let Some(marker) = &record.prewrite {
            value["rewrite_prewrite"] = marker.clone();
        }
        Ok(Some(value))
    }
    pub fn response(&mut self, event: &Value) -> io::Result<Option<Event>> {
        let Some(record) = self
            .records
            .iter_mut()
            .find(|r| r.command.as_ref().is_some_and(|c| event["id"] == c.0))
        else {
            return Ok(None);
        };
        if record.acknowledged {
            return Err(fail("Duplicate approval command response"));
        }
        let accepted = match event["ok"].as_bool() {
            Some(true)
                if event["result"]["approved"] == record.id
                    && event["result"]["allow"] == record.command.as_ref().expect("command").1
                    && event["result"]["id_key"] == "approval_id" =>
            {
                true
            }
            Some(false)
                if event["error"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && s.len() <= 4096) =>
            {
                false
            }
            _ => return Err(fail("Malformed approval command response")),
        };
        record.acknowledged = true;
        record.accepted = accepted;
        Ok(Some(Event::Acknowledged {
            id: record.id.clone(),
            accepted,
        }))
    }
    pub fn all_closed(&self) -> bool {
        self.records.iter().all(|r| r.closed)
    }
    pub fn settled(&self) -> bool {
        self.records
            .iter()
            .all(|r| r.closed && (r.command.is_none() || r.acknowledged))
    }
}

pub struct Pending {
    pub request: Request,
    pub allow: bool,
    pub waiting: bool,
    pub scroll: usize,
    pub remember: bool,
    pub confirming: bool,
    pub confirmation_shown: bool,
    pub reviewed: bool,
    pub painted_lines: std::collections::BTreeSet<usize>,
    pub layout: Option<(u16, u16)>,
    pub hits: Vec<(ratatui::layout::Rect, u8)>,
    pub pressed: Option<(ratatui::layout::Rect, u8)>,
}
impl Pending {
    pub fn select(&mut self, choice: u8) {
        self.allow = choice != 0;
        self.remember = choice == 2 && self.request.remember_scope.is_some();
        self.confirming = false;
        self.confirmation_shown = false;
    }
    pub fn cycle(&mut self, reverse: bool) {
        let count = if self.request.remember_scope.is_some() {
            3
        } else {
            2
        };
        let now = if self.remember {
            2
        } else {
            u8::from(self.allow)
        };
        self.select((now + if reverse { count - 1 } else { 1 }) % count);
    }
}
#[derive(Default)]
pub struct View {
    pending: VecDeque<Pending>,
    decision: Option<Decision>,
}
impl View {
    pub fn current(&self) -> Option<&Pending> {
        self.pending.front()
    }
    pub fn current_mut(&mut self) -> Option<&mut Pending> {
        self.pending.front_mut()
    }
    pub fn len(&self) -> usize {
        self.pending.len()
    }
    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }
    pub fn observe(&mut self, event: Event) -> io::Result<String> {
        match event {
            Event::Requested(request) => {
                if self.pending.len() >= MAX_PENDING
                    || self.pending.iter().any(|r| r.request.id == request.id)
                {
                    return Err(fail("Approval view limit or duplicate request"));
                }
                self.pending.push_back(Pending {
                    request,
                    allow: false,
                    waiting: false,
                    scroll: 0,
                    remember: false,
                    confirming: false,
                    confirmation_shown: false,
                    reviewed: false,
                    painted_lines: Default::default(),
                    layout: None,
                    hits: Vec::new(),
                    pressed: None,
                });
                Ok(
                    "Approval requested; runtime deadline still applies. No automatic answer."
                        .into(),
                )
            }
            Event::Closed {
                id,
                outcome,
                reason,
            } => {
                let index = self
                    .pending
                    .iter()
                    .position(|r| r.request.id == id)
                    .ok_or_else(|| fail("Closure without visible approval"))?;
                self.pending.remove(index);
                if self.decision.as_ref().is_some_and(|d| d.id == id) {
                    self.decision = None;
                }
                Ok(format!(
                    "Approval {id} closed: authority={}, wait={reason}; not execution proof",
                    outcome.as_deref().unwrap_or("unknown")
                ))
            }
            Event::Acknowledged { id, accepted } => Ok(format!(
                "Approval {id} decision {} (not execution); awaiting closure if still open",
                if accepted { "accepted" } else { "rejected" }
            )),
        }
    }
    pub fn confirm(&mut self) {
        let Some(current) = self.pending.front_mut() else {
            return;
        };
        if current.waiting {
            return;
        }
        if current.remember {
            if !current.reviewed {
                return;
            }
            if !current.confirming {
                current.confirming = true;
                return;
            }
            if !current.confirmation_shown {
                return;
            }
        }
        let (allow, remember) = (current.allow, current.remember);
        self.choose_exact(allow, remember);
    }
    pub fn mouse(&mut self, event: crossterm::event::MouseEvent) {
        use crossterm::event::{MouseButton, MouseEventKind};
        let Some(current) = self.pending.front_mut() else {
            return;
        };
        if current.waiting || !event.modifiers.is_empty() {
            current.pressed = None;
            return;
        }
        let hit = current
            .hits
            .iter()
            .find(|(r, _)| r.contains((event.column, event.row).into()))
            .copied();
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => current.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                let pressed = current.pressed.take();
                if let Some((_, choice)) = hit
                    && hit == pressed
                {
                    if choice == 3 {
                        self.confirm();
                    } else {
                        current.select(choice);
                    }
                }
            }
            _ => current.pressed = None,
        }
    }
    pub fn choose(&mut self, allow: bool) {
        self.choose_exact(allow, false);
    }
    fn choose_exact(&mut self, allow: bool, remember: bool) {
        if let Some(current) = self.pending.front_mut()
            && !current.waiting
        {
            current.waiting = true;
            self.decision = Some(Decision {
                id: current.request.id.clone(),
                allow,
                remember,
            });
        }
    }
    pub fn take_decision(&mut self) -> Option<Decision> {
        self.decision.take()
    }
    pub fn clear(&mut self) {
        self.pending.clear();
        self.decision = None;
    }
}
