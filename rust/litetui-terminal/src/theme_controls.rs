//! Source-owned theme selection and editing. No filesystem or runtime mutation.
use crate::{
    color_picker::{Pick, Picker},
    live::terminal_text,
    prepared_skill::Draft,
    theme::Theme,
};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    io,
};

pub const SNAPSHOT: &str = "gui.themes.native_snapshot";
pub const VALIDATE: &str = "gui.themes.native_validate";
pub const SAVE: &str = "gui.themes.native_save";
pub const OPERATIONS: [&str; 3] = [SNAPSHOT, VALIDATE, SAVE];
pub const MAX_BYTES: usize = 1024 * 1024;
pub const MAX_ROWS: usize = 256;
pub const TEXT_BYTES: usize = 256;
pub const COLORS: [&str; 14] = [
    "background",
    "surface",
    "primary",
    "panel",
    "tool_background",
    "foreground",
    "title",
    "assistant_border",
    "thinking_text",
    "thinking_border",
    "tool_text",
    "error",
    "footer_foreground",
    "footer_background",
];
pub const TOKENS: [&str; 15] = [
    "primary",
    "secondary",
    "accent",
    "success",
    "warning",
    "error",
    "background",
    "surface",
    "panel",
    "foreground",
    "thinking-text",
    "thinking-box",
    "tool-text",
    "footer-foreground",
    "footer-background",
];
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text(v: &Value, key: &str, limit: usize, empty: bool) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| (empty || !s.is_empty()) && s.len() <= limit)
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid theme {key}")))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid theme {key}")))
}
fn hash(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64, false)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid theme revision or definition"));
    }
    Ok(s)
}
fn optional(v: &Value, key: &str, limit: usize) -> io::Result<Option<String>> {
    if !v.as_object().is_some_and(|o| o.contains_key(key)) {
        return Err(fail("Missing optional theme field"));
    }
    if v[key].is_null() {
        Ok(None)
    } else {
        text(v, key, limit, true).map(Some)
    }
}
fn reason(v: &Value, flag: &str) -> io::Result<(bool, Option<String>)> {
    let eligible = boolean(v, flag)?;
    let why = optional(v, "reason", 4096)?;
    if eligible == why.is_some() || why.as_ref().is_some_and(String::is_empty) {
        return Err(fail("Contradictory theme eligibility"));
    }
    Ok((eligible, why))
}
fn revisions(v: &Value) -> io::Result<Value> {
    if v.as_object().is_none_or(|o| o.len() != 3) {
        return Err(fail("Invalid theme revisions"));
    }
    for key in ["global", "conversation", "agent"] {
        if v[key] != "absent" {
            hash(v, key)?;
        }
    }
    Ok(v.clone())
}
fn shape(v: &Value, kind: &str) -> io::Result<()> {
    if serde_json::to_vec(v)?.len() > MAX_BYTES || v["version"] != 1 || v["kind"] != kind {
        return Err(fail("Unsupported or oversized theme response"));
    }
    Ok(())
}
fn scope(v: &Value) -> io::Result<()> {
    if v["scope"] != "device" || v["apply_timing"] != "immediate" {
        return Err(fail("Unsupported theme scope or timing"));
    }
    Ok(())
}
fn hex(s: &str) -> bool {
    s.len() == 7 && s.starts_with('#') && s[1..].bytes().all(|b| b.is_ascii_hexdigit())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
}
impl Identity {
    pub fn command(&self, operation: &str) -> Value {
        json!({"type":operation,"version":1,"session_id":self.session,
            "agent_id":self.agent,"backend":self.backend,"model":self.model})
    }
    pub fn verify(&self, v: &Value) -> io::Result<()> {
        if v["session_id"] != self.session
            || v["agent_id"] != self.agent
            || v["backend"] != self.backend
            || v["model"] != self.model
        {
            return Err(fail("Theme response changed owned identity or provider"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Palette(pub [[u8; 3]; 14]);
impl Palette {
    pub fn parse(v: &Value) -> io::Result<Self> {
        if v.as_object().is_none_or(|o| o.len() != COLORS.len()) {
            return Err(fail("Invalid theme palette keys"));
        }
        let mut values = [[0; 3]; 14];
        for (index, key) in COLORS.iter().enumerate() {
            let rgb = v[*key]
                .as_array()
                .filter(|a| a.len() == 3)
                .ok_or_else(|| fail("Invalid theme RGB triple"))?;
            for (channel, n) in rgb.iter().enumerate() {
                values[index][channel] = n
                    .as_u64()
                    .and_then(|n| u8::try_from(n).ok())
                    .ok_or_else(|| fail("Theme RGB channel is outside 0..255"))?;
            }
        }
        Ok(Self(values))
    }
    pub fn theme(&self, name: &str) -> Theme {
        use ratatui::style::Color;
        let c = |i: usize| {
            let [r, g, b] = self.0[i];
            Color::Rgb(r, g, b)
        };
        Theme {
            name: name.into(),
            background: c(0),
            surface: c(1),
            primary: c(2),
            panel: c(3),
            tool_background: c(4),
            foreground: c(5),
            title: c(6),
            assistant_border: c(7),
            thinking_text: c(8),
            thinking_border: c(9),
            tool_text: c(10),
            error: c(11),
            footer_foreground: c(12),
            footer_background: c(13),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Token {
    pub key: String,
    pub required: bool,
    pub raw: Option<String>,
    pub resolved: Option<String>,
    pub source: String,
}
fn tokens(v: &Value) -> io::Result<Vec<Token>> {
    let rows = v
        .as_array()
        .filter(|a| a.len() == TOKENS.len())
        .ok_or_else(|| fail("Theme editor schema requires fifteen ordered tokens"))?;
    rows.iter()
        .enumerate()
        .map(|(i, r)| {
            if r["key"] != TOKENS[i] || r["required"] != (i < 10) {
                return Err(fail("Unsupported theme editor token order or requirement"));
            }
            let raw = optional(r, "raw", TEXT_BYTES)?;
            let resolved = optional(r, "resolved", 7)?;
            let source = text(r, "source", 16, false)?;
            if !matches!(source.as_str(), "explicit" | "derived" | "omitted")
                || resolved.as_ref().is_some_and(|s| !hex(s))
                || (source == "omitted") != resolved.is_none()
                || i < 13 && resolved.is_none()
                || source == "explicit" && raw.is_none()
            {
                return Err(fail("Contradictory theme token resolution"));
            }
            Ok(Token {
                key: TOKENS[i].into(),
                required: i < 10,
                raw,
                resolved,
                source,
            })
        })
        .collect()
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Row {
    pub id: String,
    pub name: String,
    pub definition: String,
    pub source: String,
    pub palette: Palette,
    pub tokens: Vec<Token>,
    pub selectable: bool,
    pub reason: Option<String>,
}
impl Row {
    fn parse(v: &Value) -> io::Result<Self> {
        let source = text(v, "source", 16, false)?;
        if !matches!(
            source.as_str(),
            "builtin" | "litetui" | "custom" | "registered"
        ) {
            return Err(fail("Unknown theme source"));
        }
        let (selectable, reason) = reason(v, "selectable")?;
        Ok(Self {
            id: hash(v, "row_id")?,
            name: text(v, "name", TEXT_BYTES, false)?,
            definition: hash(v, "definition")?,
            source,
            palette: Palette::parse(&v["palette"])?,
            tokens: tokens(&v["tokens"])?,
            selectable,
            reason,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub saved: String,
    pub saved_definition: Option<String>,
    pub requested: String,
    pub runtime: String,
    pub effective: String,
    pub row: Option<String>,
    pub effective_reason: Option<String>,
    pub definition: String,
}
impl Selection {
    fn parse(v: &Value) -> io::Result<Self> {
        let row = if optional(v, "effective_row_id", 64)?.is_some() {
            Some(hash(v, "effective_row_id")?)
        } else {
            None
        };
        let effective_reason = optional(v, "effective_reason", 4096)?;
        if row.is_some() == effective_reason.is_some()
            || effective_reason.as_ref().is_some_and(String::is_empty)
        {
            return Err(fail("Contradictory effective theme palette availability"));
        }
        Ok(Self {
            saved: text(v, "saved_name", TEXT_BYTES, true)?,
            saved_definition: if optional(v, "saved_definition", 64)?.is_some() {
                Some(hash(v, "saved_definition")?)
            } else {
                None
            },
            requested: text(v, "requested_name", TEXT_BYTES, true)?,
            runtime: text(v, "runtime_name", TEXT_BYTES, true)?,
            effective: text(v, "effective_name", TEXT_BYTES, false)?,
            row,
            effective_reason,
            definition: hash(v, "effective_definition")?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub identity: Identity,
    pub token: String,
    pub revisions: Value,
    pub selection: Selection,
    pub eligible: bool,
    pub reason: Option<String>,
    pub rows: Vec<Row>,
    pub registered_definitions: BTreeMap<String, String>,
    pub invalid: Vec<(String, String)>,
}
impl Snapshot {
    pub fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        shape(v, "theme_snapshot")?;
        identity.verify(v)?;
        scope(v)?;
        let entries = v["rows"]
            .as_array()
            .filter(|a| a.len() <= MAX_ROWS)
            .ok_or_else(|| fail("Theme catalog exceeds row budget or is empty"))?;
        let mut ids = HashSet::new();
        let mut names = HashSet::new();
        let mut rows = Vec::new();
        for value in entries {
            let r = Row::parse(value)?;
            if !ids.insert(r.id.clone()) || !names.insert(r.name.clone()) {
                return Err(fail("Duplicate theme row identity or name"));
            }
            rows.push(r);
        }
        let definitions = v["registered_definitions"]
            .as_object()
            .filter(|o| o.len() <= MAX_ROWS)
            .ok_or_else(|| fail("Invalid registered theme definition catalog"))?;
        let mut registered_definitions = BTreeMap::new();
        for (name, _) in definitions {
            if name.is_empty() || name.len() > TEXT_BYTES {
                return Err(fail("Invalid registered theme name"));
            }
            registered_definitions.insert(name.clone(), hash(&v["registered_definitions"], name)?);
        }
        if rows
            .iter()
            .any(|r| registered_definitions.get(&r.name) != Some(&r.definition))
        {
            return Err(fail(
                "Supported theme row differs from registered definition",
            ));
        }
        let selection = Selection::parse(&v["selection"])?;
        if registered_definitions.get(&selection.effective) != Some(&selection.definition) {
            return Err(fail("Effective theme differs from registered definition"));
        }
        if selection.row.as_ref().is_some_and(|id| {
            !rows.iter().any(|r| {
                &r.id == id && r.name == selection.effective && r.definition == selection.definition
            })
        }) {
            return Err(fail(
                "Effective theme is not the exact registered catalog row",
            ));
        }
        let invalid = v["invalid"]
            .as_array()
            .filter(|a| a.len() <= MAX_ROWS)
            .ok_or_else(|| fail("Invalid-theme diagnostics exceed budget"))?
            .iter()
            .map(|r| {
                Ok((
                    text(r, "name", TEXT_BYTES, true)?,
                    text(r, "reason", 4096, false)?,
                ))
            })
            .collect::<io::Result<_>>()?;
        let (eligible, reason) = reason(v, "eligible")?;
        Ok(Self {
            identity: identity.clone(),
            token: hash(v, "snapshot")?,
            revisions: revisions(&v["revisions"])?,
            selection,
            eligible,
            reason,
            rows,
            registered_definitions,
            invalid,
        })
    }
    pub fn effective(&self) -> Option<&Row> {
        self.selection
            .row
            .as_ref()
            .and_then(|id| self.rows.iter().find(|r| &r.id == id))
    }
    pub fn request(&self, change: &Change) -> io::Result<Value> {
        if !self.eligible {
            return Err(fail(
                self.reason.as_deref().unwrap_or("Themes are read-only"),
            ));
        }
        let mut v = self.identity.command(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["revisions"] = self.revisions.clone();
        match change {
            Change::Select { row } => {
                let r = self
                    .rows
                    .iter()
                    .find(|r| &r.id == row)
                    .filter(|r| r.selectable)
                    .ok_or_else(|| fail("Selected theme is unavailable"))?;
                v["action"] = json!("select");
                v["row_id"] = json!(r.id);
            }
            Change::Custom { name, tokens } => {
                if name.is_empty()
                    || name.len() > TEXT_BYTES
                    || tokens.len() > TOKENS.len()
                    || TOKENS[..10].iter().any(|k| !tokens.contains_key(*k))
                    || tokens
                        .iter()
                        .any(|(k, s)| !TOKENS.contains(&k.as_str()) || s.len() > TEXT_BYTES)
                {
                    return Err(fail("Custom theme draft exceeds source editor bounds"));
                }
                v["action"] = json!("custom");
                v["name"] = json!(name);
                v["tokens"] = json!(tokens);
            }
        }
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Change {
    Select {
        row: String,
    },
    Custom {
        name: String,
        tokens: BTreeMap<String, String>,
    },
}
impl Change {
    pub fn action(&self) -> &'static str {
        match self {
            Self::Select { .. } => "select",
            Self::Custom { .. } => "custom",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Target {
    pub row: Option<String>,
    pub name: String,
    pub definition: String,
    pub palette: Palette,
    pub tokens: Vec<Token>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub snapshot: Snapshot,
    pub change: Change,
    pub token: String,
    pub action_id: String,
    pub target: Target,
    pub overwrite_custom: bool,
    pub overwrite_registered: bool,
    pub previous_definition: Option<String>,
    pub fields: Value,
}
impl Review {
    pub fn parse(v: &Value, snapshot: &Snapshot, change: &Change) -> io::Result<Self> {
        shape(v, "theme_review")?;
        snapshot.identity.verify(v)?;
        scope(v)?;
        snapshot.request(change)?;
        let fields = if matches!(change, Change::Select { .. }) {
            json!(["theme_name"])
        } else {
            json!(["custom_themes", "theme_name"])
        };
        if v["snapshot"] != snapshot.token
            || v["revisions"] != snapshot.revisions
            || v["action"] != change.action()
            || v["fields"] != fields
            || Selection::parse(&v["selection_before"])? != snapshot.selection
        {
            return Err(fail("Theme review differs from requested authority"));
        }
        let t = &v["target"];
        let row = if t["row_id"].is_null() {
            None
        } else {
            Some(hash(t, "row_id")?)
        };
        let target = Target {
            row,
            name: text(t, "name", TEXT_BYTES, false)?,
            definition: hash(t, "definition")?,
            palette: Palette::parse(&t["palette"])?,
            tokens: tokens(&t["tokens"])?,
        };
        let overwrite = &v["overwrite"];
        let overwrite_custom = boolean(overwrite, "custom")?;
        let overwrite_registered = boolean(overwrite, "registered")?;
        let previous_definition = if overwrite["previous_definition"].is_null() {
            None
        } else {
            Some(hash(overwrite, "previous_definition")?)
        };
        match change {
            Change::Select { row } => {
                let r = snapshot
                    .rows
                    .iter()
                    .find(|r| &r.id == row)
                    .expect("validated selection");
                if target.row.as_ref() != Some(row)
                    || target.name != r.name
                    || target.definition != r.definition
                    || target.palette != r.palette
                    || target.tokens != r.tokens
                    || overwrite_custom
                    || overwrite_registered
                    || previous_definition.is_some()
                {
                    return Err(fail("Theme selection review changed selected definition"));
                }
            }
            Change::Custom { .. } => {
                if target.row.is_some() {
                    return Err(fail("New custom review has an existing row identity"));
                }
                let existing = snapshot.registered_definitions.get(&target.name);
                if overwrite_registered != existing.is_some()
                    || previous_definition.as_deref() != existing.map(String::as_str)
                {
                    return Err(fail(
                        "Theme overwrite disclosure changed registered definition",
                    ));
                }
            }
        }
        let action_id = text(v, "action_id", 36, false)?;
        if crate::resume::conversation_id(&action_id).is_err() {
            return Err(fail("Invalid theme action identity"));
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            change: change.clone(),
            token: hash(v, "review")?,
            action_id,
            target,
            overwrite_custom,
            overwrite_registered,
            previous_definition,
            fields,
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.snapshot.identity.command(SAVE);
        v["snapshot"] = json!(self.snapshot.token);
        v["revisions"] = self.snapshot.revisions.clone();
        v["review"] = json!(self.token);
        v["action_id"] = json!(self.action_id);
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub status: String,
    pub selection: Selection,
    pub revisions: Value,
    pub runtime: String,
    pub persistence_saved: bool,
    pub reason: Option<String>,
}
impl Outcome {
    /// Ordinary refusals prove no mutation. Other results still require fresh reads.
    pub fn parse(v: &Value, r: &Review) -> io::Result<Result<Self, String>> {
        shape(v, "theme_save")?;
        r.snapshot.identity.verify(v)?;
        if v["snapshot"] != r.snapshot.token
            || v["review"] != r.token
            || v["action_id"] != r.action_id
            || v["action"] != r.change.action()
            || v["target_name"] != r.target.name
            || v["target_definition"] != r.target.definition
        {
            return Err(fail("Theme save receipt differs from confirmed review"));
        }
        let mutated = boolean(v, "mutated")?;
        let close = boolean(v, "close_required")?;
        let status = text(v, "status", 16, false)?;
        let error = optional(v, "error", 4096)?;
        let runtime = &v["runtime"];
        let runtime_status = text(runtime, "status", 16, false)?;
        if !matches!(
            runtime_status.as_str(),
            "applied" | "unchanged" | "failed" | "unknown"
        ) {
            return Err(fail("Unknown theme application result"));
        }
        for key in ["registered", "selected", "refresh_requested"] {
            if !runtime.as_object().is_some_and(|o| o.contains_key(key))
                || !runtime[key].is_null() && runtime[key].as_bool().is_none()
            {
                return Err(fail("Malformed theme application proof"));
            }
        }
        let runtime_error = optional(runtime, "error", 4096)?;
        let p = v["persistence"]
            .as_array()
            .filter(|a| a.len() <= 1)
            .ok_or_else(|| fail("Unexpected theme persistence destinations"))?;
        for destination in p {
            let path = text(destination, "destination", 4096, false)?;
            if path.chars().any(char::is_control)
                || destination["scope"] != "device"
                || destination["fields"] != r.fields
            {
                return Err(fail("Theme persistence destination or fields changed"));
            }
            boolean(destination, "saved")?;
            optional(destination, "error", 4096)?;
            if destination["revision"].is_null() {
                if destination["saved"] != false {
                    return Err(fail("Saved theme persistence lacks revision"));
                }
            } else if destination["revision"] != "absent" {
                hash(destination, "revision")?;
            }
        }
        if close || status == "failed" {
            return Err(fail(
                error
                    .as_deref()
                    .unwrap_or("Theme save outcome is uncertain"),
            ));
        }
        if status == "refused" {
            if mutated
                || runtime_status != "unchanged"
                || !runtime["registered"].is_null()
                || !runtime["selected"].is_null()
                || runtime["refresh_requested"] != false
                || runtime_error.is_some()
                || p.iter().any(|p| p["saved"] != false)
                || !v["selection"].is_null()
                    && Selection::parse(&v["selection"])? != r.snapshot.selection
                || !v["revisions"].is_null() && revisions(&v["revisions"])? != r.snapshot.revisions
            {
                return Err(fail("Contradictory refused theme save"));
            }
            return Ok(Err(error
                .filter(|s| !s.is_empty())
                .ok_or_else(|| fail("Theme refusal lacks reason"))?));
        }
        if !matches!(status.as_str(), "saved" | "partial") || p.len() != 1 {
            return Err(fail("Theme receipt lacks a bounded persistence outcome"));
        }
        let persistence_saved = boolean(&p[0], "saved")?;
        if persistence_saved != p[0]["error"].is_null() {
            return Err(fail("Contradictory theme persistence outcome"));
        }
        let selection = Selection::parse(&v["selection"])?;
        let revisions = revisions(&v["revisions"])?;
        if (persistence_saved && p[0]["revision"] != revisions["global"])
            || revisions["conversation"] != r.snapshot.revisions["conversation"]
            || revisions["agent"] != r.snapshot.revisions["agent"]
            || selection.saved != r.target.name
        {
            return Err(fail(
                "Theme save changed unrelated revisions or saved target",
            ));
        }
        if !mutated
            && (status != "saved"
                || revisions != r.snapshot.revisions
                || selection != r.snapshot.selection
                || r.snapshot.selection.saved != r.target.name
                || r.snapshot.selection.definition != r.target.definition
                || !r.snapshot.effective().is_some_and(|before| {
                    before.palette == r.target.palette && before.tokens == r.target.tokens
                }))
        {
            return Err(fail(
                "Unchanged theme receipt describes a changed or partial result",
            ));
        }
        if status == "saved" {
            if !persistence_saved
                || !matches!(runtime_status.as_str(), "applied" | "unchanged")
                || runtime["registered"] != true
                || runtime["selected"] != true
                || selection.requested != r.target.name
                || selection.runtime != r.target.name
                || selection.effective != r.target.name
                || selection.definition != r.target.definition
                || error.is_some()
                || runtime_error.is_some()
                || runtime["refresh_requested"] != true
                    && !(r.snapshot.selection.runtime == r.target.name
                        && r.snapshot.selection.effective == r.target.name
                        && r.snapshot.selection.definition == r.target.definition
                        && r.snapshot.effective().is_some_and(|old| {
                            old.palette == r.target.palette && old.tokens == r.target.tokens
                        }))
            {
                return Err(fail("Contradictory successful theme application"));
            }
        } else if persistence_saved {
            if runtime_status != "failed" || runtime_error.as_ref().is_none_or(String::is_empty) {
                return Err(fail("Partial theme save lacks a known application failure"));
            }
        } else if runtime_status != "unchanged"
            || runtime_error.is_some()
            || error.as_ref().is_none_or(String::is_empty)
            || !runtime["registered"].is_null()
            || !runtime["selected"].is_null()
            || runtime["refresh_requested"] != false
        {
            return Err(fail("Failed theme write reports unproven runtime changes"));
        }
        Ok(Ok(Self {
            status,
            selection,
            revisions,
            runtime: runtime_status,
            persistence_saved,
            reason: runtime_error.or(error),
        }))
    }
    pub fn verify(&self, after: &Snapshot, review: &Review) -> io::Result<()> {
        if after.identity != review.snapshot.identity
            || after.revisions != self.revisions
            || after.selection != self.selection
            || after.revisions["conversation"] != review.snapshot.revisions["conversation"]
            || after.revisions["agent"] != review.snapshot.revisions["agent"]
        {
            return Err(fail("Theme save differs from independent readback"));
        }
        if matches!(review.change, Change::Custom { .. })
            && after.selection.saved_definition.as_deref()
                != Some(review.target.definition.as_str())
        {
            return Err(fail(
                "Saved custom theme definition differs from independent readback",
            ));
        }
        let matches_target = after.effective().is_some_and(|actual| {
            actual.definition == review.target.definition
                && actual.palette == review.target.palette
                && actual.tokens == review.target.tokens
                && actual.name == review.target.name
        });
        if self.status == "saved" {
            if !matches_target {
                return Err(fail(
                    "Effective theme definition differs from confirmed palette",
                ));
            }
        } else {
            let matches_before = match (after.effective(), review.snapshot.effective()) {
                (Some(actual), Some(before)) => {
                    actual.name == before.name
                        && actual.definition == before.definition
                        && actual.palette == before.palette
                        && actual.tokens == before.tokens
                }
                (None, None) => {
                    after.selection.effective == review.snapshot.selection.effective
                        && after.selection.definition == review.snapshot.selection.definition
                }
                _ => false,
            };
            if (self.runtime == "unchanged" || !matches_target) && !matches_before {
                return Err(fail(
                    "Partial theme readback introduced an unreviewed palette",
                ));
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Read,
    Validate { snapshot: String, change: Change },
    Save { review: String, action_id: String },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot(Box<Snapshot>),
    Review(Box<Review>),
    Applied {
        snapshot: Box<Snapshot>,
        outcome: Box<Outcome>,
    },
    Rejected(String),
}
#[derive(Default)]
pub struct View {
    pub snapshot: Option<Snapshot>,
    pub review: Option<Review>,
    pub notice: String,
    open: bool,
    waiting: bool,
    executing: bool,
    visible: bool,
    editor: bool,
    detail: bool,
    invalid: bool,
    query: Draft,
    name: Draft,
    fields: [Draft; 15],
    field: usize,
    cursor: usize,
    offset: usize,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    area: Rect,
    content: Rect,
    geometry: Option<Rect>,
    pressed: Option<(u16, u16, usize)>,
    intent: Option<Intent>,
    picker: Option<Picker>,
    /// A read the palette's theme pick asked for: no panel, no notice, no input taken.
    silent: bool,
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.picker = None;
        self.editor = false;
        self.detail = false;
        self.invalid = false;
        self.query = Draft::default();
        self.refresh();
    }
    pub fn owns_input(&self) -> bool {
        self.open || (self.waiting && !self.silent)
    }
    /// Read the source theme again without opening the panel (the palette's theme pick
    /// changed it in Python).
    pub fn reread(&mut self) {
        if self.waiting {
            return;
        }
        self.intent = Some(Intent::Read);
        self.waiting = true;
        self.silent = true;
    }
    pub fn is_open(&self) -> bool {
        self.open
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn accepts_paste(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && !self.detail
            && !self.invalid
    }
    pub fn editing(&self) -> bool {
        self.open && self.visible && self.editor && !self.waiting && self.review.is_none()
    }
    pub fn draft_name(&self) -> &str {
        self.name.text()
    }
    pub fn draft_token(&self, key: &str) -> Option<&str> {
        TOKENS
            .iter()
            .position(|k| *k == key)
            .map(|i| self.fields[i].text())
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
        if let Some(p) = self.picker.as_mut() {
            p.disarm();
        }
        self.page = 0;
        self.seen.clear();
    }
    pub fn close(&mut self) {
        self.open = false;
        self.picker = None;
        if self.intent.take().is_some() {
            self.waiting = false;
            self.executing = false;
        }
        if !self.executing {
            self.review = None;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.executing = false;
        self.snapshot = None;
        self.review = None;
    }
    fn refresh(&mut self) {
        if self.waiting {
            return;
        }
        self.review = None;
        self.detail = false;
        self.intent = Some(Intent::Read);
        self.waiting = true;
        self.notice = "Reading source theme catalog; edit kept.".into();
        self.hide();
    }
    pub fn observe(&mut self, update: Update) {
        let silent = std::mem::take(&mut self.silent);
        self.waiting = false;
        self.executing = false;
        self.intent = None;
        self.hide();
        match update {
            Update::Snapshot(s) => {
                self.snapshot = Some(*s);
                self.review = None;
                self.notice = if silent {
                    String::new()
                } else {
                    "Source catalog loaded. Enter reviews a selection; F2 creates a custom theme."
                        .into()
                };
            }
            Update::Review(r) if self.open => {
                self.review = Some(*r);
                self.notice = "Read every page, then confirm once. No theme has been saved.".into();
            }
            Update::Review(_) => {}
            Update::Applied { snapshot, outcome } => {
                self.snapshot = Some(*snapshot);
                self.review = None;
                self.editor = outcome.status == "partial" && self.editor;
                self.detail = false;
                self.notice = if outcome.status == "saved" {
                    "Theme saved and effective palette independently verified.".into()
                } else {
                    format!(
                        "Theme saved; runtime application reported a problem: {}",
                        outcome
                            .reason
                            .as_deref()
                            .unwrap_or("runtime application failed")
                    )
                };
            }
            Update::Rejected(message) => {
                self.snapshot = None;
                self.review = None;
                self.notice = format!("{message} | F5 refresh; edit kept.");
            }
        }
    }
    fn indices(&self) -> Vec<usize> {
        let q = self.query.text().to_lowercase();
        self.snapshot
            .as_ref()
            .map(|s| {
                s.rows
                    .iter()
                    .enumerate()
                    .filter(|(_, r)| r.name.to_lowercase().contains(&q))
                    .map(|(i, _)| i)
                    .collect()
            })
            .unwrap_or_default()
    }
    fn count(&self) -> usize {
        if self.invalid {
            self.snapshot.as_ref().map_or(0, |s| s.invalid.len())
        } else {
            self.indices().len()
        }
    }
    fn selected(&self) -> Option<&Row> {
        let indices = self.indices();
        self.snapshot.as_ref()?.rows.get(*indices.get(self.cursor)?)
    }
    fn current_draft(&mut self) -> &mut Draft {
        if self.field == 0 {
            &mut self.name
        } else {
            &mut self.fields[self.field - 1]
        }
    }
    fn change(&self) -> io::Result<Change> {
        if self.editor {
            Ok(Change::Custom {
                name: self.name.text().into(),
                tokens: TOKENS
                    .iter()
                    .enumerate()
                    .map(|(i, k)| ((*k).into(), self.fields[i].text().into()))
                    .collect(),
            })
        } else {
            self.selected()
                .map(|r| Change::Select { row: r.id.clone() })
                .ok_or_else(|| fail("Select an available source theme"))
        }
    }
    fn start_editor(&mut self, existing: bool) {
        if !self.snapshot.as_ref().is_some_and(|s| s.eligible) {
            self.notice = self
                .snapshot
                .as_ref()
                .and_then(|s| s.reason.clone())
                .unwrap_or_else(|| "Refresh source authority first.".into());
            return;
        }
        let row = self.selected().cloned();
        if existing && row.is_none() {
            return;
        }
        self.name
            .set(if existing {
                &row.as_ref().unwrap().name
            } else {
                ""
            })
            .expect("bounded source name");
        for (i, field) in self.fields.iter_mut().enumerate() {
            let Some(t) = row.as_ref().and_then(|r| r.tokens.get(i)) else {
                field.set("").expect("empty field");
                continue;
            };
            // Existing raw values preserve optional invalid/unset semantics. Builtin
            // derived core values seed an editable complete custom definition.
            field
                .set(t.raw.as_deref().or(t.resolved.as_deref()).unwrap_or(""))
                .expect("bounded source token");
        }
        self.editor = true;
        self.picker = None;
        self.detail = false;
        self.field = 0;
        self.offset = 0;
        self.notice =
            "Edit name and colors. Enter requests source review; Esc keeps the catalog.".into();
        self.hide();
    }
    fn open_picker(&mut self) {
        if self.field == 0 {
            self.notice = "Tab to a color token, then F8 opens the picker.".into();
            return;
        }
        let key = TOKENS[self.field - 1];
        self.picker = Some(Picker::new(self.fields[self.field - 1].text(), key));
        self.notice = "Picking a color. Nothing is saved; Enter only fills the field.".into();
        self.hide();
    }
    /// The picker yields a candidate hex for the one field being edited. It feeds the
    /// ordinary draft; review, explicit confirmation and readback are untouched.
    fn picker_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            self.picker = None;
            self.hide();
            self.notice = "Picker closed; the field is unchanged.".into();
            return;
        }
        if !self.visible
            || self.waiting
            || key.code == KeyCode::Enter && key.kind != KeyEventKind::Press
        {
            return;
        }
        let Some(picker) = self.picker.as_mut() else {
            return;
        };
        match picker.key(key.code) {
            Pick::Pending => {}
            Pick::Cancel => {
                self.picker = None;
                self.hide();
            }
            Pick::Accept(hex) => {
                let token = TOKENS[self.field - 1];
                self.fields[self.field - 1]
                    .set(&hex)
                    .expect("bounded picked color");
                self.picker = None;
                self.hide();
                self.notice = format!(
                    "Picked {hex} for {token}; nothing is saved. Enter requests source review."
                );
            }
        }
    }
    pub fn paste(&mut self, text: &str) {
        if !self.open
            || !self.visible
            || self.waiting
            || self.review.is_some()
            || self.detail
            || self.invalid
        {
            return;
        }
        if let Some(picker) = self.picker.as_mut() {
            picker.paste(text);
            return;
        }
        let draft = if self.editor {
            self.current_draft()
        } else {
            &mut self.query
        };
        if draft.text().len().saturating_add(text.len()) > TEXT_BYTES {
            self.notice = "Input exceeds 256 UTF-8 bytes; edit unchanged.".into();
            return;
        }
        draft.insert(text).expect("bounded editor insertion");
        if !self.editor {
            self.cursor = 0;
        }
        self.pressed = None;
    }
    fn navigate_page(&mut self, forward: bool) {
        if forward {
            if self.seen.get(self.page) == Some(&true) {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1));
            }
        } else {
            self.page = self.page.saturating_sub(1);
        }
        self.visible = false;
        self.pressed = None;
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !(key.modifiers.is_empty()
            || key.modifiers == KeyModifiers::SHIFT
                && matches!(key.code, KeyCode::Char(_) | KeyCode::BackTab))
        {
            self.hide();
            return;
        }
        if self.picker.is_some() && !self.waiting {
            self.picker_key(key);
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if !self.waiting && self.detail {
                self.detail = false;
                self.hide();
            } else if !self.waiting && self.review.is_some() {
                self.review = None;
                self.hide();
                self.notice = "Review cancelled; draft kept.".into();
            } else if !self.waiting && self.editor {
                self.editor = false;
                self.hide();
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
        if self.review.is_some() || self.detail {
            match key.code {
                KeyCode::PageDown => self.navigate_page(true),
                KeyCode::PageUp => self.navigate_page(false),
                KeyCode::F(6) if key.kind == KeyEventKind::Press && self.review.is_some() => {
                    self.detail = !self.detail;
                    self.hide();
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && self.review.is_some()
                        && !self.detail
                        && self.page + 1 == self.pages
                        && !self.seen.is_empty()
                        && self.seen.iter().all(|s| *s) =>
                {
                    let r = self.review.as_ref().unwrap();
                    self.intent = Some(Intent::Save {
                        review: r.token.clone(),
                        action_id: r.action_id.clone(),
                    });
                    self.waiting = true;
                    self.executing = true;
                    self.notice =
                        "Saving reviewed theme; independently checking palette and authority..."
                            .into();
                    self.hide();
                }
                _ => {
                    self.hide();
                }
            }
            return;
        }
        if key.code == KeyCode::Enter && key.kind == KeyEventKind::Press {
            if self.invalid {
                self.detail = true;
                self.hide();
                return;
            }
            let request = self.change().and_then(|change| {
                let s = self
                    .snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Refresh source theme catalog first"))?;
                s.request(&change)?;
                Ok(Intent::Validate {
                    snapshot: s.token.clone(),
                    change,
                })
            });
            match request {
                Ok(intent) => {
                    self.intent = Some(intent);
                    self.waiting = true;
                    self.notice = "Validating exact theme selection and saved revisions...".into();
                    self.hide();
                }
                Err(e) => self.notice = e.to_string(),
            }
            return;
        }
        if key.code == KeyCode::F(6) && key.kind == KeyEventKind::Press {
            self.detail = true;
            self.hide();
            return;
        }
        if self.editor {
            match key.code {
                KeyCode::F(8) if key.kind == KeyEventKind::Press => self.open_picker(),
                KeyCode::Tab | KeyCode::Down => self.field = (self.field + 1) % 16,
                KeyCode::BackTab | KeyCode::Up => self.field = (self.field + 15) % 16,
                KeyCode::Char(c) if !c.is_control() => self.paste(&c.to_string()),
                other => self.current_draft().key(other),
            }
            self.pressed = None;
            return;
        }
        match key.code {
            KeyCode::F(2) if !self.invalid => self.start_editor(false),
            KeyCode::F(3) if !self.invalid => self.start_editor(true),
            KeyCode::F(6) => {
                self.detail = true;
                self.hide();
            }
            KeyCode::F(7) => {
                self.invalid = !self.invalid;
                self.cursor = 0;
                self.hide();
            }
            KeyCode::Down => self.cursor = (self.cursor + 1).min(self.count().saturating_sub(1)),
            KeyCode::Up => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.count().saturating_sub(1),
            KeyCode::PageDown => {
                self.cursor = self
                    .cursor
                    .saturating_add(self.content.height as usize)
                    .min(self.count().saturating_sub(1))
            }
            KeyCode::PageUp => {
                self.cursor = self.cursor.saturating_sub(self.content.height as usize)
            }
            KeyCode::Char(c) if !c.is_control() && !self.invalid => self.paste(&c.to_string()),
            KeyCode::Backspace if !self.invalid => {
                self.query.key(KeyCode::Backspace);
                self.cursor = 0;
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if !event.modifiers.is_empty() || !self.area.contains((event.column, event.row).into()) {
            self.hide();
            return;
        }
        if let Some(picker) = self.picker.as_mut() {
            picker.mouse(event);
            return;
        }
        if self.review.is_some() || self.detail {
            match event.kind {
                MouseEventKind::ScrollDown => self.navigate_page(true),
                MouseEventKind::ScrollUp => self.navigate_page(false),
                _ => self.hide(),
            }
            return;
        }
        if !self.content.contains((event.column, event.row).into()) {
            self.pressed = None;
            return;
        }
        let index = self.offset + usize::from(event.row - self.content.y);
        let count = if self.editor { 16 } else { self.count() };
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) if index < count => {
                self.pressed = Some((event.column, event.row, index));
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take() == Some((event.column, event.row, index)) && index < count {
                    if self.editor {
                        self.field = index;
                    } else {
                        self.cursor = index;
                    }
                }
            }
            MouseEventKind::ScrollDown => {
                if self.editor {
                    self.field = (self.field + 1).min(15);
                } else {
                    self.cursor = (self.cursor + 1).min(count.saturating_sub(1));
                }
                self.pressed = None;
            }
            MouseEventKind::ScrollUp => {
                if self.editor {
                    self.field = self.field.saturating_sub(1);
                } else {
                    self.cursor = self.cursor.saturating_sub(1);
                }
                self.pressed = None;
            }
            _ => self.pressed = None,
        }
    }
    fn detail_text(&self) -> String {
        let Some(s) = &self.snapshot else {
            return display(&self.notice);
        };
        if self.invalid {
            return s
                .invalid
                .get(self.cursor)
                .map(|(n, r)| format!("Skipped stored theme: {}\n{}", display(n), display(r)))
                .unwrap_or_else(|| "No invalid stored themes.".into());
        }
        let selection = format!(
            "{}\n\n{}",
            display(&self.notice),
            selection_text(&s.selection)
        );
        let Some(r) = self.selected() else {
            return selection;
        };
        format!(
            "{selection}\n\nTheme: {}\nSource: {}\nDefinition: {}\nSelectable: {}\n{}\n\n{}",
            display(&r.name),
            r.source,
            r.definition,
            r.selectable,
            display(r.reason.as_deref().unwrap_or("")),
            tokens_text(&r.tokens)
        )
    }
    fn review_text(r: &Review) -> String {
        let heading = format!(
            "Save on this device; apply immediately.\nCurrent: {}\nTarget: {}",
            display(&r.snapshot.selection.effective),
            display(&r.target.name)
        );
        let changes = match &r.change {
            Change::Select { .. } => format!(
                "Select this preset.\n\nPalette preview (RGB):\n{}",
                palette_text(&r.target.palette)
            ),
            Change::Custom { name, tokens } => format!(
                "{}\nEntered name: {}\nReplace saved custom theme: {}\nReplace registered theme: {}\n\nEntered colors → source result:\n{}",
                if r.overwrite_custom || r.overwrite_registered {
                    "Overwrite this theme."
                } else {
                    "Create this custom theme."
                },
                display(name),
                if r.overwrite_custom { "yes" } else { "no" },
                if r.overwrite_registered { "yes" } else { "no" },
                r.target
                    .tokens
                    .iter()
                    .map(|t| format!(
                        "{}: {} → {} ({}){}",
                        t.key,
                        tokens
                            .get(&t.key)
                            .map(|v| display(v))
                            .unwrap_or_else(|| "<omitted>".into()),
                        t.resolved.as_deref().unwrap_or("omitted"),
                        t.source,
                        if tokens.get(&t.key).map(String::as_str) != t.raw.as_deref() {
                            format!(
                                "; normalized input: {}",
                                t.raw
                                    .as_deref()
                                    .map(display)
                                    .unwrap_or_else(|| "<omitted>".into())
                            )
                        } else {
                            String::new()
                        }
                    ))
                    .collect::<Vec<_>>()
                    .join("\n")
            ),
        };
        format!(
            "{heading}\n{changes}\n\nEnter on the final reviewed page saves once. Esc keeps the draft. F6: source details."
        )
    }
    fn review_details(r: &Review) -> String {
        format!(
            "Source details (no save from this screen)\nTarget: {}\nDefinition: {}\nPrevious definition: {}\nFields: {}\n\nBefore:\n{}\n\nSource token values:\n{}\n\nResolved native palette:\n{}\n\nGlobal revision: {}\nConversation revision: {}\nAgent revision: {}",
            display(&r.target.name),
            r.target.definition,
            r.previous_definition.as_deref().unwrap_or("none"),
            r.fields,
            selection_text(&r.snapshot.selection),
            tokens_text(&r.target.tokens),
            palette_text(&r.target.palette),
            r.snapshot.revisions["global"],
            r.snapshot.revisions["conversation"],
            r.snapshot.revisions["agent"]
        )
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open {
            self.hide();
            return;
        }
        if self.geometry != Some(area) {
            self.hide();
            self.geometry = Some(area);
        }
        let width = area.width.min(92);
        let height = area.height.min(34);
        let panel = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        self.area = panel;
        frame.render_widget(Clear, panel);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(" Themes · source authority ")
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(panel);
        frame.render_widget(block, panel);
        if area.width < 42 || area.height < 18 {
            frame.render_widget(
                Paragraph::new("Resize to 42×18 for theme controls.\nEsc: close"),
                inner,
            );
            self.hide();
            return;
        }
        self.visible = true;
        if self.waiting {
            frame.render_widget(
                Paragraph::new(format!(
                    "{}\n\nEsc: close view; a dispatched save still settles.",
                    display(&self.notice)
                ))
                .wrap(ratatui::widgets::Wrap { trim: false }),
                inner,
            );
            return;
        }
        if let Some(picker) = self.picker.as_mut() {
            self.visible = picker.draw(frame, inner, theme);
            return;
        }
        if self.review.is_some() || self.detail {
            let text = match (&self.review, self.detail) {
                (Some(review), true) => Self::review_details(review),
                (Some(review), false) => Self::review_text(review),
                (None, _) => self.detail_text(),
            };
            let lines = crate::ui::wrap(&text, inner.width as usize);
            let rows = usize::from(inner.height.saturating_sub(3)).max(1);
            self.pages = lines.len().div_ceil(rows).max(1);
            if self.seen.len() != self.pages {
                self.seen = vec![false; self.pages];
                self.page = 0;
            }
            self.page = self.page.min(self.pages - 1);
            self.seen[self.page] = true;
            let content = Rect::new(inner.x, inner.y, inner.width, rows as u16);
            frame.render_widget(
                Paragraph::new(
                    lines
                        .iter()
                        .skip(self.page * rows)
                        .take(rows)
                        .cloned()
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                content,
            );
            let ready = self.review.is_some()
                && !self.detail
                && self.page + 1 == self.pages
                && self.seen.iter().all(|s| *s);
            let footer = format!(
                "Page {}/{} · PgUp/PgDn · F6: details\n{} · Esc: back",
                self.page + 1,
                self.pages,
                if ready {
                    "Enter: save once"
                } else if self.detail {
                    "Source details; saving disabled"
                } else {
                    "Read all pages before saving"
                }
            );
            frame.render_widget(
                Paragraph::new(footer),
                Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
            );
            return;
        }
        let source = self
            .snapshot
            .as_ref()
            .map(|s| {
                format!(
                    "Effective: {} · saved: {}",
                    display(&s.selection.effective),
                    display(&s.selection.saved)
                )
            })
            .unwrap_or_else(|| "Source palette unavailable; current fallback is local.".into());
        frame.render_widget(
            Paragraph::new(source),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let heading = if self.editor {
            "Custom draft · Tab/↑↓ field · Enter review".into()
        } else if self.invalid {
            format!("Skipped stored themes: {} · F7: catalog", self.count())
        } else {
            format!(
                "Search: {} · {} rows",
                display(self.query.text()),
                self.count()
            )
        };
        frame.render_widget(
            Paragraph::new(heading),
            Rect::new(inner.x, inner.y + 1, inner.width, 1),
        );
        self.content = Rect::new(
            inner.x,
            inner.y + 3,
            inner.width,
            inner.height.saturating_sub(7),
        );
        let count = if self.editor { 16 } else { self.count() };
        let selected = if self.editor {
            self.field
        } else {
            self.cursor.min(count.saturating_sub(1))
        };
        let rows = self.content.height as usize;
        if selected < self.offset {
            self.offset = selected;
        }
        if selected >= self.offset + rows {
            self.offset = selected.saturating_sub(rows.saturating_sub(1));
        }
        self.offset = self.offset.min(count.saturating_sub(rows));
        let indices = self.indices();
        for (screen, index) in (self.offset..count).take(rows).enumerate() {
            let row = Rect::new(
                self.content.x,
                self.content.y + screen as u16,
                self.content.width,
                1,
            );
            let active = index == selected;
            let text = if self.editor {
                let label = if index == 0 {
                    "name"
                } else {
                    TOKENS[index - 1]
                };
                let value = if index == 0 {
                    self.name.text()
                } else {
                    self.fields[index - 1].text()
                };
                let marker = if active { ">" } else { " " };
                format!("{marker} {label}: {}", display(value))
            } else if self.invalid {
                format!(
                    "{} {}",
                    if active { ">" } else { " " },
                    display(&self.snapshot.as_ref().unwrap().invalid[index].0)
                )
            } else {
                let s = self.snapshot.as_ref().unwrap();
                let r = &s.rows[indices[index]];
                format!(
                    "{} {}{}{}",
                    if active { ">" } else { " " },
                    display(&r.name),
                    if s.selection.row.as_ref() == Some(&r.id) {
                        " [effective]"
                    } else {
                        ""
                    },
                    if !r.selectable { " [unavailable]" } else { "" }
                )
            };
            let style = if active {
                Style::default()
                    .fg(theme.title)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.foreground)
            };
            frame.render_widget(Paragraph::new(text).style(style), row);
            if active && self.editor {
                let label = if index == 0 {
                    "name"
                } else {
                    TOKENS[index - 1]
                };
                let draft = if index == 0 {
                    &self.name
                } else {
                    &self.fields[index - 1]
                };
                let prefix = format!("> {label}: ");
                let start = crate::rendered_width(&prefix).min(row.width as usize);
                let available = (row.width as usize).saturating_sub(start);
                if available > 0 {
                    let before = display(&draft.text()[..draft.cursor()]);
                    let left = tail(&before, available.saturating_sub(1));
                    let position = crate::rendered_width(&left);
                    let after = display(&draft.text()[draft.cursor()..]);
                    let value = left + &after;
                    frame.render_widget(
                        Paragraph::new(value).style(style),
                        Rect::new(row.x + start as u16, row.y, available as u16, 1),
                    );
                    frame.set_cursor_position((row.x + start as u16 + position as u16, row.y));
                }
            }
        }
        let help = if self.editor {
            "Tab field · Enter review · Esc back\nF5 refresh · F6 details · F8 picker"
        } else {
            "Enter review · F2 new · F3 edit\nF6 detail · F7 skipped · F5 read · Esc"
        };
        frame.render_widget(
            Paragraph::new(help),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 2),
        );
        frame.render_widget(
            Paragraph::new(display(&self.notice)),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}
fn display(s: &str) -> String {
    terminal_text(s).replace('\n', "\\n").replace('\t', "\\t")
}
fn palette_text(palette: &Palette) -> String {
    COLORS
        .iter()
        .zip(palette.0)
        .map(|(key, [r, g, b])| format!("{key}: #{r:02X}{g:02X}{b:02X}"))
        .collect::<Vec<_>>()
        .join("\n")
}
fn selection_text(s: &Selection) -> String {
    format!(
        "Saved: {}\nSaved definition: {}\nRequested: {}\nRuntime property: {}\nEffective: {}\nEffective definition: {}\nPalette availability: {}",
        display(&s.saved),
        s.saved_definition.as_deref().unwrap_or("unknown"),
        display(&s.requested),
        display(&s.runtime),
        display(&s.effective),
        s.definition,
        display(
            s.effective_reason
                .as_deref()
                .unwrap_or("represented exactly")
        )
    )
}
fn tokens_text(tokens: &[Token]) -> String {
    tokens
        .iter()
        .map(|t| {
            format!(
                "{}{}: raw={} → {} ({})",
                t.key,
                if t.required { " *" } else { "" },
                t.raw
                    .as_deref()
                    .map(display)
                    .unwrap_or_else(|| "<unset>".into()),
                t.resolved.as_deref().unwrap_or("<omitted>"),
                t.source
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}
fn tail(s: &str, width: usize) -> String {
    use unicode_segmentation::UnicodeSegmentation;
    let mut value = String::new();
    for g in s.graphemes(true).rev() {
        let next = format!("{g}{value}");
        if crate::rendered_width(&next) > width {
            break;
        }
        value = next;
    }
    value
}
