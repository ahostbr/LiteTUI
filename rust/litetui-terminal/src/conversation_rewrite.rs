//! Reviewed current-conversation edit/retry. Provider execution stays in the source runtime.
use crate::{
    history,
    history_view::{ReadKey, Target},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const SNAPSHOT: &str = "gui.conversations.native_rewrite_snapshot";
pub const VALIDATE: &str = "gui.conversations.native_rewrite_validate";
pub const APPLY: &str = "gui.conversations.native_rewrite_apply";
pub const SETTLED: &str = "gui.conversations.native_rewrite_settled";
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
        .ok_or_else(|| fail(&format!("Invalid conversation rewrite {key}")))
}
fn number(v: &Value, key: &str) -> io::Result<u64> {
    v[key]
        .as_u64()
        .filter(|n| *n <= history::MAX_COUNT)
        .ok_or_else(|| fail("Invalid rewrite message count"))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail("Invalid rewrite boolean"))
}
fn token(v: &Value, key: &str) -> io::Result<String> {
    let value = text(v, key, 64, false)?;
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid rewrite fingerprint"));
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
        return Err(fail("Invalid rewrite action identity"));
    }
    Ok(value)
}
fn shape(v: &Value, kind: &str) -> io::Result<()> {
    if v["version"] != 1 || v["kind"] != kind || serde_json::to_vec(v)?.len() > RESPONSE_BYTES {
        return Err(fail(
            "Unsupported or oversized conversation rewrite response",
        ));
    }
    Ok(())
}
fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
pub fn validate_text(value: &str) -> io::Result<()> {
    if value.trim().is_empty() || value.len() > TEXT_BYTES {
        return Err(fail(
            "Message must contain text within 64 KiB; edit kept unchanged",
        ));
    }
    Ok(())
}

