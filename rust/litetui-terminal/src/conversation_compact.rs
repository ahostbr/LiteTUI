//! Reviewed manual compaction. Source-owned maintenance is distinct from prompt admission.
use crate::{
    history,
    history_view::{ReadKey, Target},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const SNAPSHOT: &str = "gui.conversations.native_compact_snapshot";
pub const VALIDATE: &str = "gui.conversations.native_compact_validate";
pub const APPLY: &str = "gui.conversations.native_compact_apply";
pub const SETTLED: &str = "gui.conversations.native_compact_settled";
pub const OPERATIONS: [&str; 4] = [SNAPSHOT, VALIDATE, APPLY, SETTLED];
pub const RESPONSE_BYTES: usize = 1024 * 1024;
fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text(v: &Value, key: &str, max: usize, empty: bool) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| s.len() <= max && (empty || !s.is_empty()))
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid conversation compaction {key}")))
}
fn number(v: &Value, key: &str) -> io::Result<u64> {
    v[key]
        .as_u64()
        .filter(|n| *n <= history::MAX_COUNT)
        .ok_or_else(|| fail("Invalid compaction message count"))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail("Invalid compaction boolean"))
}
fn token(v: &Value, key: &str) -> io::Result<String> {
    let value = text(v, key, 64, false)?;
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid compaction fingerprint"));
    }
    Ok(value)
}
fn uuid(v: &Value, key: &str) -> io::Result<String> {
    let value = text(v, key, 36, false)?;
    crate::resume::conversation_id(&value)?;
    if value.len() != 36
        || value.as_bytes()[14] != b'4'
        || !matches!(value.as_bytes()[19], b'8' | b'9' | b'a' | b'b')
    {
        return Err(fail("Invalid compaction action identity"));
    }
    Ok(value)
}
fn shape(v: &Value, kind: &str) -> io::Result<()> {
    if v["version"] != 1 || v["kind"] != kind || serde_json::to_vec(v)?.len() > RESPONSE_BYTES {
        return Err(fail(
            "Unsupported or oversized conversation compaction response",
        ));
    }
    Ok(())
}
fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
pub fn validate_hint(value: &str) -> io::Result<()> {
    if value.len() > TEXT_BYTES || value.contains('\0') {
        return Err(fail(
            "Compaction hint must fit 64 KiB without NUL; hint kept unchanged",
        ));
    }
    Ok(())
}

