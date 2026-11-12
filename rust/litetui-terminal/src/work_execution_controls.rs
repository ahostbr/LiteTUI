//! Explicit reviewed execution. Receipts are evidence, never replay or turn authority.
use crate::{
    deferred_control::strict_object,
    scheduled_transition::{boolean, digest, local_minute},
    work_controls, work_lifecycle as life,
    work_protocol::{Row, Snapshot},
};
use life::{bad, bounded, hash, keys, number, optional, optional_uuid, text, uuid};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, io};

pub const VALIDATE: &str = "gui.work.native_execution_validate";
pub const APPLY: &str = "gui.work.native_execution_apply";
pub const READBACK: &str = "gui.work.native_execution_readback";
pub const STOP: &str = "gui.work.native_execution_stop";
pub const OPERATIONS: [&str; 4] = [VALIDATE, APPLY, READBACK, STOP];
const BASE: &[&str] = &[
    "version",
    "execution_version",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "work_instance",
];
const ENVELOPE: usize = 1_048_576;
const DOCUMENT: usize = 524_288;
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Capabilities(());
impl Capabilities {
    pub fn negotiate(operations: &[String]) -> io::Result<Option<Self>> {
        let counts = OPERATIONS.map(|op| operations.iter().filter(|s| *s == op).count());
        if counts == [0; 4] {
            return Ok(None);
        }
        if counts != [1; 4]
            || [life::CAPTURE, life::ACTIVATE, life::SETTLED]
                .iter()
                .any(|op| operations.iter().filter(|v| *v == op).count() != 1)
        {
            return Err(bad("Incomplete Work execution capability negotiation"));
        }
        Ok(Some(Self(())))
    }
    pub fn capture_request(&self, identity: &life::Identity) -> Value {
        let mut v = identity.request(life::CAPTURE);
        v["execution_version"] = json!(1);
        v
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    JobRun,
    GoalStart,
    GoalResume,
}
impl Action {
    pub fn key(self) -> &'static str {
        match self {
            Self::JobRun => "job_run",
            Self::GoalStart => "goal_start",
            Self::GoalResume => "goal_resume",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::JobRun => "Run now",
            Self::GoalStart => "Start goal",
            Self::GoalResume => "Resume goal",
        }
    }
    fn parse(v: &Value) -> io::Result<Self> {
        match v.as_str() {
            Some("job_run") => Ok(Self::JobRun),
            Some("goal_start") => Ok(Self::GoalStart),
            Some("goal_resume") => Ok(Self::GoalResume),
            _ => Err(bad("Invalid execution action")),
        }
    }
}
fn objective_trim(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}'))
}
fn sha(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn opt_hash(v: &Value, k: &str) -> io::Result<Option<String>> {
    if v.get(k) == Some(&Value::Null) {
        Ok(None)
    } else {
        hash(v, k).map(Some)
    }
}
fn revision(v: &Value, k: &str) -> io::Result<String> {
    if v.get(k) == Some(&json!("absent")) {
        Ok("absent".into())
    } else {
        hash(v, k)
    }
}
fn identity(v: &Value) -> io::Result<life::Identity> {
    life::Identity::parse(
        &json!({"session_id":v["session_id"],"agent_id":v["agent_id"],"backend":v["backend"],"model":v["model"]}),
    )
}
fn base(
    v: &Value,
    expected: &life::Identity,
    instance: &str,
    kind: &str,
    fields: &[&str],
) -> io::Result<()> {
    bounded(v, ENVELOPE)?;
    let mut all = BASE.to_vec();
    all.push("kind");
    all.extend_from_slice(fields);
    keys(v, &all)?;
    if number(v, "version", true)? != 1
        || number(v, "execution_version", true)? != 1
        || identity(v)? != *expected
        || hash(v, "work_instance")? != instance
        || v["kind"] != kind
    {
        return Err(bad("Execution identity/version/instance changed"));
    }
    Ok(())
}
fn request(identity: &life::Identity, instance: &str, op: &str) -> Value {
    let mut v = identity.request(op);
    v["execution_version"] = json!(1);
    v["work_instance"] = json!(instance);
    v
}
fn projected(v: &Value, fields: &[&str]) -> io::Result<Value> {
    let m = v
        .as_object()
        .ok_or_else(|| bad("Saved execution row is not an object"))?;
    let mut out = serde_json::Map::new();
    for k in fields {
        out.insert(
            (*k).into(),
            m.get(*k)
                .ok_or_else(|| bad("Saved execution row lacks a known field"))?
                .clone(),
        );
    }
    Ok(Value::Object(out))
}
pub(crate) fn canonical(v: &Value, action: Action) -> io::Result<Value> {
    let fields = if action == Action::JobRun {
        work_controls::JOB
    } else {
        work_controls::GOAL
    };
    let mut defaults = if action == Action::JobRun {
        json!({"enabled":true,"new_conversation":false,"last_fired_slot":null,"run_count":0,"label":"","tool_profile":"interactive","kind":"cron","owner_convo_id":"","interval_minutes":0,"next_run_at":null,"owner_agent_id":"","deferred_slot":null})
    } else {
        json!({"status":"active","evaluated_turns":0,"max_turns":0,"last_verdict":"","last_reason":"","missing_evidence":[],"no_progress_count":0,"tool_profile":"interactive","started_by":""})
    };
    let map = v
        .as_object()
        .ok_or_else(|| bad("Saved execution row is not an object"))?;
    for key in fields {
        if let Some(value) = map.get(*key) {
            defaults[*key] = value.clone();
        }
    }
    projected(&defaults, fields)
}
fn saved(v: &Value, action: Action) -> io::Result<()> {
    bounded(v, 131_072)?;
    let value = canonical(v, action)?;
    if action == Action::JobRun {
        work_controls::job(&value)
    } else {
        work_controls::goal(&value)
    }
}

pub(crate) fn second(s: &str) -> io::Result<i64> {
    if !s.is_ascii()
        || s.len() != 19
        || s.as_bytes()[16] != b':'
        || !s.as_bytes()[17..].iter().all(u8::is_ascii_digit)
    {
        return Err(bad("Execution time is not canonical local seconds"));
    }
    let n = s[17..]
        .parse::<i64>()
        .map_err(|_| bad("Invalid execution seconds"))?;
    if n >= 60 {
        return Err(bad("Invalid execution seconds"));
    }
    Ok(local_minute(&s[..16])? * 60 + n)
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raw {
    pub json: Option<String>,
    pub sha256: Option<String>,
    pub value: Value,
}
impl Raw {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(v, &["json", "sha256"])?;
        let s = optional(v, "json", 131_072)?;
        let h = opt_hash(v, "sha256")?;
        match (&s, &h) {
            (None, None) => Ok(Self {
                json: s,
                sha256: h,
                value: Value::Null,
            }),
            (Some(raw), Some(expected)) if sha(raw) == *expected => {
                let value = strict_object(raw)?;
                if serde_json::to_string(&value)? != *raw {
                    return Err(bad("Execution raw document is not compact sorted JSON"));
                }
                Ok(Self {
                    value,
                    json: s,
                    sha256: h,
                })
            }
            _ => Err(bad("Execution raw document hash/absence changed")),
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Edit {
    pub snapshot: Snapshot,
    pub action: Action,
    pub selected_id: Option<String>,
    pub target_revision: String,
    pub section: String,
    pub draft: Value,
    pub before: Value,
}
impl Edit {
    pub fn new(
        snapshot: &Snapshot,
        action: Action,
        row: Option<&Row>,
        draft: Value,
    ) -> io::Result<Self> {
        if snapshot.busy || snapshot.quitting {
            return Err(bad("Work execution requires a quiet current inspection"));
        }
        let (selected_id, target_revision, section, before) = match action {
            Action::JobRun => {
                let r = row.ok_or_else(|| bad("Run requires an inspected job"))?;
                saved(&r.raw, action)?;
                if snapshot.paused || r.raw["new_conversation"] != false || r.raw["id"] != r.id {
                    return Err(bad("Run requires an unpaused same-conversation job"));
                }
                let section = match r.raw["kind"].as_str() {
                    Some("cron") => "jobs",
                    Some("loop") if r.raw["owner_convo_id"] == snapshot.identity.session => "loops",
                    _ => return Err(bad("Run selected a foreign loop")),
                };
                (
                    Some(r.id.clone()),
                    r.revision.clone(),
                    section.to_owned(),
                    r.raw.clone(),
                )
            }
            _ => {
                if row.is_some() {
                    return Err(bad("Goal execution cannot use a job row"));
                }
                let g = &snapshot.goal;
                match g["status"].as_str() {
                    Some("present") => {
                        saved(&g["value"], action)?;
                        (
                            Some(text(&g["value"], "id", 128)?),
                            revision(g, "revision")?,
                            "goal".into(),
                            g["value"].clone(),
                        )
                    }
                    Some("absent") if action == Action::GoalStart => {
                        (None, "absent".into(), "goal".into(), Value::Null)
                    }
                    _ => return Err(bad("Goal execution requires an available inspected goal")),
                }
            }
        };
        if action == Action::GoalStart {
            keys(&draft, &["objective"])?;
            if objective_trim(&text(&draft, "objective", 65_536)?).is_empty() {
                return Err(bad("Goal objective is empty"));
            }
        } else {
            keys(&draft, &[])?;
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            action,
            selected_id,
            target_revision,
            section,
            draft,
            before,
        })
    }
    pub fn validate_request(&self, work_instance: &str) -> io::Result<Value> {
        hash(&json!({"i":work_instance}), "i")?;
        let mut v = request(
            &life::Identity {
                session: self.snapshot.identity.session.clone(),
                agent: self.snapshot.identity.agent.clone(),
                backend: self.snapshot.identity.backend.clone(),
                model: self.snapshot.identity.model.clone(),
            },
            work_instance,
            VALIDATE,
        );
        v["inspection_instance"] = json!(self.snapshot.instance);
        v["snapshot"] = json!(self.snapshot.token);
        v["action"] = json!(self.action.key());
        v["section"] = json!(self.section);
        v["target_id"] = json!(self.selected_id);
        v["target_revision"] = json!(self.target_revision);
        v["draft"] = self.draft.clone();
        bounded(&v, ENVELOPE)?;
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub edit: Edit,
    pub action: Action,
    pub action_id: String,
    pub token: String,
    pub target_id: String,
    pub work_instance: String,
    pub provider: life::Provider,
    pub identity: life::Identity,
    pub before: Raw,
    pub after_template: Raw,
    pub payload: life::Payload,
    pub producer_generation: u64,
    pub wire: Value,
}
impl Review {
    pub fn parse(
        v: &Value,
        edit: &Edit,
        work_instance: &str,
        provider: life::Provider,
    ) -> io::Result<Self> {
        let expected = identity(&edit.validate_request(work_instance)?)?;
        base(
            v,
            &expected,
            work_instance,
            "work_execution_review",
            &[
                "inspection_instance",
                "snapshot",
                "action",
                "action_id",
                "review_token",
                "section",
                "selected_id",
                "target_revision",
                "target_id",
                "draft",
                "before",
                "after_template",
                "time_fields",
                "payload",
                "payload_sha256",
                "producer_generation",
                "started_by",
                "materialize",
                "previous_receipt_sha256",
                "context_sha256",
                "consequence",
            ],
        )?;
        if hash(v, "inspection_instance")? != edit.snapshot.instance
            || hash(v, "snapshot")? != edit.snapshot.token
            || v["action"] != edit.action.key()
            || v["section"] != edit.section
            || optional(v, "selected_id", 128)? != edit.selected_id
            || revision(v, "target_revision")? != edit.target_revision
        {
            return Err(bad("Execution review changed the selected inspection"));
        }
        let mut draft = edit.draft.clone();
        if edit.action == Action::GoalStart {
            draft["objective"] = json!(objective_trim(&text(&draft, "objective", 65_536)?));
        }
        if v["draft"] != draft {
            return Err(bad("Execution review changed the literal objective draft"));
        }
        let before = Raw::parse(&v["before"])?;
        let after_template = Raw::parse(&v["after_template"])?;
        if (if edit.action == Action::JobRun {
            canonical(&before.value, edit.action)? != edit.before
        } else if before.value.is_null() {
            !edit.before.is_null()
        } else {
            canonical(&before.value, edit.action)? != edit.before
        }) || after_template.value.is_null()
        {
            return Err(bad("Execution review changed exact saved row"));
        }
        if edit.action == Action::JobRun {
            let raw = &edit.before;
            let stored = raw["tool_profile"].as_str().unwrap_or("");
            let effective = if matches!(stored, "strict" | "interactive" | "autonomous") {
                stored
            } else {
                "interactive"
            };
            if digest(&json!({"kind":raw["kind"],"effective_tool_profile":effective,"row":raw}))?
                != edit.target_revision
            {
                return Err(bad("Run review changed selected inspection revision"));
            }
        }
        boolean(v, "materialize")?;
        opt_hash(v, "previous_receipt_sha256")?;
        hash(v, "context_sha256")?;
        text(v, "consequence", 4096)?;
        let payload = payload(&v["payload"], &hash(v, "payload_sha256")?)?;
        let r = Self {
            edit: edit.clone(),
            action: edit.action,
            action_id: uuid(v, "action_id")?,
            token: hash(v, "review_token")?,
            target_id: text(v, "target_id", 128)?,
            work_instance: work_instance.into(),
            provider,
            identity: expected,
            before,
            after_template,
            payload,
            producer_generation: number(v, "producer_generation", true)?,
            wire: v.clone(),
        };
        r.verify_template()?;
        if serde_json::to_vec(&r.wire)?.len()
            + serde_json::to_vec(&r.wire["payload"])?.len()
            + 16_384
            > DOCUMENT
        {
            return Err(bad(
                "Execution review cannot fit its complete launch receipt",
            ));
        }
        Ok(r)
    }
    pub fn request(&self) -> Value {
        json!({"type":APPLY,"review":self.wire})
    }
    pub fn stop_request(&self, apply_id: &str) -> io::Result<Value> {
        text(&json!({"id":apply_id}), "id", 128)?;
        let mut v = request(&self.identity, &self.work_instance, STOP);
        v["action_id"] = json!(self.action_id);
        v["review_token"] = json!(self.token);
        v["apply_operation_id"] = json!(apply_id);
        Ok(v)
    }
    pub fn detail_lines(&self) -> Vec<String> {
        serde_json::to_string_pretty(&self.wire)
            .expect("review JSON")
            .lines()
            .map(str::to_owned)
            .collect()
    }
    fn verify_template(&self) -> io::Result<()> {
        let v = &self.wire;
        let raw_before = &self.before.value;
        let canonical_before = if raw_before.is_null() {
            Value::Null
        } else {
            canonical(raw_before, self.action)?
        };
        let before = &canonical_before;
        let after = &self.after_template.value;
        let mut expected = raw_before.clone();
        let (times, content, source, profile) = match self.action {
            Action::JobRun => {
                for (key, value) in before.as_object().expect("canonical job") {
                    expected[key] = value.clone();
                }
                if before["new_conversation"] != false
                    || before["id"] != self.target_id
                    || !v["started_by"].is_null()
                {
                    return Err(bad("Run changed target or conversation semantics"));
                }
                let count = number(before, "run_count", false)?
                    .checked_add(1)
                    .ok_or_else(|| bad("Run generation overflow"))?;
                if self.producer_generation != count {
                    return Err(bad("Run generation changed"));
                }
                expected["run_count"] = json!(count);
                expected["last_fired_slot"] = Value::Null;
                let times = if before["kind"] == "loop" {
                    if before["owner_convo_id"] != self.identity.session {
                        return Err(bad("Run loop owner changed"));
                    }
                    expected["next_run_at"] = Value::Null;
                    json!(["last_fired_slot", "next_run_at"])
                } else {
                    json!(["last_fired_slot"])
                };
                let p = before["tool_profile"].as_str().unwrap_or("");
                (
                    times,
                    text(before, "prompt", 65_536)?,
                    "scheduled",
                    if matches!(p, "strict" | "interactive" | "autonomous") {
                        p
                    } else {
                        "interactive"
                    },
                )
            }
            Action::GoalStart => {
                if self.target_id.len() != 12
                    || !self
                        .target_id
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    || self.edit.selected_id.as_deref() == Some(self.target_id.as_str())
                {
                    return Err(bad("New goal identity is not fresh canonical source ID"));
                }
                keys(after, work_controls::GOAL)?;
                let started = text(v, "started_by", 4096)?;
                let p = text(after, "tool_profile", 4096)?;
                crate::tool_mode::Profile::parse(&p)?;
                expected = json!({"objective":v["draft"]["objective"],"id":self.target_id,"status":"active","created":null,"updated":null,"evaluated_turns":0,"max_turns":0,"last_verdict":"","last_reason":"","missing_evidence":[],"no_progress_count":0,"tool_profile":p,"started_by":started});
                let content = format!(
                    "[goal {}]\nObjective: {}\nWork autonomously until the transcript contains verifiable completion evidence.",
                    self.target_id,
                    v["draft"]["objective"].as_str().unwrap_or("")
                );
                (
                    json!(["created", "updated"]),
                    content,
                    if matches!(v["started_by"].as_str(), Some("typed" | "gui")) {
                        "goal-owner"
                    } else {
                        "goal"
                    },
                    after["tool_profile"].as_str().unwrap_or(""),
                )
            }
            Action::GoalResume => {
                if before["id"] != self.target_id
                    || v["started_by"] != before["started_by"]
                    || self.producer_generation <= number(before, "evaluated_turns", false)?
                {
                    return Err(bad("Goal resume changed origin or generation"));
                }
                expected["status"] = json!("active");
                expected["updated"] = Value::Null;
                let content = format!(
                    "[goal {}]\nObjective: {}\nResume from the existing transcript evidence.",
                    self.target_id,
                    text(before, "objective", 65_536)?
                );
                (
                    json!(["updated"]),
                    content,
                    if matches!(v["started_by"].as_str(), Some("typed" | "gui")) {
                        "goal-owner"
                    } else {
                        "goal"
                    },
                    before["tool_profile"].as_str().unwrap_or(""),
                )
            }
        };
        if v["time_fields"] != times
            || *after != expected
            || self.payload.content != content
            || self.payload.source != source
            || v["payload"]["tool_profile"] != profile
        {
            return Err(bad("Execution review changed canonical effects/payload"));
        }
        let mut filled = after.clone();
        for k in times.as_array().expect("time array") {
            let k = k.as_str().expect("time key");
            filled[k] = json!(if k == "last_fired_slot" {
                "2000-01-01T00:00"
            } else {
                "2000-01-01T00:00:00"
            });
        }
        saved(&filled, self.action)
    }
    fn verify_after(&self, after: &Value) -> io::Result<()> {
        saved(after, self.action)?;
        let mut template = after.clone();
        for k in self.wire["time_fields"]
            .as_array()
            .expect("validated time fields")
        {
            let k = k.as_str().expect("time key");
            let s = text(after, k, 128)?;
            if k == "last_fired_slot" {
                local_minute(&s)?;
            } else {
                second(&s)?;
            }
            template[k] = Value::Null;
        }
        if template != self.after_template.value {
            return Err(bad("Launch changed an unreviewed saved field"));
        }
        if self.action == Action::GoalStart && after["created"] != after["updated"] {
            return Err(bad("Goal start used different clocks"));
        }
        if self.action == Action::JobRun && after["kind"] == "loop" {
            let next = second(&text(after, "next_run_at", 128)?)? / 60;
            let slot = local_minute(&text(after, "last_fired_slot", 128)?)?;
            let interval = i64::try_from(number(after, "interval_minutes", true)?)
                .map_err(|_| bad("Loop interval overflow"))?;
            if next.checked_sub(interval) != Some(slot) {
                return Err(bad("Manual loop cadence does not match its firing clock"));
            }
        }
        Ok(())
    }
}
fn payload(v: &Value, h: &str) -> io::Result<life::Payload> {
    keys(v, &["content", "text", "source", "tool_profile"])?;
    let source = text(v, "source", 128)?;
    if !matches!(source.as_str(), "scheduled" | "goal" | "goal-owner") || v["content"] != v["text"]
    {
        return Err(bad("Invalid execution payload semantics"));
    }
    life::Payload::evidence(v, &source, life::profile(v, "tool_profile")?, Some(h))
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Document {
    pub identity: life::Identity,
    pub work_instance: String,
    pub provider: life::Provider,
    pub action: Action,
    pub action_id: String,
    pub apply_operation_id: String,
    pub target_id: String,
    pub before: Raw,
    pub after: Raw,
    pub producer: life::Producer,
    pub input: life::Input,
    pub payload: life::Payload,
    pub raw: String,
    pub sha256: String,
    pub wire: Value,
}
impl Document {
    fn parse(raw: &str, h: &str) -> io::Result<Self> {
        if raw.len() > DOCUMENT || sha(raw) != h {
            return Err(bad("Execution receipt file hash/budget changed"));
        }
        let v = strict_object(raw)?;
        keys(
            &v,
            &[
                "version",
                "kind",
                "identity",
                "work_instance",
                "provider",
                "action_id",
                "apply_operation_id",
                "review_sha256",
                "action",
                "target_id",
                "previous_receipt_sha256",
                "before",
                "after",
                "store_revision",
                "payload",
                "payload_sha256",
                "producer",
                "input",
            ],
        )?;
        if number(&v, "version", true)? != 1 || v["kind"] != "work_execution_launch" {
            return Err(bad("Invalid execution receipt version/kind"));
        }
        let action = Action::parse(&v["action"])?;
        let before = Raw::parse(&v["before"])?;
        let after = Raw::parse(&v["after"])?;
        if !before.value.is_null() {
            saved(&before.value, action)?;
        } else if action != Action::GoalStart {
            return Err(bad("Execution receipt lacks required prior row"));
        }
        saved(&after.value, action)?;
        let identity = life::Identity::parse(&v["identity"])?;
        let producer = life::Producer::parse(&v["producer"])?;
        let input = life::Input::parse(&v["input"])?;
        let payload = payload(&v["payload"], &hash(&v, "payload_sha256")?)?;
        let target_id = text(&v, "target_id", 128)?;
        let kind = match action {
            Action::JobRun if after.value["kind"] == "loop" => life::ProducerKind::Loop,
            Action::JobRun => life::ProducerKind::Cron,
            _ => life::ProducerKind::Goal,
        };
        if after.value["id"] != target_id
            || producer.kind != kind
            || producer.object != target_id
            || producer.session != identity.session
            || input.producer != producer.id
            || input.session != identity.session
            || input.payload_sha256 != hash(&v, "payload_sha256")?
            || input.source != payload.source
            || input.requested != payload.profile
        {
            return Err(bad(
                "Execution receipt changed producer/input/payload ownership",
            ));
        }
        if action == Action::JobRun
            && (after.value["new_conversation"] != false
                || producer.generation != number(&after.value, "run_count", true)?
                || kind == life::ProducerKind::Loop
                    && after.value["owner_convo_id"] != identity.session)
        {
            return Err(bad("Execution receipt changed manual job generation/owner"));
        }
        if action != Action::JobRun
            && producer.generation
                <= number(&canonical(&after.value, action)?, "evaluated_turns", false)?
        {
            return Err(bad("Execution receipt goal generation regressed"));
        }
        hash(&v, "review_sha256")?;
        hash(&v, "store_revision")?;
        opt_hash(&v, "previous_receipt_sha256")?;
        let document = Self {
            identity,
            work_instance: hash(&v, "work_instance")?,
            provider: life::Provider::parse(&v, "provider")?,
            action,
            action_id: uuid(&v, "action_id")?,
            apply_operation_id: text(&v, "apply_operation_id", 128)?,
            target_id,
            before,
            after,
            producer,
            input,
            payload,
            raw: raw.into(),
            sha256: h.into(),
            wire: v,
        };
        document.verify_effects()?;
        Ok(document)
    }
    fn verify_effects(&self) -> io::Result<()> {
        let after = &self.after.value;
        let before = if self.before.value.is_null() {
            Value::Null
        } else {
            canonical(&self.before.value, self.action)?
        };
        let mut expected = self.before.value.clone();
        let (content, source, profile) = match self.action {
            Action::JobRun => {
                for (k, v) in before.as_object().expect("validated prior job") {
                    expected[k] = v.clone();
                }
                expected["run_count"] = json!(
                    number(&before, "run_count", false)?
                        .checked_add(1)
                        .ok_or_else(|| bad("Historical job generation overflow"))?
                );
                expected["last_fired_slot"] = after["last_fired_slot"].clone();
                let slot = local_minute(&text(after, "last_fired_slot", 128)?)?;
                if before["kind"] == "loop" {
                    expected["next_run_at"] = after["next_run_at"].clone();
                    let next = second(&text(after, "next_run_at", 128)?)? / 60;
                    let interval = i64::try_from(number(&before, "interval_minutes", true)?)
                        .map_err(|_| bad("Historical loop interval overflow"))?;
                    if next.checked_sub(interval) != Some(slot) {
                        return Err(bad("Historical loop receipt changed cadence"));
                    }
                }
                let p = before["tool_profile"].as_str().unwrap_or("");
                (
                    text(&before, "prompt", 65_536)?,
                    "scheduled",
                    if matches!(p, "strict" | "interactive" | "autonomous") {
                        p
                    } else {
                        "interactive"
                    }
                    .to_owned(),
                )
            }
            Action::GoalStart => {
                keys(after, work_controls::GOAL)?;
                if self.target_id.len() != 12
                    || !self
                        .target_id
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    || before["id"] == self.target_id
                {
                    return Err(bad(
                        "Historical new goal identity is not fresh canonical source ID",
                    ));
                }
                let objective = text(after, "objective", 65_536)?;
                if objective_trim(&objective) != objective {
                    return Err(bad(
                        "Historical goal objective changed canonical normalization",
                    ));
                }
                let p = life::profile(after, "tool_profile")?.as_str().to_owned();
                expected = json!({"id":self.target_id,"objective":objective,"status":"active","created":after["created"],"updated":after["updated"],"evaluated_turns":0,"max_turns":0,"last_verdict":"","last_reason":"","missing_evidence":[],"no_progress_count":0,"tool_profile":p,"started_by":after["started_by"]});
                second(&text(after, "created", 128)?)?;
                second(&text(after, "updated", 128)?)?;
                if after["created"] != after["updated"] {
                    return Err(bad("Historical goal start used different clocks"));
                }
                (
                    format!(
                        "[goal {}]\nObjective: {}\nWork autonomously until the transcript contains verifiable completion evidence.",
                        self.target_id, objective
                    ),
                    if matches!(after["started_by"].as_str(), Some("typed" | "gui")) {
                        "goal-owner"
                    } else {
                        "goal"
                    },
                    p,
                )
            }
            Action::GoalResume => {
                expected["status"] = json!("active");
                expected["updated"] = after["updated"].clone();
                second(&text(after, "updated", 128)?)?;
                (
                    format!(
                        "[goal {}]\nObjective: {}\nResume from the existing transcript evidence.",
                        self.target_id,
                        text(&before, "objective", 65_536)?
                    ),
                    if matches!(before["started_by"].as_str(), Some("typed" | "gui")) {
                        "goal-owner"
                    } else {
                        "goal"
                    },
                    text(&before, "tool_profile", 4096)?,
                )
            }
        };
        if expected != *after
            || self.payload.content != content
            || self.payload.source != source
            || self.payload.profile.as_str() != profile
        {
            return Err(bad(
                "Historical execution receipt changed canonical effects or payload",
            ));
        }
        Ok(())
    }

    fn verify(&self, r: &Review, apply_id: &str) -> io::Result<()> {
        if self.producer.action.as_deref() == Some(r.action_id.as_str())
            || self.producer.operation.as_deref() == Some(apply_id)
        {
            return Err(bad(
                "GUI execution identity was substituted for actual Work causality",
            ));
        }
        if self.identity != r.identity
            || self.work_instance != r.work_instance
            || self.provider != r.provider
            || self.action != r.action
            || self.action_id != r.action_id
            || self.apply_operation_id != apply_id
            || self.target_id != r.target_id
            || self.wire["review_sha256"] != digest(&r.wire)?
            || self.wire["previous_receipt_sha256"] != r.wire["previous_receipt_sha256"]
            || self.before != r.before
            || self.wire["payload"] != r.wire["payload"]
            || self.producer.generation != r.producer_generation
        {
            return Err(bad(
                "Launch receipt differs from exact accepted execution review",
            ));
        }
        r.verify_after(&self.after.value)
    }
}
fn document(v: &Value, r: &Review, apply_id: &str) -> io::Result<Option<Document>> {
    match (
        optional(v, "document", DOCUMENT)?,
        opt_hash(v, "document_sha256")?,
    ) {
        (None, None) => Ok(None),
        (Some(raw), Some(h)) => {
            let d = Document::parse(&raw, &h)?;
            d.verify(r, apply_id)?;
            Ok(Some(d))
        }
        _ => Err(bad("Incomplete execution receipt file evidence")),
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OwnerState {
    Absent,
    Pending,
    Retired,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Owners {
    pub launch: OwnerState,
    pub delivery: OwnerState,
    pub admission: OwnerState,
    pub callbacks: OwnerState,
}
impl Owners {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(v, &["launch", "delivery", "admission", "callbacks"])?;
        let one = |k| match v[k].as_str() {
            Some("absent") => Ok(OwnerState::Absent),
            Some("pending") => Ok(OwnerState::Pending),
            Some("retired") => Ok(OwnerState::Retired),
            _ => Err(bad("Invalid execution owner state")),
        };
        Ok(Self {
            launch: one("launch")?,
            delivery: one("delivery")?,
            admission: one("admission")?,
            callbacks: one("callbacks")?,
        })
    }
    pub fn retired(&self) -> bool {
        [self.launch, self.delivery, self.admission, self.callbacks]
            .iter()
            .all(|s| *s != OwnerState::Pending)
    }
    fn absent(&self) -> bool {
        [self.launch, self.delivery, self.admission, self.callbacks]
            .iter()
            .all(|s| *s == OwnerState::Absent)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Lifecycle {
    pub producer_phase: Option<life::ProducerState>,
    pub input_state: Option<life::InputState>,
    pub admission: Option<life::Admission>,
    pub settlement: Option<Value>,
    pub owners: Owners,
    pub wire: Value,
}
impl Lifecycle {
    fn parse(
        v: &Value,
        identity: &life::Identity,
        instance: &str,
        provider: life::Provider,
    ) -> io::Result<Self> {
        keys(
            v,
            &[
                "producer_phase",
                "input_state",
                "admission",
                "settlement",
                "owners",
            ],
        )?;
        let producer_phase = match optional(v, "producer_phase", 32)?.as_deref() {
            None => None,
            Some("registered") => Some(life::ProducerState::Registered),
            Some("running") => Some(life::ProducerState::Running),
            Some("stop_requested") => Some(life::ProducerState::StopRequested),
            Some("retired") => Some(life::ProducerState::Retired),
            _ => return Err(bad("Invalid execution producer phase")),
        };
        let input_state = match optional(v, "input_state", 32)?.as_deref() {
            None => None,
            Some("queued") => Some(life::InputState::Queued),
            Some("claiming") => Some(life::InputState::Claiming),
            Some("admitted") => Some(life::InputState::Admitted),
            Some("held") => Some(life::InputState::Held),
            Some("uncertain") => Some(life::InputState::Uncertain),
            Some("cancelled") => Some(life::InputState::Cancelled),
            Some("settled") => Some(life::InputState::Settled),
            Some("reconciled") => Some(life::InputState::Reconciled),
            _ => return Err(bad("Invalid execution input state")),
        };
        let admission = if v["admission"].is_null() {
            None
        } else {
            Some(life::Admission::parse(&v["admission"])?)
        };
        if admission
            .as_ref()
            .is_some_and(|a| a.provider != provider || a.origin.session != identity.session)
            || input_state.is_some() && producer_phase.is_none()
            || admission.is_some()
                && !matches!(
                    input_state,
                    Some(
                        life::InputState::Admitted
                            | life::InputState::Uncertain
                            | life::InputState::Settled
                    )
                )
            || matches!(
                input_state,
                Some(life::InputState::Admitted | life::InputState::Settled)
            ) && admission.is_none()
        {
            return Err(bad(
                "Execution lifecycle references contradict current ownership",
            ));
        }
        let settlement = if v["settlement"].is_null() {
            None
        } else {
            let s = &v["settlement"];
            life::validate_execution_settlement(s, identity, instance)?;
            if admission.as_ref() != Some(&life::Admission::parse(&s["admission"])?)
                || input_state != Some(life::InputState::Settled)
            {
                return Err(bad("Execution settlement changed actual admission"));
            }
            Some(s.clone())
        };
        if input_state == Some(life::InputState::Settled) && settlement.is_none() {
            return Err(bad("Settled execution lacks exact Work proof"));
        }
        Ok(Self {
            producer_phase,
            input_state,
            admission,
            settlement,
            owners: Owners::parse(&v["owners"])?,
            wire: v.clone(),
        })
    }
    fn bind(&self, d: &Document) -> io::Result<()> {
        if self.producer_phase.is_none()
            || self.input_state.is_none()
            || self.owners.launch == OwnerState::Absent
        {
            return Err(bad(
                "Execution receipt lacks observed producer/input lifecycle",
            ));
        }
        if self.admission.as_ref().is_some_and(|a| {
            a.input != d.input.id
                || a.operation == d.apply_operation_id
                || a.requested != d.input.requested
                || a.provider != d.provider
                || a.origin.session != d.identity.session
        }) {
            return Err(bad("Execution lifecycle changed launch input/admission"));
        }
        if let Some(s) = &self.settlement
            && (life::Input::parse(&s["input"])? != d.input)
        {
            return Err(bad("Execution settlement changed launch input"));
        }
        Ok(())
    }
    fn no_effect(&self) -> bool {
        self.producer_phase.is_none()
            && self.input_state.is_none()
            && self.admission.is_none()
            && self.settlement.is_none()
            && self.owners.retired()
            && self.owners.delivery == OwnerState::Absent
            && self.owners.admission == OwnerState::Absent
            && self.owners.callbacks == OwnerState::Absent
    }
}
fn correlated(v: &Value, r: &Review, apply: &str) -> io::Result<()> {
    if uuid(v, "action_id")? != r.action_id
        || hash(v, "review_token")? != r.token
        || text(v, "apply_operation_id", 128)? != apply
    {
        return Err(bad("Execution result changed accepted invocation"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt {
    pub review: Review,
    pub apply_operation_id: String,
    pub status: String,
    pub reason: Option<String>,
    pub document: Option<Document>,
    pub lifecycle: Lifecycle,
    pub wire: Value,
}
impl Receipt {
    pub fn parse(v: &Value, r: &Review, apply: &str) -> io::Result<Self> {
        base(
            v,
            &r.identity,
            &r.work_instance,
            "work_execution_result",
            &[
                "action_id",
                "review_token",
                "apply_operation_id",
                "review_sha256",
                "action",
                "target_id",
                "status",
                "effect_attempted",
                "close_required",
                "reason",
                "document",
                "document_sha256",
                "lifecycle",
            ],
        )?;
        correlated(v, r, apply)?;
        if hash(v, "review_sha256")? != digest(&r.wire)?
            || v["action"] != r.action.key()
            || v["target_id"] != r.target_id
        {
            return Err(bad("Execution result changed review digest/action"));
        }
        let status = text(v, "status", 16)?;
        let reason = optional(v, "reason", 2048)?;
        let effect = boolean(v, "effect_attempted")?;
        let close = boolean(v, "close_required")?;
        let document = document(v, r, apply)?;
        let lifecycle =
            Lifecycle::parse(&v["lifecycle"], &r.identity, &r.work_instance, r.provider)?;
        if let Some(d) = &document {
            lifecycle.bind(d)?;
        }
        match status.as_str() {
            "refused"
                if !effect
                    && !close
                    && document.is_none()
                    && reason.is_some()
                    && lifecycle.no_effect() => {}
            "published" if effect && !close && document.is_some() && reason.is_none() => {}
            "uncertain" if effect && close && reason.is_some() => {}
            _ => {
                return Err(bad(
                    "Execution result status contradicts effect/ownership proof",
                ));
            }
        }
        Ok(Self {
            review: r.clone(),
            apply_operation_id: apply.into(),
            status,
            reason,
            document,
            lifecycle,
            wire: v.clone(),
        })
    }
    pub fn readback_request(&self) -> io::Result<Value> {
        readback_request(
            &self.review,
            &self.apply_operation_id,
            "launch",
            None,
            &self.apply_operation_id,
            &self.wire,
        )
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct StopReceipt {
    pub review: Review,
    pub apply_operation_id: String,
    pub stop_id: String,
    pub stop_operation_id: String,
    pub status: String,
    pub reason: Option<String>,
    pub lifecycle: Lifecycle,
    pub wire: Value,
}
impl StopReceipt {
    pub fn parse(v: &Value, r: &Review, apply: &str, stop: &str) -> io::Result<Self> {
        base(
            v,
            &r.identity,
            &r.work_instance,
            "work_execution_stop_result",
            &[
                "action_id",
                "review_token",
                "apply_operation_id",
                "stop_id",
                "stop_operation_id",
                "status",
                "effect_attempted",
                "close_required",
                "reason",
                "document_sha256",
                "disposition",
                "lifecycle",
            ],
        )?;
        correlated(v, r, apply)?;
        let stop_id = uuid(v, "stop_id")?;
        if text(v, "stop_operation_id", 128)? != stop {
            return Err(bad("Execution Stop response changed request"));
        }
        let status = text(v, "status", 16)?;
        let reason = optional(v, "reason", 2048)?;
        let effect = boolean(v, "effect_attempted")?;
        let close = boolean(v, "close_required")?;
        let h = opt_hash(v, "document_sha256")?;
        let disposition = text(v, "disposition", 32)?;
        let lifecycle =
            Lifecycle::parse(&v["lifecycle"], &r.identity, &r.work_instance, r.provider)?;
        let cancelled = lifecycle.owners.retired()
            && lifecycle.admission.is_none()
            && lifecycle.settlement.is_none()
            && lifecycle.input_state == Some(life::InputState::Cancelled)
            && lifecycle.producer_phase == Some(life::ProducerState::Retired);
        match status.as_str() {
            "stopped"
                if !close
                    && ((disposition == "no_effect"
                        && !effect
                        && h.is_none()
                        && lifecycle.no_effect())
                        || (disposition == "cancelled_before_admission"
                            && effect
                            && cancelled)) => {}
            "admitted"
                if disposition == "admitted"
                    && effect
                    && !close
                    && h.is_some()
                    && lifecycle.admission.is_some()
                    && lifecycle.settlement.is_none() => {}
            "settled"
                if disposition == "settled"
                    && effect
                    && !close
                    && h.is_some()
                    && lifecycle.settlement.is_some()
                    && lifecycle.owners.retired() => {}
            "uncertain" if disposition == "uncertain" && close && reason.is_some() => {}
            _ => return Err(bad("Execution Stop contradicts its owned disposition")),
        }
        Ok(Self {
            review: r.clone(),
            apply_operation_id: apply.into(),
            stop_id,
            stop_operation_id: stop.into(),
            status,
            reason,
            lifecycle,
            wire: v.clone(),
        })
    }
    pub fn readback_request(&self) -> io::Result<Value> {
        readback_request(
            &self.review,
            &self.apply_operation_id,
            "stop",
            Some(&self.stop_id),
            &self.stop_operation_id,
            &self.wire,
        )
    }
}
fn readback_request(
    r: &Review,
    apply: &str,
    scope: &str,
    stop_id: Option<&str>,
    result_id: &str,
    result: &Value,
) -> io::Result<Value> {
    let mut v = request(&r.identity, &r.work_instance, READBACK);
    v["action_id"] = json!(r.action_id);
    v["review_token"] = json!(r.token);
    v["apply_operation_id"] = json!(apply);
    v["scope"] = json!(scope);
    v["stop_id"] = json!(stop_id);
    v["result_operation_id"] = json!(result_id);
    v["result_sha256"] = json!(digest(result)?);
    Ok(v)
}
#[derive(Clone, Debug, PartialEq)]
pub struct Readback {
    pub document: Option<Document>,
    pub lifecycle: Lifecycle,
    pub wire: Value,
}
impl Readback {
    pub fn parse_launch(v: &Value, result: &Receipt) -> io::Result<Self> {
        let out = Self::parse(
            v,
            &result.review,
            &result.apply_operation_id,
            &result.readback_request()?,
        )?;
        if out.document != result.document
            || result.status == "refused" && !out.lifecycle.no_effect()
        {
            return Err(bad(
                "Launch readback changed immutable receipt/no-effect proof",
            ));
        }
        Ok(out)
    }
    pub fn parse_stop(v: &Value, r: &Review, result: &StopReceipt) -> io::Result<Self> {
        if *r != result.review {
            return Err(bad("Stop readback changed review owner"));
        }
        let out = Self::parse(
            v,
            r,
            &result.apply_operation_id,
            &result.readback_request()?,
        )?;
        if v["document_sha256"] != result.wire["document_sha256"]
            || result.wire["disposition"] == "no_effect" && !out.lifecycle.no_effect()
        {
            return Err(bad(
                "Stop readback changed immutable receipt/no-effect proof",
            ));
        }
        if let Some(a) = &result.lifecycle.admission
            && (out.lifecycle.admission.as_ref() != Some(a))
        {
            return Err(bad("Stop readback replaced exact admitted owner"));
        }
        Ok(out)
    }
    fn parse(v: &Value, r: &Review, apply: &str, expected: &Value) -> io::Result<Self> {
        base(
            v,
            &r.identity,
            &r.work_instance,
            "work_execution_readback",
            &[
                "action_id",
                "review_token",
                "apply_operation_id",
                "scope",
                "stop_id",
                "result_operation_id",
                "result_sha256",
                "verified",
                "document",
                "document_sha256",
                "ledger_revision",
                "ledger_sha256",
                "current",
                "lifecycle",
            ],
        )?;
        correlated(v, r, apply)?;
        for k in ["scope", "stop_id", "result_operation_id", "result_sha256"] {
            if v[k] != expected[k] {
                return Err(bad(
                    "Execution readback changed its independent result scope",
                ));
            }
        }
        if !boolean(v, "verified")? {
            return Err(bad("Execution readback is unverified"));
        }
        let rev = number(v, "ledger_revision", false)?;
        let ledger = opt_hash(v, "ledger_sha256")?;
        let document = document(v, r, apply)?;
        let lifecycle =
            Lifecycle::parse(&v["lifecycle"], &r.identity, &r.work_instance, r.provider)?;
        if ledger.is_none()
            && (rev != 0
                || document.is_some()
                || lifecycle.producer_phase.is_some()
                || lifecycle.input_state.is_some())
        {
            return Err(bad("Execution readback omitted owned Work ledger evidence"));
        }
        if let Some(d) = &document {
            lifecycle.bind(d)?;
        }
        let current = &v["current"];
        keys(current, &["status", "row", "store_revision", "reason"])?;
        let row = Raw::parse(&current["row"])?;
        let reason = optional(current, "reason", 2048)?;
        match current["status"].as_str() {
            Some("present") if !row.value.is_null() && reason.is_none() => {
                saved(&row.value, r.action)?;
                hash(current, "store_revision")?;
                if r.action == Action::JobRun && row.value["id"] != r.target_id {
                    return Err(bad(
                        "Execution current readback selected a different target",
                    ));
                }
            }
            Some("absent") if row.value.is_null() && reason.is_none() => {
                revision(current, "store_revision")?;
            }
            Some("unavailable")
                if row.value.is_null()
                    && reason.is_some()
                    && current["store_revision"].is_null() => {}
            _ => return Err(bad("Execution current state contradicts availability")),
        }
        Ok(Self {
            document,
            lifecycle,
            wire: v.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Recovery {
    pub status: String,
    pub document: Option<Document>,
    pub reason: Option<String>,
    pub wire: Value,
}
impl Recovery {
    pub fn parse(v: &Value, current: &life::Identity) -> io::Result<Self> {
        bounded(v, ENVELOPE)?;
        keys(v, &["status", "document", "document_sha256", "reason"])?;
        let status = text(v, "status", 16)?;
        let reason = optional(v, "reason", 2048)?;
        let raw = optional(v, "document", DOCUMENT)?;
        let h = opt_hash(v, "document_sha256")?;
        let document = match (raw, h) {
            (None, None) => None,
            (Some(raw), Some(h)) => {
                let d = Document::parse(&raw, &h)?;
                if d.identity.session != current.session || d.identity.agent != current.agent {
                    return Err(bad(
                        "Historical execution belongs to another conversation/agent",
                    ));
                }
                Some(d)
            }
            _ => return Err(bad("Incomplete historical execution receipt")),
        };
        match status.as_str() {
            "absent" if document.is_none() && reason.is_none() => {}
            "historical" if document.is_some() && reason.is_none() => {}
            "unavailable" if document.is_none() && reason.is_some() => {}
            _ => return Err(bad("Execution recovery availability is contradictory")),
        }
        Ok(Self {
            status,
            document,
            reason,
            wire: v.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Shutdown {
    pub identity: life::Identity,
    pub work_instance: String,
    pub owners: Owners,
    pub wire: Value,
}
impl Shutdown {
    pub fn parse(v: &Value) -> io::Result<Self> {
        bounded(v, ENVELOPE)?;
        keys(
            v,
            &[
                "version",
                "execution_version",
                "identity",
                "work_instance",
                "action_id",
                "review_sha256",
                "apply_operation_id",
                "stop_ids",
                "stop_operation_ids",
                "launch_readback_published",
                "stop_readbacks_published",
                "effect_attempted",
                "document_sha256",
                "disposition",
                "owners",
                "input_id",
                "admission_id",
                "admission_closed",
                "worker_settled",
                "callback_settled",
                "effects_verified",
            ],
        )?;
        if number(v, "version", true)? != 1
            || number(v, "execution_version", true)? != 1
            || !boolean(v, "admission_closed")?
            || !boolean(v, "worker_settled")?
            || !boolean(v, "callback_settled")?
            || boolean(v, "effects_verified")?
        {
            return Err(bad("Execution shutdown version/retirement proof changed"));
        }
        let action = optional_uuid(v, "action_id")?;
        let review = opt_hash(v, "review_sha256")?;
        let apply = optional(v, "apply_operation_id", 128)?;
        let document = opt_hash(v, "document_sha256")?;
        let input = optional_uuid(v, "input_id")?;
        let admission = optional_uuid(v, "admission_id")?;
        let launch = boolean(v, "launch_readback_published")?;
        let effect = boolean(v, "effect_attempted")?;
        let ids = list(v, "stop_ids", true)?;
        let ops = list(v, "stop_operation_ids", false)?;
        let published = list(v, "stop_readbacks_published", true)?;
        if ids.len() != ops.len() || published.iter().any(|s| !ids.contains(s)) {
            return Err(bad(
                "Execution shutdown changed Stop publication identities",
            ));
        }
        if document.is_some() && input.is_none() {
            return Err(bad("Execution shutdown receipt lacks its actual input"));
        }
        let owners = Owners::parse(&v["owners"])?;
        if !owners.retired() {
            return Err(bad("Execution shutdown retains an unjoined owner"));
        }
        match text(v, "disposition", 32)?.as_str() {
            "idle"
                if action.is_none()
                    && review.is_none()
                    && apply.is_none()
                    && document.is_none()
                    && input.is_none()
                    && admission.is_none()
                    && ids.is_empty()
                    && !launch
                    && !effect
                    && owners.absent() => {}
            "no_effect"
                if action.is_some()
                    && review.is_some()
                    && apply.is_some()
                    && !effect
                    && document.is_none()
                    && input.is_none()
                    && admission.is_none()
                    && owners.delivery == OwnerState::Absent
                    && owners.admission == OwnerState::Absent
                    && owners.callbacks == OwnerState::Absent => {}
            "published" | "cancelled_before_admission"
                if action.is_some()
                    && review.is_some()
                    && apply.is_some()
                    && effect
                    && input.is_some()
                    && admission.is_none()
                    && document.is_some() => {}
            "admitted_settled"
                if action.is_some()
                    && review.is_some()
                    && apply.is_some()
                    && effect
                    && input.is_some()
                    && admission.is_some()
                    && document.is_some() => {}
            "uncertain"
                if action.is_some()
                    && review.is_some()
                    && apply.is_some()
                    && effect
                    && (admission.is_none() || input.is_some()) => {}
            _ => {
                return Err(bad(
                    "Execution shutdown disposition contradicts retained effects",
                ));
            }
        }
        Ok(Self {
            identity: life::Identity::parse(&v["identity"])?,
            work_instance: hash(v, "work_instance")?,
            owners,
            wire: v.clone(),
        })
    }
    pub fn verify_idle(&self, identity: &life::Identity, instance: &str) -> io::Result<()> {
        if self.identity != *identity
            || self.work_instance != instance
            || self.wire["disposition"] != "idle"
        {
            return Err(bad("Execution shutdown is not exact negotiated idle epoch"));
        }
        Ok(())
    }
    pub fn verify_invocation(
        &self,
        r: &Review,
        apply: &str,
        result: Option<&Receipt>,
        stops: &[StopReceipt],
        expected_stop_operation_id: Option<&str>,
    ) -> io::Result<()> {
        let v = &self.wire;
        if self.identity != r.identity
            || self.work_instance != r.work_instance
            || v["action_id"] != r.action_id
            || v["review_sha256"] != digest(&r.wire)?
            || v["apply_operation_id"] != apply
            || v["disposition"] == "idle"
        {
            return Err(bad("Execution shutdown lost accepted launch owner"));
        }
        let ids = list(v, "stop_ids", true)?;
        let ops = list(v, "stop_operation_ids", false)?;
        let mut expected = BTreeSet::new();
        for stop in stops {
            if stop.review != *r || stop.apply_operation_id != apply {
                return Err(bad("Shutdown supplied foreign Stop evidence"));
            }
            expected.insert(stop.stop_operation_id.clone());
            let pos = ops
                .iter()
                .position(|id| *id == stop.stop_operation_id)
                .ok_or_else(|| bad("Shutdown omitted a completed Stop identity"))?;
            if ids[pos] != stop.stop_id {
                return Err(bad("Shutdown paired Stop with a different RPC"));
            }
            if stop.wire["disposition"] == "no_effect"
                && (v["effect_attempted"] != false || v["disposition"] != "no_effect")
            {
                return Err(bad("Shutdown erased proven pre-effect Stop"));
            }
            if !stop.wire["document_sha256"].is_null()
                && stop.wire["document_sha256"] != v["document_sha256"]
            {
                return Err(bad("Shutdown changed Stop launch receipt"));
            }
            if stop.wire["effect_attempted"] == true && v["effect_attempted"] != true {
                return Err(bad("Shutdown erased Stop effect evidence"));
            }
            if let Some(a) = &stop.lifecycle.admission
                && (v["admission_id"] != a.id || v["input_id"] != a.input)
            {
                return Err(bad("Shutdown erased Stop admission evidence"));
            }
        }
        if let Some(id) = expected_stop_operation_id {
            text(&json!({"id":id}), "id", 128)?;
            expected.insert(id.into());
        }
        if ops.iter().cloned().collect::<BTreeSet<_>>() != expected {
            return Err(bad("Execution shutdown changed sent Stop obligations"));
        }
        if let Some(result) = result {
            if result.review != *r || result.apply_operation_id != apply {
                return Err(bad("Shutdown supplied foreign launch result"));
            }
            if result.wire["effect_attempted"] == true && v["effect_attempted"] != true {
                return Err(bad("Shutdown erased launch effects"));
            }
            if result.status == "refused" && v["disposition"] != "no_effect" {
                return Err(bad("Shutdown changed proven no-effect launch"));
            }
            if let Some(d) = &result.document
                && (v["document_sha256"] != d.sha256 || v["input_id"] != d.input.id)
            {
                return Err(bad("Shutdown changed durable launch input/receipt"));
            }
            if let Some(a) = &result.lifecycle.admission
                && (v["admission_id"] != a.id)
            {
                return Err(bad("Shutdown changed admitted launch owner"));
            }
        }
        Ok(())
    }
}
fn list(v: &Value, k: &str, is_uuid: bool) -> io::Result<Vec<String>> {
    let a = v[k]
        .as_array()
        .filter(|a| a.len() <= 2)
        .ok_or_else(|| bad("Execution Stop proof list exceeds bounded pending/latest slots"))?;
    let mut out = Vec::new();
    for item in a {
        let value = json!({"v":item});
        let id = if is_uuid {
            uuid(&value, "v")?
        } else {
            text(&value, "v", 128)?
        };
        if out.contains(&id) {
            return Err(bad("Execution Stop proof repeats an identity"));
        }
        out.push(id);
    }
    Ok(out)
}
