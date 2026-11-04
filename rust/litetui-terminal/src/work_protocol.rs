//! Bounded Work inspection and pause receipts. Runtime execution has a separate ledger.
use serde_json::{Value, json};
use std::{collections::HashSet, io};
pub const READ: &str = "gui.work.native_snapshot";
pub const PAGE: &str = "gui.work.native_page";
pub const TAIL: &str = "gui.work.native_tail";
pub const CALENDAR: &str = "gui.work.native_calendar";
pub const VALIDATE: &str = "gui.work.native_pause_validate";
pub const APPLY: &str = "gui.work.native_pause_apply";
pub const READBACK: &str = "gui.work.native_pause_readback";
pub const STOP_VALIDATE: &str = "gui.work.native_stop_validate";
pub const STOP_APPLY: &str = "gui.work.native_stop_apply";
pub const STOP_SETTLED: &str = "gui.work.native_stop_settled";
pub const STOP_CONSEQUENCE: &str =
    "Stop only this owned background task. Its final output may still enter this conversation.";
pub const MAX_BYTES: usize = 1024 * 1024;
pub const PAGE_SIZE: usize = 32;
pub const CONSEQUENCE: &str = "Pause future scheduled prompts only; active tasks, goals, queued input and saved jobs remain unchanged.";
pub fn fail(reason: &str) -> io::Error {
    io::Error::other(reason.to_owned())
}
fn bounded(v: &Value, max: usize) -> io::Result<()> {
    if serde_json::to_vec(v)?.len() > max {
        Err(fail("Work response exceeds its byte limit"))
    } else {
        Ok(())
    }
}
fn text<'a>(v: &'a Value, key: &str, max: usize, empty: bool) -> io::Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| s.len() <= max && (empty || !s.is_empty()))
        .ok_or_else(|| fail(&format!("Invalid Work {key}")))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v.get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| fail(&format!("Invalid Work {key}")))
}
fn integer(v: &Value, key: &str, max: u64) -> io::Result<u64> {
    v.get(key)
        .and_then(Value::as_u64)
        .filter(|n| *n <= max)
        .ok_or_else(|| fail(&format!("Invalid Work {key}")))
}
fn hex(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64, false)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid Work revision"));
    }
    Ok(s.into())
}
fn nullable_text(v: &Value, key: &str, max: usize) -> io::Result<Option<String>> {
    match v.get(key) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.len() <= max => Ok(Some(s.clone())),
        _ => Err(fail(&format!("Invalid optional Work {key}"))),
    }
}
fn finite(v: &Value, key: &str, nullable: bool) -> io::Result<()> {
    if nullable && v.get(key) == Some(&Value::Null) {
        return Ok(());
    }
    if v.get(key)
        .and_then(Value::as_f64)
        .is_some_and(|n| n.is_finite() && n >= 0.0)
    {
        Ok(())
    } else {
        Err(fail(&format!("Invalid Work time {key}")))
    }
}
fn nullable_integer(v: &Value, key: &str) -> io::Result<()> {
    if v.get(key) == Some(&Value::Null) {
        Ok(())
    } else {
        integer(v, key, 9_007_199_254_740_991).map(|_| ())
    }
}
fn revision(v: &Value, key: &str, unavailable: bool) -> io::Result<()> {
    if v.get(key) == Some(&json!("absent")) || unavailable && v.get(key) == Some(&Value::Null) {
        Ok(())
    } else {
        hex(v, key).map(|_| ())
    }
}
fn reason(v: &Value, allowed: bool) -> io::Result<Option<String>> {
    let r = nullable_text(v, "reason", 65536)?;
    if allowed && r.is_some() || !allowed && r.as_deref().is_none_or(str::is_empty) {
        return Err(fail("Work availability contradicts its reason"));
    }
    Ok(r)
}
fn portable(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s == s.trim()
        && !s.ends_with('.')
        && !matches!(s, "." | "..")
        && s.chars()
            .all(|c| !c.is_control() && !"<>:\"/\\|?*".contains(c))
}
fn profile(s: &str) -> bool {
    matches!(s, "strict" | "interactive" | "autonomous")
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
}
impl Identity {
    pub fn request(&self) -> Value {
        json!({"version":1,"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"model":self.model})
    }
    fn check(&self, v: &Value, kind: &str) -> io::Result<()> {
        bounded(v, MAX_BYTES)?;
        if v["kind"] != kind
            || v["version"] != 1
            || v["session_id"] != self.session
            || v["agent_id"] != self.agent
            || v["backend"] != self.backend
            || v["model"] != self.model
        {
            return Err(fail("Work response belongs to another runtime or protocol"));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Section {
    Tasks,
    ProviderTasks,
    Jobs,
    Loops,
}
impl Section {
    pub const ALL: [Self; 4] = [Self::Tasks, Self::ProviderTasks, Self::Jobs, Self::Loops];
    pub fn key(self) -> &'static str {
        match self {
            Self::Tasks => "tasks",
            Self::ProviderTasks => "provider_tasks",
            Self::Jobs => "jobs",
            Self::Loops => "loops",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Tasks => "Tasks",
            Self::ProviderTasks => "Provider tasks",
            Self::Jobs => "Scheduled jobs",
            Self::Loops => "Loops",
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Source {
    pub count: usize,
    pub available: bool,
    pub reason: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub identity: Identity,
    pub instance: String,
    pub token: String,
    pub calendar_month: String,
    pub busy: bool,
    pub paused: bool,
    pub quitting: bool,
    pub sources: [Source; 4],
    pub goal: Value,
    pub can_pause: bool,
    pub pause_reason: Option<String>,
    pub scheduler_notice: Option<String>,
    pub deferred_job: Option<crate::deferred_control::Snapshot>,
    pub deferred_advertised: bool,
}
impl Snapshot {
    pub fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        identity.check(v, "work_snapshot")?;
        finite(v, "captured_at", false)?;
        let instance = hex(v, "instance")?;
        let token = hex(v, "snapshot")?;
        let mut sources = Vec::new();
        for section in Section::ALL {
            let key = section.key();
            let count = integer(&v["counts"], key, 4096)? as usize;
            let value = &v["sources"][key];
            let available = boolean(value, "available")?;
            let r = reason(value, available)?;
            revision(value, "revision", !available)?;
            if !available && (count != 0 || !value["revision"].is_null()) {
                return Err(fail("Unavailable Work source reports rows or revision"));
            }
            sources.push(Source {
                count,
                available,
                reason: r,
            });
        }
        let calendar_month = text(v, "calendar_month", 7, false)?.to_owned();
        month_parts(&calendar_month)?;
        validate_goal(&v["goal"])?;
        let can_pause = boolean(&v["pause"], "allowed")?;
        let pause_reason = reason(&v["pause"], can_pause)?;
        let paused = boolean(v, "schedules_paused")?;
        let quitting = boolean(v, "quitting")?;
        if can_pause && (paused || quitting) {
            return Err(fail("Work pause capability contradicts current state"));
        }
        Ok(Self {
            identity: identity.clone(),
            instance,
            token,
            calendar_month,
            busy: boolean(v, "busy")?,
            paused,
            quitting,
            sources: sources.try_into().expect("four sections"),
            goal: v["goal"].clone(),
            can_pause,
            pause_reason,
            deferred_advertised: v.get("deferred_job").is_some(),
            deferred_job: match v.get("deferred_job") {
                None | Some(Value::Null) => None,
                Some(row) => Some(crate::deferred_control::Snapshot::parse(
                    row,
                    &crate::work_lifecycle::Identity {
                        session: identity.session.clone(),
                        agent: identity.agent.clone(),
                        backend: identity.backend.clone(),
                        model: identity.model.clone(),
                    },
                )?),
            },
            scheduler_notice: if v["scheduler_notice"].is_null() {
                None
            } else {
                Some(text(v, "scheduler_notice", 2048, false)?.to_owned())
            },
        })
    }
    pub fn require_deferred(
        &self,
        _: &crate::scheduled_transition::Capabilities,
    ) -> io::Result<()> {
        if !self.deferred_advertised {
            return Err(fail("Negotiated Work snapshot omitted deferred evidence"));
        }
        Ok(())
    }
    pub fn source(&self, section: Section) -> &Source {
        &self.sources[Section::ALL.iter().position(|s| *s == section).unwrap()]
    }
    pub fn request(&self) -> Value {
        let mut v = self.identity.request();
        v["instance"] = json!(self.instance);
        v["snapshot"] = json!(self.token);
        v
    }
    fn check(&self, v: &Value, kind: &str) -> io::Result<()> {
        self.identity.check(v, kind)?;
        if v["instance"] != self.instance || v["snapshot"] != self.token {
            return Err(fail("Work response uses another inspection snapshot"));
        }
        Ok(())
    }
}
fn validate_goal(v: &Value) -> io::Result<()> {
    let status = text(v, "status", 16, false)?;
    match status {
        "absent" if v["revision"] == "absent" && v["value"].is_null() && v["reason"].is_null() => {
            Ok(())
        }
        "unavailable" if v["revision"].is_null() && v["value"].is_null() => {
            text(v, "reason", 65536, false)?;
            Ok(())
        }
        "present" => {
            hex(v, "revision")?;
            if !v["reason"].is_null() {
                return Err(fail("Present goal has unavailable reason"));
            }
            let g = &v["value"];
            text(g, "objective", 65536, false)?;
            text(g, "id", 128, false)?;
            if !matches!(
                text(g, "status", 16, false)?,
                "active" | "paused" | "met" | "blocked" | "impossible"
            ) {
                return Err(fail("Unknown goal state"));
            }
            for key in ["created", "updated"] {
                text(g, key, 128, false)?;
            }
            for key in ["evaluated_turns", "max_turns", "no_progress_count"] {
                integer(g, key, 9_007_199_254_740_991)?;
            }
            for key in ["last_verdict", "tool_profile", "started_by"] {
                text(g, key, 4096, true)?;
            }
            text(g, "last_reason", 65536, true)?;
            let missing = g["missing_evidence"]
                .as_array()
                .filter(|a| a.len() <= 256)
                .ok_or_else(|| fail("Invalid goal evidence"))?;
            if missing
                .iter()
                .any(|s| s.as_str().is_none_or(|s| s.len() > 4096))
            {
                return Err(fail("Invalid goal evidence text"));
            }
            Ok(())
        }
        _ => Err(fail("Invalid goal availability")),
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub id: String,
    pub revision: String,
    pub kind: String,
    pub label: String,
    pub status: String,
    pub owner: String,
    pub tail_available: bool,
    pub raw: Value,
}
impl Row {
    fn parse(v: &Value, section: Section, snapshot: &Snapshot) -> io::Result<Self> {
        bounded(v, 128 * 1024)?;
        let revision = hex(v, "revision")?;
        let r = &v["row"];
        let id = text(r, "id", 4096, false)?.to_owned();
        let kind = text(v, "kind", 32, false)?.to_owned();
        let (label, status, owner, tail_available) = match section {
            Section::Tasks => {
                if kind != "host_task"
                    || !portable(&id)
                    || r["convo_id"] != snapshot.identity.session
                {
                    return Err(fail("Foreign or invalid host task"));
                }
                text(r, "tool", 256, false)?;
                let label = text(r, "label", 4096, true)?.to_owned();
                text(r, "prompt", 65536, true)?;
                text(r, "log", 4096, true)?;
                finite(r, "started", false)?;
                finite(r, "ended", true)?;
                nullable_integer(r, "tokens")?;
                nullable_integer(r, "owner_pid")?;
                if r["owner_pid"] == 0 {
                    return Err(fail("Invalid task PID"));
                }
                nullable_text(r, "owner_instance", 4096)?;
                nullable_text(r, "owner_created", 4096)?;
                if !r["ok"].is_null() && !r["ok"].is_boolean() {
                    return Err(fail("Invalid task outcome"));
                }
                let status = text(r, "state", 16, false)?.to_owned();
                if !matches!(
                    status.as_str(),
                    "running" | "done" | "failed" | "killed" | "lost"
                ) {
                    return Err(fail("Unknown task state"));
                }
                let owner = text(v, "owner", 16, false)?.to_owned();
                if !matches!(owner.as_str(), "current" | "other" | "unknown") {
                    return Err(fail("Unknown task owner"));
                }
                let tail = boolean(v, "tail_available")?;
                let why = nullable_text(v, "tail_reason", 65536)?;
                if tail && (status == "running" || why.is_some())
                    || !tail && why.as_deref().is_none_or(str::is_empty)
                {
                    return Err(fail("Invalid task output availability"));
                }
                (label, status, owner, tail)
            }
            Section::ProviderTasks => {
                if kind != "provider_task"
                    || v["owner"] != "provider"
                    || v["tail_available"] != false
                    || r["convo_id"] != snapshot.identity.session
                    || r["state"] != "running"
                {
                    return Err(fail("Invalid provider task"));
                }
                text(v, "tail_reason", 65536, false)?;
                for key in ["thread_id", "turn_id", "item_id"] {
                    text(r, key, 4096, false)?;
                }
                text(r, "tool", 4096, false)?;
                finite(r, "started", false)?;
                nullable_integer(r, "tokens")?;
                (
                    text(r, "label", 16384, true)?.into(),
                    "running".into(),
                    "provider".into(),
                    false,
                )
            }
            Section::Jobs | Section::Loops => {
                let expected = if section == Section::Jobs {
                    "cron"
                } else {
                    "loop"
                };
                if kind != expected || r["kind"] != expected || id.len() > 128 {
                    return Err(fail("Invalid scheduled job kind"));
                }
                text(r, "prompt", 65536, true)?;
                text(r, "schedule", 1024, true)?;
                let enabled = boolean(r, "enabled")?;
                boolean(r, "new_conversation")?;
                for key in ["created", "tool_profile", "owner_convo_id"] {
                    text(r, key, 4096, true)?;
                }
                for key in ["last_fired_slot", "next_run_at"] {
                    nullable_text(r, key, 4096)?;
                }
                integer(r, "run_count", 9_007_199_254_740_991)?;
                let interval = integer(r, "interval_minutes", 9_007_199_254_740_991)?;
                if section == Section::Loops
                    && (interval == 0
                        || r["owner_convo_id"].as_str().is_none_or(str::is_empty)
                        || r["next_run_at"].as_str().is_none_or(str::is_empty))
                {
                    return Err(fail("Invalid loop schedule"));
                }
                if !profile(text(v, "effective_tool_profile", 32, false)?) {
                    return Err(fail("Invalid effective scheduled profile"));
                }
                (
                    text(r, "label", 4096, true)?.into(),
                    if enabled { "enabled" } else { "disabled" }.into(),
                    "schedule".into(),
                    false,
                )
            }
        };
        Ok(Self {
            id,
            revision,
            kind,
            label,
            status,
            owner,
            tail_available,
            raw: r.clone(),
        })
    }
    pub fn details(&self) -> Vec<String> {
        let mut lines = vec![
            format!("{} · {} · {}", self.kind, self.status, self.owner),
            format!("ID: {}", self.id),
        ];
        let labels = [
            ("tool", "Tool"),
            ("prompt", "Request"),
            ("schedule", "Schedule"),
            ("tool_profile", "Requested tool profile"),
            ("run_count", "Runs"),
            ("interval_minutes", "Interval minutes"),
            ("next_run_at", "Next run"),
            ("started", "Started"),
            ("ended", "Ended"),
            ("ok", "Succeeded"),
            ("tokens", "Tokens"),
            ("log", "Recorded output path"),
            ("thread_id", "Provider thread"),
            ("turn_id", "Provider turn"),
        ];
        for (key, label) in labels {
            if let Some(v) = self.raw.get(key).filter(|v| !v.is_null()) {
                let value = v
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string());
                if !value.is_empty() {
                    lines.push(format!("{label}: {value}"));
                }
            }
        }
        lines
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    pub section: Section,
    pub offset: usize,
    pub total: usize,
    pub next: Option<usize>,
    pub rows: Vec<Row>,
}
impl Page {
    pub fn parse(
        v: &Value,
        s: &Snapshot,
        section: Section,
        offset: usize,
        limit: usize,
    ) -> io::Result<Self> {
        s.check(v, "work_page")?;
        if !s.source(section).available
            || v["section"] != section.key()
            || v["offset"].as_u64() != Some(offset as u64)
            || limit == 0
            || limit > 64
        {
            return Err(fail("Work page differs from its request"));
        }
        let total = integer(v, "total", 4096)? as usize;
        if total != s.source(section).count {
            return Err(fail("Work page count changed"));
        }
        let values = v["rows"]
            .as_array()
            .filter(|r| r.len() <= limit)
            .ok_or_else(|| fail("Invalid Work page rows"))?;
        let next = page_end(v, offset, total, values.len())?;
        let mut seen = HashSet::new();
        let mut rows = Vec::new();
        for value in values {
            let r = Row::parse(value, section, s)?;
            if !seen.insert(r.id.clone()) {
                return Err(fail("Duplicate Work row"));
            }
            rows.push(r);
        }
        Ok(Self {
            section,
            offset,
            total,
            next,
            rows,
        })
    }
}
fn page_end(v: &Value, offset: usize, total: usize, count: usize) -> io::Result<Option<usize>> {
    let end = offset
        .checked_add(count)
        .filter(|end| offset <= total && *end <= total)
        .ok_or_else(|| fail("Work page exceeded its total"))?;
    let expected = if end < total {
        if count == 0 {
            return Err(fail("Work page made no progress"));
        }
        Some(end)
    } else {
        None
    };
    if v.get("next_offset") != Some(&expected.map_or(Value::Null, |n| json!(n))) {
        return Err(fail("Invalid Work page continuation"));
    }
    Ok(expected)
}
#[derive(Clone, Debug, PartialEq)]
pub struct Tail {
    pub task_id: String,
    pub text: String,
    pub status: String,
    pub reason: Option<String>,
    pub path: String,
    pub total: u64,
    pub offset: u64,
    pub lossy: bool,
}
impl Tail {
    pub fn parse(v: &Value, s: &Snapshot, row: &Row) -> io::Result<Self> {
        s.check(v, "work_tail")?;
        if row.kind != "host_task" || v["task_id"] != row.id || v["task_revision"] != row.revision {
            return Err(fail("Output belongs to another selected task"));
        }
        let status = text(v, "status", 16, false)?.to_owned();
        let content = text(v, "text", 3 * 65536, true)?.to_owned();
        let why = nullable_text(v, "reason", 65536)?;
        let path = text(v, "path", 32768, false)?.to_owned();
        let total = integer(v, "total_bytes", 9_007_199_254_740_991)?;
        let offset = integer(v, "offset", total)?;
        let read = integer(v, "bytes_read", 65536)?;
        let truncated = boolean(v, "truncated")?;
        let lossy = boolean(v, "lossy")?;
        match status.as_str() {
            "available" => {
                hex(v, "revision")?;
                if why.is_some() || offset + read != total || truncated != (offset > 0) {
                    return Err(fail("Invalid task output bounds"));
                }
            }
            "absent" | "running" => {
                if !content.is_empty()
                    || total != 0
                    || offset != 0
                    || read != 0
                    || truncated
                    || lossy
                    || v["revision"] != "absent"
                    || why.as_deref().is_none_or(str::is_empty)
                {
                    return Err(fail("Unavailable task output reports content"));
                }
            }
            _ => return Err(fail("Unknown task output status")),
        }
        Ok(Self {
            task_id: row.id.clone(),
            text: content,
            status,
            reason: why,
            path,
            total,
            offset,
            lossy,
        })
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct PauseReview {
    pub snapshot: Snapshot,
    pub token: String,
    pub action_id: String,
    pub wire: Value,
}
impl PauseReview {
    pub fn parse(v: &Value, s: &Snapshot) -> io::Result<Self> {
        s.check(v, "work_pause_review")?;
        if !s.can_pause
            || v["action"] != "pause_schedules"
            || v["paused_before"] != false
            || v["quitting_before"] != false
            || v["scope"] != "runtime"
            || v["consequence"] != CONSEQUENCE
        {
            return Err(fail("Work pause review changed its effects"));
        }
        let token = hex(v, "review")?;
        let action_id = crate::stream::required_uuid4(v, "action_id")
            .map_err(|_| fail("Invalid pause action identity"))?
            .to_owned();
        Ok(Self {
            snapshot: s.clone(),
            token,
            action_id,
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.wire.clone();
        v.as_object_mut().expect("validated review").remove("kind");
        v
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct PauseReceipt {
    pub receipt: String,
}
impl PauseReceipt {
    pub fn parse(v: &Value, r: &PauseReview) -> io::Result<Self> {
        r.snapshot.check(v, "work_pause_result")?;
        if v["action"] != "pause_schedules"
            || v["review"] != r.token
            || v["action_id"] != r.action_id
            || v["status"] != "paused"
            || v["mutated"] != true
            || v["close_required"] != false
            || v["schedules_paused"] != true
            || v["quitting"] != false
        {
            return Err(fail("Unverified scheduler pause result"));
        }
        Ok(Self {
            receipt: hex(v, "receipt")?,
        })
    }
    pub fn request(&self, r: &PauseReview) -> Value {
        let mut v = r.snapshot.request();
        v["review"] = json!(r.token);
        v["action_id"] = json!(r.action_id);
        v["receipt"] = json!(self.receipt);
        v
    }
    pub fn verify(&self, v: &Value, r: &PauseReview) -> io::Result<()> {
        r.snapshot.check(v, "work_pause_readback")?;
        if v["review"] != r.token
            || v["action_id"] != r.action_id
            || v["receipt"] != self.receipt
            || v["schedules_paused"] != true
            || v["quitting"] != false
            || v["verified"] != true
        {
            return Err(fail("Scheduler pause readback disagrees"));
        }
        boolean(v, "busy")?;
        Ok(())
    }
}

pub fn month_parts(month: &str) -> io::Result<(u16, u8)> {
    let b = month.as_bytes();
    if b.len() != 7
        || b[4] != b'-'
        || b.iter()
            .enumerate()
            .any(|(i, c)| i != 4 && !c.is_ascii_digit())
    {
        return Err(fail("Invalid calendar month"));
    }
    let year = month[..4]
        .parse::<u16>()
        .map_err(|_| fail("Invalid calendar year"))?;
    let number = month[5..]
        .parse::<u8>()
        .map_err(|_| fail("Invalid calendar month"))?;
    if year == 0 || !(1..=12).contains(&number) {
        return Err(fail("Invalid calendar month"));
    }
    Ok((year, number))
}
pub fn shift_month(month: &str, forward: bool) -> String {
    let Ok((year, number)) = month_parts(month) else {
        return month.into();
    };
    let index = (year as i32 - 1) * 12 + number as i32 - 1;
    let next = (index + if forward { 1 } else { -1 }).clamp(0, 9999 * 12 - 1);
    format!("{:04}-{:02}", next / 12 + 1, next % 12 + 1)
}
fn days_in_month(year: u16, month: u8) -> u8 {
    match month {
        4 | 6 | 9 | 11 => 30,
        2 => {
            if year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400)) {
                29
            } else {
                28
            }
        }
        _ => 31,
    }
}
fn date_in_month(value: &str, month: &str) -> io::Result<u8> {
    let (year, number) = month_parts(month)?;
    let b = value.as_bytes();
    if b.len() != 10
        || !b.is_ascii()
        || &value[..7] != month
        || b[7] != b'-'
        || !b[8].is_ascii_digit()
        || !b[9].is_ascii_digit()
    {
        return Err(fail("Calendar day belongs to another month"));
    }
    let day = value[8..]
        .parse::<u8>()
        .map_err(|_| fail("Invalid calendar day"))?;
    if day == 0 || day > days_in_month(year, number) {
        return Err(fail("Invalid calendar day"));
    }
    Ok(day)
}
#[derive(Clone, Debug, PartialEq)]
pub struct DayEntry {
    pub job: String,
    pub label: String,
    pub times: Vec<String>,
    pub total: u64,
    pub enabled: bool,
    pub broken: bool,
}
impl DayEntry {
    fn parse(v: &Value) -> io::Result<Self> {
        let job = text(v, "job_id", 128, false)?.to_owned();
        let label = text(v, "label", 65536, true)?.to_owned();
        let times = v["times"]
            .as_array()
            .filter(|a| a.len() <= 2)
            .ok_or_else(|| fail("Invalid calendar firing preview"))?
            .iter()
            .map(|v| {
                let s = v.as_str().ok_or_else(|| fail("Invalid calendar time"))?;
                let b = s.as_bytes();
                if b.len() != 5
                    || b[2] != b':'
                    || b.iter()
                        .enumerate()
                        .any(|(i, b)| i != 2 && !b.is_ascii_digit())
                    || s[..2].parse::<u8>().unwrap() > 23
                    || s[3..].parse::<u8>().unwrap() > 59
                {
                    return Err(fail("Invalid calendar time"));
                }
                Ok(s.to_owned())
            })
            .collect::<io::Result<Vec<_>>>()?;
        let total = integer(v, "total", 1440)?;
        let broken = boolean(v, "broken")?;
        if broken && (total != 0 || !times.is_empty())
            || !broken && (total == 0 || times.len() as u64 > total)
        {
            return Err(fail("Calendar counts contradict firing preview"));
        }
        Ok(Self {
            job,
            label,
            times,
            total,
            enabled: boolean(v, "enabled")?,
            broken,
        })
    }
    pub fn summary(&self) -> String {
        format!(
            "{}{}{} · {}{}",
            if self.enabled { "" } else { "Disabled · " },
            if self.broken {
                "Invalid schedule · "
            } else {
                ""
            },
            self.times.join(", "),
            self.label,
            if self.total > self.times.len() as u64 {
                format!(" · {} firings", self.total)
            } else {
                String::new()
            }
        )
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Day {
    pub date: Option<String>,
    pub total: usize,
    pub entries: Vec<DayEntry>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Calendar {
    pub month: String,
    pub date: Option<String>,
    pub days: Vec<Day>,
    pub rows: Vec<DayEntry>,
    pub offset: usize,
    pub total: usize,
    pub next: Option<usize>,
}
impl Calendar {
    pub fn parse(
        v: &Value,
        s: &Snapshot,
        month: &str,
        date: Option<&str>,
        offset: usize,
        limit: usize,
    ) -> io::Result<Self> {
        s.check(v, "work_calendar")?;
        let (year, number) = month_parts(month)?;
        if !s.source(Section::Jobs).available
            || v["month"] != month
            || v.get("date") != Some(&date.map_or(Value::Null, |d| json!(d)))
        {
            return Err(fail("Calendar response differs from its request"));
        }
        let raw_days = v["days"]
            .as_array()
            .ok_or_else(|| fail("Invalid calendar grid"))?;
        let raw_rows = v["rows"]
            .as_array()
            .ok_or_else(|| fail("Invalid calendar entries"))?;
        if let Some(date) = date {
            date_in_month(date, month)?;
            if !raw_days.is_empty()
                || raw_rows.len() > limit
                || limit == 0
                || limit > 64
                || v["offset"].as_u64() != Some(offset as u64)
            {
                return Err(fail("Invalid selected calendar day"));
            }
            let total = integer(v, "total", 4096)? as usize;
            let next = page_end(v, offset, total, raw_rows.len())?;
            let rows = entries(raw_rows)?;
            return Ok(Self {
                month: month.into(),
                date: Some(date.into()),
                days: vec![],
                rows,
                offset,
                total,
                next,
            });
        }
        if raw_days.len() != 42
            || !raw_rows.is_empty()
            || v["offset"] != 0
            || v["total"] != 42
            || !v["next_offset"].is_null()
        {
            return Err(fail("Calendar must contain the complete six-week grid"));
        }
        let mut days = Vec::new();
        let mut expected = 1;
        let mut ended = false;
        let previous_year = u32::from(year) - 1;
        let before_month: u32 = (1..number).map(|m| u32::from(days_in_month(year, m))).sum();
        let weekday = ((365 * previous_year + previous_year / 4 - previous_year / 100
            + previous_year / 400
            + before_month)
            % 7) as usize;
        for (index, value) in raw_days.iter().enumerate() {
            let date = nullable_text(value, "date", 10)?;
            let total = integer(value, "total", 4096)? as usize;
            let raw = value["entries"]
                .as_array()
                .filter(|a| a.len() <= 2)
                .ok_or_else(|| fail("Invalid calendar cell preview"))?;
            let entries = entries(raw)?;
            if let Some(date) = &date {
                let day = date_in_month(date, month)?;
                if ended
                    || day != expected
                    || entries.len() != total.min(2)
                    || index != weekday + usize::from(day) - 1
                {
                    return Err(fail("Calendar omits or reorders a day or entry"));
                }
                expected += 1;
            } else {
                if expected > 1 {
                    ended = true;
                }
                if total != 0 || !entries.is_empty() {
                    return Err(fail("Outside-month cell contains work"));
                }
            }
            days.push(Day {
                date,
                total,
                entries,
            });
        }
        if expected != days_in_month(year, number) + 1 {
            return Err(fail("Calendar omitted a month day"));
        }
        Ok(Self {
            month: month.into(),
            date: None,
            days,
            rows: vec![],
            offset: 0,
            total: 42,
            next: None,
        })
    }
}
fn entries(raw: &[Value]) -> io::Result<Vec<DayEntry>> {
    let mut seen = HashSet::new();
    raw.iter()
        .map(|v| {
            let e = DayEntry::parse(v)?;
            if !seen.insert(e.job.clone()) {
                return Err(fail("Duplicate calendar job"));
            }
            Ok(e)
        })
        .collect()
}

const STOP_REVIEW_FIELDS: &[&str] = &[
    "kind",
    "version",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "instance",
    "snapshot",
    "task_id",
    "task_revision",
    "action",
    "review",
    "action_id",
    "producer_id",
    "worker_id",
    "lifecycle_instance",
    "owner_pid",
    "owner_instance",
    "owner_created",
    "stop_mode",
    "process_id",
    "scope",
    "consequence",
];
fn exact_fields(v: &Value, fields: &[&str]) -> io::Result<()> {
    let object = v.as_object().ok_or_else(|| fail("Expected Work object"))?;
    if object.len() != fields.len() || fields.iter().any(|key| !object.contains_key(*key)) {
        return Err(fail("Work receipt fields differ from its contract"));
    }
    Ok(())
}
fn positive(v: &Value, key: &str) -> io::Result<u64> {
    let result = integer(v, key, 9_007_199_254_740_991)?;
    if result == 0 {
        return Err(fail("Invalid Work process identity"));
    }
    Ok(result)
}
impl Row {
    pub fn can_stop(&self) -> bool {
        self.kind == "host_task" && self.owner == "current" && self.status == "running"
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct StopReview {
    pub snapshot: Snapshot,
    pub row: Row,
    pub token: String,
    pub action_id: String,
    pub wire: Value,
}
impl StopReview {
    pub fn parse(v: &Value, snapshot: &Snapshot, row: &Row) -> io::Result<Self> {
        snapshot.check(v, "work_stop_review")?;
        exact_fields(v, STOP_REVIEW_FIELDS)?;
        if !row.can_stop()
            || snapshot.quitting
            || v["task_id"] != row.id
            || v["task_revision"] != row.revision
            || v["action"] != "stop_task"
            || v["scope"] != "task"
            || v["consequence"] != STOP_CONSEQUENCE
        {
            return Err(fail("Task Stop review changed its selected task or effect"));
        }
        positive(v, "owner_pid")?;
        text(v, "owner_instance", 4096, false)?;
        nullable_text(v, "owner_created", 4096)?;
        for key in ["owner_pid", "owner_instance", "owner_created"] {
            if v[key] != row.raw[key] {
                return Err(fail("Task Stop owner differs from selected row"));
            }
        }
        let token = hex(v, "review")?;
        hex(v, "lifecycle_instance")?;
        for key in ["producer_id", "worker_id", "action_id"] {
            crate::stream::required_uuid4(v, key)
                .map_err(|_| fail("Invalid Task Stop identity"))?;
        }
        match text(v, "stop_mode", 32, false)? {
            "process" => {
                positive(v, "process_id")?;
            }
            "pending_spawn" if v["process_id"].is_null() => {}
            _ => return Err(fail("Task Stop process mode contradicts its identity")),
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            row: row.clone(),
            token,
            action_id: v["action_id"].as_str().expect("validated").into(),
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> Value {
        let mut value = self.wire.clone();
        value
            .as_object_mut()
            .expect("validated review")
            .remove("kind");
        value
    }
    fn result_identity(&self, v: &Value) -> io::Result<()> {
        self.snapshot.check(v, "work_stop_result")?;
        for key in STOP_REVIEW_FIELDS.iter().filter(|key| **key != "kind") {
            if v[*key] != self.wire[*key] {
                return Err(fail("Task Stop result changed reviewed authority"));
            }
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct StopReceipt {
    pub receipt: String,
    pub row: Row,
    row_wire: Value,
    pub proof: Value,
}
#[derive(Clone, Debug, PartialEq)]
pub enum StopResult {
    Stopped(Box<StopReceipt>),
    Refused(String),
    Uncertain(String),
}
impl StopResult {
    pub fn parse(v: &Value, review: &StopReview) -> io::Result<Self> {
        review.result_identity(v)?;
        let mut fields = STOP_REVIEW_FIELDS.to_vec();
        fields.extend([
            "status",
            "mutated",
            "close_required",
            "error",
            "row",
            "store_revision",
            "proof",
            "receipt",
        ]);
        exact_fields(v, &fields)?;
        let receipt = hex(v, "receipt")?;
        let error = nullable_text(v, "error", 2048)?;
        match text(v, "status", 16, false)? {
            "stopped"
                if v["mutated"] == true && v["close_required"] == false && error.is_none() =>
            {
                let row = stopped_row(&v["row"], review)?;
                stop_proof(&v["proof"], review)?;
                hex(v, "store_revision")?;
                Ok(Self::Stopped(Box::new(StopReceipt {
                    receipt,
                    row,
                    row_wire: v["row"].clone(),
                    proof: v["proof"].clone(),
                })))
            }
            status @ ("refused" | "uncertain")
                if v["mutated"] == (status == "uncertain")
                    && v["close_required"] == (status == "uncertain")
                    && v["row"].is_null()
                    && v["proof"].is_null()
                    && v["store_revision"].is_null() =>
            {
                let error = error
                    .filter(|e| !e.is_empty())
                    .ok_or_else(|| fail("Task Stop failure omitted its reason"))?;
                Ok(if status == "refused" {
                    Self::Refused(error)
                } else {
                    Self::Uncertain(error)
                })
            }
            _ => Err(fail("Task Stop outcome contradicts its proof or effects")),
        }
    }
}
fn stopped_row(v: &Value, review: &StopReview) -> io::Result<Row> {
    let row = Row::parse(v, Section::Tasks, &review.snapshot)?;
    if row.id != review.row.id
        || row.owner != "current"
        || row.status == "running"
        || v["row"]["ended"].is_null()
    {
        return Err(fail("Task Stop did not return its exact terminal task"));
    }
    for key in [
        "id",
        "tool",
        "label",
        "convo_id",
        "started",
        "prompt",
        "owner_pid",
        "owner_instance",
        "owner_created",
    ] {
        if v["row"][key] != review.row.raw[key] {
            return Err(fail("Task Stop terminal row changed its task identity"));
        }
    }
    Ok(row)
}
fn stop_proof(v: &Value, review: &StopReview) -> io::Result<()> {
    exact_fields(
        v,
        &[
            "producer_id",
            "worker_id",
            "lifecycle_instance",
            "worker_retired",
            "callbacks_retired",
            "process_retired",
            "pending_spawn_settled",
            "task_persisted",
            "process_state",
            "process_id",
        ],
    )?;
    for key in ["producer_id", "worker_id", "lifecycle_instance"] {
        if v[key] != review.wire[key] {
            return Err(fail("Task Stop retirement belongs to another worker"));
        }
    }
    for key in [
        "worker_retired",
        "callbacks_retired",
        "process_retired",
        "pending_spawn_settled",
        "task_persisted",
    ] {
        if !boolean(v, key)? {
            return Err(fail("Task Stop retirement is incomplete"));
        }
    }
    match text(v, "process_state", 32, false)? {
        "not_spawned"
            if review.wire["stop_mode"] == "pending_spawn" && v["process_id"].is_null() => {}
        "retired" => {
            positive(v, "process_id")?;
            if review.wire["stop_mode"] == "process" && v["process_id"] != review.wire["process_id"]
            {
                return Err(fail("Task Stop retired a different process"));
            }
        }
        _ => return Err(fail("Task Stop process retirement is inconsistent")),
    }
    Ok(())
}
impl StopReceipt {
    pub fn request(&self, review: &StopReview) -> Value {
        let mut value = review.snapshot.request();
        value["review"] = json!(review.token);
        value["action_id"] = json!(review.action_id);
        value["receipt"] = json!(self.receipt);
        value
    }
    pub fn verify(&self, v: &Value, review: &StopReview) -> io::Result<()> {
        review.snapshot.check(v, "work_stop_settled")?;
        exact_fields(
            v,
            &[
                "kind",
                "version",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "instance",
                "snapshot",
                "review",
                "action_id",
                "receipt",
                "task_id",
                "task_revision",
                "verified",
                "row",
                "proof",
                "store_revision",
            ],
        )?;
        if v["review"] != review.token
            || v["action_id"] != review.action_id
            || v["receipt"] != self.receipt
            || v["task_id"] != review.row.id
            || v["task_revision"] != review.row.revision
            || v["verified"] != true
            || v["row"] != self.row_wire
            || v["proof"] != self.proof
        {
            return Err(fail("Task Stop independent readback disagrees"));
        }
        stopped_row(&v["row"], review)?;
        stop_proof(&v["proof"], review)?;
        hex(v, "store_revision")?;
        Ok(())
    }
}
