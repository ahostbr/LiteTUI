//! Reviewed durable job/loop configuration and goal pause/clear. No execution authority.
use crate::{
    work_lifecycle::{bad, bounded, hash, keys, number, uuid},
    work_protocol::{Row, Section, Snapshot},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const VALIDATE: &str = "gui.work.native_control_validate";
pub const APPLY: &str = "gui.work.native_control_apply";
pub const READBACK: &str = "gui.work.native_control_readback";
pub const OPERATIONS: [&str; 3] = [VALIDATE, APPLY, READBACK];
const COMMON: &[&str] = &[
    "kind",
    "version",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "instance",
    "snapshot",
];
pub(crate) const JOB: &[&str] = &[
    "prompt",
    "schedule",
    "id",
    "enabled",
    "new_conversation",
    "last_fired_slot",
    "run_count",
    "created",
    "label",
    "tool_profile",
    "kind",
    "owner_convo_id",
    "interval_minutes",
    "next_run_at",
    // develop scheduler.Job (T0495 self-schedules and the deferred slot)
    "owner_agent_id",
    "deferred_slot",
];
pub(crate) const GOAL: &[&str] = &[
    "objective",
    "id",
    "status",
    "created",
    "updated",
    "evaluated_turns",
    "max_turns",
    "last_verdict",
    "last_reason",
    "missing_evidence",
    "no_progress_count",
    "tool_profile",
    "started_by",
];
const EXECUTION: &[&str] = &["run_count", "last_fired_slot", "next_run_at"];

fn text<'a>(v: &'a Value, key: &str, limit: usize, empty: bool) -> io::Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| s.len() <= limit && (empty || !s.is_empty()))
        .ok_or_else(|| bad(&format!("Invalid control {key}")))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v.get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| bad("Invalid control boolean"))
}
fn integer(v: &Value, key: &str, positive: bool) -> io::Result<u64> {
    let n = number(v, key, positive)?;
    if n > i32::MAX as u64 {
        return Err(bad("Control integer exceeds source bound"));
    }
    Ok(n)
}
fn revision(v: &Value, key: &str) -> io::Result<String> {
    if v.get(key) == Some(&json!("absent")) {
        Ok("absent".into())
    } else {
        hash(v, key)
    }
}
fn response(v: &Value, snapshot: &Snapshot, kind: &str, fields: &[&str]) -> io::Result<()> {
    bounded(v, 1024 * 1024)?;
    let mut expected = COMMON.to_vec();
    expected.extend_from_slice(fields);
    keys(v, &expected)?;
    if v["kind"] != kind {
        return Err(bad("Wrong durable control response kind"));
    }
    for (key, value) in snapshot.request().as_object().expect("snapshot object") {
        if v.get(key) != Some(value) {
            return Err(bad("Control response changed owned snapshot"));
        }
    }
    Ok(())
}
fn timestamp(v: &Value, key: &str, nullable: bool) -> io::Result<()> {
    if nullable && v.get(key) == Some(&Value::Null) {
        return Ok(());
    }
    let s = text(v, key, 128, false)?;
    // Canonical source writes ISO local timestamps; imported legacy forms remain bounded facts.
    // Equality checks below bind them without interpreting the user's local timezone.
    if !s
        .chars()
        .all(|c| c.is_ascii_digit() || "-:+.TZ W".contains(c))
        || s.len() < 8
    {
        return Err(bad("Invalid persisted control timestamp"));
    }
    Ok(())
}
pub(crate) fn job(v: &Value) -> io::Result<()> {
    bounded(v, 128 * 1024)?;
    keys(v, JOB)?;
    for (key, max, empty) in [
        ("id", 128, false),
        ("prompt", 65536, true),
        ("schedule", 1024, true),
        ("label", 4096, true),
        ("tool_profile", 4096, true),
        ("owner_convo_id", 128, true),
        ("owner_agent_id", 128, true),
    ] {
        text(v, key, max, empty)?;
    }
    timestamp(v, "deferred_slot", true)?;
    boolean(v, "enabled")?;
    boolean(v, "new_conversation")?;
    integer(v, "run_count", false)?;
    integer(v, "interval_minutes", false)?;
    timestamp(v, "created", false)?;
    timestamp(v, "last_fired_slot", true)?;
    timestamp(v, "next_run_at", true)?;
    match v["kind"].as_str() {
        Some("cron") => {}
        Some("loop") => {
            integer(v, "interval_minutes", true)?;
            uuid(v, "owner_convo_id")?;
            timestamp(v, "next_run_at", false)?;
        }
        _ => return Err(bad("Invalid control job kind")),
    }
    Ok(())
}
pub(crate) fn goal(v: &Value) -> io::Result<()> {
    bounded(v, 128 * 1024)?;
    keys(v, GOAL)?;
    for (key, max, empty) in [
        ("id", 128, false),
        ("objective", 65536, false),
        ("last_reason", 65536, true),
        ("last_verdict", 128, true),
        ("tool_profile", 4096, true),
        ("started_by", 4096, true),
    ] {
        text(v, key, max, empty)?;
    }
    for key in ["evaluated_turns", "max_turns", "no_progress_count"] {
        integer(v, key, false)?;
    }
    timestamp(v, "created", false)?;
    timestamp(v, "updated", false)?;
    if !matches!(
        v["status"].as_str(),
        Some("active" | "paused" | "met" | "blocked" | "impossible")
    ) {
        return Err(bad("Invalid goal state"));
    }
    let evidence = v["missing_evidence"]
        .as_array()
        .filter(|a| a.len() <= 256)
        .ok_or_else(|| bad("Invalid goal evidence"))?;
    if evidence
        .iter()
        .any(|s| s.as_str().is_none_or(|s| s.len() > 4096))
    {
        return Err(bad("Oversized goal evidence"));
    }
    Ok(())
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    JobCreate,
    JobUpdate,
    JobDelete,
    LoopEnable,
    GoalPause,
    GoalClear,
}
impl Action {
    pub fn key(self) -> &'static str {
        match self {
            Self::JobCreate => "job_create",
            Self::JobUpdate => "job_update",
            Self::JobDelete => "job_delete",
            Self::LoopEnable => "loop_enable",
            Self::GoalPause => "goal_pause",
            Self::GoalClear => "goal_clear",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::JobCreate => "Create schedule",
            Self::JobUpdate => "Edit schedule",
            Self::JobDelete => "Delete schedule",
            Self::LoopEnable => "Change loop enabled state",
            Self::GoalPause => "Pause goal",
            Self::GoalClear => "Clear goal",
        }
    }
    pub fn is_goal(self) -> bool {
        matches!(self, Self::GoalPause | Self::GoalClear)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Edit {
    pub snapshot: Snapshot,
    pub action: Action,
    pub target_id: Option<String>,
    pub target_revision: Option<String>,
    pub before: Value,
    pub draft: Value,
}
fn patch(draft: &Value, allowed: &[&str], required: &[&str]) -> io::Result<()> {
    let fields = draft
        .as_object()
        .ok_or_else(|| bad("Control draft must be an object"))?;
    if fields.keys().any(|k| !allowed.contains(&k.as_str()))
        || required.iter().any(|k| !fields.contains_key(*k))
    {
        return Err(bad("Unsupported or missing control draft field"));
    }
    for key in fields.keys().map(String::as_str) {
        match key {
            "prompt" => {
                if text(draft, key, 65536, false)?.trim().is_empty() {
                    return Err(bad("Blank scheduled prompt"));
                }
            }
            "label" => {
                text(draft, key, 4096, true)?;
            }
            "schedule" => {
                text(draft, key, 1024, false)?;
            }
            "enabled" | "new_conversation" => {
                boolean(draft, key)?;
            }
            "interval_minutes" => {
                if integer(draft, key, true)? > 525600 {
                    return Err(bad("Loop interval exceeds limit"));
                }
            }
            "tool_profile" => {
                crate::tool_mode::Profile::parse(text(draft, key, 4096, false)?)?;
            }
            "kind" => {
                if !matches!(draft[key].as_str(), Some("cron" | "loop")) {
                    return Err(bad("Invalid new schedule kind"));
                }
            }
            _ => return Err(bad("Unsupported control draft field")),
        }
    }
    bounded(draft, 128 * 1024)
}
impl Edit {
    pub fn new(
        snapshot: &Snapshot,
        action: Action,
        selected: Option<&Row>,
        draft: Value,
    ) -> io::Result<Self> {
        if snapshot.quitting || snapshot.busy {
            return Err(bad("Wait for idle owned Work before editing"));
        }
        let (target_id, target_revision, before) = if action.is_goal() {
            if selected.is_some() || snapshot.goal["status"] != "present" {
                return Err(bad("Select the current goal"));
            }
            goal(&snapshot.goal["value"])?;
            (
                Some(text(&snapshot.goal["value"], "id", 128, false)?.into()),
                Some(hash(&snapshot.goal, "revision")?),
                snapshot.goal["value"].clone(),
            )
        } else if action == Action::JobCreate {
            if selected.is_some() || !snapshot.source(Section::Jobs).available {
                return Err(bad("Refresh available schedules before create"));
            }
            (None, None, Value::Null)
        } else {
            let row = selected.ok_or_else(|| bad("Select a saved job"))?;
            job(&row.raw)?;
            let section = if row.raw["kind"] == "loop" {
                Section::Loops
            } else {
                Section::Jobs
            };
            if row.raw["id"] != row.id
                || row.raw["kind"] != row.kind
                || !snapshot.source(section).available
                || row.kind == "loop" && row.raw["owner_convo_id"] != snapshot.identity.session
            {
                return Err(bad(
                    "Selected schedule has foreign ownership or unavailable source",
                ));
            }
            (
                Some(row.id.clone()),
                Some(row.revision.clone()),
                row.raw.clone(),
            )
        };
        match action {
            Action::JobCreate => match draft["kind"].as_str() {
                Some("cron") => patch(
                    &draft,
                    &[
                        "kind",
                        "prompt",
                        "schedule",
                        "label",
                        "enabled",
                        "new_conversation",
                        "tool_profile",
                    ],
                    &["kind", "prompt", "schedule"],
                )?,
                Some("loop") => patch(
                    &draft,
                    &["kind", "prompt", "interval_minutes", "label", "enabled"],
                    &["kind", "prompt", "interval_minutes"],
                )?,
                _ => return Err(bad("Choose a new schedule kind")),
            },
            Action::JobUpdate => {
                let allowed = if before["kind"] == "loop" {
                    &["prompt", "label"][..]
                } else {
                    &["prompt", "schedule", "label", "enabled", "new_conversation"][..]
                };
                patch(&draft, allowed, &[])?;
                if draft.as_object().is_none_or(|o| o.is_empty()) {
                    return Err(bad("Empty schedule edit"));
                }
            }
            Action::LoopEnable => {
                if before["kind"] != "loop" {
                    return Err(bad("Select a loop"));
                }
                patch(&draft, &["enabled"], &["enabled"])?;
            }
            _ => patch(&draft, &[], &[])?,
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            action,
            target_id,
            target_revision,
            before,
            draft,
        })
    }
    pub fn request(&self) -> Value {
        let mut value = self.snapshot.request();
        value["action"] = json!(self.action.key());
        value["target_id"] = json!(self.target_id);
        value["target_revision"] = json!(self.target_revision);
        value["draft"] = self.draft.clone();
        value
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub edit: Edit,
    pub token: String,
    pub action_id: String,
    pub target_id: String,
    pub before: Value,
    pub after: Value,
    pub rearm_minutes: Option<u64>,
    pub consequence: String,
    pub wire: Value,
}
impl Review {
    pub fn parse(v: &Value, edit: &Edit) -> io::Result<Self> {
        response(
            v,
            &edit.snapshot,
            "work_control_review",
            &[
                "action",
                "target_id",
                "target_revision",
                "draft",
                "review",
                "action_id",
                "scope",
                "before",
                "after",
                "rearm_minutes",
                "consequence",
                "raw_revision",
            ],
        )?;
        if v["action"] != edit.action.key()
            || v["draft"] != edit.draft
            || v["target_revision"] != json!(edit.target_revision)
            || v["before"] != edit.before
            || v["scope"]
                != if edit.action.is_goal() {
                    "conversation"
                } else {
                    "data_root"
                }
        {
            return Err(bad("Control review differs from the exact requested edit"));
        }
        let token = hash(v, "review")?;
        let action_id = crate::stream::required_uuid4(v, "action_id")
            .map_err(|_| bad("Invalid control action ID"))?
            .to_owned();
        let target_id = text(v, "target_id", 128, false)?.to_owned();
        if edit.target_id.as_ref().is_some_and(|id| *id != target_id) {
            return Err(bad("Control target changed"));
        }
        let raw_revision = revision(v, "raw_revision")?;
        let rearm_minutes = match v.get("rearm_minutes") {
            Some(Value::Null) => None,
            Some(_) => Some(integer(v, "rearm_minutes", true)?),
            None => return Err(bad("Missing loop rearm policy")),
        };
        let consequence = text(v, "consequence", 2048, false)?.to_owned();
        let mut expected = edit.before.clone();
        match edit.action {
            Action::JobCreate => {
                job(&v["after"])?;
                let after = &v["after"];
                if !edit.before.is_null()
                    || raw_revision != "absent"
                    || after["id"] != target_id
                    || after["kind"] != edit.draft["kind"]
                    || after["run_count"] != 0
                    || !after["last_fired_slot"].is_null()
                {
                    return Err(bad("Created job has invented existing execution state"));
                }
                for (key, value) in edit.draft.as_object().expect("validated draft") {
                    if after[key] != *value {
                        return Err(bad("New job differs from requested configuration"));
                    }
                }
                for (key, default) in [
                    ("label", json!("")),
                    ("enabled", json!(true)),
                    ("new_conversation", json!(false)),
                ] {
                    if edit.draft.get(key).is_none() && after[key] != default {
                        return Err(bad("New job changed a source default"));
                    }
                }
                crate::tool_mode::Profile::parse(text(after, "tool_profile", 4096, false)?)?;
                if after["kind"] == "loop" {
                    if after["owner_convo_id"] != edit.snapshot.identity.session
                        || rearm_minutes != after["interval_minutes"].as_u64()
                        || after["schedule"]
                            != format!("@every {}m", rearm_minutes.expect("checked interval"))
                    {
                        return Err(bad("New loop changed ownership/cadence"));
                    }
                } else if rearm_minutes.is_some()
                    || after["interval_minutes"] != 0
                    || after["owner_convo_id"] != ""
                    || !after["next_run_at"].is_null()
                {
                    return Err(bad("Cron creation invented loop state"));
                }
                expected = after.clone();
            }
            Action::JobUpdate => {
                for (key, value) in edit.draft.as_object().expect("draft") {
                    expected[key] = value.clone();
                }
            }
            Action::JobDelete | Action::GoalClear => expected = Value::Null,
            Action::LoopEnable => expected["enabled"] = edit.draft["enabled"].clone(),
            Action::GoalPause => expected["status"] = json!("paused"),
        }
        if edit.action != Action::JobCreate {
            if raw_revision == "absent" {
                return Err(bad("Existing target lacks raw revision"));
            }
            let expected_rearm =
                if edit.action == Action::LoopEnable && edit.draft["enabled"] == true {
                    edit.before["interval_minutes"].as_u64()
                } else {
                    None
                };
            if rearm_minutes != expected_rearm {
                return Err(bad("Control changed loop rearm policy"));
            }
        }
        if v["after"] != expected {
            return Err(bad(
                "Control review changes fields outside the requested operation",
            ));
        }
        if !v["after"].is_null() {
            if edit.action.is_goal() {
                goal(&v["after"])?;
            } else {
                job(&v["after"])?;
            }
        }
        let expected_consequence = match edit.action {
            Action::GoalClear => {
                "Clear the saved goal only; no transcript is removed and no work is started."
            }
            Action::GoalPause => {
                "Pause future goal evaluation; no work is started and no transcript is changed."
            }
            Action::JobDelete => "Delete only the saved job; accepted work is not cancelled.",
            _ => {
                "Save this job configuration only. An enabled job may run on a later normal scheduler tick; this save starts no work."
            }
        };
        if consequence != expected_consequence {
            return Err(bad("Control consequence changed"));
        }
        Ok(Self {
            edit: edit.clone(),
            token,
            action_id,
            target_id,
            before: v["before"].clone(),
            after: v["after"].clone(),
            rearm_minutes,
            consequence,
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.wire.clone();
        v.as_object_mut().expect("review").remove("kind");
        v
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt {
    pub token: String,
    pub row: Value,
    pub store_revision: String,
}
fn binding(v: &Value, review: &Review) -> io::Result<()> {
    if v["action"] != review.edit.action.key()
        || v["target_id"] != review.target_id
        || v["review"] != review.token
        || v["action_id"] != review.action_id
    {
        return Err(bad("Control publication changed review/target identity"));
    }
    Ok(())
}
impl Receipt {
    pub fn parse(v: &Value, review: &Review) -> io::Result<Self> {
        response(
            v,
            &review.edit.snapshot,
            "work_control_result",
            &[
                "action",
                "target_id",
                "review",
                "action_id",
                "status",
                "mutated",
                "close_required",
                "row",
                "store_revision",
                "receipt",
            ],
        )?;
        binding(v, review)?;
        if v["status"] != "applied" || v["mutated"] != true || v["close_required"] != false {
            return Err(bad(
                "Control result is not a definitive configuration publication",
            ));
        }
        let token = hash(v, "receipt")?;
        let mut signed = v.clone();
        signed.as_object_mut().expect("result").remove("receipt");
        if token != format!("{:x}", Sha256::digest(serde_json::to_vec(&signed)?)) {
            return Err(bad("Control receipt digest changed"));
        }
        let mut expected = review.after.clone();
        if review.rearm_minutes.is_some() {
            job(&v["row"])?;
            expected["next_run_at"] = v["row"]["next_run_at"].clone();
        }
        if v["row"] != expected {
            return Err(bad("Persisted control differs from reviewed configuration"));
        }
        let store_revision = revision(v, "store_revision")?;
        if (review.edit.action == Action::GoalClear) != (store_revision == "absent") {
            return Err(bad("Control document existence contradicts publication"));
        }
        Ok(Self {
            token,
            row: v["row"].clone(),
            store_revision,
        })
    }
    pub fn request(&self, review: &Review) -> Value {
        let mut v = review.edit.snapshot.request();
        v["review"] = json!(review.token);
        v["action_id"] = json!(review.action_id);
        v["receipt"] = json!(self.token);
        v
    }
}

fn execution_progress(row: &Value, previous: &Value) -> io::Result<()> {
    let slot = crate::scheduled_transition::local_minute(text(row, "last_fired_slot", 16, false)?)?;
    if !previous["last_fired_slot"].is_null()
        && slot
            < crate::scheduled_transition::local_minute(text(
                previous,
                "last_fired_slot",
                16,
                false,
            )?)?
    {
        return Err(bad("Scheduler execution slot regressed"));
    }
    if row["kind"] == "loop" {
        let next = text(row, "next_run_at", 19, false)?;
        let bytes = next.as_bytes();
        if bytes.len() != 19
            || bytes[16] != b':'
            || !bytes[17..].iter().all(u8::is_ascii_digit)
            || next[17..]
                .parse::<u8>()
                .map_err(|_| bad("Invalid loop execution seconds"))?
                > 59
        {
            return Err(bad("Loop execution lacks canonical local seconds"));
        }
        let next_minute = crate::scheduled_transition::local_minute(&next[..16])?;
        if next_minute - integer(row, "interval_minutes", true)? as i64 != slot {
            return Err(bad(
                "Loop execution cadence differs from its stamped minute",
            ));
        }
        // Manual Run may legitimately rearm earlier than a previous due time,
        // or again in the same second. Bind cadence to this stamp, not old next.
    } else if row["next_run_at"] != previous["next_run_at"] {
        return Err(bad("Cron execution changed loop cadence"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq)]
pub struct Readback {
    pub row: Value,
    pub store_revision: String,
}
impl Readback {
    pub fn parse(v: &Value, review: &Review, receipt: &Receipt) -> io::Result<Self> {
        response(
            v,
            &review.edit.snapshot,
            "work_control_readback",
            &[
                "action",
                "target_id",
                "review",
                "action_id",
                "receipt",
                "verified",
                "row",
                "store_revision",
            ],
        )?;
        binding(v, review)?;
        if v["receipt"] != receipt.token || v["verified"] != true {
            return Err(bad("Independent configuration readback is unverified"));
        }
        let row = &v["row"];
        if review.edit.action.is_goal() || receipt.row.is_null() {
            if *row != receipt.row {
                return Err(bad("Goal/deletion readback changed"));
            }
        } else {
            job(row)?;
            let count = integer(row, "run_count", false)?;
            let before = integer(&receipt.row, "run_count", false)?;
            if count < before {
                return Err(bad("Scheduler run count regressed"));
            }
            if count == before {
                if *row != receipt.row {
                    return Err(bad("Configuration readback changed"));
                }
            } else {
                execution_progress(row, &receipt.row)?;
                for key in JOB {
                    if !EXECUTION.contains(key) && row[*key] != receipt.row[*key] {
                        return Err(bad("Scheduler execution changed saved configuration"));
                    }
                }
            }
        }
        let store_revision = revision(v, "store_revision")?;
        if (review.edit.action == Action::GoalClear) != (store_revision == "absent") {
            return Err(bad("Readback document existence changed"));
        }
        Ok(Self {
            row: row.clone(),
            store_revision,
        })
    }
}