pub const TEXT_BYTES: usize = 65536;
/// A fresh current-history descriptor, never execution authority by itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub history_key: ReadKey,
    pub identity: history::Identity,
    pub history_snapshot: String,
    pub context_sha256: String,
    pub messages: u64,
}
impl Selection {
    pub fn verify(&self) -> io::Result<()> {
        self.identity.verify(&self.identity.command(SNAPSHOT))?;
        let hash = |s: &str| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        if self.history_key.target != Target::Current
            || self.history_key.generation == 0
            || self.identity.scope != history::Scope::Current
            || self.identity.from_session != self.identity.session
            || self.identity.catalog.is_some()
            || self.messages > history::MAX_COUNT
            || !hash(&self.history_snapshot)
            || !hash(&self.context_sha256)
        {
            return Err(io::Error::other(
                "Compaction requires a fresh current-conversation selection",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestKey {
    pub selection: Selection,
    pub generation: u64,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Read {
        key: RequestKey,
    },
    Validate {
        key: RequestKey,
        snapshot: String,
        hint: String,
    },
    Apply {
        key: RequestKey,
        review: String,
    },
}
impl Intent {
    pub fn key(&self) -> &RequestKey {
        match self {
            Self::Read { key, .. } | Self::Validate { key, .. } | Self::Apply { key, .. } => key,
        }
    }
}

impl Selection {
    pub fn command(&self, operation: &str) -> Value {
        json!({"type":operation,"version":1,"session_id":self.identity.session,
            "agent_id":self.identity.agent,"backend":self.identity.backend,"model":self.identity.model,
            "history_snapshot":self.history_snapshot,"context_sha256":self.context_sha256})
    }
    pub fn verify_wire(&self, v: &Value) -> io::Result<()> {
        self.verify()?;
        for field in [
            "version",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "history_snapshot",
            "context_sha256",
        ] {
            if v[field] != self.command(SNAPSHOT)[field] {
                return Err(fail(
                    "Compaction response changed selected history or owner",
                ));
            }
        }
        Ok(())
    }
}

fn nullable_reason(v: &Value) -> io::Result<Option<String>> {
    if v.get("reason") == Some(&Value::Null) {
        Ok(None)
    } else {
        Ok(Some(text(v, "reason", 4096, false)?))
    }
}
fn keys(v: &Value, expected: &[&str]) -> io::Result<()> {
    let map = v
        .as_object()
        .ok_or_else(|| fail("Expected a compaction object"))?;
    if map.len() != expected.len() || expected.iter().any(|key| !map.contains_key(*key)) {
        return Err(fail("Unexpected compaction object fields"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub keep_recent: u64,
    pub max_tokens: u64,
    pub max_tool_iters: u64,
    pub thinking_level: String,
    pub tools_enabled: bool,
    pub wake_after_compact: bool,
    pub clear_screen_after_compact: bool,
}
impl Config {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "keep_recent",
                "max_tokens",
                "max_tool_iters",
                "thinking_level",
                "tools_enabled",
                "wake_after_compact",
                "clear_screen_after_compact",
            ],
        )?;
        let max_tokens = v["max_tokens"]
            .as_u64()
            .filter(|n| (256..=2147483647).contains(n))
            .ok_or_else(|| fail("Invalid compaction token budget"))?;
        let max_tool_iters = v["max_tool_iters"]
            .as_u64()
            .filter(|n| (1..=512).contains(n))
            .ok_or_else(|| fail("Invalid compaction tool round budget"))?;
        let thinking_level = text(v, "thinking_level", 16, false)?;
        if ![
            "default", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        ]
        .contains(&thinking_level.as_str())
        {
            return Err(fail("Invalid compaction thinking level"));
        }
        Ok(Self {
            keep_recent: number(v, "keep_recent")?,
            max_tokens,
            max_tool_iters,
            thinking_level,
            tools_enabled: boolean(v, "tools_enabled")?,
            wake_after_compact: boolean(v, "wake_after_compact")?,
            clear_screen_after_compact: boolean(v, "clear_screen_after_compact")?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnerPlan {
    pub directory: String,
    pub head: String,
    pub documents: [String; 3],
}
fn git_oid(v: &Value, key: &str) -> io::Result<String> {
    let oid = text(v, key, 64, false)?;
    if !matches!(oid.len(), 40 | 64)
        || !oid
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid owner Git commit identity"));
    }
    Ok(oid)
}
fn owner_documents(v: &Value) -> io::Result<[String; 3]> {
    keys(v, &["SOUL.md", "NOW.md", "DOMAIN.md"])?;
    Ok([
        token(v, "SOUL.md")?,
        token(v, "NOW.md")?,
        token(v, "DOMAIN.md")?,
    ])
}
impl OwnerPlan {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(v, &["directory", "head", "files"])?;
        let directory = text(v, "directory", 32768, false)?;
        if !std::path::Path::new(&directory).is_absolute()
            || directory.chars().any(char::is_control)
        {
            return Err(fail("Invalid owner store directory"));
        }
        Ok(Self {
            directory,
            head: git_oid(v, "head")?,
            documents: owner_documents(&v["files"])?,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostPlan {
    pub owner_handoff: Option<OwnerPlan>,
    pub messages: u64,
    pub system_messages: u64,
    pub head_messages: u64,
    pub tail_messages: u64,
    pub head_context_sha256: String,
    pub tail_context_sha256: String,
    pub prompt_sha256: String,
    pub config_sha256: String,
}
impl HostPlan {
    fn parse(v: &Value, config: &Value, selection: &Selection) -> io::Result<Self> {
        let mut fields = vec![
            "messages",
            "system_messages",
            "head_messages",
            "tail_messages",
            "head_context_sha256",
            "tail_context_sha256",
            "prompt_sha256",
            "config_sha256",
        ];
        let owner_handoff = v.get("owner_handoff").map(OwnerPlan::parse).transpose()?;
        if owner_handoff.is_some() {
            fields.push("owner_handoff");
        }
        keys(v, &fields)?;
        let result = Self {
            owner_handoff,
            messages: number(v, "messages")?,
            system_messages: number(v, "system_messages")?,
            head_messages: number(v, "head_messages")?,
            tail_messages: number(v, "tail_messages")?,
            head_context_sha256: token(v, "head_context_sha256")?,
            tail_context_sha256: token(v, "tail_context_sha256")?,
            prompt_sha256: token(v, "prompt_sha256")?,
            config_sha256: token(v, "config_sha256")?,
        };
        if result.messages != selection.messages
            || result.system_messages > 1
            || result.messages
                != result.system_messages + result.head_messages + result.tail_messages
            || result.config_sha256 != format!("{:x}", Sha256::digest(serde_json::to_vec(config)?))
        {
            return Err(fail(
                "Compaction plan changed history counts or configuration fingerprint",
            ));
        }
        Ok(result)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudePlan {
    pub segment: String,
    pub session: String,
    pub ledger: String,
    pub prompt: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Plan {
    Host(Box<HostPlan>),
    Codex { thread_id: Option<String> },
    Claude(Option<ClaudePlan>),
}

fn native_id(v: &Value, key: &str) -> io::Result<String> {
    let id = text(v, key, 256, false)?;
    if !id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
    {
        return Err(fail("Invalid Codex native identity"));
    }
    Ok(id)
}

fn segment_id(v: &Value, key: &str) -> io::Result<String> {
    let value = text(v, key, 32, false)?;
    if value.len() != 32
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid Claude segment identity"));
    }
    Ok(value)
}

fn sdk_uuid(v: &Value, key: &str) -> io::Result<String> {
    let value = text(v, key, 36, false)?;
    if value.len() != 36
        || !value.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
    {
        return Err(fail("Invalid Claude SDK UUID"));
    }
    Ok(value)
}

impl Plan {
    fn parse(v: &Value, config: &Value, selection: &Selection, eligible: bool) -> io::Result<Self> {
        if v.get("kind").is_none() {
            if eligible && selection.identity.backend == "claude" {
                return Err(fail("Claude requires a provider segment plan"));
            }
            return Ok(Self::Host(Box::new(HostPlan::parse(v, config, selection)?)));
        }
        if v["kind"] == "claude" {
            keys(
                v,
                &[
                    "kind",
                    "messages",
                    "segment",
                    "native_session",
                    "ledger_before",
                    "prompt_sha256",
                ],
            )?;
            if selection.identity.backend != "claude"
                || number(v, "messages")? != selection.messages
                || eligible
                    && (config["tools_enabled"] != false || config["wake_after_compact"] != false)
            {
                return Err(fail("Invalid Claude segment plan"));
            }
            let fields = [
                "segment",
                "native_session",
                "ledger_before",
                "prompt_sha256",
            ];
            if !eligible && fields.iter().all(|field| v[*field].is_null()) {
                return Ok(Self::Claude(None));
            }
            return Ok(Self::Claude(Some(ClaudePlan {
                segment: segment_id(v, "segment")?,
                session: sdk_uuid(v, "native_session")?,
                ledger: token(v, "ledger_before")?,
                prompt: token(v, "prompt_sha256")?,
            })));
        }
        keys(v, &["kind", "messages", "thread_id"])?;
        if v["kind"] != "codex"
            || selection.identity.backend != "codex"
            || number(v, "messages")? != selection.messages
            || [
                "tools_enabled",
                "wake_after_compact",
                "clear_screen_after_compact",
            ]
            .iter()
            .any(|key| config[*key] != false)
        {
            return Err(fail("Invalid native Codex compaction plan"));
        }
        let thread_id = if v["thread_id"].is_null() && !eligible {
            None
        } else {
            Some(native_id(v, "thread_id")?)
        };
        Ok(Self::Codex { thread_id })
    }

    fn native(&self) -> bool {
        matches!(self, Self::Codex { .. })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub selection: Selection,
    pub token: String,
    pub eligible: bool,
    pub reason: Option<String>,
    pub plan: Plan,
    pub config: Config,
}
impl Snapshot {
    pub fn parse(v: &Value, selection: &Selection) -> io::Result<Self> {
        shape(v, "conversation_compact_snapshot")?;
        selection.verify_wire(v)?;
        let eligible = boolean(v, "eligible")?;
        let reason = nullable_reason(v)?;
        if eligible == reason.is_some() {
            return Err(fail("Contradictory compaction eligibility"));
        }
        Ok(Self {
            selection: selection.clone(),
            token: token(v, "snapshot")?,
            eligible,
            reason,
            config: Config::parse(&v["config"])?,
            plan: Plan::parse(&v["plan"], &v["config"], selection, eligible)?,
        })
    }
    pub fn request(&self, hint: &str) -> Value {
        let mut v = self.selection.command(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["hint"] = json!(hint);
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub before: Snapshot,
    pub token: String,
    pub action_id: String,
    pub root_execution_id: String,
    pub hint: String,
    pub hint_sha256: String,
    pub request_prompt_sha256: Option<String>,
    wire: Value,
}
impl Review {
    pub fn parse(v: &Value, before: Snapshot, expected_hint: &str) -> io::Result<Self> {
        shape(v, "conversation_compact_review")?;
        before.selection.verify_wire(v)?;
        validate_hint(expected_hint)?;
        let hint = text(v, "hint", TEXT_BYTES, true)?;
        validate_hint(&hint)?;
        if !before.eligible
            || v["snapshot"] != before.token
            || v["action"] != "compact"
            || hint != expected_hint
            || Config::parse(&v["config"])? != before.config
            || Plan::parse(&v["plan"], &v["config"], &before.selection, before.eligible)?
                != before.plan
        {
            return Err(fail(
                "Compaction review changed the selected plan, configuration or hint",
            ));
        }
        let effects = &v["effects"];
        keys(
            effects,
            &[
                "provider_execution",
                "provider_rewind",
                "tools_enabled",
                "wake_after_compact",
                "clear_screen_after_compact",
            ],
        )?;
        if !boolean(effects, "provider_execution")?
            || boolean(effects, "provider_rewind")?
            || boolean(effects, "tools_enabled")? != before.config.tools_enabled
            || boolean(effects, "wake_after_compact")? != before.config.wake_after_compact
            || boolean(effects, "clear_screen_after_compact")?
                != before.config.clear_screen_after_compact
        {
            return Err(fail(
                "Compaction effects contradict the reviewed configuration",
            ));
        }
        let hint_sha256 = token(v, "hint_sha256")?;
        let request_prompt_sha256 = match &before.plan {
            Plan::Claude(None) => return Err(fail("Claude review lacks an active session")),
            Plan::Claude(Some(plan)) => {
                let hash = token(v, "request_prompt_sha256")?;
                if hint.is_empty() && hash != plan.prompt {
                    return Err(fail("Claude summary prompt changed"));
                }
                Some(hash)
            }
            Plan::Host(plan) => {
                let hash = token(v, "request_prompt_sha256")?;
                if hint.is_empty() && hash != plan.prompt_sha256 {
                    return Err(fail("Compaction prompt fingerprint changed"));
                }
                Some(hash)
            }
            Plan::Codex { .. } => {
                if !hint.is_empty() || v.get("request_prompt_sha256") != Some(&Value::Null) {
                    return Err(fail("Native Codex compaction cannot accept a host prompt"));
                }
                None
            }
        };
        if hint_sha256 != digest(&hint) {
            return Err(fail("Compaction hint fingerprint changed"));
        }
        Ok(Self {
            before,
            token: token(v, "review")?,
            action_id: uuid(v, "action_id")?,
            root_execution_id: uuid(v, "root_execution_id")?,
            hint,
            hint_sha256,
            request_prompt_sha256,
            wire: v.clone(),
        })
    }
    pub fn clear_display(&self) -> bool {
        self.before.config.clear_screen_after_compact
    }
    pub fn request(&self) -> Value {
        let mut v = self.wire.clone();
        v.as_object_mut().expect("parsed review").remove("kind");
        v["type"] = json!(APPLY);
        v
    }
    pub fn settled_request(&self, operation: &str) -> Value {
        let i = &self.before.selection.identity;
        json!({"type":SETTLED,"version":1,"session_id":i.session,"agent_id":i.agent,"backend":i.backend,"model":i.model,
            "action_id":self.action_id,"operation_id":operation})
    }
    fn verify_receipt(&self, v: &Value, operation: &str) -> io::Result<()> {
        self.before.selection.verify_wire(v)?;
        if operation.is_empty()
            || operation.len() > 128
            || v["operation_id"] != operation
            || v["action"] != "compact"
            || v["snapshot"] != self.before.token
            || v["review"] != self.token
            || v["action_id"] != self.action_id
            || v["root_execution_id"] != self.root_execution_id
        {
            return Err(fail("Compaction receipt changed reviewed identity"));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionStatus {
    Accepted,
    Refused,
    Failed,
}
impl AdmissionStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Refused => "refused",
            Self::Failed => "failed",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Admission {
    pub status: AdmissionStatus,
    pub reason: Option<String>,
    pub close_required: bool,
    pub root_execution_id: Option<String>,
    pub action_id: String,
    pub operation_id: String,
    pub refusal_history: Option<history::Snapshot>,
}
fn saved(v: &Value, identity: &history::Identity) -> io::Result<history::Snapshot> {
    let s = history::Snapshot::parse(v, identity)?;
    if s.read_only || s.pending || !s.materialized {
        return Err(fail(
            "Compaction proof lacks writable current saved history",
        ));
    }
    Ok(s)
}
impl Admission {
    pub fn parse(v: &Value, review: &Review, operation: &str) -> io::Result<Self> {
        shape(v, "conversation_compact_apply")?;
        review.verify_receipt(v, operation)?;
        let status = match v["status"].as_str() {
            Some("accepted") => AdmissionStatus::Accepted,
            Some("refused") => AdmissionStatus::Refused,
            Some("failed") => AdmissionStatus::Failed,
            _ => return Err(fail("Invalid compaction admission status")),
        };
        let reason = nullable_reason(v)?;
        let close_required = boolean(v, "close_required")?;
        if close_required != (status == AdmissionStatus::Failed)
            || (status == AdmissionStatus::Accepted) != reason.is_none()
        {
            return Err(fail("Contradictory compaction admission"));
        }
        let refusal_history = if status == AdmissionStatus::Refused {
            Some(saved(
                &v["refusal_history"],
                &review.before.selection.identity,
            )?)
        } else {
            if v.get("refusal_history") != Some(&Value::Null) {
                return Err(fail(
                    "Only a refused compaction may publish refusal history",
                ));
            }
            None
        };
        Ok(Self {
            status,
            reason,
            close_required,
            root_execution_id: Some(review.root_execution_id.clone()),
            action_id: review.action_id.clone(),
            operation_id: operation.into(),
            refusal_history,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Compacted,
    NoChange,
    Cancelled,
    Failed,
}
impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Compacted => "compacted",
            Self::NoChange => "no_change",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeReceipt {
    pub result_id: String,
    pub new_segment: String,
    pub seed_sha256: String,
    pub ledger_after: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeCompaction {
    pub thread_id: String,
    pub turn_id: String,
    pub item_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Settlement {
    pub native_compaction: Option<NativeCompaction>,
    pub owner_commit: Option<String>,
    pub claude_receipt: Option<ClaudeReceipt>,
    pub outcome: Outcome,
    pub reason: Option<String>,
    pub reason_code: String,
    pub history_changed: bool,
    pub history: history::Snapshot,
    pub root_history: Option<history::Snapshot>,
    pub execution_segments: u64,
    pub last_execution_id: Option<String>,
}
impl Settlement {
    /// Keep a bounded source explanation; otherwise describe its validated code.
    pub fn explanation(&self) -> Option<String> {
        if let Some(commit) = &self.owner_commit {
            return Some(format!("Owner handoff verified at Git commit {commit}."));
        }
        if let Some(receipt) = &self.claude_receipt {
            return Some(format!(
                "Claude summary saved in segment {}.",
                receipt.new_segment
            ));
        }
        if let Some(receipt) = &self.native_compaction {
            return Some(format!(
                "Codex confirmed turn {} in its thread history.",
                receipt.turn_id
            ));
        }
        self.reason.clone().or_else(|| {
            let message = match self.reason_code.as_str() {
                "too_short" => "Too few messages to compact.",
                "already_recent" => "All messages are kept as recent context.",
                "no_summary" => "No summary was produced.",
                "provider_failed" => "The provider could not complete compaction.",
                "cancelled" => "Compaction was cancelled.",
                "admission_refused" => "The source refused compaction admission.",
                _ => return None,
            };
            Some(message.into())
        })
    }
    pub fn parse(
        v: &Value,
        admission: &Admission,
        review: &Review,
        operation: &str,
    ) -> io::Result<Self> {
        shape(v, "conversation_compact_settled")?;
        review.verify_receipt(v, operation)?;
        if admission.status == AdmissionStatus::Failed
            || admission.operation_id != operation
            || admission.action_id != review.action_id
            || admission.root_execution_id.as_deref() != Some(review.root_execution_id.as_str())
            || v["status"] != admission.status.as_str()
            || !boolean(v, "worker_settled")?
            || boolean(v, "close_required")?
        {
            return Err(fail("Compaction settlement changed its admitted action"));
        }
        let outcome = match v["outcome"].as_str() {
            Some("compacted") => Outcome::Compacted,
            Some("no_change") => Outcome::NoChange,
            Some("cancelled") => Outcome::Cancelled,
            Some("failed") => Outcome::Failed,
            _ => return Err(fail("Invalid compaction outcome")),
        };
        let reason_code = text(v, "reason_code", 32, false)?;
        let reason = nullable_reason(v)?;
        let history = saved(&v["history"], &review.before.selection.identity)?;
        let history_changed = boolean(v, "history_changed")?;
        if history_changed != (history.context_sha256 != review.before.selection.context_sha256) {
            return Err(fail(
                "Compaction history change flag contradicts its exact context",
            ));
        }
        let w = &v["witness"];
        let native = review.before.plan.native();
        let mut fields = vec![
            "root_started",
            "input_context_sha256",
            "root_context_sha256",
            "publication_attempted",
            "publication_verified",
        ];
        if native {
            fields.push("native_compaction");
        }
        if matches!(review.before.plan, Plan::Claude(_)) {
            fields.push("claude_compaction");
        }
        let owner = match &review.before.plan {
            Plan::Host(plan) => plan.owner_handoff.as_ref(),
            _ => None,
        };
        if owner.is_some() {
            fields.push("owner_handoff");
        }
        keys(w, &fields)?;
        let mut owner_commit = None;
        if let Some(plan) = owner {
            let receipt = &w["owner_handoff"];
            if receipt.is_null() {
                if outcome == Outcome::Compacted {
                    return Err(fail("Published owner compaction lacks a verified commit"));
                }
            } else {
                keys(receipt, &["parent", "commit", "files"])?;
                let commit = git_oid(receipt, "commit")?;
                let files = owner_documents(&receipt["files"])?;
                if admission.status == AdmissionStatus::Refused
                    || outcome == Outcome::NoChange
                    || git_oid(receipt, "parent")? != plan.head
                    || commit == plan.head
                    || commit.len() != plan.head.len()
                    || files[0] != plan.documents[0]
                    || files[1] == plan.documents[1]
                {
                    return Err(fail(
                        "Owner commit contradicts its reviewed repository or documents",
                    ));
                }
                owner_commit = Some(commit);
            }
        }
        let mut native_compaction = None;
        if let Plan::Codex { thread_id } = &review.before.plan {
            let evidence = &w["native_compaction"];
            if outcome == Outcome::Compacted {
                keys(evidence, &["thread_id", "turn_id", "item_id"])?;
                let observed = native_id(evidence, "thread_id")?;
                if thread_id.as_ref() != Some(&observed) {
                    return Err(fail("Codex compaction settled another native thread"));
                }
                native_compaction = Some(NativeCompaction {
                    thread_id: observed,
                    turn_id: native_id(evidence, "turn_id")?,
                    item_id: native_id(evidence, "item_id")?,
                });
            } else if !evidence.is_null() {
                return Err(fail("Uncompleted Codex compaction claims a native outcome"));
            }
        }
        let mut claude_receipt = None;
        if let Plan::Claude(Some(plan)) = &review.before.plan {
            let receipt = &w["claude_compaction"];
            if outcome == Outcome::Compacted {
                keys(
                    receipt,
                    &[
                        "old_segment",
                        "native_session",
                        "result_id",
                        "new_segment",
                        "seed_sha256",
                        "ledger_before",
                        "ledger_after",
                    ],
                )?;
                let new_segment = segment_id(receipt, "new_segment")?;
                let ledger_after = token(receipt, "ledger_after")?;
                if receipt["old_segment"] != plan.segment
                    || receipt["native_session"] != plan.session
                    || receipt["ledger_before"] != plan.ledger
                    || new_segment == plan.segment
                    || ledger_after == plan.ledger
                    || history.token == review.before.selection.history_snapshot
                    || history.counts.non_system != 2
                {
                    return Err(fail(
                        "Claude ledger or compacted transcript contradicts its reviewed segment",
                    ));
                }
                claude_receipt = Some(ClaudeReceipt {
                    result_id: sdk_uuid(receipt, "result_id")?,
                    new_segment,
                    seed_sha256: token(receipt, "seed_sha256")?,
                    ledger_after,
                });
            } else if !receipt.is_null() {
                return Err(fail(
                    "Uncompleted Claude compaction claims a replacement segment",
                ));
            }
        }
        let started = boolean(w, "root_started")?;
        let attempted = boolean(w, "publication_attempted")?;
        let verified = boolean(w, "publication_verified")?;
        if token(w, "input_context_sha256")? != review.before.selection.context_sha256 {
            return Err(fail("Compaction witness changed its reviewed input"));
        }
        let root_context = token(w, "root_context_sha256")?;
        let root_history = if admission.status == AdmissionStatus::Refused {
            if started
                || attempted
                || verified
                || outcome != Outcome::NoChange
                || reason_code != "admission_refused"
                || v.get("root_history") != Some(&Value::Null)
                || admission.refusal_history.as_ref() != Some(&history)
                || root_context != history.context_sha256
            {
                return Err(fail(
                    "Refused compaction has contradictory effects or history",
                ));
            }
            None
        } else {
            let root = saved(&v["root_history"], &review.before.selection.identity)?;
            let valid = match outcome {
                Outcome::Compacted => reason_code == "compacted",
                Outcome::NoChange => {
                    ["too_short", "already_recent"].contains(&reason_code.as_str())
                }
                Outcome::Cancelled => reason_code == "cancelled",
                Outcome::Failed => {
                    ["no_summary", "provider_failed"].contains(&reason_code.as_str())
                }
            };
            if !started
                || matches!(review.before.plan, Plan::Claude(_))
                    && (!matches!(outcome, Outcome::Compacted | Outcome::Cancelled)
                        || root != history
                        || outcome == Outcome::Cancelled
                            && history.token != review.before.selection.history_snapshot)
                || root_context != root.context_sha256
                || !valid
                || attempted != (outcome == Outcome::Compacted && !native)
                || native
                    && (!matches!(outcome, Outcome::Compacted | Outcome::Cancelled)
                        || history_changed
                        || root != history
                        || history.token != review.before.selection.history_snapshot)
                || verified != attempted
            {
                return Err(fail(
                    "Compaction settlement lacks a coherent publication witness",
                ));
            }
            Some(root)
        };
        let execution = &v["execution"];
        keys(execution, &["segments", "last_execution_id"])?;
        let execution_segments = execution["segments"]
            .as_u64()
            .filter(|n| *n <= 100)
            .ok_or_else(|| fail("Invalid compaction stream count"))?;
        let last_execution_id = if execution_segments == 0 {
            if execution.get("last_execution_id") != Some(&Value::Null) {
                return Err(fail("Zero-stream compaction invented a terminal execution"));
            }
            None
        } else {
            if admission.status == AdmissionStatus::Refused
                || outcome != Outcome::Compacted
                || !review.before.config.wake_after_compact
            {
                return Err(fail("Unpublished compaction cannot wake a turn"));
            }
            Some(uuid(execution, "last_execution_id")?)
        };
        Ok(Self {
            native_compaction,
            owner_commit,
            claude_receipt,
            outcome,
            reason,
            reason_code,
            history_changed,
            history,
            root_history,
            execution_segments,
            last_execution_id,
        })
    }
}

use crate::{prepared_skill::Draft, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot {
        key: RequestKey,
        snapshot: Box<Snapshot>,
    },
    Review {
        key: RequestKey,
        review: Box<Review>,
    },
    Rejected {
        key: RequestKey,
        reason: String,
    },
    Submitted {
        key: RequestKey,
    },
    Settled {
        key: RequestKey,
        outcome: Outcome,
        reason: Option<String>,
    },
}
impl Update {
    pub fn key(&self) -> &RequestKey {
        match self {
            Self::Snapshot { key, .. }
            | Self::Review { key, .. }
            | Self::Rejected { key, .. }
            | Self::Submitted { key }
            | Self::Settled { key, .. } => key,
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Read,
    Validate,
    Apply,
}
#[derive(Default)]
pub struct View {
    open: bool,
    waiting: bool,
    applying: bool,
    submitted: bool,
    visible: bool,
    reviewed: bool,
    generation: u64,
    pending: Option<RequestKey>,
    pending_phase: Option<Phase>,
    intent: Option<Intent>,
    selection: Option<Selection>,
    snapshot: Option<Snapshot>,
    review: Option<Review>,
    draft: Draft,
    area: Rect,
    page: usize,
    pages: usize,
    pub notice: String,
}
impl View {
    pub fn open(&mut self, selection: Selection) -> io::Result<()> {
        selection.verify()?;
        if self.waiting {
            return Err(fail("A conversation compaction action is still pending"));
        }
        self.open = true;
        self.selection = Some(selection);
        self.snapshot = None;
        self.review = None;
        self.draft = Draft::default();
        self.submitted = false;
        self.applying = false;
        self.refresh();
        Ok(())
    }
    pub(crate) fn prefill(&mut self, hint: &str) -> io::Result<()> {
        validate_hint(hint)?;
        self.draft.set(hint)
    }
    fn begin(&mut self, phase: Phase) -> Option<RequestKey> {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice = "Compaction request identity exhausted; reopen the runtime.".into();
            return None;
        };
        self.generation = generation;
        let key = RequestKey {
            selection: self.selection.clone().expect("opened selection"),
            generation,
        };
        self.pending = Some(key.clone());
        self.pending_phase = Some(phase);
        self.waiting = true;
        self.hide();
        Some(key)
    }
    fn refresh(&mut self) {
        if self.waiting {
            return;
        }
        let Some(key) = self.begin(Phase::Read) else {
            return;
        };
        self.snapshot = None;
        self.review = None;
        self.applying = false;
        self.intent = Some(Intent::Read { key });
        self.notice = "Reading the current saved history and compaction plan…".into();
    }
    pub fn is_open(&self) -> bool {
        self.open
    }
    pub fn owns_input(&self) -> bool {
        (self.open || self.waiting) && !self.submitted
    }
    pub fn blocks_composer(&self) -> bool {
        self.open || self.waiting
    }
    pub fn waiting(&self) -> bool {
        self.waiting
    }
    pub fn pending_key(&self) -> Option<&RequestKey> {
        self.pending.as_ref()
    }
    pub fn draft(&self) -> &str {
        self.draft.text()
    }
    pub fn review(&self) -> Option<&Review> {
        self.review.as_ref()
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.reviewed = false;
    }
    pub fn close(&mut self) {
        self.open = false;
        if self.intent.take().is_some() {
            self.pending = None;
            self.pending_phase = None;
            self.waiting = false;
            self.applying = false;
        }
        if !self.applying {
            self.review = None;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        let generation = self.generation;
        *self = Self {
            generation,
            ..Self::default()
        };
    }
    pub fn prepare_apply(&self, intent: &Intent) -> io::Result<&Review> {
        let Intent::Apply { key, review } = intent else {
            return Err(fail("Expected a confirmed compaction Apply"));
        };
        if self.pending.as_ref() != Some(key) || !self.waiting || !self.applying || self.submitted {
            return Err(fail("Compaction Apply no longer belongs to this control"));
        }
        self.review
            .as_ref()
            .filter(|r| &r.token == review && r.before.selection == key.selection)
            .ok_or_else(|| fail("Compaction Apply changed its visible source review"))
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if self.pending.as_ref() != Some(update.key()) {
            return Ok(());
        }
        match &update {
            Update::Submitted { .. } => {
                if !self.applying || self.submitted {
                    return Err(fail("Unexpected compaction submission handoff"));
                }
                self.submitted = true;
                self.open = false;
                self.intent = None;
                self.hide();
                self.notice="Compaction dispatched; live approvals and Stop remain available. Waiting for verified settlement.".into();
                return Ok(());
            }
            Update::Settled {
                outcome, reason, ..
            } => {
                if !self.applying || !self.submitted {
                    return Err(fail("Compaction settled without a dispatched action"));
                }
                let notice = format!(
                    "Compaction {}. Saved context and worker retirement verified. {}",
                    outcome.as_str(),
                    reason.as_deref().unwrap_or("")
                );
                self.finish();
                self.notice = notice;
                return Ok(());
            }
            Update::Rejected { .. } if self.submitted => {
                return Err(fail("Dispatched compaction requires source settlement"));
            }
            Update::Snapshot { snapshot, key } => {
                if self.pending_phase != Some(Phase::Read)
                    || self.applying
                    || snapshot.selection != key.selection
                {
                    return Err(fail("Compaction snapshot belongs to another selection"));
                }
            }
            Update::Review { review, key } => {
                if self.pending_phase != Some(Phase::Validate)
                    || self.applying
                    || review.before.selection != key.selection
                    || review.hint != self.draft.text()
                {
                    return Err(fail("Compaction review belongs to another history or hint"));
                }
            }
            _ => {}
        }
        self.waiting = false;
        self.pending = None;
        self.pending_phase = None;
        self.intent = None;
        self.applying = false;
        self.hide();
        self.page = 0;
        if !self.open {
            return Ok(());
        }
        match update {
            Update::Snapshot { snapshot, .. } => {
                self.notice = snapshot.reason.clone().unwrap_or_else(|| {
                    if snapshot.plan.native() {
                        "Enter to review compaction of this Codex thread.".into()
                    } else {
                        "Optional guidance below. Enter reviews the exact compaction plan.".into()
                    }
                });
                if snapshot.plan.native() {
                    self.draft = Draft::default();
                }
                self.snapshot = Some(*snapshot);
                self.review = None;
            }
            Update::Review { review, .. } => {
                self.review = Some(*review);
                self.notice = "Read every page, then explicitly confirm compacting once.".into();
            }
            Update::Rejected { reason, .. } => {
                self.snapshot = None;
                self.review = None;
                self.notice =
                    format!("{reason} · hint kept; close and refresh history if it changed.");
            }
            _ => unreachable!(),
        }
        Ok(())
    }
    pub fn editing(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self
                .snapshot
                .as_ref()
                .is_some_and(|s| s.eligible && !s.plan.native())
    }
    pub fn accepts_paste(&self) -> bool {
        self.editing()
    }
    pub fn paste(&mut self, text: &str) {
        if !self.editing() {
            return;
        }
        if let Err(error) = self.draft.insert(text) {
            self.notice = error.to_string();
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            if self.review.is_some() && !self.waiting {
                self.review = None;
                self.hide();
                self.notice = "Review cancelled; hint kept.".into();
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if !(key.modifiers.is_empty()
            || key.modifiers == KeyModifiers::SHIFT
                && matches!(key.code, KeyCode::Char(_) | KeyCode::Enter))
        {
            self.hide();
            return;
        }
        if key.code == KeyCode::F(5) && key.kind == KeyEventKind::Press {
            self.refresh();
            return;
        }
        if self.review.is_some() {
            match key.code {
                KeyCode::PageDown if self.reviewed => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                    self.hide();
                }
                KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    self.hide();
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && key.modifiers.is_empty()
                        && self.reviewed
                        && self.page + 1 == self.pages =>
                {
                    let review = self.review.as_ref().expect("review").token.clone();
                    if let Some(key) = self.begin(Phase::Apply) {
                        self.intent = Some(Intent::Apply { key, review });
                        self.applying = true;
                        self.notice =
                            "Dispatching reviewed compaction; Esc hides this control only.".into();
                    }
                }
                _ => self.hide(),
            }
            return;
        }
        if key.code == KeyCode::Enter && key.modifiers == KeyModifiers::SHIFT {
            self.paste("\n");
            return;
        }
        if key.code == KeyCode::Enter && key.kind == KeyEventKind::Press {
            if let Some(snapshot) = &self.snapshot {
                if !snapshot.eligible {
                    self.notice = snapshot.reason.clone().unwrap_or_default();
                    return;
                }
                let hint = self.draft.text().to_owned();
                if let Err(error) = validate_hint(&hint) {
                    self.notice = error.to_string();
                    return;
                }
                let snapshot = snapshot.token.clone();
                if let Some(key) = self.begin(Phase::Validate) {
                    self.intent = Some(Intent::Validate {
                        key,
                        snapshot,
                        hint,
                    });
                    self.notice = "Reviewing exact compaction guidance, plan and effects…".into();
                }
            }
            return;
        }
        if self.editing() {
            match key.code {
                KeyCode::Char(c) if !c.is_control() => self.paste(&c.to_string()),
                other => self.draft.key(other),
            }
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        if event.modifiers.is_empty() && self.area.contains((event.column, event.row).into()) {
            match event.kind {
                MouseEventKind::ScrollDown => {
                    self.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
                    return;
                }
                MouseEventKind::ScrollUp => {
                    self.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
                    return;
                }
                _ => {}
            }
        }
        self.hide();
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if self.submitted || !self.owns_input() {
            self.hide();
            return;
        }
        if self.area != area {
            self.hide();
            self.page = 0;
            self.area = area;
        }
        let style = Style::default().fg(theme.foreground).bg(theme.surface);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .title(" Compact current conversation ")
                .style(style),
            area,
        );
        if area.width < 42 || area.height < 18 {
            self.hide();
            frame.render_widget(
                Paragraph::new("Resize to 42×18.\nEsc cancels the control, not a turn.")
                    .style(style),
                area.inner(ratatui::layout::Margin::new(1, 1)),
            );
            return;
        }
        let body = Rect::new(area.x + 2, area.y + 2, area.width - 4, area.height - 6);
        let display = crate::live::terminal_text;
        let mut caret_line = None;
        let details = if let Some(review) = &self.review {
            match &review.before.plan {
                Plan::Codex { thread_id } => format!(
                    "Confirm Codex context compaction\nSession {}\nModel {}\nThread {}\n\nCodex will compact this thread on the provider. The visible conversation stays on disk. This operation can run inference and cannot be undone here.",
                    display(&review.before.selection.identity.session),
                    display(&review.before.selection.identity.model),
                    display(thread_id.as_deref().unwrap_or_default())
                ),
                Plan::Claude(Some(plan)) => format!(
                    "Confirm Claude compaction\nSession {}\nModel {}\n\nClaude will summarize this session and close it. The summary will seed a new segment and replace the saved context. Your next message starts a new SDK session with an empty cache and full input pricing.\nClear previous displayed rows: {}\n\nGuidance\n{}",
                    display(&plan.session),
                    display(&review.before.selection.identity.model),
                    review.before.config.clear_screen_after_compact,
                    display(&review.hint)
                ),
                Plan::Claude(None) => display(&self.notice),
                Plan::Host(p) => {
                    let c = &review.before.config;
                    let owner = p.owner_handoff.as_ref().map(|plan| format!(
                        "\nOwner handoff in {}\nThe provider may rewrite NOW.md and append to DOMAIN.md. The host will commit those files on top of {} before replacing chat history. It may ask the provider once more for a fresh NOW.md. Stopping can leave edited or committed files.\n",
                        display(&plan.directory), plan.head
                    )).unwrap_or_default();
                    format!(
                        "Confirm manual compaction once{owner}\nSession: {}\nBackend: {} · model: {}\nSummarise {} older messages; retain {} recent messages at complete tool boundaries.\nOriginal transcript records stay on disk. A provider request may run; a smaller result is not guaranteed.\nTools enabled: {}. Completed tool writes or approval audits may remain after Stop or failure.\nWake after success: {}. Clear older displayed rows: {}.\nCompaction budget: {} tokens · {} tool rounds · thinking {}.\nPost-compaction token counts are estimates until a later provider request.\nBackground and automatically promoted tools refuse before starting.\nYour composer draft stays unchanged.\n\nExact optional guidance:\n{}",
                        review.before.selection.identity.session,
                        display(&review.before.selection.identity.backend),
                        display(&review.before.selection.identity.model),
                        p.head_messages,
                        p.tail_messages,
                        c.tools_enabled,
                        c.wake_after_compact,
                        c.clear_screen_after_compact,
                        c.max_tokens,
                        c.max_tool_iters,
                        c.thinking_level,
                        display(&review.hint)
                    )
                }
            }
        } else if let Some(snapshot) = &self.snapshot {
            match &snapshot.plan {
                Plan::Codex { thread_id } => format!(
                    "Compact Codex context\nThread {}\nSaved messages {}\n\n{}",
                    thread_id.as_deref().unwrap_or("unavailable"),
                    snapshot.selection.messages,
                    display(&self.notice)
                ),
                plan => {
                    let header = match plan {
                        Plan::Claude(Some(plan)) => format!(
                            "Compact Claude session {}\n{}\n\nOptional compaction guidance:\n",
                            plan.session,
                            display(&self.notice)
                        ),
                        Plan::Claude(None) => display(&self.notice),
                        Plan::Codex { .. } => unreachable!(),
                        Plan::Host(plan) => format!(
                            "Current saved history: {} messages · {} older / {} recent\n{}\n\nOptional compaction guidance:\n",
                            plan.messages,
                            plan.head_messages,
                            plan.tail_messages,
                            display(&self.notice)
                        ),
                    };
                    let cursor = self.draft.cursor();
                    let before = display(&self.draft.text()[..cursor]);
                    caret_line = Some(
                        crate::ui::wrap(&(header.clone() + &before), body.width as usize)
                            .len()
                            .saturating_sub(1),
                    );
                    format!("{header}{before}▏{}", display(&self.draft.text()[cursor..]))
                }
            }
        } else {
            display(&self.notice)
        };
        let lines = crate::ui::wrap(&details, body.width as usize);
        let height = usize::from(body.height).max(1);
        self.pages = lines.len().div_ceil(height).max(1);
        self.page = self.page.min(self.pages - 1);
        if let Some(cursor_line) = caret_line {
            self.page = cursor_line / height;
        }
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(self.page * height)
                    .take(height)
                    .collect::<Vec<_>>()
                    .join("\n"),
            )
            .style(style),
            body,
        );
        let footer = if self.waiting {
            "Waiting · Esc hides pending control"
        } else if self.review.is_some() {
            "PgUp/PgDn review · Enter compact · Esc back"
        } else if self.snapshot.as_ref().is_some_and(|s| s.plan.native()) {
            "Enter review · Esc close"
        } else {
            "Enter review · Shift+Enter line · Esc close"
        };
        frame.render_widget(
            Paragraph::new(footer).style(Style::default().fg(theme.primary)),
            Rect::new(area.x + 2, area.bottom() - 3, area.width - 4, 1),
        );
        frame.render_widget(
            Paragraph::new(format!(
                "Page {}/{} · {}",
                self.page + 1,
                self.pages,
                display(&self.notice)
            ))
            .style(style),
            Rect::new(area.x + 2, area.bottom() - 2, area.width - 4, 1),
        );
        self.visible = self.open && !self.waiting;
        self.reviewed = self.visible && self.review.is_some();
    }
}

/// Retirement proof only. A successful shutdown never proves compaction effects.
pub fn verify_shutdown(
    result: &Value,
    dispatched: Option<&Value>,
    current_session: &str,
) -> io::Result<bool> {
    let proof = &result["conversation_compact_shutdown"];
    if proof["version"] != 1
        || proof["current_session_id"] != current_session
        || !boolean(proof, "worker_settled")?
        || !boolean(proof, "admission_settled")?
        || !boolean(proof, "hooks_settled")?
        || boolean(proof, "effects_verified")?
    {
        return Err(fail("Shutdown lacks settled compaction lifecycle proof"));
    }
    let pending = boolean(proof, "pending_at_close")?;
    let mutated = boolean(proof, "mutated")?;
    let fields = [
        "action_id",
        "action",
        "session_id",
        "agent_id",
        "backend",
        "model",
    ];
    if let Some(command) = dispatched {
        if command["type"] != APPLY
            || command["version"] != 1
            || !matches!(command["action"].as_str(), Some("compact"))
        {
            return Err(fail(
                "Shutdown expected an actual dispatched compaction command",
            ));
        }
        uuid(command, "action_id")?;
        let operation = text(command, "id", 128, false)?;
        if proof["operation_id"] != operation
            || fields.iter().any(|field| proof[*field] != command[*field])
            || pending && command["session_id"] != current_session
        {
            return Err(fail("Shutdown changed the tracked compaction identity"));
        }
    } else if pending
        || mutated
        || proof.get("operation_id") != Some(&Value::Null)
        || fields
            .iter()
            .any(|field| proof.get(*field) != Some(&Value::Null))
    {
        return Err(fail("Shutdown invented an undispatched compaction action"));
    }
    Ok(pending)
}
