//! Reviewed source-owned compaction preferences; no provider or storage access.
use crate::{live::terminal_text, prepared_skill::Draft, settings_editor, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

pub const READ: &str = "gui.settings.native_compact_snapshot";
pub const VALIDATE: &str = "gui.settings.native_compact_validate";
pub const SAVE: &str = "gui.settings.native_compact_save";
pub const MAX_BYTES: usize = 65_536;
pub const FIELDS: [&str; 8] = [
    "compact_keep_recent",
    "compact_max_tokens",
    "compact_max_tool_iters",
    "compact_thinking_level",
    "wake_after_compact",
    "clear_screen_after_compact",
    "autocompact_enabled",
    "autocompact_at_percent",
];
pub const LEVELS: [&str; 9] = [
    "default", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
];
const LABELS: [&str; 8] = [
    "Recent messages to keep",
    "Summary token cap",
    "Compaction tool rounds",
    "Compaction thinking preference",
    "Wake after compaction",
    "Clear prior display after compaction",
    "Auto-compact when context fills",
    "Auto-compact at percent of window",
];
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text<'a>(v: &'a Value, key: &str, max: usize) -> io::Result<&'a str> {
    settings_editor::text(v, key, max)
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail("Invalid compaction preference flag"))
}
fn optional(v: &Value, key: &str) -> io::Result<Option<String>> {
    if !v.as_object().is_some_and(|o| o.contains_key(key)) {
        return Err(fail("Missing compaction preference metadata"));
    }
    if v[key].is_null() {
        Ok(None)
    } else {
        text(v, key, 4096).map(|s| Some(s.into()))
    }
}
fn effort(v: &Value, key: &str) -> io::Result<Option<String>> {
    let value = optional(v, key)?;
    if value.as_ref().is_some_and(|s| s.len() > 64) {
        return Err(fail("Resolved compaction effort exceeds 64 UTF-8 bytes"));
    }
    Ok(value)
}
fn index(field: &str) -> io::Result<usize> {
    FIELDS
        .iter()
        .position(|f| *f == field)
        .ok_or_else(|| fail("Unsupported compaction preference"))
}
fn bounds(i: usize) -> Option<(u64, u64)> {
    match i {
        0 => Some((0, 1_000_000)),
        1 => Some((256, 2_147_483_647)),
        2 => Some((1, 512)),
        7 => Some((1, 99)),
        _ => None,
    }
}
fn scope(i: usize) -> &'static str {
    if i == 5 { "device" } else { "conversation" }
}
fn timing(i: usize) -> &'static str {
    if i == 5 { "immediate" } else { "next_turn" }
}
fn destination_key(i: usize) -> &'static str {
    if i == 5 { "global" } else { "conversation" }
}
fn scalar(i: usize, value: &Value) -> io::Result<()> {
    let valid = match bounds(i) {
        Some((min, max)) => value.as_u64().is_some_and(|n| (min..=max).contains(&n)),
        None if i == 3 => value.as_str().is_some_and(|s| LEVELS.contains(&s)),
        None => value.is_boolean(),
    };
    if valid {
        Ok(())
    } else {
        Err(fail(
            "Compaction preference has an invalid scalar type or value",
        ))
    }
}
fn shape(v: &Value, kind: &str, s: &str, a: &str, b: &str, m: &str) -> io::Result<()> {
    settings_editor::bounded(v, kind, s, a, b, m)?;
    if serde_json::to_vec(v)?.len() > MAX_BYTES {
        return Err(fail("Compaction preference response exceeds 64 KiB"));
    }
    Ok(())
}
fn destination(v: &Value, key: &str) -> io::Result<String> {
    let p = text(v, key, 4096)?;
    let normalized = p.replace('\\', "/");
    if !std::path::Path::new(p).is_absolute()
        || normalized.chars().any(char::is_control)
        || normalized.split('/').any(|s| matches!(s, "." | ".."))
        || !normalized.ends_with("/settings.json")
    {
        return Err(fail("Invalid compaction preference destination"));
    }
    Ok(p.into())
}
fn action_id(v: &Value) -> io::Result<String> {
    let s = text(v, "action_id", 36)?;
    crate::resume::conversation_id(s)?;
    if s.len() != 36
        || s.as_bytes()[14] != b'4'
        || !matches!(s.as_bytes()[19], b'8' | b'9' | b'a' | b'b')
    {
        return Err(fail("Invalid preference action identity"));
    }
    Ok(s.into())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Setting {
    pub saved: Value,
    pub effective: Value,
    pub editable: bool,
    pub reason: Option<String>,
    pub scope: String,
    pub apply_timing: String,
    pub destination: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reasoning {
    pub available: bool,
    pub reason: Option<String>,
    pub source: String,
    pub supported_levels: Vec<String>,
    pub saved: Option<String>,
    pub effective: Option<String>,
}
impl Reasoning {
    fn parse(v: &Value) -> io::Result<Self> {
        if v.as_object().is_none_or(|o| o.len() != 6) {
            return Err(fail("Invalid resolved reasoning metadata"));
        }
        let available = boolean(v, "available")?;
        let reason = optional(v, "reason")?;
        let source = text(v, "source", 1024)?.into();
        let rows = v["supported_levels"]
            .as_array()
            .filter(|r| r.len() <= 32)
            .ok_or_else(|| fail("Invalid supported reasoning levels"))?;
        let mut seen = HashSet::new();
        let mut supported_levels = Vec::new();
        for row in rows {
            let s = row
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 64 && !s.chars().any(char::is_control))
                .ok_or_else(|| fail("Invalid reasoning level"))?;
            if !seen.insert(s) {
                return Err(fail("Duplicate reasoning level"));
            }
            supported_levels.push(s.to_owned());
        }
        let saved = effort(v, "saved")?;
        let effective = effort(v, "effective")?;
        if available == reason.is_some()
            || (!available
                && (!supported_levels.is_empty() || saved.is_some() || effective.is_some()))
            || [&saved, &effective].iter().any(|v| {
                v.as_ref()
                    .is_some_and(|s| !supported_levels.is_empty() && !supported_levels.contains(s))
            })
        {
            return Err(fail("Contradictory resolved reasoning metadata"));
        }
        Ok(Self {
            available,
            reason,
            source,
            supported_levels,
            saved,
            effective,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub token: String,
    pub revisions: Value,
    pub destinations: Value,
    pub fields: [Setting; 8],
    pub tools: Value,
    pub reasoning: Reasoning,
}
impl Snapshot {
    pub fn parse(
        v: &Value,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        shape(
            v,
            "compaction_settings_snapshot",
            session,
            agent,
            backend,
            model,
        )?;
        if v["destinations"].as_object().is_none_or(|o| o.len() != 2) {
            return Err(fail("Invalid preference destinations"));
        }
        let global = destination(&v["destinations"], "global")?;
        let conversation = destination(&v["destinations"], "conversation")?;
        if global == conversation
            || !conversation
                .replace('\\', "/")
                .ends_with(&format!("/conversations/{session}/settings.json"))
        {
            return Err(fail(
                "Preference destination is not the selected conversation",
            ));
        }
        let rows = v["fields"]
            .as_object()
            .filter(|o| o.len() == FIELDS.len())
            .ok_or_else(|| fail("Compaction preferences must contain exactly eight fields"))?;
        let mut fields = Vec::new();
        for (i, key) in FIELDS.iter().enumerate() {
            let f = rows
                .get(*key)
                .ok_or_else(|| fail("Missing compaction preference"))?;
            let (min, max) =
                bounds(i).map_or((Value::Null, Value::Null), |(a, b)| (json!(a), json!(b)));
            let kind = if bounds(i).is_some() {
                "integer"
            } else if i == 3 {
                "choice"
            } else {
                "boolean"
            };
            if f.as_object().is_none_or(|o| o.len() != 11)
                || f["type"] != kind
                || f["scope"] != scope(i)
                || f["apply_timing"] != timing(i)
                || f["destination"] != destination_key(i)
                || f["minimum"] != min
                || f["maximum"] != max
                || f["choices"] != if i == 3 { json!(LEVELS) } else { json!([]) }
            {
                return Err(fail("Unsupported compaction field metadata"));
            }
            scalar(i, &f["saved"])?;
            scalar(i, &f["effective"])?;
            let editable = boolean(f, "editable")?;
            let reason = optional(f, "reason")?;
            if editable == reason.is_some() {
                return Err(fail("Contradictory preference edit authority"));
            }
            fields.push(Setting {
                saved: f["saved"].clone(),
                effective: f["effective"].clone(),
                editable,
                reason,
                scope: scope(i).into(),
                apply_timing: timing(i).into(),
                destination: destination_key(i).into(),
            });
        }
        if v["tools"].as_object().is_none_or(|o| o.len() != 3) || v["tools"]["editable"] != false {
            return Err(fail("Tools are not editable in compaction preferences"));
        }
        boolean(&v["tools"], "effective")?;
        text(&v["tools"], "reason", 4096)?;
        let reasoning = Reasoning::parse(&v["reasoning"])?;
        if reasoning.available != fields[3].editable {
            return Err(fail("Reasoning availability differs from edit authority"));
        }
        Ok(Self {
            session: session.into(),
            agent: agent.into(),
            backend: backend.into(),
            model: model.into(),
            token: settings_editor::token(v, "snapshot")?,
            revisions: settings_editor::revisions(&v["revisions"])?,
            destinations: v["destinations"].clone(),
            fields: fields.try_into().expect("eight fields"),
            tools: v["tools"].clone(),
            reasoning,
        })
    }
    pub fn field(&self, field: &str) -> io::Result<&Setting> {
        Ok(&self.fields[index(field)?])
    }
    pub fn effective(&self, field: &str) -> io::Result<&Value> {
        Ok(&self.field(field)?.effective)
    }
    pub fn request(&self, field: &str, value: &Value) -> io::Result<Value> {
        let f = self.field(field)?;
        if !f.editable {
            return Err(fail(
                f.reason.as_deref().unwrap_or("Preference is read-only"),
            ));
        }
        scalar(index(field)?, value)?;
        Ok(
            json!({"version":1,"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"model":self.model,"snapshot":self.token,"revisions":self.revisions,"field":field,"value":value}),
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub snapshot: Snapshot,
    pub field: String,
    pub value: Value,
    pub token: String,
    pub action_id: String,
    pub destination: String,
    pub resolved_value: Option<String>,
    wire: Value,
}
impl Review {
    pub fn parse(v: &Value, snapshot: &Snapshot, field: &str, value: &Value) -> io::Result<Self> {
        shape(
            v,
            "compaction_settings_review",
            &snapshot.session,
            &snapshot.agent,
            &snapshot.backend,
            &snapshot.model,
        )?;
        snapshot.request(field, value)?;
        let f = snapshot.field(field)?;
        if v["snapshot"] != snapshot.token
            || v["revisions"] != snapshot.revisions
            || v["field"] != field
            || v["value"] != *value
            || v["scope"] != f.scope
            || v["apply_timing"] != f.apply_timing
            || v["destination"] != snapshot.destinations[&f.destination]
            || v["saved_before"] != f.saved
            || v["effective_before"] != f.effective
        {
            return Err(fail("Compaction review differs from the requested edit"));
        }
        let resolved_value = effort(v, "resolved_value")?;
        if (field != "compact_thinking_level" && resolved_value.is_some())
            || resolved_value.as_ref().is_some_and(|r| {
                !snapshot.reasoning.supported_levels.is_empty()
                    && !snapshot.reasoning.supported_levels.contains(r)
            })
        {
            return Err(fail("Invalid reviewed reasoning resolution"));
        }
        if field == "compact_thinking_level"
            && ((*value == f.saved && resolved_value != snapshot.reasoning.saved)
                || (*value == f.effective && resolved_value != snapshot.reasoning.effective))
        {
            return Err(fail(
                "Reviewed reasoning changed for an unchanged preference",
            ));
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            field: field.into(),
            value: value.clone(),
            token: settings_editor::token(v, "review")?,
            action_id: action_id(v)?,
            destination: destination(v, "destination")?,
            resolved_value,
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> io::Result<Value> {
        let mut v = self.wire.clone();
        v.as_object_mut()
            .ok_or_else(|| fail("Invalid retained preference review"))?
            .remove("kind");
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub status: String,
    pub saved: Value,
    pub effective: Value,
    pub runtime: String,
    pub action: String,
    pub reason: Option<String>,
    pub revisions: Value,
}
impl Outcome {
    pub fn parse(v: &Value, r: &Review) -> io::Result<Result<Self, String>> {
        let s = &r.snapshot;
        let i = index(&r.field)?;
        let f = s.field(&r.field)?;
        shape(
            v,
            "compaction_settings_save",
            &s.session,
            &s.agent,
            &s.backend,
            &s.model,
        )?;
        if v["snapshot"] != s.token
            || v["review"] != r.token
            || v["action_id"] != r.action_id
            || v["field"] != r.field
            || v["value"] != r.value
            || v["scope"] != f.scope
            || v["apply_timing"] != f.apply_timing
            || v["destination"] != r.destination
        {
            return Err(fail("Preference receipt does not match confirmed edit"));
        }
        let mutated = boolean(v, "mutated")?;
        let close = boolean(v, "close_required")?;
        let status = text(v, "status", 16)?;
        if close || status == "failed" {
            return Err(fail(
                "Preference save could not establish a safe state; reconnect required",
            ));
        }
        let persistence = v["persistence"]
            .as_array()
            .filter(|r| r.len() <= 1)
            .ok_or_else(|| fail("Invalid preference persistence receipt"))?;
        if status == "refused" {
            if mutated || v["runtime"] != json!([]) {
                return Err(fail("Refused preference save reports effects"));
            }
            if let Some(p) = persistence.first() {
                if p["destination"] != r.destination
                    || p["scope"] != f.scope
                    || p["saved"] != false
                    || p["fields"] != json!([r.field])
                    || text(p, "error", 4096).is_err()
                    || v["revisions"] != s.revisions
                    || v["saved"] != f.saved
                    || v["effective"] != f.effective
                {
                    return Err(fail("Contradictory non-writing preference refusal"));
                }
            } else if !v["saved"].is_null()
                || !v["effective"].is_null()
                || !v["revisions"].is_null()
            {
                return Err(fail("Preflight refusal reports uncaptured values"));
            }
            return Ok(Err(text(v, "error", 4096)?.into()));
        }
        if !mutated || !matches!(status, "saved" | "partial") || persistence.len() != 1 {
            return Err(fail("Invalid preference save status"));
        }
        let revisions = settings_editor::revisions(&v["revisions"])?;
        let p = &persistence[0];
        if p["destination"] != r.destination
            || p["scope"] != f.scope
            || p["saved"] != true
            || !p["error"].is_null()
            || p["fields"] != json!([r.field])
            || p["revision"] != revisions[&f.destination]
            || revisions[&f.destination] == "absent"
        {
            return Err(fail("Preference persistence proof disagrees"));
        }
        for key in ["global", "conversation", "agent"] {
            if key != f.destination && revisions[key] != s.revisions[key] {
                return Err(fail("Unselected preference destination changed"));
            }
        }
        let rows = v["runtime"]
            .as_array()
            .filter(|r| r.len() == 1)
            .ok_or_else(|| fail("Invalid preference runtime receipt"))?;
        let row = &rows[0];
        scalar(i, &v["saved"])?;
        scalar(i, &v["effective"])?;
        let runtime = text(row, "status", 16)?;
        let action = text(row, "action", 16)?;
        let reason = optional(row, "reason")?;
        if v["saved"] != r.value
            || row["field"] != r.field
            || row["scope"] != f.scope
            || row["requested"] != v["saved"]
            || row["effective"] != v["effective"]
            || (v["effective"] != r.value && v["effective"] != f.effective)
            || !matches!(runtime, "applied" | "pending" | "failed")
            || !matches!(
                action,
                "none" | "retry" | "reconnect" | "reload" | "restart"
            )
        {
            return Err(fail("Preference runtime proof disagrees"));
        }
        if status == "saved" {
            if runtime != "applied"
                || action != "none"
                || v["effective"] != r.value
                || reason.is_some()
                || !v["error"].is_null()
            {
                return Err(fail("Contradictory saved preference receipt"));
            }
        } else if reason.is_none()
            || !matches!(
                (runtime, action),
                ("failed", "retry") | ("pending", "restart" | "reconnect" | "reload")
            )
        {
            return Err(fail(
                "Partial preference save lacks a canonical runtime limitation",
            ));
        }
        Ok(Ok(Self {
            status: status.into(),
            saved: v["saved"].clone(),
            effective: v["effective"].clone(),
            runtime: runtime.into(),
            action: action.into(),
            reason,
            revisions,
        }))
    }
    pub fn verify(&self, after: &Snapshot, r: &Review) -> io::Result<()> {
        let old = &r.snapshot;
        if after.session != old.session
            || after.agent != old.agent
            || after.backend != old.backend
            || after.model != old.model
            || after.revisions != self.revisions
            || after.destinations != old.destinations
            || after.tools != old.tools
        {
            return Err(fail("Preference identity or independent readback changed"));
        }
        for (i, name) in FIELDS.iter().enumerate() {
            let mut expected = old.fields[i].clone();
            if *name == r.field {
                expected.saved = self.saved.clone();
                expected.effective = self.effective.clone();
            }
            if after.fields[i] != expected {
                return Err(fail("Preference field differs from independent readback"));
            }
        }
        let mut expected = old.reasoning.clone();
        if r.field == "compact_thinking_level" {
            expected.saved = r.resolved_value.clone();
            expected.effective = if self.effective == r.value {
                r.resolved_value.clone()
            } else {
                old.reasoning.effective.clone()
            };
        }
        if after.reasoning != expected {
            return Err(fail("Resolved reasoning differs from confirmed preference"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Read {
        generation: u64,
    },
    Validate {
        generation: u64,
        snapshot: String,
        field: String,
        value: Value,
    },
    Save {
        generation: u64,
        review: Box<Review>,
    },
}
impl Intent {
    pub fn generation(&self) -> u64 {
        match self {
            Self::Read { generation }
            | Self::Validate { generation, .. }
            | Self::Save { generation, .. } => *generation,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot {
        generation: u64,
        snapshot: Box<Snapshot>,
    },
    Review {
        generation: u64,
        review: Box<Review>,
    },
    Saved {
        generation: u64,
        outcome: Box<Outcome>,
        snapshot: Box<Snapshot>,
    },
    Rejected {
        generation: u64,
        reason: String,
    },
}
impl Update {
    pub fn generation(&self) -> u64 {
        match self {
            Self::Snapshot { generation, .. }
            | Self::Review { generation, .. }
            | Self::Saved { generation, .. }
            | Self::Rejected { generation, .. } => *generation,
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Read,
    Validate,
    Save,
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub snapshot: Option<Snapshot>,
    selected: usize,
    drafts: [Draft; 8],
    initialized: bool,
    review: Option<Review>,
    intent: Option<Intent>,
    generation: u64,
    pending: Option<(u64, Phase)>,
    visible: bool,
    reviewed: bool,
    geometry: Option<Rect>,
    page: usize,
    seen: Vec<bool>,
}
impl View {
    pub fn open(&mut self) {
        self.open_field(FIELDS[0]);
    }
    pub fn open_field(&mut self, field: &str) {
        if self.waiting {
            return;
        }
        let Ok(selected) = index(field) else {
            return;
        };
        if !self.open {
            self.initialized = false;
            self.drafts = Default::default();
        }
        self.selected = selected;
        self.open = true;
        self.refresh();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn pending_generation(&self) -> Option<u64> {
        self.pending.map(|(g, _)| g)
    }
    pub fn selected_field(&self) -> &'static str {
        FIELDS[self.selected]
    }
    pub fn draft(&self) -> &str {
        self.drafts[self.selected].text()
    }
    pub fn editing(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self.page == 0
            && self
                .snapshot
                .as_ref()
                .is_some_and(|s| s.fields[self.selected].editable)
    }
    pub fn accepts_paste(&self) -> bool {
        self.editing() && bounds(self.selected).is_some()
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
        self.hide();
        let saving = self.pending.is_some_and(|(_, p)| p == Phase::Save) && self.intent.is_none();
        if !saving {
            self.pending = None;
            self.waiting = false;
            self.intent = None;
            self.review = None;
        }
    }
    pub fn finish(&mut self) {
        self.close();
        self.pending = None;
        self.waiting = false;
        self.intent = None;
        self.review = None;
        self.snapshot = None;
        self.initialized = false;
        self.drafts = Default::default();
        self.seen.clear();
        self.page = 0;
    }
    fn next(&mut self, phase: Phase) -> Option<u64> {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice = "Preference request limit reached; reconnect.".into();
            return None;
        };
        self.generation = generation;
        self.pending = Some((generation, phase));
        self.waiting = true;
        self.hide();
        Some(generation)
    }
    fn refresh(&mut self) {
        if let Some(generation) = self.next(Phase::Read) {
            self.intent = Some(Intent::Read { generation });
            self.review = None;
            self.notice = "Reading compaction preferences…".into();
        }
    }
    pub fn observe(&mut self, update: Update) {
        let Some((generation, phase)) = self.pending else {
            return;
        };
        if update.generation() != generation
            || !matches!(
                (&update, phase),
                (Update::Snapshot { .. }, Phase::Read)
                    | (Update::Review { .. }, Phase::Validate)
                    | (Update::Saved { .. }, Phase::Save)
                    | (Update::Rejected { .. }, _)
            )
        {
            return;
        }
        if let Update::Review { review, .. } = &update {
            let expected = self.value();
            if self.snapshot.as_ref() != Some(&review.snapshot)
                || review.field != self.selected_field()
                || expected.as_ref().ok() != Some(&review.value)
            {
                return;
            }
        }
        self.pending = None;
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.seen.clear();
        self.page = 0;
        match update {
            Update::Snapshot { snapshot, .. } => {
                if !self.initialized {
                    for (i, f) in snapshot.fields.iter().enumerate() {
                        self.drafts[i]
                            .set(&display_value(&f.saved))
                            .expect("bounded scalar");
                    }
                    self.initialized = true;
                }
                self.notice = "Select a field; edit locally, then Enter to review.".into();
                self.snapshot = Some(*snapshot);
                self.review = None;
            }
            Update::Review { review, .. } => {
                self.review = Some(*review);
                self.notice =
                    "Read every page; Enter saves only after the last page is visible.".into();
            }
            Update::Saved {
                snapshot, outcome, ..
            } => {
                self.notice = if outcome.status == "saved" {
                    format!(
                        "Saved {}. Preference applies {}; no compaction was run.",
                        display_value(&outcome.saved),
                        timing(self.selected)
                    )
                } else {
                    format!(
                        "Saved {}. Effective {}. Runtime {}: {}",
                        display_value(&outcome.saved),
                        display_value(&outcome.effective),
                        outcome.runtime,
                        outcome.reason.as_deref().unwrap_or("pending")
                    )
                };
                self.snapshot = Some(*snapshot);
                self.review = None;
            }
            Update::Rejected { reason, .. } => {
                self.review = None;
                self.snapshot = None;
                self.notice = format!("{reason} · F5 refresh; drafts kept");
            }
        }
    }
    fn value(&self) -> io::Result<Value> {
        let value = if bounds(self.selected).is_some() {
            json!(
                self.draft()
                    .parse::<u64>()
                    .map_err(|_| fail("Enter a decimal integer"))?
            )
        } else if self.selected == 3 {
            json!(self.draft())
        } else {
            json!(
                self.draft()
                    .parse::<bool>()
                    .map_err(|_| fail("Choose true or false"))?
            )
        };
        scalar(self.selected, &value)?;
        Ok(value)
    }
    pub fn paste(&mut self, text: &str) {
        self.reviewed = false;
        if !self.accepts_paste() {
            return;
        }
        if text.is_empty()
            || !text.bytes().all(|b| b.is_ascii_digit())
            || self.draft().len().saturating_add(text.len()) > 10
        {
            self.notice = "Use at most 10 decimal digits; draft kept.".into();
            return;
        }
        self.drafts[self.selected]
            .insert(text)
            .expect("bounded numeric draft");
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !key.modifiers.is_empty() {
            self.reviewed = false;
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if !self.waiting && self.review.take().is_some() {
                self.hide();
                self.page = 0;
                self.seen.clear();
                self.notice = "Review cancelled; draft kept.".into();
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if key.code == KeyCode::F(5) && key.kind == KeyEventKind::Press {
            self.refresh();
            return;
        }
        if let Some(review) = self.review.as_ref() {
            match key.code {
                KeyCode::PageDown if self.reviewed => {
                    self.page = (self.page + 1).min(self.seen.len().saturating_sub(1));
                    self.reviewed = false;
                }
                KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    self.reviewed = false;
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && self.reviewed
                        && self.page + 1 == self.seen.len()
                        && self.seen.iter().all(|seen| *seen) =>
                {
                    let review = review.clone();
                    if let Some(generation) = self.next(Phase::Save) {
                        self.intent = Some(Intent::Save {
                            generation,
                            review: Box::new(review),
                        });
                        self.notice = "Saving the confirmed preference…".into();
                    }
                }
                _ => self.reviewed = false,
            }
            return;
        }
        match key.code {
            KeyCode::PageDown => {
                self.page = (self.page + 1).min(self.seen.len().saturating_sub(1));
                self.hide();
                return;
            }
            KeyCode::PageUp => {
                self.page = self.page.saturating_sub(1);
                self.hide();
                return;
            }
            KeyCode::Tab | KeyCode::Down => {
                self.selected = (self.selected + 1) % FIELDS.len();
                self.page = 0;
                self.seen.clear();
                return;
            }
            KeyCode::Up => {
                self.selected = (self.selected + FIELDS.len() - 1) % FIELDS.len();
                self.page = 0;
                self.seen.clear();
                return;
            }
            _ => {}
        }
        if !self.editing() {
            return;
        }
        match key.code {
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                match self.value().and_then(|v| {
                    self.snapshot
                        .as_ref()
                        .expect("editable")
                        .request(self.selected_field(), &v)
                        .map(|_| v)
                }) {
                    Ok(value) => {
                        let snapshot = self.snapshot.as_ref().expect("editable").token.clone();
                        let field = self.selected_field().to_owned();
                        if let Some(generation) = self.next(Phase::Validate) {
                            self.intent = Some(Intent::Validate {
                                generation,
                                snapshot,
                                field,
                                value,
                            });
                            self.notice = "Validating the preference with the source…".into();
                        }
                    }
                    Err(e) => self.notice = e.to_string(),
                }
            }
            KeyCode::Char(c) if bounds(self.selected).is_some() && c.is_ascii_digit() => {
                self.paste(&c.to_string())
            }
            code if bounds(self.selected).is_some() => self.drafts[self.selected].key(code),
            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ') => {
                let options: Vec<&str> = if self.selected == 3 {
                    LEVELS.to_vec()
                } else {
                    vec!["false", "true"]
                };
                let position = options.iter().position(|v| *v == self.draft()).unwrap_or(0);
                let delta = if key.code == KeyCode::Left {
                    options.len() - 1
                } else {
                    1
                };
                self.drafts[self.selected]
                    .set(options[(position + delta) % options.len()])
                    .expect("bounded choice");
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, _event: MouseEvent) {
        self.reviewed = false;
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            self.hide();
            return;
        }
        if area.width < 48 || area.height < 20 {
            self.hide();
            self.seen.clear();
            self.page = 0;
            if area.width >= 24 && area.height >= 6 {
                frame.render_widget(Clear, area);
                frame.render_widget(
                    Paragraph::new("Compaction preferences need 48×20. Resize, or Esc to return.")
                        .block(Block::bordered().title(" Compaction preferences ")),
                    area,
                );
            }
            return;
        }
        let w = area.width.saturating_sub(2).min(100);
        let h = area.height.saturating_sub(2).min(32);
        let rect = Rect::new(
            area.x + (area.width - w) / 2,
            area.y + (area.height - h) / 2,
            w,
            h,
        );
        if self.geometry != Some(rect) {
            self.geometry = Some(rect);
            self.reviewed = false;
            self.seen.clear();
            self.page = 0;
        }
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(" Compaction preferences ")
            .style(Style::default().fg(theme.text()).bg(theme.screen()))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let body = Rect::new(
            inner.x,
            inner.y,
            inner.width,
            inner.height.saturating_sub(4),
        );
        let mut lines = Vec::new();
        if let Some(review) = &self.review {
            let f = review
                .snapshot
                .field(&review.field)
                .expect("validated review");
            lines.extend([
                "CONFIRM PREFERENCE SAVE".into(),
                format!("Field: {}", review.field),
                format!("Session: {}", review.snapshot.session),
                format!(
                    "Backend/model: {} / {}",
                    review.snapshot.backend, review.snapshot.model
                ),
                format!(
                    "Saved: {} → {}",
                    display_value(&f.saved),
                    display_value(&review.value)
                ),
                format!("Effective before: {}", display_value(&f.effective)),
                format!("Scope: {} · applies: {}", f.scope, f.apply_timing),
                format!("Destination: {}", review.destination),
            ]);
            if review.field == "compact_thinking_level" {
                lines.push(format!(
                    "Resolved request effort: {}",
                    review.resolved_value.as_deref().unwrap_or("omitted")
                ));
            }
            lines.extend(disclosures().into_iter().map(str::to_owned));
            if review.field.starts_with("autocompact_") {
                lines.push(AUTOCOMPACT_DISCLOSURE.into());
            }
        } else {
            for (i, label) in LABELS.iter().enumerate() {
                lines.push(format!(
                    "{} {}: {}",
                    if self.selected == i { ">" } else { " " },
                    label,
                    self.drafts[i].text()
                ));
            }
            if let Some(s) = &self.snapshot {
                let f = &s.fields[self.selected];
                lines.extend([
                    String::new(),
                    format!(
                        "Saved {} · effective {}",
                        display_value(&f.saved),
                        display_value(&f.effective)
                    ),
                    format!("{} · {}", f.scope, f.apply_timing),
                    format!("Tools enabled: {} (read-only)", s.tools["effective"]),
                    format!(
                        "Resolved effort: saved {}; effective {}",
                        s.reasoning.saved.as_deref().unwrap_or("omitted"),
                        s.reasoning.effective.as_deref().unwrap_or("omitted")
                    ),
                ]);
                if let Some(reason) = &f.reason {
                    lines.push(reason.clone());
                }
            }
        }
        let lines: Vec<String> = lines
            .iter()
            .flat_map(|s| {
                crate::ui::wrap(
                    &terminal_text(s).replace('\n', "\\n").replace('\t', "\\t"),
                    body.width as usize,
                )
            })
            .collect();
        let pages = lines.len().div_ceil(body.height as usize).max(1);
        if self.seen.len() != pages {
            self.seen = vec![false; pages];
            self.page = 0;
        }
        self.page = self.page.min(pages - 1);
        if self.review.is_some() {
            self.seen[self.page] = true;
            self.reviewed = self.open && !self.waiting;
        }
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(self.page * body.height as usize)
                    .take(body.height as usize)
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            body,
        );
        let selected_reason = self
            .snapshot
            .as_ref()
            .filter(|_| self.review.is_none() && !self.waiting)
            .and_then(|s| s.fields[self.selected].reason.as_deref());
        let notice =
            selected_reason.map(|reason| format!("Read-only · PgUp/PgDn for details: {reason}"));
        let notice = crate::ui::wrap(
            &terminal_text(notice.as_deref().unwrap_or(&self.notice)).replace('\n', "\\n"),
            inner.width as usize,
        )
        .into_iter()
        .take(2)
        .collect::<Vec<_>>()
        .join("\n");
        frame.render_widget(
            Paragraph::new(notice),
            Rect::new(inner.x, inner.bottom() - 4, inner.width, 2),
        );
        let help = if self.review.is_some() {
            format!(
                "Page {}/{} · PgUp/PgDn · Enter save",
                self.page + 1,
                self.seen.len()
            )
        } else {
            "↑/↓/Tab field · ←/→ choice · Enter review".into()
        };
        frame.render_widget(
            Paragraph::new(help),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
        );
        let footer = if self.review.is_some() {
            "F5 refresh · Esc back · local drafts kept".into()
        } else {
            format!(
                "PgUp/PgDn {}/{} · F5 refresh · Esc back",
                self.page + 1,
                pages
            )
        };
        frame.render_widget(
            Paragraph::new(footer),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
        if self.accepts_paste() {
            let x = body.x
                + 4
                + LABELS[self.selected].len() as u16
                + self.drafts[self.selected].cursor() as u16;
            if x < body.right() {
                frame.set_cursor_position((x, body.y + self.selected as u16));
            }
        }
    }
}
fn display_value(v: &Value) -> String {
    v.as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string())
}
const AUTOCOMPACT_DISCLOSURE: &str = "Auto-compact is tested after a turn and between tool rounds against the percent of the context window in use. The next such test uses the saved value and may start a compaction if the context is already past it; saving never starts one. The official Codex engine owns its own context, so this does not apply there.";
fn disclosures() -> [&'static str; 5] {
    [
        "Saving preferences does not run compaction, wake a turn, or clear the display now.",
        "The summary token cap also affects tool/card summaries, goal evaluation, and subagent requests.",
        "Recent messages is a safe-tail target; tool-safe retention can keep more rows.",
        "Thinking is a saved preference; the adapter can lower or omit the request effort.",
        "Tools remain shared turn authority and are read-only here.",
    ]
}