pub const TEXT_BYTES: usize = 65536;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Edit,
    Retry,
}
impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Edit => "edit",
            Self::Retry => "retry",
        }
    }
}
/// A displayed coordinate, never mutation authority by itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub history_key: ReadKey,
    pub identity: history::Identity,
    pub history_snapshot: String,
    pub context_sha256: String,
    pub messages: u64,
    pub message_index: u64,
    pub message_sha256: String,
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
            || self.message_index >= self.messages
            || self.messages > history::MAX_COUNT
            || !hash(&self.history_snapshot)
            || !hash(&self.context_sha256)
            || !hash(&self.message_sha256)
        {
            return Err(io::Error::other(
                "Edit/retry requires a fresh current-conversation selection",
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
        action: Action,
    },
    Validate {
        key: RequestKey,
        snapshot: String,
        action: Action,
        message: String,
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
            "history_snapshot":self.history_snapshot,"context_sha256":self.context_sha256,
            "message_index":self.message_index,"message_sha256":self.message_sha256})
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
            "message_index",
            "message_sha256",
        ] {
            if v[field] != self.command(SNAPSHOT)[field] {
                return Err(fail("Rewrite response changed selected history or owner"));
            }
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Eligibility {
    pub eligible: bool,
    pub reason: Option<String>,
}
impl Eligibility {
    fn parse(v: &Value) -> io::Result<Self> {
        let eligible = boolean(v, "eligible")?;
        let reason = if v.get("reason") == Some(&Value::Null) {
            None
        } else {
            Some(text(v, "reason", 4096, false)?)
        };
        if eligible == reason.is_some() {
            return Err(fail("Contradictory rewrite eligibility"));
        }
        Ok(Self { eligible, reason })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub selection: Selection,
    pub token: String,
    pub original_text: String,
    pub prefix_context_sha256: String,
    pub discarded_messages: u64,
    pub edit: Eligibility,
    pub retry: Eligibility,
}
impl Snapshot {
    pub fn parse(v: &Value, selection: &Selection) -> io::Result<Self> {
        shape(v, "conversation_rewrite_snapshot")?;
        selection.verify_wire(v)?;
        let original_text = text(v, "original_text", TEXT_BYTES, true)?;
        let edit = Eligibility::parse(&v["actions"]["edit"])?;
        let retry = Eligibility::parse(&v["actions"]["retry"])?;
        if v["original_bytes"].as_u64() != Some(original_text.len() as u64)
            || number(v, "prefix_messages")? != selection.message_index
            || number(v, "discarded_messages")? != selection.messages - selection.message_index - 1
        {
            return Err(fail(
                "Rewrite snapshot changed message bytes or prefix counts",
            ));
        }
        if edit.eligible || retry.eligible {
            validate_text(&original_text)?;
        }
        Ok(Self {
            selection: selection.clone(),
            token: token(v, "snapshot")?,
            original_text,
            prefix_context_sha256: token(v, "prefix_context_sha256")?,
            discarded_messages: number(v, "discarded_messages")?,
            edit,
            retry,
        })
    }
    pub fn eligibility(&self, action: Action) -> &Eligibility {
        match action {
            Action::Edit => &self.edit,
            Action::Retry => &self.retry,
        }
    }
    pub fn request(&self, action: Action, message: &str) -> Value {
        let mut v = self.selection.command(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["action"] = json!(action.as_str());
        if action == Action::Edit {
            v["text"] = json!(message);
        }
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativePlan {
    pub thread_id: String,
    wire: Value,
}

fn native_id(v: &Value, key: &str) -> io::Result<String> {
    let id = text(v, key, 128, false)?;
    if !id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
    {
        return Err(fail("Invalid native history identity"));
    }
    Ok(id)
}

fn keys(v: &Value, expected: &[&str]) -> bool {
    v.as_object().is_some_and(|obj| {
        obj.len() == expected.len() && expected.iter().all(|k| obj.contains_key(*k))
    })
}

impl NativePlan {
    pub fn fork(&self) -> bool {
        self.wire.get("fork").is_some()
    }
    fn operation(&self) -> &'static str {
        if self.fork() {
            "native_fork"
        } else {
            "native_rewind"
        }
    }
    fn parse(v: &Value, before: &Snapshot, fork: bool) -> io::Result<Self> {
        let mut fields = vec![
            "thread_id",
            "before_sha256",
            "prefix_sha256",
            "retained_turns",
            "suffix",
        ];
        if fork {
            fields.push("fork");
            let plan = &v["fork"];
            if !keys(
                plan,
                &[
                    "old_segment",
                    "new_segment",
                    "delivery",
                    "checkpoint",
                    "ledger_before",
                ],
            ) || ["old_segment", "new_segment", "delivery"].iter().any(|k| {
                !plan[k].as_str().is_some_and(|id| {
                    id.len() == 32
                        && id
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                })
            }) || plan["old_segment"] == plan["new_segment"]
                || plan["delivery"] == plan["new_segment"]
            {
                return Err(fail("Malformed Claude fork or delivery identity"));
            }
            crate::resume::conversation_id(&native_id(plan, "checkpoint")?)?;
            crate::resume::conversation_id(&native_id(v, "thread_id")?)?;
            token(plan, "ledger_before")?;
        }
        let suffix = v["suffix"]
            .as_array()
            .ok_or_else(|| fail("Missing native history suffix"))?;
        let retained = number(v, "retained_turns")?;
        if before.selection.identity.backend != if fork { "claude" } else { "codex" }
            || serde_json::to_vec(v)?.len() > TEXT_BYTES
            || !keys(v, &fields)
            || fork && retained == 0
            || suffix.is_empty()
            || retained + suffix.len() as u64 > 32
            || retained > before.selection.message_index
            || suffix[0]["text"] != before.original_text
            || token(v, "before_sha256")? == token(v, "prefix_sha256")?
        {
            return Err(fail("Invalid native session or excluded suffix"));
        }
        let mut turns = std::collections::HashSet::new();
        let mut items = std::collections::HashSet::new();
        for turn in suffix {
            let id = native_id(turn, "user_item_id")?;
            let content = text(turn, "text", TEXT_BYTES, true)?;
            if !keys(turn, &["turn_id", "user_item_id", "text", "items"])
                || !turns.insert(native_id(turn, "turn_id")?)
            {
                return Err(fail("Repeated or malformed native turn in history review"));
            }
            if fork {
                let records = turn["items"]
                    .as_array()
                    .ok_or_else(|| fail("Missing Claude fork messages"))?;
                if records.len() != 2
                    || records[0]["kind"] != "userMessage"
                    || records[1]["kind"] != "agentMessage"
                    || records[1]["id"] != turn["turn_id"]
                    || records[0]["id"] == records[1]["id"]
                    || records.iter().any(|item| {
                        item["id"]
                            .as_str()
                            .is_none_or(|id| crate::resume::conversation_id(id).is_err())
                    })
                {
                    return Err(fail(
                        "Claude fork review changed the native user and assistant pair",
                    ));
                }
            }
            let mut users = 0;
            for item in turn["items"]
                .as_array()
                .ok_or_else(|| fail("Missing native history items"))?
            {
                let item_id = native_id(item, "id")?;
                let kind = native_id(item, "kind")?;
                let details = text(item, "details", TEXT_BYTES, true)?;
                if !keys(item, &["id", "kind", "details"])
                    || !items.insert(item_id.clone())
                    || items.len() > 256
                {
                    return Err(fail("Repeated or excessive native history items"));
                }
                if kind == "userMessage" {
                    if item_id != id || !details.is_empty() || content.is_empty() {
                        return Err(fail("History review changed the selected native user item"));
                    }
                    users += 1;
                }
            }
            if users != 1 {
                return Err(fail(
                    "History review requires one native user item per turn",
                ));
            }
        }
        if fork
            && v["fork"]["checkpoint"]
                .as_str()
                .is_some_and(|id| items.contains(id) || turns.contains(id))
        {
            return Err(fail(
                "Claude fork checkpoint was included among excluded messages",
            ));
        }
        Ok(Self {
            thread_id: native_id(v, "thread_id")?,
            wire: v.clone(),
        })
    }
    fn description(&self) -> String {
        let display = crate::live::terminal_text;
        let mut out = if self.fork() {
            format!(
                "Claude will fork session {} at checkpoint {}, preserving its first {} turns. The original session stays on disk. The new session may read the full prefix at full input price. Existing file changes stay in effect.\nTurns excluded from the fork:\n",
                self.thread_id, self.wire["fork"]["checkpoint"], self.wire["retained_turns"]
            )
        } else {
            format!(
                "Codex will rewind thread {} to {} retained turns. Prior commands and file changes stay in effect. Resending may run tools again.\nDiscarded native turns and items:\n",
                self.thread_id, self.wire["retained_turns"]
            )
        };
        for turn in self.wire["suffix"].as_array().expect("verified turns") {
            out.push_str(&format!(
                "Turn {} · {}\n",
                turn["turn_id"].as_str().unwrap(),
                display(turn["text"].as_str().unwrap())
            ));
            for item in turn["items"].as_array().expect("verified items") {
                out.push_str(&format!(
                    "  {} {} {}\n",
                    item["kind"].as_str().unwrap(),
                    item["id"].as_str().unwrap(),
                    display(item["details"].as_str().unwrap())
                ));
            }
        }
        out
    }
    fn receipt(&self, v: &Value) -> io::Result<String> {
        let turn = native_id(v, "turn_id")?;
        let user = native_id(v, "user_item_id")?;
        let mut fields = vec![
            "thread_id",
            "turn_id",
            "user_item_id",
            "prefix_sha256",
            "history_sha256",
        ];
        if self.fork() {
            fields.extend(["session_id", "result_id", "ledger_after"]);
            for key in ["turn_id", "user_item_id", "session_id", "result_id"] {
                crate::resume::conversation_id(&native_id(v, key)?)?;
            }
            if v["session_id"] == self.thread_id
                || token(v, "ledger_after")? == self.wire["fork"]["ledger_before"]
            {
                return Err(fail(
                    "Claude fork requires a distinct native session and durable ledger publication",
                ));
            }
        }
        if !keys(v, &fields)
            || v["thread_id"] != self.thread_id
            || v["prefix_sha256"] != self.wire["prefix_sha256"]
            || token(v, "history_sha256")? == self.wire["before_sha256"]
            || self.wire["suffix"].as_array().unwrap().iter().any(|old| {
                old["turn_id"] == turn
                    || old["items"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|i| i["id"] == user)
            })
        {
            return Err(fail(
                "Replacement receipt changed the native session, prefix or message identities",
            ));
        }
        Ok(turn)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub before: Snapshot,
    pub action: Action,
    pub token: String,
    pub action_id: String,
    pub text: String,
    pub text_sha256: String,
    pub prepared_context_sha256: String,
    pub prepared_messages: u64,
    pub prompt_hooks: bool,
    pub native_rewind: Option<Box<NativePlan>>,
}
impl Review {
    pub fn parse(
        v: &Value,
        before: Snapshot,
        action: Action,
        expected_text: &str,
    ) -> io::Result<Self> {
        shape(v, "conversation_rewrite_review")?;
        before.selection.verify_wire(v)?;
        validate_text(expected_text)?;
        let chosen = text(v, "text", TEXT_BYTES, false)?;
        let text_sha256 = token(v, "text_sha256")?;
        let prepared_messages = number(v, "prepared_messages")?;
        let prompt_hooks = boolean(&v["effects"], "prompt_hooks")?;
        let fork = v.get("native_fork").is_some();
        if fork && v.get("native_rewind").is_some()
            || v["effects"].get("provider_fork").is_some_and(|x| x != true)
            || (v["effects"].get("provider_fork").is_some() != fork)
        {
            return Err(fail("Contradictory native fork actions"));
        }
        let native_rewind = v
            .get(if fork { "native_fork" } else { "native_rewind" })
            .map(|plan| NativePlan::parse(plan, &before, fork).map(Box::new))
            .transpose()?;
        if !before.eligibility(action).eligible
            || v["snapshot"] != before.token
            || v["action"] != action.as_str()
            || chosen != expected_text
            || (action == Action::Retry && chosen != before.original_text)
            || text_sha256 != digest(&chosen)
            || prepared_messages != before.selection.message_index + 1
            || v["prefix_context_sha256"] != before.prefix_context_sha256
            || number(v, "prefix_messages")? != before.selection.message_index
            || number(v, "discarded_messages")? != before.discarded_messages
            || !boolean(&v["effects"], "provider_execution")?
            || boolean(&v["effects"], "provider_rewind")? != (native_rewind.is_some() && !fork)
        {
            return Err(fail(
                "Rewrite review changed text, selection, prefix or effects",
            ));
        }
        Ok(Self {
            before,
            action,
            token: token(v, "review")?,
            action_id: uuid(v, "action_id")?,
            text: chosen,
            text_sha256,
            prepared_context_sha256: token(v, "prepared_context_sha256")?,
            prepared_messages,
            prompt_hooks,
            native_rewind,
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.before.selection.command(APPLY);
        v["snapshot"] = json!(self.before.token);
        v["review"] = json!(self.token);
        v["action_id"] = json!(self.action_id);
        v["action"] = json!(self.action.as_str());
        v["text"] = json!(self.text);
        v["text_sha256"] = json!(self.text_sha256);
        v["prepared_context_sha256"] = json!(self.prepared_context_sha256);
        v["prepared_messages"] = json!(self.prepared_messages);
        v["prefix_context_sha256"] = json!(self.before.prefix_context_sha256);
        v["prefix_messages"] = json!(self.before.selection.message_index);
        v["discarded_messages"] = json!(self.before.discarded_messages);
        if let Some(plan) = &self.native_rewind {
            v[plan.operation()] = plan.wire.clone();
            v["effects"] = json!({"prompt_hooks":self.prompt_hooks,"provider_execution":true,"provider_rewind":!plan.fork()});
            if plan.fork() {
                v["effects"]["provider_fork"] = json!(true);
            }
        }
        v
    }
    pub fn verify_receipt(&self, v: &Value, operation: &str) -> io::Result<()> {
        self.before.selection.verify_wire(v)?;
        if operation.is_empty() || operation.len() > 128 || v["operation_id"] != operation {
            return Err(fail("Rewrite receipt changed execution operation"));
        }
        let expected = self.request();
        for field in ["snapshot", "review", "action_id", "action"] {
            if v[field] != expected[field] {
                return Err(fail("Rewrite receipt changed confirmed action"));
            }
        }
        Ok(())
    }
    pub fn settled_request(&self, operation: &str) -> Value {
        let identity = &self.before.selection.identity;
        json!({"type":SETTLED,"version":1,"session_id":identity.session,"agent_id":identity.agent,
            "backend":identity.backend,"model":identity.model,"action_id":self.action_id,"operation_id":operation})
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionOutcome {
    Accepted,
    Refused,
    Failed,
}
impl AdmissionOutcome {
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
    pub outcome: AdmissionOutcome,
    pub operation_id: String,
    pub action_id: String,
    pub mutated: bool,
    pub close_required: bool,
    pub reason: Option<String>,
    pub prepared_context_sha256: String,
    pub prepared_messages: u64,
    pub snapshot_written: bool,
    pub readback_verified: bool,
    pub refusal_history: Option<history::Snapshot>,
}
impl Admission {
    pub fn parse(v: &Value, review: &Review, operation: &str) -> io::Result<Self> {
        shape(v, "conversation_rewrite_apply")?;
        review.verify_receipt(v, operation)?;
        let outcome = match v["turn"].as_str() {
            Some("accepted") => AdmissionOutcome::Accepted,
            Some("refused") => AdmissionOutcome::Refused,
            Some("failed") => AdmissionOutcome::Failed,
            _ => return Err(fail("Invalid rewrite admission outcome")),
        };
        let mutated = boolean(v, "mutated")?;
        let close_required = boolean(v, "close_required")?;
        let snapshot_written = boolean(&v["persistence"], "snapshot_written")?;
        let readback_verified = boolean(&v["persistence"], "readback_verified")?;
        let reason = if v.get("reason") == Some(&Value::Null) {
            None
        } else {
            Some(text(v, "reason", 4096, false)?)
        };
        if v["prepared_context_sha256"] != review.prepared_context_sha256
            || v["text_sha256"] != review.text_sha256
            || number(v, "prepared_messages")? != review.prepared_messages
            || readback_verified && !snapshot_written
        {
            return Err(fail("Rewrite admission changed durable preparation proof"));
        }
        let valid = match outcome {
            AdmissionOutcome::Accepted => {
                mutated
                    && !close_required
                    && reason.is_none()
                    && snapshot_written
                    && readback_verified
            }
            AdmissionOutcome::Refused => {
                !mutated
                    && !close_required
                    && reason.is_some()
                    && !snapshot_written
                    && !readback_verified
            }
            AdmissionOutcome::Failed => close_required && reason.is_some(),
        };
        if !valid {
            return Err(fail("Rewrite admission contradicts publication or refusal"));
        }
        let refusal_history = if outcome == AdmissionOutcome::Refused {
            let descriptor =
                history::Snapshot::parse(&v["refusal_history"], &review.before.selection.identity)?;
            if descriptor.read_only || descriptor.pending || !descriptor.materialized {
                return Err(fail(
                    "Refused rewrite lacks coherent writable current history",
                ));
            }
            Some(descriptor)
        } else {
            if v.get("refusal_history") != Some(&Value::Null) {
                return Err(fail("Only a refused rewrite may publish refusal history"));
            }
            None
        };
        Ok(Self {
            outcome,
            operation_id: operation.into(),
            action_id: review.action_id.clone(),
            mutated,
            close_required,
            reason,
            prepared_context_sha256: review.prepared_context_sha256.clone(),
            prepared_messages: review.prepared_messages,
            snapshot_written,
            readback_verified,
            refusal_history,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Settlement {
    pub outcome: AdmissionOutcome,
    pub history: history::Snapshot,
    pub execution_segments: u64,
    pub last_execution_id: Option<String>,
    pub native_turn: Option<String>,
    pub fork_result: Option<(String, String)>,
}
impl Settlement {
    pub fn parse(
        v: &Value,
        admission: &Admission,
        review: &Review,
        operation: &str,
    ) -> io::Result<Self> {
        shape(v, "conversation_rewrite_settled")?;
        review.verify_receipt(v, operation)?;
        if admission.outcome == AdmissionOutcome::Failed
            || admission.operation_id != operation
            || admission.action_id != review.action_id
            || v["turn"] != admission.outcome.as_str()
            || !boolean(v, "worker_settled")?
            || boolean(v, "close_required")?
            || boolean(v, "mutated")? != admission.mutated
        {
            return Err(fail("Rewrite settlement changed its admitted execution"));
        }
        let history = history::Snapshot::parse(&v["history"], &review.before.selection.identity)?;
        if history.read_only || history.pending || !history.materialized {
            return Err(fail(
                "Rewrite settlement lacks writable saved current history",
            ));
        }
        if admission.outcome == AdmissionOutcome::Refused
            && admission.refusal_history.as_ref() != Some(&history)
        {
            return Err(fail(
                "Refused rewrite changed its coherent refusal checkpoint",
            ));
        }
        let execution = &v["execution"];
        let execution_segments = execution["segments"]
            .as_u64()
            .filter(|n| *n <= 1000)
            .ok_or_else(|| fail("Invalid rewrite execution segment count"))?;
        let last_execution_id = if admission.outcome == AdmissionOutcome::Refused {
            if execution_segments != 0 || execution.get("last_execution_id") != Some(&Value::Null) {
                return Err(fail("Refused rewrite cannot have executed a segment"));
            }
            None
        } else {
            if execution_segments == 0 {
                return Err(fail("Accepted rewrite omitted its executed segments"));
            }
            Some(uuid(execution, "last_execution_id")?)
        };
        if v.get("native_rewind").is_some() && v.get("native_fork").is_some() {
            return Err(fail("Unexpected second native provider history receipt"));
        }
        let mut fork_result = None;
        let native_turn = match (&review.native_rewind, admission.outcome) {
            (Some(plan), AdmissionOutcome::Accepted) if execution_segments == 1 => {
                let record = &v[plan.operation()];
                let turn = plan.receipt(record)?;
                if plan.fork() {
                    fork_result = Some((
                        native_id(record, "session_id")?,
                        native_id(record, "result_id")?,
                    ));
                    None
                } else {
                    Some(turn)
                }
            }
            (Some(plan), AdmissionOutcome::Refused)
                if v.get(plan.operation()) == Some(&Value::Null) =>
            {
                None
            }
            (None, _) if v.get("native_rewind").is_none() && v.get("native_fork").is_none() => None,
            _ => {
                return Err(fail(
                    "Missing or unexpected native history replacement receipt",
                ));
            }
        };
        Ok(Self {
            native_turn,
            fork_result,
            outcome: admission.outcome,
            history,
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
        outcome: AdmissionOutcome,
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
    action: Option<Action>,
    snapshot: Option<Snapshot>,
    review: Option<Review>,
    draft: Draft,
    initialized: bool,
    area: Rect,
    page: usize,
    pages: usize,
    pub notice: String,
}
impl View {
    pub fn open(&mut self, action: Action, selection: Selection) -> io::Result<()> {
        selection.verify()?;
        if self.waiting {
            return Err(fail("A conversation rewrite action is still pending"));
        }
        self.open = true;
        self.action = Some(action);
        self.selection = Some(selection);
        self.snapshot = None;
        self.review = None;
        self.draft = Draft::default();
        self.initialized = false;
        self.submitted = false;
        self.applying = false;
        self.refresh();
        Ok(())
    }
    fn begin(&mut self, phase: Phase) -> Option<RequestKey> {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice = "Rewrite request identity exhausted; reopen the runtime.".into();
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
        self.intent = Some(Intent::Read {
            key,
            action: self.action.expect("open action"),
        });
        self.notice = "Reading the exact saved user message and source eligibility…".into();
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
            return Err(fail("Expected a confirmed rewrite Apply"));
        };
        if self.pending.as_ref() != Some(key) || !self.waiting || !self.applying || self.submitted {
            return Err(fail("Rewrite Apply no longer belongs to this control"));
        }
        self.review
            .as_ref()
            .filter(|r| &r.token == review && r.before.selection == key.selection)
            .ok_or_else(|| fail("Rewrite Apply changed its visible source review"))
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if self.pending.as_ref() != Some(update.key()) {
            return Ok(());
        }
        match &update {
            Update::Submitted { .. } => {
                if !self.applying || self.submitted {
                    return Err(fail("Unexpected rewrite submission handoff"));
                }
                self.submitted = true;
                self.open = false;
                self.intent = None;
                self.hide();
                self.notice="Rewrite dispatched; live approvals and Stop own the turn. Waiting for verified settlement.".into();
                return Ok(());
            }
            Update::Settled {
                outcome, reason, ..
            } => {
                if !self.applying || !self.submitted || *outcome == AdmissionOutcome::Failed {
                    return Err(fail(
                        "Rewrite settled without a dispatched verified execution",
                    ));
                }
                let notice = match outcome {
                    AdmissionOutcome::Accepted => {
                        "Rewrite turn settled; saved history and current context verified."
                            .to_owned()
                    }
                    AdmissionOutcome::Refused => format!(
                        "Rewrite was not admitted; current saved history verified. {}",
                        reason.as_deref().unwrap_or("Prompt refused")
                    ),
                    AdmissionOutcome::Failed => unreachable!(),
                };
                self.finish();
                self.notice = notice;
                return Ok(());
            }
            Update::Rejected { .. } if self.submitted => {
                return Err(fail("Dispatched rewrite requires source settlement"));
            }
            Update::Snapshot { snapshot, key } => {
                if self.pending_phase != Some(Phase::Read)
                    || self.applying
                    || snapshot.selection != key.selection
                {
                    return Err(fail("Rewrite snapshot belongs to another selection"));
                }
            }
            Update::Review { review, key } => {
                if self.pending_phase != Some(Phase::Validate)
                    || self.applying
                    || review.before.selection != key.selection
                    || Some(review.action) != self.action
                    || self.snapshot.as_ref() != Some(&review.before)
                    || review.text
                        != if review.action == Action::Retry {
                            review.before.original_text.as_str()
                        } else {
                            self.draft.text()
                        }
                {
                    return Err(fail("Rewrite review belongs to another message or edit"));
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
                if !self.initialized {
                    self.draft.set(&snapshot.original_text)?;
                    self.initialized = true;
                }
                self.notice = if let Some(reason) = &snapshot
                    .eligibility(self.action.expect("open action"))
                    .reason
                {
                    reason.clone()
                } else if self.action == Some(Action::Edit) {
                    "Edit this message. Enter reviews; Shift+Enter adds a line.".into()
                } else {
                    "Enter reviews retrying this exact saved message.".into()
                };
                self.snapshot = Some(*snapshot);
                self.review = None;
            }
            Update::Review { review, .. } => {
                self.review = Some(*review);
                self.notice = "Read every page, then explicitly confirm sending once.".into();
            }
            Update::Rejected { reason, .. } => {
                self.snapshot = None;
                self.review = None;
                self.notice =
                    format!("{reason} · edit kept; close and refresh history if it changed.");
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
            && self.action == Some(Action::Edit)
            && self.snapshot.as_ref().is_some_and(|s| s.edit.eligible)
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
                self.notice = "Review cancelled; edit kept.".into();
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
                            "Dispatching the reviewed message; Esc hides this control only.".into();
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
                let action = self.action.expect("action");
                if !snapshot.eligibility(action).eligible {
                    self.notice = snapshot
                        .eligibility(action)
                        .reason
                        .clone()
                        .unwrap_or_default();
                    return;
                }
                let message = if action == Action::Retry {
                    snapshot.original_text.clone()
                } else {
                    self.draft.text().into()
                };
                if let Err(error) = validate_text(&message) {
                    self.notice = error.to_string();
                    return;
                }
                let snapshot = snapshot.token.clone();
                if let Some(key) = self.begin(Phase::Validate) {
                    self.intent = Some(Intent::Validate {
                        key,
                        snapshot,
                        action,
                        message,
                    });
                    self.notice =
                        "Reviewing exact replacement and discarded later messages…".into();
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
                .title(" Edit / retry saved message ")
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
            format!(
                "Confirm {} and send once\nSession: {}\nBackend: {} · model: {}\nReplace user message {} and discard {} later messages from model context.\nOriginal transcript records stay on disk. Earlier tool effects are not undone.\nRun prompt hooks: {}\n{}A new provider turn will execute. {}\nTools requested in the background, or configured to auto-promote to the background, will refuse before starting.\nYour composer draft stays unchanged.\n\nExact message to send:\n{}",
                review.action.as_str(),
                review.before.selection.identity.session,
                display(&review.before.selection.identity.backend),
                display(&review.before.selection.identity.model),
                review.before.selection.message_index + 1,
                review.before.discarded_messages,
                review.prompt_hooks,
                if review.prompt_hooks {
                    "Hooks that need a new approval will refuse this edit/retry. Approvals during the new turn remain available.\n"
                } else {
                    ""
                },
                review.native_rewind.as_ref().map_or_else(
                    || "Provider history is not rewound.".to_owned(),
                    |plan| plan.description()
                ),
                display(&review.text)
            )
        } else if let Some(snapshot) = &self.snapshot {
            let header = format!(
                "{} user message {} · {} later messages\n{}\n\n",
                self.action.expect("action").as_str(),
                snapshot.selection.message_index + 1,
                snapshot.discarded_messages,
                display(&self.notice)
            );
            let text = if self.action == Some(Action::Edit) {
                let cursor = self.draft.cursor();
                let before = display(&self.draft.text()[..cursor]);
                caret_line = Some(
                    crate::ui::wrap(&(header.clone() + &before), body.width as usize)
                        .len()
                        .saturating_sub(1),
                );
                format!("{before}▏{}", display(&self.draft.text()[cursor..]))
            } else {
                display(&snapshot.original_text)
            };
            format!("{header}{text}")
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
            "PgUp/PgDn review · Enter send · Esc back"
        } else if self.action == Some(Action::Edit) {
            "Enter review · Shift+Enter line · Esc close"
        } else {
            "Enter review exact retry · Esc close"
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

/// Retirement proof only. A successful shutdown never proves rewrite effects.
pub fn verify_shutdown(
    result: &Value,
    dispatched: Option<&Value>,
    current_session: &str,
) -> io::Result<bool> {
    let proof = &result["conversation_rewrite_shutdown"];
    if proof["version"] != 1
        || proof["current_session_id"] != current_session
        || !boolean(proof, "worker_settled")?
        || !boolean(proof, "admission_settled")?
        || !boolean(proof, "hooks_settled")?
        || boolean(proof, "effects_verified")?
    {
        return Err(fail("Shutdown lacks settled rewrite lifecycle proof"));
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
            || !matches!(command["action"].as_str(), Some("edit" | "retry"))
        {
            return Err(fail(
                "Shutdown expected an actual dispatched rewrite command",
            ));
        }
        uuid(command, "action_id")?;
        let operation = text(command, "id", 128, false)?;
        if proof["operation_id"] != operation
            || fields.iter().any(|field| proof[*field] != command[*field])
            || pending && command["session_id"] != current_session
        {
            return Err(fail("Shutdown changed the tracked rewrite identity"));
        }
    } else if pending
        || mutated
        || proof.get("operation_id") != Some(&Value::Null)
        || fields
            .iter()
            .any(|field| proof.get(*field) != Some(&Value::Null))
    {
        return Err(fail("Shutdown invented an undispatched rewrite action"));
    }
    Ok(pending)
}
