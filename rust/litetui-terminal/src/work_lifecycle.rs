//! Captured background producer/input ownership; this ledger never launches work.
use crate::tool_mode::Profile;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
};

pub const CAPTURE: &str = "gui.work.native_capture";
pub const ACTIVATE: &str = "gui.work.native_activate";
pub const SETTLED: &str = "gui.work.native_settled";
pub const EVENTS: [&str; 7] = [
    "work_producer",
    "work_input",
    "work_admission",
    "work_settled",
    "work_closing",
    "work_reconciled",
    "work_transition_proposed",
];
pub const ENVELOPE_BYTES: usize = 131_072;
pub const PAYLOAD_BYTES: usize = 65_536;
pub const REPLY_BYTES: usize = 1_048_576;
pub(crate) fn bad(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
pub(crate) fn keys(v: &Value, expected: &[&str]) -> io::Result<()> {
    let o = v
        .as_object()
        .ok_or_else(|| bad("Work value is not an object"))?;
    if o.len() != expected.len() || expected.iter().any(|k| !o.contains_key(*k)) {
        return Err(bad("Work object fields differ from version 1"));
    }
    Ok(())
}
pub(crate) fn text(v: &Value, key: &str, max: usize) -> io::Result<String> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && !s.contains('\0') && s.len() <= max)
        .map(str::to_owned)
        .ok_or_else(|| bad(&format!("Invalid Work {key}")))
}
pub(crate) fn optional(v: &Value, key: &str, max: usize) -> io::Result<Option<String>> {
    match v.get(key) {
        Some(Value::Null) => Ok(None),
        Some(_) => text(v, key, max).map(Some),
        None => Err(bad("Missing nullable Work field")),
    }
}
pub(crate) fn number(v: &Value, key: &str, positive: bool) -> io::Result<u64> {
    v.get(key)
        .and_then(Value::as_u64)
        .filter(|n| !positive || *n > 0)
        .ok_or_else(|| bad(&format!("Invalid Work integer {key}")))
}
fn uuid_text(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                *c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(c)
            }
        })
}
pub(crate) fn uuid(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 36)?;
    if !uuid_text(&s) {
        return Err(bad("Invalid canonical Work UUID"));
    }
    Ok(s)
}
pub(crate) fn optional_uuid(v: &Value, key: &str) -> io::Result<Option<String>> {
    let s = optional(v, key, 36)?;
    if s.as_ref().is_some_and(|s| !uuid_text(s)) {
        return Err(bad("Invalid nullable Work UUID"));
    }
    Ok(s)
}
pub(crate) fn hash(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(bad("Invalid Work SHA256/token"));
    }
    Ok(s)
}
pub(crate) fn bounded(v: &Value, max: usize) -> io::Result<()> {
    if serde_json::to_vec(v)?.len() > max {
        return Err(bad("Work wire byte budget exceeded"));
    }
    Ok(())
}
pub(crate) fn profile(v: &Value, key: &str) -> io::Result<Profile> {
    Profile::parse(&text(v, key, 32)?)
}
fn rank(p: Profile) -> u8 {
    match p {
        Profile::Strict => 0,
        Profile::Interactive => 1,
        Profile::Autonomous => 2,
    }
}
fn proof(v: &Value, names: &[&str]) -> io::Result<()> {
    keys(v, names)?;
    if names.iter().any(|key| v[*key] != true) {
        return Err(bad("Work retirement proof is incomplete"));
    }
    Ok(())
}
fn ids(v: &Value, key: &str, max: usize) -> io::Result<BTreeSet<String>> {
    let a = v[key]
        .as_array()
        .filter(|a| a.len() <= max)
        .ok_or_else(|| bad("Invalid Work identity set"))?;
    let mut out = BTreeSet::new();
    for item in a {
        let s = item
            .as_str()
            .filter(|s| uuid_text(s))
            .ok_or_else(|| bad("Invalid Work set UUID"))?;
        if !out.insert(s.into()) {
            return Err(bad("Duplicate Work identity set entry"));
        }
    }
    Ok(out)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
}
impl Identity {
    pub fn parse(v: &Value) -> io::Result<Self> {
        keys(v, &["session_id", "agent_id", "backend", "model"])?;
        Ok(Self {
            session: uuid(v, "session_id")?,
            agent: uuid(v, "agent_id")?,
            backend: text(v, "backend", 64)?,
            model: text(v, "model", 1024)?,
        })
    }
    pub fn value(&self) -> Value {
        json!({"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"model":self.model})
    }
    pub fn request(&self, operation: &str) -> Value {
        json!({"type":operation,"version":1,"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"model":self.model})
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Provider {
    Host,
    Codex,
    Claude,
}
impl Provider {
    pub(crate) fn parse(v: &Value, key: &str) -> io::Result<Self> {
        match text(v, key, 16)?.as_str() {
            "host" => Ok(Self::Host),
            "codex" => Ok(Self::Codex),
            "claude" => Ok(Self::Claude),
            _ => Err(bad("Invalid Work provider")),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Origin {
    pub session: String,
    pub operation: String,
    pub hook: Option<String>,
    pub source: String,
    pub execution: String,
}
impl Origin {
    pub fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "session_id",
                "operation_id",
                "hook_id",
                "source",
                "execution_id",
            ],
        )?;
        let execution = crate::stream::required_uuid4(v, "execution_id")
            .map_err(|_| bad("Invalid Work execution UUID"))?
            .to_owned();
        Ok(Self {
            session: uuid(v, "session_id")?,
            operation: text(v, "operation_id", 128)?,
            hook: optional_uuid(v, "hook_id")?,
            source: text(v, "source", 128)?,
            execution,
        })
    }
    pub fn value(&self) -> Value {
        json!({"session_id":self.session,"operation_id":self.operation,"hook_id":self.hook,"source":self.source,"execution_id":self.execution})
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProducerKind {
    HostTask,
    Inbox,
    ChildResult,
    CompactWake,
    ProviderQuestion,
    ProviderRecovery,
    Goal,
    Cron,
    Loop,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Producer {
    pub id: String,
    pub kind: ProducerKind,
    pub object: String,
    pub session: String,
    pub owner: String,
    pub owner_created: Option<String>,
    pub generation: u64,
    pub action: Option<String>,
    pub operation: Option<String>,
    pub execution: Option<String>,
}
impl Producer {
    pub(crate) fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "producer_id",
                "kind",
                "object_id",
                "producer_session_id",
                "owner_instance",
                "owner_created",
                "generation",
                "causal_action_id",
                "causal_operation_id",
                "causal_execution_id",
            ],
        )?;
        let kind = match text(v, "kind", 32)?.as_str() {
            "host_task" => ProducerKind::HostTask,
            "inbox" => ProducerKind::Inbox,
            "child_result" => ProducerKind::ChildResult,
            "compact_wake" => ProducerKind::CompactWake,
            "provider_question" => ProducerKind::ProviderQuestion,
            "provider_recovery" => ProducerKind::ProviderRecovery,
            "goal" => ProducerKind::Goal,
            "cron" => ProducerKind::Cron,
            "loop" => ProducerKind::Loop,
            _ => return Err(bad("Unknown Work producer kind")),
        };
        let action = optional_uuid(v, "causal_action_id")?;
        let operation = optional(v, "causal_operation_id", 128)?;
        let execution = optional_uuid(v, "causal_execution_id")?;
        if action.is_some() && (operation.is_none() || execution.is_none())
            || execution.is_some() != operation.is_some()
        {
            return Err(bad("Incomplete Work causal binding"));
        }
        Ok(Self {
            id: uuid(v, "producer_id")?,
            kind,
            object: text(v, "object_id", 128)?,
            session: uuid(v, "producer_session_id")?,
            owner: text(v, "owner_instance", 128)?,
            owner_created: optional(v, "owner_created", 128)?,
            generation: number(v, "generation", true)?,
            action,
            operation,
            execution,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Input {
    pub id: String,
    pub producer: String,
    pub payload_sha256: String,
    pub source: String,
    pub requested: Profile,
    pub session: String,
    pub revision: u64,
}
impl Input {
    pub(crate) fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "input_id",
                "producer_id",
                "payload_sha256",
                "source",
                "requested_profile",
                "delivery_session_id",
                "queue_revision",
            ],
        )?;
        Ok(Self {
            id: uuid(v, "input_id")?,
            producer: uuid(v, "producer_id")?,
            payload_sha256: hash(v, "payload_sha256")?,
            source: text(v, "source", 128)?,
            requested: profile(v, "requested_profile")?,
            session: uuid(v, "delivery_session_id")?,
            revision: number(v, "queue_revision", true)?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommittedHistory {
    pub session: String,
    pub transcript_sha256: String,
    pub context_sha256: String,
    pub receipt_ids: Vec<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Delivery {
    Append,
    CommittedHistory(CommittedHistory),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Payload {
    /// Committed history grants status/reference display, never a user append.
    pub delivery: Delivery,
    pub content: String,
    pub text: Option<String>,
    pub source: String,
    pub profile: Profile,
}
impl Payload {
    fn parse(v: &Value, input: &Input) -> io::Result<Self> {
        let payload = Self::evidence(
            v,
            &input.source,
            input.requested,
            Some(&input.payload_sha256),
        )?;
        if let Delivery::CommittedHistory(history) = &payload.delivery
            && history.session != input.session
        {
            return Err(bad("Committed history changed delivery conversation"));
        }
        Ok(payload)
    }
    pub(crate) fn evidence(
        v: &Value,
        expected_source: &str,
        requested: Profile,
        digest: Option<&str>,
    ) -> io::Result<Self> {
        let committed = v.get("delivery").is_some();
        let fields: &[&str] = if committed {
            &["delivery", "source", "tool_profile", "text", "history"]
        } else if v.get("text").is_some() {
            &["content", "text", "tool_profile", "source"]
        } else {
            &["content", "tool_profile", "source"]
        };
        keys(v, fields)?;
        // Value objects use sorted keys. This frozen object contains strings only,
        // matching Python compact sorted UTF-8 JSON without ASCII escaping.
        let encoded = serde_json::to_vec(v)?;
        if encoded.len() > PAYLOAD_BYTES
            || digest.is_some_and(|expected| format!("{:x}", Sha256::digest(&encoded)) != expected)
        {
            return Err(bad(
                "Work payload differs from its durable input digest/budget",
            ));
        }
        let (delivery, content, display) = if committed {
            if v["delivery"] != "committed_history" || v["source"] != "child-result" {
                return Err(bad("Unknown committed Work delivery"));
            }
            let history = &v["history"];
            keys(
                history,
                &[
                    "session_id",
                    "transcript_sha256",
                    "context_sha256",
                    "receipt_ids",
                ],
            )?;
            let rows = history["receipt_ids"]
                .as_array()
                .filter(|rows| !rows.is_empty() && rows.len() <= 256)
                .ok_or_else(|| bad("Invalid committed receipt count"))?;
            let mut receipt_ids = Vec::new();
            let mut seen = BTreeSet::new();
            for row in rows {
                let id = row
                    .as_str()
                    .filter(|id| {
                        !id.is_empty()
                            && id.len() <= 128
                            && id
                                .bytes()
                                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
                    })
                    .ok_or_else(|| bad("Invalid committed receipt identity"))?;
                if !seen.insert(id) {
                    return Err(bad("Duplicate committed receipt identity"));
                }
                receipt_ids.push(id.to_owned());
            }
            (
                Delivery::CommittedHistory(CommittedHistory {
                    session: uuid(history, "session_id")?,
                    transcript_sha256: hash(history, "transcript_sha256")?,
                    context_sha256: hash(history, "context_sha256")?,
                    receipt_ids,
                }),
                String::new(),
                Some(text(v, "text", 1024)?),
            )
        } else {
            let content = v["content"]
                .as_str()
                .filter(|value| !value.is_empty() && value.len() <= PAYLOAD_BYTES)
                .ok_or_else(|| bad("Invalid Work content"))?
                .to_owned();
            let display = match v.get("text") {
                None => None,
                Some(Value::String(value)) if value.len() <= PAYLOAD_BYTES => Some(value.clone()),
                _ => return Err(bad("Invalid Work display text")),
            };
            (Delivery::Append, content, display)
        };
        let source = text(v, "source", 128)?;
        let profile = profile(v, "tool_profile")?;
        if source != expected_source || profile != requested {
            return Err(bad("Work payload changed its original source/profile"));
        }
        Ok(Self {
            delivery,
            content,
            text: display,
            source,
            profile,
        })
    }
    pub fn display_text(&self) -> &str {
        self.text.as_deref().unwrap_or(&self.content)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    NewTurn,
    InTurn,
    ActionSegment,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Admission {
    pub id: String,
    pub input: String,
    pub mode: Mode,
    pub operation: String,
    pub origin: Origin,
    pub parent: Option<String>,
    pub boundary: u64,
    pub requested: Profile,
    pub effective: Profile,
    pub seat: Profile,
    pub provider: Provider,
    pub delivery: Option<(String, String)>,
}
impl Admission {
    pub(crate) fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "admission_id",
                "input_id",
                "mode",
                "operation_id",
                "origin",
                "parent_execution_id",
                "boundary",
                "requested_profile",
                "effective_profile",
                "seat_profile",
                "provider",
                "provider_delivery",
            ],
        )?;
        let mode = match text(v, "mode", 32)?.as_str() {
            "new_turn" => Mode::NewTurn,
            "in_turn" => Mode::InTurn,
            "action_segment" => Mode::ActionSegment,
            _ => return Err(bad("Unknown Work admission mode")),
        };
        let operation = text(v, "operation_id", 128)?;
        let origin = Origin::parse(&v["origin"])?;
        let parent = optional_uuid(v, "parent_execution_id")?;
        let boundary = number(v, "boundary", false)?;
        let requested = profile(v, "requested_profile")?;
        let effective = profile(v, "effective_profile")?;
        let seat = profile(v, "seat_profile")?;
        let provider = Provider::parse(v, "provider")?;
        let delivery = if v["provider_delivery"].is_null() {
            None
        } else {
            let d = &v["provider_delivery"];
            keys(d, &["id", "state"])?;
            let state = text(d, "state", 32)?;
            if !matches!(state.as_str(), "prepared" | "pending" | "accepted") {
                return Err(bad("Invalid native Work delivery state"));
            }
            Some((text(d, "id", 128)?, state))
        };
        if operation != origin.operation
            || rank(effective) > rank(seat)
            || provider == Provider::Host && delivery.is_some()
            || provider == Provider::Claude && delivery.as_ref().is_some_and(|d| d.1 != "prepared")
        {
            return Err(bad("Contradictory Work admission authority/delivery"));
        }
        match mode {
            Mode::NewTurn if parent.is_none() && boundary == 0 && uuid_text(&operation) => {}
            Mode::InTurn
                if parent.as_deref() == Some(origin.execution.as_str()) && boundary > 0 => {}
            Mode::ActionSegment
                if parent.is_some()
                    && parent.as_deref() != Some(origin.execution.as_str())
                    && boundary == 0 => {}
            _ => return Err(bad("Contradictory Work admission execution relationship")),
        }
        Ok(Self {
            id: uuid(v, "admission_id")?,
            input: uuid(v, "input_id")?,
            mode,
            operation,
            origin,
            parent,
            boundary,
            requested,
            effective,
            seat,
            provider,
            delivery,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputState {
    Queued,
    Claiming,
    Held,
    Uncertain,
    Cancelled,
    Admitted,
    Settled,
    Reconciled,
}
impl InputState {
    fn parse(s: &str) -> io::Result<Self> {
        match s {
            "queued" => Ok(Self::Queued),
            "claiming" => Ok(Self::Claiming),
            "held" => Ok(Self::Held),
            "uncertain" => Ok(Self::Uncertain),
            "cancelled" => Ok(Self::Cancelled),
            _ => Err(bad("Invalid Work input phase")),
        }
    }
    fn terminal(self) -> bool {
        matches!(self, Self::Cancelled | Self::Settled | Self::Reconciled)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProducerState {
    Registered,
    Running,
    StopRequested,
    Retired,
    Recovered,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Epoch {
    pub instance: String,
    pub identity: Identity,
    pub provider: Provider,
}
impl Epoch {
    fn parse(v: &Value, current: Option<&Identity>) -> io::Result<Self> {
        keys(v, &["instance", "identity", "provider"])?;
        let identity = Identity::parse(&v["identity"])?;
        if current.is_some_and(|current| {
            identity.session != current.session || identity.agent != current.agent
        }) {
            return Err(bad(
                "Recovered Work epoch belongs to another conversation/agent",
            ));
        }
        Ok(Self {
            instance: hash(v, "instance")?,
            identity,
            provider: Provider::parse(v, "provider")?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Recovered {
    pub epoch: Epoch,
    pub producer: Producer,
    pub input: Input,
    pub state: InputState,
    pub reason: String,
    pub admission: Option<Admission>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeferredJob {
    pub invocation_id: String,
    pub job_id: String,
    pub kind: ProducerKind,
    pub generation: u64,
    pub source: String,
    pub requested: Profile,
    pub payload: Payload,
    pub job_snapshot: String,
    pub job_sha256: String,
    pub epoch: Epoch,
    pub target_session: String,
    pub target_agent: String,
    /// Historical retirement evidence, never authority for a current worker.
    pub shutdown: Value,
    pub state: InputState,
    pub reason: String,
}
impl DeferredJob {
    pub fn parse(v: &Value, current: &Identity) -> io::Result<Self> {
        keys(
            v,
            &[
                "invocation_id",
                "job_id",
                "kind",
                "generation",
                "source",
                "requested_profile",
                "payload",
                "job_snapshot",
                "job_sha256",
                "epoch",
                "target",
                "shutdown",
                "state",
                "reason",
            ],
        )?;
        let epoch = Epoch::parse(&v["epoch"], None)?;
        keys(&v["target"], &["session_id", "agent_id"])?;
        let target_session = uuid(&v["target"], "session_id")?;
        let target_agent = uuid(&v["target"], "agent_id")?;
        if epoch.identity.agent != current.agent
            || target_agent != current.agent
            || target_session != current.session
        {
            return Err(bad("Deferred Work job belongs to another target/agent"));
        }
        let kind = match text(v, "kind", 16)?.as_str() {
            "cron" => ProducerKind::Cron,
            "loop" => ProducerKind::Loop,
            _ => return Err(bad("Invalid deferred Work producer kind")),
        };
        let source = text(v, "source", 128)?;
        let requested = profile(v, "requested_profile")?;
        if source != "scheduled" {
            return Err(bad("Deferred Work job changed original source"));
        }
        let payload = Payload::evidence(&v["payload"], &source, requested, None)?;
        let job_snapshot = text(v, "job_snapshot", PAYLOAD_BYTES)?;
        // Preserve exact opaque JSON, including its original numeric spelling.
        crate::deferred_control::strict_object(&job_snapshot)?;
        let job_sha256 = hash(v, "job_sha256")?;
        if format!("{:x}", Sha256::digest(job_snapshot.as_bytes())) != job_sha256 {
            return Err(bad("Deferred Work job snapshot changed"));
        }
        let state = InputState::parse(&text(v, "state", 16)?)?;
        if !matches!(state, InputState::Held | InputState::Uncertain) {
            return Err(bad("Deferred Work job is not retained historical evidence"));
        }
        let shutdown = &v["shutdown"];
        keys(
            shutdown,
            &[
                "version",
                "instance",
                "closing_sequence",
                "final_sequence",
                "admission_closed",
                "worker_settled",
                "callback_settled",
                "process_settled",
                "producer_ids",
                "inputs",
                "admission_ids",
                "ledger_sha256",
                "effects_verified",
            ],
        )?;
        if shutdown["version"] != 1
            || shutdown["instance"] != epoch.instance
            || number(shutdown, "final_sequence", true)?
                < number(shutdown, "closing_sequence", true)?
            || shutdown["effects_verified"] != false
            || [
                "admission_closed",
                "worker_settled",
                "callback_settled",
                "process_settled",
            ]
            .iter()
            .any(|key| shutdown[*key] != true)
        {
            return Err(bad(
                "Deferred Work shutdown evidence is incomplete or foreign",
            ));
        }
        ids(shutdown, "producer_ids", 256)?;
        ids(shutdown, "admission_ids", 256)?;
        hash(shutdown, "ledger_sha256")?;
        let inputs = shutdown["inputs"]
            .as_array()
            .filter(|a| a.len() <= 256)
            .ok_or_else(|| bad("Invalid deferred Work input evidence"))?;
        let mut seen = BTreeSet::new();
        for row in inputs {
            keys(row, &["input_id", "disposition"])?;
            if !seen.insert(uuid(row, "input_id")?)
                || !matches!(
                    text(row, "disposition", 16)?.as_str(),
                    "settled" | "held" | "cancelled" | "uncertain" | "reconciled"
                )
            {
                return Err(bad("Invalid deferred Work input disposition"));
            }
        }
        Ok(Self {
            invocation_id: uuid(v, "invocation_id")?,
            job_id: text(v, "job_id", 128)?,
            kind,
            generation: number(v, "generation", true)?,
            source,
            requested,
            payload,
            job_snapshot,
            job_sha256,
            epoch,
            target_session,
            target_agent,
            shutdown: shutdown.clone(),
            state,
            reason: text(v, "reason", 1024)?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Capture {
    pub identity: Identity,
    pub instance: String,
    pub activation_token: String,
    pub provider: Provider,
    pub recovered: Vec<Recovered>,
    pub deferred_jobs: Vec<DeferredJob>,
    pub execution_receipt: Option<crate::work_execution_controls::Recovery>,
}
impl Capture {
    pub fn parse(v: &Value, expected: &Identity) -> io::Result<Self> {
        Self::parse_execution(v, expected, None)
    }
    pub fn parse_execution(
        v: &Value,
        expected: &Identity,
        execution: Option<&crate::work_execution_controls::Capabilities>,
    ) -> io::Result<Self> {
        let execution_receipt = if execution.is_some() {
            if number(v, "execution_version", true)? != 1 {
                return Err(bad("Work execution capture version changed"));
            }
            Some(crate::work_execution_controls::Recovery::parse(
                &v["execution_receipt"],
                expected,
            )?)
        } else {
            None
        };
        let mut legacy = v.clone();
        if execution.is_some() {
            legacy
                .as_object_mut()
                .ok_or_else(|| bad("Work capture is not an object"))?
                .remove("execution_version");
            legacy
                .as_object_mut()
                .expect("capture object")
                .remove("execution_receipt");
        }
        bounded(v, REPLY_BYTES)?;
        let v = &legacy;
        bounded(v, REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "instance",
                "activation_token",
                "sequence",
                "identity",
                "provider",
                "limits",
                "recovered",
                "deferred_jobs",
            ],
        )?;
        let identity = Identity::parse(&v["identity"])?;
        if v["kind"] != "work_capture"
            || v["version"] != 1
            || number(v, "sequence", false)? != 0
            || &identity != expected
        {
            return Err(bad("Work capture identity/version changed"));
        }
        let limits = &v["limits"];
        keys(
            limits,
            &[
                "producers",
                "inputs",
                "payload_bytes",
                "ledger_bytes",
                "envelope_bytes",
                "reply_bytes",
            ],
        )?;
        for (key, n) in [
            ("producers", 256),
            ("inputs", 256),
            ("payload_bytes", 65_536),
            ("ledger_bytes", 33_554_432),
            ("envelope_bytes", 131_072),
            ("reply_bytes", 1_048_576),
        ] {
            if number(limits, key, true)? != n {
                return Err(bad("Unsupported Work lifecycle capacity"));
            }
        }
        let rows = v["recovered"]
            .as_array()
            .filter(|a| a.len() <= 256)
            .ok_or_else(|| bad("Invalid recovered Work rows"))?;
        let mut recovered = Vec::new();
        let mut ps = BTreeSet::new();
        let mut ins = BTreeSet::new();
        let mut admissions = BTreeSet::new();
        for r in rows {
            keys(
                r,
                &["epoch", "producer", "input", "state", "reason", "admission"],
            )?;
            let epoch = Epoch::parse(&r["epoch"], Some(&identity))?;
            let producer = Producer::parse(&r["producer"])?;
            let input = Input::parse(&r["input"])?;
            let state = InputState::parse(&text(r, "state", 16)?)?;
            let admission = if r["admission"].is_null() {
                None
            } else {
                Some(Admission::parse(&r["admission"])?)
            };
            let committed = producer.kind == ProducerKind::ChildResult;
            if committed
                && (input.source != "child-result"
                    || admission
                        .as_ref()
                        .is_some_and(|a| a.mode != Mode::NewTurn || a.delivery.is_some()))
            {
                return Err(bad("Recovered child wake changed delivery evidence"));
            }
            if admission.as_ref().is_some_and(|a| {
                a.provider == Provider::Claude && a.delivery.is_none() && !committed
            }) {
                return Err(bad("Recovered Claude append lacks prepared delivery"));
            }
            if !matches!(state, InputState::Held | InputState::Uncertain)
                || input.producer != producer.id
                || input.session != identity.session
                || !ps.insert(producer.id.clone())
                || !ins.insert(input.id.clone())
                || admission.as_ref().is_some_and(|a| {
                    a.input != input.id
                        || a.provider != epoch.provider
                        || a.requested != input.requested
                        || a.origin.session != input.session
                        || state != InputState::Uncertain
                        || !admissions.insert(a.id.clone())
                })
            {
                return Err(bad("Contradictory recovered Work identity"));
            }
            recovered.push(Recovered {
                epoch,
                producer,
                input,
                state,
                reason: text(r, "reason", 1024)?,
                admission,
            });
        }
        let deferred_jobs = v["deferred_jobs"]
            .as_array()
            .filter(|rows| rows.len() <= 1)
            .ok_or_else(|| bad("Deferred Work job capture exceeds one retained invocation"))?
            .iter()
            .map(|row| DeferredJob::parse(row, &identity))
            .collect::<io::Result<Vec<_>>>()?;
        Ok(Self {
            identity,
            instance: hash(v, "instance")?,
            activation_token: hash(v, "activation_token")?,
            provider: Provider::parse(v, "provider")?,
            recovered,
            deferred_jobs,
            execution_receipt,
        })
    }
    pub fn activation_request_execution(
        &self,
        scheduled: Option<&crate::scheduled_transition::Capabilities>,
        _: &crate::work_execution_controls::Capabilities,
    ) -> Value {
        let mut v = if let Some(cap) = scheduled {
            self.activation_request_scheduled(cap)
        } else {
            self.activation_request()
        };
        v["execution_version"] = json!(1);
        v
    }
    pub fn activation_request_scheduled(
        &self,
        _: &crate::scheduled_transition::Capabilities,
    ) -> Value {
        let mut v = self.activation_request();
        v["scheduled_transition_version"] = json!(1);
        v
    }
    pub fn activation_request(&self) -> Value {
        let mut v = self.identity.request(ACTIVATE);
        v["instance"] = json!(self.instance);
        v["activation_token"] = json!(self.activation_token);
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CurrentExecution {
    pub origin: Origin,
    pub action_id: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StorageStatus {
    Verified,
    Failed(String),
    NotApplicable,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageProof {
    pub task: StorageStatus,
    pub output: StorageStatus,
}
fn storage(v: &Value) -> io::Result<StorageStatus> {
    keys(v, &["status", "reason"])?;
    let reason = optional(v, "reason", 1024)?;
    match (text(v, "status", 32)?.as_str(), reason) {
        ("verified", None) => Ok(StorageStatus::Verified),
        ("not_applicable", None) => Ok(StorageStatus::NotApplicable),
        ("failed", Some(r)) => Ok(StorageStatus::Failed(r)),
        _ => Err(bad("Contradictory Work persistence receipt")),
    }
}
fn producer_proof(v: &Value) -> io::Result<StorageProof> {
    keys(
        v,
        &[
            "worker_retired",
            "callback_retired",
            "process_retired",
            "persistence",
        ],
    )?;
    if ["worker_retired", "callback_retired", "process_retired"]
        .iter()
        .any(|k| v[*k] != true)
    {
        return Err(bad("Work producer retirement remains uncertain"));
    }
    keys(&v["persistence"], &["task", "output"])?;
    Ok(StorageProof {
        task: storage(&v["persistence"]["task"])?,
        output: storage(&v["persistence"]["output"])?,
    })
}
/// Positive provider-history evidence resolving an existing durable input.
/// This receipt never authorizes a new append, replay, or execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reconciliation {
    pub operation: String,
    pub provider: Provider,
    pub delivery: String,
    pub thread: String,
    pub turn: String,
    pub provider_history_sha256: String,
    pub journal_instance: String,
    pub journal_sha256: String,
    pub payload_sha256: String,
}
impl Reconciliation {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "operation_id",
                "provider",
                "delivery_id",
                "thread_id",
                "turn_id",
                "provider_history_sha256",
                "journal_instance",
                "journal_sha256",
                "payload_sha256",
            ],
        )?;
        let provider = Provider::parse(v, "provider")?;
        if provider != Provider::Codex {
            return Err(bad("Unsupported Work reconciliation provider"));
        }
        Ok(Self {
            operation: text(v, "operation_id", 128)?,
            provider,
            delivery: text(v, "delivery_id", 128)?,
            thread: text(v, "thread_id", 128)?,
            turn: text(v, "turn_id", 128)?,
            provider_history_sha256: hash(v, "provider_history_sha256")?,
            journal_instance: hash(v, "journal_instance")?,
            journal_sha256: hash(v, "journal_sha256")?,
            payload_sha256: hash(v, "payload_sha256")?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Effect {
    ScheduledProposal {
        proposal: Box<crate::scheduled_transition::Proposal>,
    },
    Producer {
        producer: Producer,
        state: ProducerState,
        worker: Option<String>,
        storage: Option<StorageProof>,
    },
    Input {
        input: Input,
        state: InputState,
        reason: Option<String>,
    },
    Admission {
        payload: Payload,
        input: Input,
        admission: Admission,
        drain_only: bool,
    },
    Settled {
        input: Input,
        admission: Admission,
        outcome: String,
    },
    Reconciled {
        input: Input,
        reconciliation: Reconciliation,
        recovered: bool,
    },
    Closing,
}
#[derive(Clone)]
struct ProducerRecord {
    producer: Producer,
    state: ProducerState,
    worker: Option<String>,
    input: Option<String>,
}
#[derive(Clone)]
struct InputRecord {
    input: Input,
    state: InputState,
    recovered: bool,
    admission: Option<Admission>,
    settled: Option<Value>,
    epoch_instance: String,
    reconciled: Option<Value>,
}
#[derive(Clone)]
struct Cutoff {
    sequence: u64,
    producers: BTreeSet<String>,
    inputs: BTreeSet<String>,
    admissions: BTreeSet<String>,
    late_inputs: BTreeSet<String>,
}
#[derive(Clone)]
pub struct Ledger {
    capture: Capture,
    seat: Profile,
    active: bool,
    scheduled_requested: bool,
    scheduled_ids: BTreeSet<String>,
    scheduled_tokens: BTreeSet<String>,
    scheduled_actions: BTreeSet<String>,
    failed: bool,
    local_closing: bool,
    shutdown: bool,
    shutdown_proof: Option<Value>,
    sequence: u64,
    producers: BTreeMap<String, ProducerRecord>,
    inputs: BTreeMap<String, InputRecord>,
    admissions: BTreeSet<String>,
    executions: BTreeSet<String>,
    operations: BTreeSet<String>,
    boundaries: BTreeMap<String, u64>,
    events: BTreeSet<String>,
    cutoff: Option<Cutoff>,
}
impl Ledger {
    pub fn new(capture: Capture, seat: Profile) -> Self {
        let mut producers = BTreeMap::new();
        let mut inputs = BTreeMap::new();
        let mut admissions = BTreeSet::new();
        let mut operations = BTreeSet::new();
        let mut executions = BTreeSet::new();
        for r in &capture.recovered {
            producers.insert(
                r.producer.id.clone(),
                ProducerRecord {
                    producer: r.producer.clone(),
                    state: ProducerState::Recovered,
                    worker: None,
                    input: Some(r.input.id.clone()),
                },
            );
            if let Some(a) = &r.admission {
                admissions.insert(a.id.clone());
                operations.insert(a.operation.clone());
                executions.insert(a.origin.execution.clone());
            }
            inputs.insert(
                r.input.id.clone(),
                InputRecord {
                    input: r.input.clone(),
                    state: r.state,
                    recovered: true,
                    admission: r.admission.clone(),
                    settled: None,
                    epoch_instance: r.epoch.instance.clone(),
                    reconciled: None,
                },
            );
        }
        Self {
            capture,
            seat,
            active: false,
            scheduled_requested: false,
            scheduled_ids: BTreeSet::new(),
            scheduled_tokens: BTreeSet::new(),
            scheduled_actions: BTreeSet::new(),
            failed: false,
            local_closing: false,
            shutdown: false,
            shutdown_proof: None,
            sequence: 0,
            producers,
            inputs,
            admissions,
            executions,
            operations,
            boundaries: BTreeMap::new(),
            events: BTreeSet::new(),
            cutoff: None,
        }
    }
    pub fn capture(&self) -> &Capture {
        &self.capture
    }
    /// Compare immutable launch references with genuine events already consumed by this ledger.
    pub fn verify_execution_document(
        &self,
        d: &crate::work_execution_controls::Document,
    ) -> io::Result<()> {
        if self.failed
            || d.identity != self.capture.identity
            || d.work_instance != self.capture.instance
            || d.provider != self.capture.provider
        {
            return Err(bad(
                "Execution receipt belongs to a different or failed Work epoch",
            ));
        }
        let p = self
            .producers
            .get(&d.producer.id)
            .ok_or_else(|| bad("Execution producer has not been observed"))?;
        let i = self
            .inputs
            .get(&d.input.id)
            .ok_or_else(|| bad("Execution input has not been observed"))?;
        if p.producer != d.producer
            || p.input.as_deref() != Some(d.input.id.as_str())
            || i.input != d.input
            || i.recovered
            || i.epoch_instance != d.work_instance
        {
            return Err(bad("Execution receipt changed observed Work references"));
        }
        Payload::parse(&d.wire["payload"], &i.input)?;
        Ok(())
    }
    /// Return only the genuine admission already observed for this exact immutable launch input.
    pub fn execution_admission(
        &self,
        document: &crate::work_execution_controls::Document,
    ) -> io::Result<Option<Admission>> {
        self.verify_execution_document(document)?;
        Ok(self.inputs[&document.input.id].admission.clone())
    }
    /// A sampled lifecycle may lag newer genuine events; it cannot invent ownership or regress a terminal proof.
    pub fn verify_execution_lifecycle(
        &self,
        d: &crate::work_execution_controls::Document,
        l: &crate::work_execution_controls::Lifecycle,
    ) -> io::Result<()> {
        self.verify_execution_document(d)?;
        let p = &self.producers[&d.producer.id];
        let i = &self.inputs[&d.input.id];
        let producer_ok = match l.producer_phase {
            Some(ProducerState::Registered) => p.state != ProducerState::Recovered,
            Some(ProducerState::Running) => matches!(
                p.state,
                ProducerState::Running | ProducerState::StopRequested | ProducerState::Retired
            ),
            Some(ProducerState::StopRequested) => matches!(
                p.state,
                ProducerState::StopRequested | ProducerState::Retired
            ),
            Some(ProducerState::Retired) => p.state == ProducerState::Retired,
            _ => false,
        };
        let input_ok = match l.input_state {
            Some(InputState::Queued) => true,
            Some(InputState::Held | InputState::Claiming) => i.state != InputState::Queued,
            Some(InputState::Admitted) => matches!(
                i.state,
                InputState::Admitted | InputState::Uncertain | InputState::Settled
            ),
            Some(InputState::Uncertain) => matches!(
                i.state,
                InputState::Uncertain | InputState::Settled | InputState::Reconciled
            ),
            Some(InputState::Cancelled) => i.state == InputState::Cancelled,
            Some(InputState::Settled) => i.state == InputState::Settled,
            Some(InputState::Reconciled) => i.state == InputState::Reconciled,
            None => false,
        };
        if !producer_ok
            || !input_ok
            || l.admission
                .as_ref()
                .is_some_and(|a| i.admission.as_ref() != Some(a))
            || l.settlement
                .as_ref()
                .is_some_and(|s| i.settled.as_ref() != Some(s))
        {
            return Err(bad(
                "Execution lifecycle invents or contradicts observed Work progress",
            ));
        }
        Ok(())
    }
    /// Cross-check execution Quit evidence against the independently verified Work close cutoff.
    pub fn verify_execution_shutdown(
        &self,
        e: &crate::work_execution_controls::Shutdown,
        review: Option<&crate::work_execution_controls::Review>,
    ) -> io::Result<()> {
        let proof = self
            .shutdown_proof
            .as_ref()
            .filter(|_| self.shutdown && !self.failed)
            .ok_or_else(|| bad("Execution shutdown requires verified Work shutdown first"))?;
        if e.identity != self.capture.identity || e.work_instance != self.capture.instance {
            return Err(bad("Execution shutdown changed observed Work epoch"));
        }
        let input = optional_uuid(&e.wire, "input_id")?;
        let admission = optional_uuid(&e.wire, "admission_id")?;
        let Some(r) = review else {
            if input.is_some() || admission.is_some() || e.wire["disposition"] != "idle" {
                return Err(bad("Execution shutdown has no retained review owner"));
            }
            return Ok(());
        };
        if r.identity != self.capture.identity
            || r.work_instance != self.capture.instance
            || r.provider != self.capture.provider
            || e.wire["action_id"] != r.action_id
        {
            return Err(bad(
                "Execution shutdown review belongs to another Work epoch",
            ));
        }
        let kind = match r.action {
            crate::work_execution_controls::Action::JobRun if r.edit.section == "loops" => {
                ProducerKind::Loop
            }
            crate::work_execution_controls::Action::JobRun => ProducerKind::Cron,
            _ => ProducerKind::Goal,
        };
        let matches_producer = |p: &ProducerRecord| {
            p.producer.kind == kind
                && p.producer.object == r.target_id
                && p.producer.generation == r.producer_generation
                && p.producer.session == r.identity.session
        };
        let matches_input = |i: &InputRecord| {
            !i.recovered
                && i.epoch_instance == r.work_instance
                && i.input.session == r.identity.session
                && i.input.source == r.payload.source
                && i.input.requested == r.payload.profile
                && r.wire["payload_sha256"] == i.input.payload_sha256
                && self
                    .producers
                    .get(&i.input.producer)
                    .is_some_and(matches_producer)
        };
        if let Some(id) = input {
            let i = self
                .inputs
                .get(&id)
                .filter(|i| matches_input(i))
                .ok_or_else(|| {
                    bad("Execution shutdown input does not match reviewed generation/payload")
                })?;
            if i.admission.as_ref().map(|a| a.id.as_str()) != admission.as_deref() {
                return Err(bad(
                    "Execution shutdown omitted or changed actual Work admission",
                ));
            }
            let cutoff = self.cutoff.as_ref().expect("verified cutoff");
            let producer = &self.producers[&i.input.producer];
            if producer.state != ProducerState::Retired
                && !cutoff.producers.contains(&i.input.producer)
            {
                return Err(bad(
                    "Execution shutdown lacks producer retirement/cutoff proof",
                ));
            }
            // Already-settled records intentionally leave the active shutdown cutoff.
            // Their exact prior Work terminal event supplies the independent proof instead.
            let disposition = match i.state {
                InputState::Settled if i.settled.is_some() => "settled",
                InputState::Reconciled if i.reconciled.is_some() => "reconciled",
                InputState::Cancelled if producer.state == ProducerState::Retired => "cancelled",
                _ => {
                    if !cutoff.inputs.contains(&id) && !cutoff.late_inputs.contains(&id)
                        || admission
                            .as_ref()
                            .is_some_and(|a| !cutoff.admissions.contains(a))
                    {
                        return Err(bad(
                            "Execution shutdown refs are outside the verified Work cutoff",
                        ));
                    }
                    proof["inputs"]
                        .as_array()
                        .expect("verified shutdown inputs")
                        .iter()
                        .find(|v| v["input_id"] == id)
                        .and_then(|row| row["disposition"].as_str())
                        .ok_or_else(|| bad("Execution shutdown omitted its input disposition"))?
                }
            };
            match e.wire["disposition"].as_str() {
                Some("admitted_settled") if disposition != "settled" => {
                    return Err(bad(
                        "Execution shutdown overstated uncertain admission as settled",
                    ));
                }
                Some("cancelled_before_admission") if disposition != "cancelled" => {
                    return Err(bad("Execution shutdown invented cancellation"));
                }
                _ => {}
            }
        } else if admission.is_some() {
            return Err(bad("Execution shutdown admission lacks its exact input"));
        }
        // Null refs are attested by the exact invocation proof. Matching a job,
        // generation and payload cannot attribute an unrelated automatic firing.
        Ok(())
    }
    pub fn sequence(&self) -> u64 {
        self.sequence
    }
    pub fn failed(&self) -> bool {
        self.failed
    }
    pub fn begin_close(&mut self) {
        self.local_closing = true;
    }
    pub fn request_scheduled_transitions(
        &mut self,
        _: &crate::scheduled_transition::Capabilities,
    ) -> io::Result<()> {
        if self.active || self.failed || self.local_closing || self.scheduled_requested {
            return Err(bad(
                "Scheduled transitions must be requested once before activation",
            ));
        }
        self.scheduled_requested = true;
        Ok(())
    }
    pub fn scheduled_transitions_active(&self) -> bool {
        self.active && self.scheduled_requested && !self.failed && !self.local_closing
    }
    pub fn activate(&mut self, v: &Value) -> io::Result<()> {
        let result = (|| {
            bounded(v, REPLY_BYTES)?;
            let mut fields = vec!["kind", "version", "instance", "sequence", "identity"];
            if self.scheduled_requested {
                fields.push("scheduled_transition_version");
            }
            if self.capture.execution_receipt.is_some() {
                fields.push("execution_version");
            }
            keys(v, &fields)?;
            if self.capture.execution_receipt.is_some()
                && number(v, "execution_version", true)? != 1
            {
                return Err(bad("Work execution activation echo changed"));
            }
            if self.scheduled_requested && number(v, "scheduled_transition_version", true)? != 1 {
                return Err(bad("Scheduled transition activation echo changed"));
            }
            if self.active
                || self.failed
                || self.local_closing
                || v["kind"] != "work_active"
                || v["version"] != 1
                || v["instance"] != self.capture.instance
                || number(v, "sequence", false)? != 0
                || Identity::parse(&v["identity"])? != self.capture.identity
            {
                return Err(bad("Work activation changed or repeated"));
            }
            Ok(())
        })();
        if result.is_err() {
            self.failed = true;
        } else {
            self.active = true;
        }
        result
    }
    /// Validate against a copy, so a rejected event cannot partly advance IDs.
    pub fn observe(
        &mut self,
        envelope: &Value,
        current: Option<&CurrentExecution>,
    ) -> io::Result<Effect> {
        let mut next = self.clone();
        match next.observe_inner(envelope, current) {
            Ok(effect) => {
                *self = next;
                Ok(effect)
            }
            Err(e) => {
                self.failed = true;
                Err(e)
            }
        }
    }
    fn observe_inner(
        &mut self,
        envelope: &Value,
        current: Option<&CurrentExecution>,
    ) -> io::Result<Effect> {
        bounded(envelope, ENVELOPE_BYTES)?;
        keys(
            envelope,
            &[
                "type",
                "event_id",
                "session_id",
                "operation_id",
                "hook_id",
                "source",
                "execution_id",
                "event",
            ],
        )?;
        if !self.active || self.failed || self.shutdown {
            return Err(bad("Work lifecycle is not active"));
        }
        let event = &envelope["event"];
        let kind = text(event, "type", 32)?;
        if !EVENTS.contains(&kind.as_str())
            || event["version"] != 1
            || event["instance"] != self.capture.instance
            || Identity::parse(&event["identity"])? != self.capture.identity
            || number(event, "sequence", true)?
                != self
                    .sequence
                    .checked_add(1)
                    .ok_or_else(|| bad("Work sequence exhausted"))?
        {
            return Err(bad("Work event identity/order changed"));
        }
        let event_id = crate::stream::required_uuid4(envelope, "event_id")
            .map_err(|_| bad("Work emission lacks event UUID"))?;
        if envelope["type"] != "gui.event"
            || self.events.len() >= 8192
            || !self.events.insert(event_id.to_owned())
        {
            return Err(bad("Repeated or excessive Work emission"));
        }
        let common = ["type", "version", "instance", "sequence", "identity"];
        let extra: &[&str] = match kind.as_str() {
            "work_producer" => &[
                "phase",
                "producer",
                "worker_id",
                "task_state",
                "reason",
                "proof",
            ],
            "work_transition_proposed" => &["scheduled_transition_version", "proposal"],
            "work_input" => &["phase", "input", "reason"],
            "work_admission" => &["status", "input", "admission", "payload"],
            "work_settled" => &[
                "input",
                "admission",
                "outcome",
                "proof",
                "history_sha256",
                "ledger_sha256",
            ],
            "work_reconciled" => &[
                "input",
                "reconciliation",
                "proof",
                "history_sha256",
                "ledger_sha256",
            ],
            "work_closing" => &[
                "cutoff_sequence",
                "producer_ids",
                "input_ids",
                "admission_ids",
            ],
            _ => unreachable!(),
        };
        keys(
            event,
            &common
                .into_iter()
                .chain(extra.iter().copied())
                .collect::<Vec<_>>(),
        )?;
        let effect = match kind.as_str() {
            "work_transition_proposed" => {
                if !self.scheduled_requested
                    || self.cutoff.is_some()
                    || number(event, "scheduled_transition_version", true)? != 1
                {
                    return Err(bad("Unnegotiated or source-closed scheduled proposal"));
                }
                let proposal = crate::scheduled_transition::Proposal::parse(
                    &event["proposal"],
                    &self.capture.identity,
                    &self.capture.instance,
                    self.capture.provider,
                )?;
                if !self.scheduled_ids.insert(proposal.id.clone())
                    || !self.scheduled_tokens.insert(proposal.token.clone())
                    || !self
                        .scheduled_actions
                        .insert(proposal.transition.action_id.clone())
                {
                    return Err(bad(
                        "Scheduled proposal reused an identity or review action",
                    ));
                }
                Effect::ScheduledProposal {
                    proposal: Box::new(proposal),
                }
            }
            "work_producer" => self.producer_event(event)?,
            "work_input" => self.input_event(event)?,
            "work_admission" => self.admission_event(event, current)?,
            "work_settled" => self.settled_event(event)?,
            "work_reconciled" => self.reconciled_event(event)?,
            "work_closing" => self.closing_event(event)?,
            _ => unreachable!(),
        };
        let expected = match &effect {
            Effect::Admission { admission, .. } | Effect::Settled { admission, .. } => {
                admission.origin.value()
            }
            Effect::Reconciled { reconciliation, .. } => {
                json!({"session_id":self.capture.identity.session,"operation_id":reconciliation.operation,"hook_id":null,"source":"rpc","execution_id":null})
            }
            _ => {
                json!({"session_id":self.capture.identity.session,"operation_id":null,"hook_id":null,"source":"work","execution_id":null})
            }
        };
        for key in [
            "session_id",
            "operation_id",
            "hook_id",
            "source",
            "execution_id",
        ] {
            if envelope.get(key) != expected.get(key) {
                return Err(bad("Work envelope does not match captured origin"));
            }
        }
        self.sequence += 1;
        Ok(effect)
    }
    fn producer_event(&mut self, e: &Value) -> io::Result<Effect> {
        let p = Producer::parse(&e["producer"])?;
        let state = match text(e, "phase", 32)?.as_str() {
            "registered" => ProducerState::Registered,
            "running" => ProducerState::Running,
            "stop_requested" => ProducerState::StopRequested,
            "retired" => ProducerState::Retired,
            _ => return Err(bad("Invalid producer phase")),
        };
        let worker = optional_uuid(e, "worker_id")?;
        let reason = optional(e, "reason", 1024)?;
        let task_state = optional(e, "task_state", 32)?;
        if task_state.as_ref().is_some_and(|s| {
            !matches!(
                s.as_str(),
                "running" | "done" | "failed" | "killed" | "lost"
            )
        }) || p.kind != ProducerKind::HostTask && task_state.is_some()
        {
            return Err(bad("Invalid canonical task state"));
        }
        let storage = if state == ProducerState::Retired {
            Some(producer_proof(&e["proof"])?)
        } else {
            if !e["proof"].is_null() {
                return Err(bad("Premature Work retirement proof"));
            }
            None
        };
        if state == ProducerState::Running
            && worker.as_ref().is_some_and(|id| {
                self.producers
                    .values()
                    .any(|prior| prior.worker.as_ref() == Some(id))
            })
        {
            return Err(bad("Work worker already belongs to another producer"));
        }
        if state == ProducerState::Registered {
            let active = self
                .producers
                .values()
                .filter(|r| !matches!(r.state, ProducerState::Retired | ProducerState::Recovered))
                .count();
            let pending = self.inputs.values().filter(|r| !r.state.terminal()).count();
            if self.cutoff.is_some()
                || worker.is_some()
                || reason.is_some()
                || self.producers.contains_key(&p.id)
                || active >= 256
                || active.saturating_add(pending) >= 256
                || self.producers.values().any(|r| {
                    r.producer.kind == p.kind
                        && r.producer.object == p.object
                        && r.producer.owner == p.owner
                        && r.producer.generation >= p.generation
                })
            {
                return Err(bad("Invalid or duplicate producer registration"));
            }
            self.producers.insert(
                p.id.clone(),
                ProducerRecord {
                    producer: p.clone(),
                    state,
                    worker: None,
                    input: None,
                },
            );
        } else {
            let prior = self
                .producers
                .get_mut(&p.id)
                .ok_or_else(|| bad("Unknown Work producer"))?;
            if prior.producer != p
                || matches!(
                    prior.state,
                    ProducerState::Retired | ProducerState::Recovered
                )
            {
                return Err(bad("Producer identity changed or already retired"));
            }
            match state {
                ProducerState::Running
                    if prior.state == ProducerState::Registered
                        && worker.is_some()
                        && reason.is_none()
                        && self.cutoff.is_none() => {}
                ProducerState::StopRequested
                    if matches!(
                        prior.state,
                        ProducerState::Registered | ProducerState::Running
                    ) && worker == prior.worker => {}
                ProducerState::Retired
                    if worker == prior.worker
                        && (worker.is_some() || prior.input.is_none() && reason.is_some()) => {}
                _ => return Err(bad("Invalid producer lifetime transition")),
            }
            prior.state = state;
            prior.worker = worker.clone();
        }
        Ok(Effect::Producer {
            producer: p,
            state,
            worker,
            storage,
        })
    }
    fn input_event(&mut self, e: &Value) -> io::Result<Effect> {
        let input = Input::parse(&e["input"])?;
        let state = InputState::parse(&text(e, "phase", 16)?)?;
        let reason = optional(e, "reason", 1024)?;
        if input.session != self.capture.identity.session
            || matches!(state, InputState::Queued | InputState::Claiming) == reason.is_some()
        {
            return Err(bad("Invalid Work input session/reason"));
        }
        let p = self
            .producers
            .get_mut(&input.producer)
            .ok_or_else(|| bad("Input producer is unknown"))?;
        if let Some(prior) = self.inputs.get_mut(&input.id) {
            if prior.input != input
                || prior.recovered
                || p.input.as_deref() != Some(input.id.as_str())
            {
                return Err(bad("Input identity changed or recovered input replayed"));
            }
            let allowed = matches!(
                (prior.state, state),
                (
                    InputState::Queued,
                    InputState::Claiming | InputState::Held | InputState::Cancelled
                ) | (
                    InputState::Held,
                    InputState::Claiming | InputState::Cancelled
                ) | (
                    InputState::Claiming,
                    InputState::Held | InputState::Uncertain
                ) | (InputState::Admitted, InputState::Uncertain)
            );
            if !allowed || self.cutoff.is_some() && state == InputState::Claiming {
                return Err(bad("Invalid/uncertain Work input transition"));
            }
            prior.state = state;
        } else {
            if !matches!(state, InputState::Queued | InputState::Held)
                || p.input.is_some()
                || matches!(p.state, ProducerState::Retired | ProducerState::Recovered)
                || self.inputs.values().filter(|r| !r.state.terminal()).count() >= 256
            {
                return Err(bad("Input duplicate or missing live producer"));
            }
            if let Some(cutoff) = self.cutoff.as_mut() {
                if state != InputState::Held || !cutoff.producers.contains(&input.producer) {
                    return Err(bad("Late input is not held for a closing producer"));
                }
                cutoff.late_inputs.insert(input.id.clone());
            }
            p.input = Some(input.id.clone());
            self.inputs.insert(
                input.id.clone(),
                InputRecord {
                    input: input.clone(),
                    state,
                    recovered: false,
                    admission: None,
                    settled: None,
                    epoch_instance: self.capture.instance.clone(),
                    reconciled: None,
                },
            );
        }
        Ok(Effect::Input {
            input,
            state,
            reason,
        })
    }
    fn admission_event(
        &mut self,
        e: &Value,
        current: Option<&CurrentExecution>,
    ) -> io::Result<Effect> {
        let input = Input::parse(&e["input"])?;
        let payload = Payload::parse(&e["payload"], &input)?;
        let a = Admission::parse(&e["admission"])?;
        let record = self
            .inputs
            .get_mut(&input.id)
            .ok_or_else(|| bad("Admission input is unknown"))?;
        if self.cutoff.is_some()
            || e["status"] != "accepted"
            || record.input != input
            || record.state != InputState::Claiming
            || record.recovered
            || record.admission.is_some()
            || a.input != input.id
            || a.origin.session != input.session
            || a.requested != input.requested
            || a.seat != self.seat
            || a.provider != self.capture.provider
            || !self.admissions.insert(a.id.clone())
        {
            return Err(bad("Work admission changed identity, state, or authority"));
        }
        let p = &self
            .producers
            .get(&input.producer)
            .expect("input producer")
            .producer;
        let committed = matches!(payload.delivery, Delivery::CommittedHistory(_));
        if committed != (p.kind == ProducerKind::ChildResult)
            || committed
                && (a.mode != Mode::NewTurn
                    || a.delivery.is_some()
                    || a.provider == Provider::Codex
                    || p.action.is_some()
                    || input.source != "child-result")
            || a.provider == Provider::Claude && a.delivery.is_none() && !committed
        {
            return Err(bad(
                "Work payload lacks exact producer/provider delivery authority",
            ));
        }
        match a.mode {
            Mode::NewTurn => {
                if current.is_some()
                    || a.origin.source != input.source
                    || p.operation.as_deref() == Some(a.operation.as_str())
                    || !self.operations.insert(a.operation.clone())
                    || !self.executions.insert(a.origin.execution.clone())
                {
                    return Err(bad("New Work turn overlaps or changes origin"));
                }
            }
            Mode::InTurn => {
                let current =
                    current.ok_or_else(|| bad("In-turn Work input has no active execution"))?;
                let boundary = self
                    .boundaries
                    .entry(a.origin.execution.clone())
                    .or_default();
                if current.origin != a.origin
                    || a.boundary
                        != boundary
                            .checked_add(1)
                            .ok_or_else(|| bad("Work boundary exhausted"))?
                {
                    return Err(bad("In-turn Work admission changed parent or order"));
                }
                *boundary = a.boundary;
                self.operations.insert(a.operation.clone());
            }
            Mode::ActionSegment => {
                let current =
                    current.ok_or_else(|| bad("Work action segment lacks predecessor"))?;
                if current.action_id.is_none()
                    || current.action_id != p.action
                    || p.operation.as_deref() != Some(a.operation.as_str())
                    || current.origin.operation != a.operation
                    || a.parent.as_deref() != Some(current.origin.execution.as_str())
                    || a.origin.source != "rpc"
                    || !self.executions.insert(a.origin.execution.clone())
                {
                    return Err(bad("Work segment lacks exact action/producer binding"));
                }
                self.operations.insert(a.operation.clone());
            }
        }
        record.state = InputState::Admitted;
        record.admission = Some(a.clone());
        Ok(Effect::Admission {
            payload,
            input,
            admission: a,
            drain_only: self.local_closing,
        })
    }
    fn settled_event(&mut self, e: &Value) -> io::Result<Effect> {
        let input = Input::parse(&e["input"])?;
        let a = Admission::parse(&e["admission"])?;
        let record = self
            .inputs
            .get_mut(&input.id)
            .ok_or_else(|| bad("Settled Work input is unknown"))?;
        let outcome = text(e, "outcome", 16)?;
        if !matches!(
            outcome.as_str(),
            "completed" | "cancelled" | "failed" | "deferred"
        ) || record.input != input
            || record.recovered
            || !matches!(record.state, InputState::Admitted | InputState::Uncertain)
            || record.admission.as_ref() != Some(&a)
            || record.settled.is_some()
        {
            return Err(bad("Work settlement changed admission or repeated"));
        }
        proof(
            &e["proof"],
            &[
                "worker_retired",
                "callbacks_retired",
                "streams_retired",
                "input_durable",
            ],
        )?;
        hash(e, "history_sha256")?;
        hash(e, "ledger_sha256")?;
        record.state = InputState::Settled;
        record.settled = Some(e.clone());
        Ok(Effect::Settled {
            input,
            admission: a,
            outcome,
        })
    }
    fn reconciled_event(&mut self, e: &Value) -> io::Result<Effect> {
        let input = Input::parse(&e["input"])?;
        let reconciliation = Reconciliation::parse(&e["reconciliation"])?;
        let record = self
            .inputs
            .get_mut(&input.id)
            .ok_or_else(|| bad("Reconciled Work input is unknown"))?;
        let producer = self
            .producers
            .get(&input.producer)
            .ok_or_else(|| bad("Reconciled Work producer is unknown"))?;
        if self.cutoff.is_some()
            || record.input != input
            || !matches!(record.state, InputState::Held | InputState::Uncertain)
            || record.admission.is_some()
            || record.reconciled.is_some()
            || reconciliation.journal_instance != record.epoch_instance
            || reconciliation.payload_sha256 != input.payload_sha256
            || producer.input.as_deref() != Some(input.id.as_str())
            || producer.state
                != if record.recovered {
                    ProducerState::Recovered
                } else {
                    ProducerState::Retired
                }
            || !self.operations.insert(reconciliation.operation.clone())
        {
            return Err(bad(
                "Work reconciliation changed retained input, epoch, or retirement",
            ));
        }
        proof(
            &e["proof"],
            &[
                "current_workers_retired",
                "current_callbacks_retired",
                "input_durable",
                "no_replay",
            ],
        )?;
        hash(e, "history_sha256")?;
        hash(e, "ledger_sha256")?;
        record.state = InputState::Reconciled;
        record.reconciled = Some(e.clone());
        Ok(Effect::Reconciled {
            input,
            reconciliation,
            recovered: record.recovered,
        })
    }
    fn closing_event(&mut self, e: &Value) -> io::Result<Effect> {
        let sequence = number(e, "cutoff_sequence", true)?;
        let producers = ids(e, "producer_ids", 256)?;
        let inputs = ids(e, "input_ids", 256)?;
        let admissions = ids(e, "admission_ids", 256)?;
        let expected_producers = self
            .producers
            .iter()
            .filter(|(_, p)| !matches!(p.state, ProducerState::Retired | ProducerState::Recovered))
            .map(|(id, _)| id.clone())
            .collect();
        let expected_inputs = self
            .inputs
            .iter()
            .filter(|(_, r)| !r.state.terminal())
            .map(|(id, _)| id.clone())
            .collect();
        let expected_admissions = self
            .inputs
            .values()
            .filter(|r| r.admission.is_some() && r.settled.is_none() && !r.recovered)
            .filter_map(|r| r.admission.as_ref().map(|a| a.id.clone()))
            .collect();
        if self.cutoff.is_some()
            || sequence != self.sequence + 1
            || producers != expected_producers
            || inputs != expected_inputs
            || admissions != expected_admissions
        {
            return Err(bad(
                "Work close barrier omitted or invented outstanding identity",
            ));
        }
        self.cutoff = Some(Cutoff {
            sequence,
            producers,
            inputs,
            admissions,
            late_inputs: BTreeSet::new(),
        });
        Ok(Effect::Closing)
    }
    pub fn settled_request(&self, admission: &str) -> io::Result<Value> {
        if self.failed
            || self.local_closing
            || self.cutoff.is_some()
            || !self
                .inputs
                .values()
                .any(|r| !r.recovered && r.admission.as_ref().is_some_and(|a| a.id == admission))
        {
            return Err(bad("Cannot request this Work settlement"));
        }
        let mut v = self.capture.identity.request(SETTLED);
        v["instance"] = json!(self.capture.instance);
        v["admission_id"] = json!(admission);
        Ok(v)
    }
    pub fn verify_settled(&self, v: &Value) -> io::Result<()> {
        bounded(v, REPLY_BYTES)?;
        if self.failed || !self.inputs.values().any(|r| r.settled.as_ref() == Some(v)) {
            return Err(bad("Work settlement response has no exact emitted receipt"));
        }
        Ok(())
    }
    /// The RPC result echoes the already observed event; it is never reapplied.
    pub fn verify_reconciled(&self, v: &Value) -> io::Result<()> {
        bounded(v, REPLY_BYTES)?;
        if self.failed
            || !self
                .inputs
                .values()
                .any(|r| r.reconciled.as_ref() == Some(v))
        {
            return Err(bad(
                "Work reconciliation response has no exact emitted receipt",
            ));
        }
        Ok(())
    }
    pub fn verify_shutdown(&mut self, v: &Value) -> io::Result<()> {
        let result = self.shutdown_inner(v);
        if result.is_err() {
            self.failed = true;
        } else {
            self.shutdown = true;
            self.shutdown_proof = Some(v.clone());
        }
        result
    }
    fn shutdown_inner(&self, v: &Value) -> io::Result<()> {
        bounded(v, REPLY_BYTES)?;
        keys(
            v,
            &[
                "version",
                "instance",
                "closing_sequence",
                "final_sequence",
                "admission_closed",
                "worker_settled",
                "callback_settled",
                "process_settled",
                "producer_ids",
                "inputs",
                "admission_ids",
                "ledger_sha256",
                "effects_verified",
            ],
        )?;
        let cutoff = self
            .cutoff
            .as_ref()
            .ok_or_else(|| bad("Missing Work source close barrier"))?;
        if self.failed
            || self.shutdown
            || !self.local_closing
            || v["version"] != 1
            || v["instance"] != self.capture.instance
            || number(v, "closing_sequence", true)? != cutoff.sequence
            || number(v, "final_sequence", true)? != self.sequence
            || [
                "admission_closed",
                "worker_settled",
                "callback_settled",
                "process_settled",
            ]
            .iter()
            .any(|k| v[*k] != true)
            || v["effects_verified"] != false
            || ids(v, "producer_ids", 256)? != cutoff.producers
            || ids(v, "admission_ids", 256)? != cutoff.admissions
        {
            return Err(bad("Work shutdown lacks exact lifetime proof"));
        }
        hash(v, "ledger_sha256")?;
        let expected = cutoff
            .inputs
            .union(&cutoff.late_inputs)
            .cloned()
            .collect::<BTreeSet<_>>();
        let rows = v["inputs"]
            .as_array()
            .filter(|rows| rows.len() <= 256)
            .ok_or_else(|| bad("Invalid Work shutdown inputs"))?;
        let mut seen = BTreeSet::new();
        for row in rows {
            keys(row, &["input_id", "disposition"])?;
            let id = uuid(row, "input_id")?;
            if !seen.insert(id.clone()) {
                return Err(bad("Duplicate shutdown input"));
            }
            let record = self
                .inputs
                .get(&id)
                .ok_or_else(|| bad("Unknown shutdown input"))?;
            let disposition = text(row, "disposition", 16)?;
            let valid = match record.state {
                InputState::Settled => disposition == "settled",
                InputState::Reconciled => disposition == "reconciled",
                InputState::Cancelled => disposition == "cancelled",
                InputState::Uncertain => disposition == "uncertain",
                InputState::Admitted => matches!(disposition.as_str(), "settled" | "uncertain"),
                InputState::Queued | InputState::Held | InputState::Claiming => {
                    matches!(disposition.as_str(), "held" | "cancelled" | "uncertain")
                }
            };
            if !valid {
                return Err(bad(
                    "Shutdown input disposition contradicts observed admission",
                ));
            }
        }
        if seen != expected {
            return Err(bad("Shutdown omitted retained Work input"));
        }
        Ok(())
    }
}

/// Validate a sampled genuine settlement without granting admission or advancing the ledger.
pub(crate) fn validate_execution_settlement(
    v: &Value,
    identity: &Identity,
    instance: &str,
) -> io::Result<()> {
    bounded(v, ENVELOPE_BYTES)?;
    keys(
        v,
        &[
            "type",
            "version",
            "instance",
            "sequence",
            "identity",
            "input",
            "admission",
            "outcome",
            "proof",
            "history_sha256",
            "ledger_sha256",
        ],
    )?;
    if v["type"] != "work_settled"
        || number(v, "version", true)? != 1
        || hash(v, "instance")? != instance
        || Identity::parse(&v["identity"])? != *identity
    {
        return Err(bad("Execution settlement changed Work epoch"));
    }
    number(v, "sequence", true)?;
    let input = Input::parse(&v["input"])?;
    let admission = Admission::parse(&v["admission"])?;
    if input.session != identity.session
        || admission.input != input.id
        || admission.origin.session != input.session
        || admission.requested != input.requested
        || !matches!(
            text(v, "outcome", 16)?.as_str(),
            "completed" | "cancelled" | "failed" | "deferred"
        )
    {
        return Err(bad("Execution settlement changed Work input/admission"));
    }
    proof(
        &v["proof"],
        &[
            "worker_retired",
            "callbacks_retired",
            "streams_retired",
            "input_durable",
        ],
    )?;
    hash(v, "history_sha256")?;
    hash(v, "ledger_sha256")?;
    Ok(())
}
