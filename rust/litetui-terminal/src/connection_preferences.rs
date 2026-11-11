//! Reviewed connection and local-router preferences. Saving records a preference at the
//! timing the source registry declares; it never reconnects, loads a model, contacts a
//! host or starts a process. Key values are never read or shown: only the environment
//! variable NAME of the custom server is editable.
use crate::{
    theme::Theme,
    work_lifecycle::{bounded, hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, io};

pub const OPERATIONS: [&str; 4] = [
    "gui.connection.native_preferences",
    "gui.connection.native_review",
    "gui.connection.native_apply",
    "gui.connection.native_readback",
];
const FIELDS: [&str; 16] = [
    "lm_host",
    "pin_default_model",
    "default_context_length",
    "custom_base_url",
    "custom_api_key_env",
    "custom_context_length",
    "claude_executable",
    "llama_host",
    "llama_executable",
    "llama_attach_hosts",
    "llama_models_max",
    "lms_load_timeout_s",
    "llama_scan_litesuite",
    "llama_scan_lmstudio",
    "llama_scan_hf_cache",
    "llama_models_dirs",
];
/// The free-tier key settings. Their values are secrets and are never part of this wire.
const KEY_SETTINGS: [&str; 12] = [
    "cerebras_api_key",
    "cloudflare_api_key",
    "gemini_api_key",
    "github_models_token",
    "groq_api_key",
    "longcat_api_key",
    "mistral_api_key",
    "nvidia_api_key",
    "ollama_api_key",
    "openrouter_api_key",
    "sealion_api_key",
    "zai_api_key",
];
const HIDDEN: &str = "(unsupported value hidden)";
const MAX_BYTES: usize = 131_072;
const MAX_INT: i64 = 2_147_483_647;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Bool,
    Int,
    OptionalInt,
    Url,
    UrlOrBlank,
    UrlList,
    PathList,
    EnvName,
    Path,
}
impl Kind {
    fn of(key: &str) -> Self {
        match key {
            "pin_default_model"
            | "llama_scan_litesuite"
            | "llama_scan_lmstudio"
            | "llama_scan_hf_cache" => Self::Bool,
            "custom_context_length" | "llama_models_max" | "lms_load_timeout_s" => Self::Int,
            "default_context_length" => Self::OptionalInt,
            "lm_host" | "llama_host" => Self::Url,
            "custom_base_url" => Self::UrlOrBlank,
            "llama_attach_hosts" => Self::UrlList,
            "llama_models_dirs" => Self::PathList,
            "custom_api_key_env" => Self::EnvName,
            _ => Self::Path,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Bool => "bool",
            Self::Int => "int",
            Self::OptionalInt => "optional_int",
            Self::Url => "url",
            Self::UrlOrBlank => "url_or_blank",
            Self::UrlList => "url_list",
            Self::PathList => "path_list",
            Self::EnvName => "env_name",
            Self::Path => "path",
        }
    }
    fn urls(self) -> bool {
        matches!(self, Self::Url | Self::UrlOrBlank | Self::UrlList)
    }
}
fn label(key: &str) -> &'static str {
    match key {
        "lm_host" => "LM Studio address",
        "pin_default_model" => "Re-select the default model on connect",
        "default_context_length" => "Context length to load",
        "custom_base_url" => "Custom server URL",
        "custom_api_key_env" => "Custom server key variable (name only)",
        "custom_context_length" => "Custom context budget",
        "claude_executable" => "Claude executable",
        "llama_host" => "llama.cpp router address",
        "llama_executable" => "llama-server executable",
        "llama_attach_hosts" => "llama.cpp attach addresses",
        "llama_models_max" => "Resident model limit (router)",
        "lms_load_timeout_s" => "LM Studio load timeout (seconds)",
        "llama_scan_litesuite" => "Scan LiteSuite models",
        "llama_scan_lmstudio" => "Scan LM Studio models",
        "llama_scan_hf_cache" => "Scan HuggingFace cache",
        "llama_models_dirs" => "Extra model folders",
        _ => "Connection preference",
    }
}
fn when(timing: &str) -> &'static str {
    match timing {
        "reconnect" => "the next reconnect or relaunch",
        "restart" => "the next local server start or restart",
        "reload" => "the next model load or settings reload",
        _ => "now",
    }
}
fn bad() -> io::Error {
    io::Error::other("Connection preference response differs from its reviewed settings")
}
fn plain(s: &str, max: usize) -> bool {
    s.len() <= max && !s.chars().any(crate::display_control)
}
/// A server URL that cannot carry a credential, query or fragment. The source redacts
/// stored userinfo to `***@`; `redacted` admits only that spelling (read-only fields).
fn url_ok(s: &str, redacted: bool) -> bool {
    if redacted && s == HIDDEN {
        return true;
    }
    if !plain(s, 2048) || s.contains(['?', '#']) || s != s.trim() {
        return false;
    }
    let Some(rest) = s
        .strip_prefix("http://")
        .or_else(|| s.strip_prefix("https://"))
    else {
        return false;
    };
    let authority = rest.split('/').next().unwrap_or("");
    match authority.rsplit_once('@') {
        Some((user, host)) => redacted && user == "***" && !host.is_empty() && !host.contains('@'),
        None => !authority.is_empty(),
    }
}
/// Display form of a stored URL: userinfo, query and fragment are never painted.
fn redact_url(s: &str) -> String {
    let (scheme, rest) = match s.split_once("://") {
        Some(parts) => parts,
        None => return crate::ui::display_safe(s),
    };
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    // Any '@' before the query ends the userinfo, however malformed the credential is.
    let (user, rest) = match rest.rfind('@') {
        Some(at) => ("***@", &rest[at + 1..]),
        None => ("", rest),
    };
    crate::ui::display_safe(&format!("{scheme}://{user}{rest}"))
}
fn env_name_ok(s: &str) -> bool {
    s.is_empty()
        || s.len() <= 64
            && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            && !s.as_bytes()[0].is_ascii_digit()
}
fn int_in(v: &Value, min: Option<i64>, max: Option<i64>) -> bool {
    v.as_i64().is_some_and(|n| {
        (-MAX_INT..=MAX_INT).contains(&n)
            && min.is_none_or(|m| n >= m)
            && max.is_none_or(|m| n <= m)
    })
}
/// A value the source may display. `exact` (an editable row) forbids redaction markers.
fn displayable(kind: Kind, v: &Value, exact: bool) -> bool {
    match kind {
        Kind::Bool => v.is_boolean() || !exact && v.is_null(),
        Kind::Int => int_in(v, None, None) || !exact && v.is_null(),
        Kind::OptionalInt => v.is_null() || int_in(v, None, None),
        Kind::Url | Kind::UrlOrBlank => v
            .as_str()
            .is_some_and(|s| s.is_empty() || url_ok(s, !exact)),
        Kind::UrlList => v.as_array().is_some_and(|a| {
            a.len() <= 16
                && a.iter()
                    .all(|i| i.as_str().is_some_and(|s| url_ok(s, !exact)))
        }),
        Kind::PathList => v.as_array().is_some_and(|a| {
            a.len() <= 16 && a.iter().all(|i| i.as_str().is_some_and(|s| plain(s, 1024)))
        }),
        Kind::EnvName => v
            .as_str()
            .is_some_and(|s| if exact { env_name_ok(s) } else { plain(s, 64) }),
        Kind::Path => v
            .as_str()
            .is_some_and(|s| plain(s, if exact { 1024 } else { 2048 })),
    }
}
/// A value the user may request. Stricter than display: no hidden marker, no credentials.
fn requestable(kind: Kind, min: Option<i64>, max: Option<i64>, v: &Value) -> bool {
    match kind {
        Kind::Bool => v.is_boolean(),
        Kind::Int => int_in(v, min, max),
        Kind::OptionalInt => v.is_null() || int_in(v, min, max),
        Kind::Url => v.as_str().is_some_and(|s| url_ok(s, false)),
        Kind::PathList => {
            displayable(kind, v, true)
                && v.as_array().is_some_and(|a| {
                    a.iter().enumerate().all(|(i, x)| {
                        x.as_str().is_some_and(|s| !s.is_empty() && s == s.trim())
                            && a.iter().skip(i + 1).all(|y| x != y)
                    })
                })
        }
        Kind::UrlList => {
            displayable(kind, v, true)
                && v.as_array().is_some_and(|a| {
                    a.iter()
                        .enumerate()
                        .all(|(i, x)| a.iter().skip(i + 1).all(|y| x != y))
                })
        }
        _ => displayable(kind, v, true),
    }
}
fn show(kind: Kind, v: &Value) -> String {
    match v {
        Value::Null => "unset".into(),
        Value::Bool(true) => "on".into(),
        Value::Bool(false) => "off".into(),
        Value::String(s) if s.is_empty() => "unset".into(),
        Value::String(s) if kind.urls() => redact_url(s),
        Value::String(s) => crate::ui::display_safe(s),
        Value::Array(a) if a.is_empty() => "none".into(),
        Value::Array(_) if kind == Kind::PathList => crate::ui::display_safe(&v.to_string()),
        Value::Array(a) => a
            .iter()
            .filter_map(Value::as_str)
            .map(redact_url)
            .collect::<Vec<_>>()
            .join(", "),
        other => other.to_string(),
    }
}
/// The editable text for a stored value.
fn draft_of(kind: Kind, v: &Value) -> String {
    if kind == Kind::PathList {
        return v.to_string();
    }
    match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", "),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}
/// Parse typed text into the value the source expects, or say why not.
fn parse_draft(
    kind: Kind,
    min: Option<i64>,
    max: Option<i64>,
    text: &str,
) -> Result<Value, &'static str> {
    let trimmed = text.trim();
    let value = match kind {
        Kind::Bool => return Err("Choose on or off with the arrow keys"),
        Kind::OptionalInt if trimmed.is_empty() => Value::Null,
        Kind::Int | Kind::OptionalInt => {
            if trimmed.is_empty() || !trimmed.bytes().all(|b| b.is_ascii_digit()) {
                return Err("Enter a whole number");
            }
            trimmed
                .parse::<i64>()
                .map(|n| json!(n))
                .map_err(|_| "Enter a smaller number")?
        }
        Kind::PathList => serde_json::from_str(trimmed)
            .map_err(|_| "Enter a JSON list of folder paths; [] clears the list")?,
        Kind::UrlList => json!(
            trimmed
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
        ),
        _ => json!(trimmed),
    };
    if requestable(kind, min, max, &value) {
        Ok(value)
    } else {
        Err(match kind {
            Kind::Url | Kind::UrlOrBlank | Kind::UrlList => {
                "Enter distinct http(s) URLs without credentials, query or fragment"
            }
            Kind::EnvName => "Enter the variable NAME (letters, digits, underscore), never the key",
            Kind::Path => "Enter one line of at most 1024 bytes",
            Kind::PathList => "Enter up to 16 distinct nonempty folders, at most 1024 bytes each",
            _ => "Value is outside the allowed range",
        })
    }
}
fn verify_binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
        || v["work_instance"] != instance
        || !id
            .request()
            .as_object()
            .expect("identity")
            .iter()
            .all(|(k, val)| v[k] == *val)
    {
        return Err(bad());
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Field {
    scope: String,
    timing: String,
    minimum: Option<i64>,
    maximum: Option<i64>,
    saved: Value,
    effective: Value,
    editable: bool,
    reason: Option<String>,
    effect: String,
}
impl Field {
    fn pending(&self) -> bool {
        self.saved != self.effective
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    fields: BTreeMap<String, Field>,
    revision: String,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        bounded(v, MAX_BYTES)?;
        verify_binding(v, id, instance, "connection_preferences")?;
        keys(
            v,
            &[
                "version",
                "scope",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "fields",
                "revision",
                "key_settings",
            ],
        )?;
        if v["version"] != 1 || v["scope"] != "mixed" {
            return Err(bad());
        }
        // Key values are secrets: the source sends names only, and nothing else may ride along.
        keys(&v["key_settings"], &["names", "readable"])?;
        if v["key_settings"]["names"] != json!(KEY_SETTINGS)
            || v["key_settings"]["readable"] != false
        {
            return Err(bad());
        }
        keys(&v["fields"], &FIELDS)?;
        let mut fields = BTreeMap::new();
        for key in FIELDS {
            let raw = &v["fields"][key];
            keys(
                raw,
                &[
                    "scope",
                    "timing",
                    "kind",
                    "minimum",
                    "maximum",
                    "saved",
                    "effective",
                    "editable",
                    "reason",
                    "effect",
                ],
            )?;
            let kind = Kind::of(key);
            let ranged = matches!(kind, Kind::Int | Kind::OptionalInt);
            let bound = |name: &str| -> io::Result<Option<i64>> {
                match (&raw[name], ranged) {
                    (Value::Null, false) => Ok(None),
                    (n, true) => n
                        .as_i64()
                        .filter(|n| (0..=MAX_INT).contains(n))
                        .map(Some)
                        .ok_or_else(bad),
                    _ => Err(bad()),
                }
            };
            let (minimum, maximum) = (bound("minimum")?, bound("maximum")?);
            let text = |name: &str, max: usize| -> io::Result<String> {
                raw[name]
                    .as_str()
                    .filter(|s| !s.is_empty() && plain(s, max))
                    .map(str::to_owned)
                    .ok_or_else(bad)
            };
            let editable = raw["editable"].as_bool().ok_or_else(bad)?;
            let reason = if raw["reason"].is_null() {
                None
            } else {
                Some(text("reason", 400)?)
            };
            let scope = text("scope", 16)?;
            let timing = text("timing", 16)?;
            if raw["kind"] != kind.name()
                || !["conversation", "device"].contains(&scope.as_str())
                || !["reconnect", "restart", "reload", "next_turn", "immediate"]
                    .contains(&timing.as_str())
                || editable != reason.is_none()
                || !displayable(kind, &raw["saved"], editable)
                || !displayable(kind, &raw["effective"], editable)
            {
                return Err(bad());
            }
            fields.insert(
                key.into(),
                Field {
                    scope,
                    timing,
                    minimum,
                    maximum,
                    saved: raw["saved"].clone(),
                    effective: raw["effective"].clone(),
                    editable,
                    reason,
                    effect: text("effect", 600)?,
                },
            );
        }
        Ok(Self {
            fields,
            revision: hash(v, "revision")?,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edit {
    before: Snapshot,
    key: String,
    value: Value,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    edit: Edit,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Edit),
    Apply(Review),
    Readback(Review, String, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Review),
    Saved(Snapshot),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut cmd = id.request();
        cmd["work_instance"] = json!(instance);
        cmd["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Review(e) => {
                let row = e
                    .before
                    .fields
                    .get(&e.key)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                if row.saved == e.value
                    || !requestable(Kind::of(&e.key), row.minimum, row.maximum, &e.value)
                {
                    return Err(bad());
                }
                cmd["revision"] = json!(e.before.revision);
                cmd["field"] = json!(e.key);
                cmd["value"] = e.value.clone();
                OPERATIONS[1]
            }
            Self::Apply(r) => {
                cmd["token"] = json!(r.token);
                OPERATIONS[2]
            }
            Self::Readback(r, apply, _) => {
                cmd["token"] = json!(r.token);
                cmd["apply_id"] = json!(apply);
                OPERATIONS[3]
            }
        });
        Ok(cmd)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        cmd_id: &str,
    ) -> io::Result<Reply> {
        let (kind, e, token, applied) = match &self {
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance,
                )?)));
            }
            Self::Review(e) => ("connection_review", e, None, None),
            Self::Apply(r) => ("connection_applied", &r.edit, Some(&r.token), Some(cmd_id)),
            Self::Readback(r, apply, _) => (
                "connection_saved",
                &r.edit,
                Some(&r.token),
                Some(apply.as_str()),
            ),
        };
        verify_binding(v, id, instance, kind)?;
        bounded(v, MAX_BYTES)?;
        let mut members = vec![
            "kind",
            "version",
            "scope",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "token",
            "field",
            "value",
            "timing",
            "effect",
            "before",
            "after",
            "apply_id",
        ];
        if kind == "connection_saved" {
            members.push("preferences")
        }
        keys(v, &members)?;
        let row = e.before.fields.get(&e.key).ok_or_else(bad)?;
        let actual = hash(v, "token")?;
        if v["version"] != 1
            || v["scope"] != row.scope
            || v["timing"] != row.timing
            || v["effect"] != row.effect
            || v["field"] != e.key
            || v["value"] != e.value
            || v["before"] != e.before.revision
            || token.is_some_and(|t| *t != actual)
            || v["apply_id"] != json!(applied)
        {
            return Err(bad());
        }
        match self {
            Self::List => unreachable!(),
            Self::Review(edit) if v["after"].is_null() => {
                Ok(Reply::Update(Update::Review(Review {
                    edit,
                    token: actual,
                })))
            }
            Self::Apply(review) => {
                let after = hash(v, "after")?;
                if after == review.edit.before.revision {
                    return Err(bad());
                }
                Ok(Reply::Next(Self::Readback(review, cmd_id.into(), after)))
            }
            Self::Readback(review, _, after) => {
                let actual = Snapshot::parse(&v["preferences"], id, instance)?;
                let edit = review.edit;
                let mut expected = edit.before;
                expected.revision = after.clone();
                let field = expected.fields.get_mut(&edit.key).ok_or_else(bad)?;
                // A reconnect/restart/reload field stays pending: only the saved value moves.
                field.saved = edit.value.clone();
                if matches!(field.timing.as_str(), "next_turn" | "immediate") {
                    field.effective = edit.value;
                }
                if actual != expected || v["after"] != after {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(actual)))
            }
            _ => Err(bad()),
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    visible: bool,
    data: Option<Snapshot>,
    selected: usize,
    /// A boolean choice, or the text being typed for any other kind.
    toggle: Option<bool>,
    text: Option<String>,
    review: Option<Review>,
    note: String,
    scroll: usize,
    height: usize,
    lines: usize,
    review_seen: usize,
    review_width: u16,
}
impl View {
    pub fn open(&mut self) {
        *self = Self {
            open: true,
            waiting: true,
            ..Self::default()
        }
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn accepts_paste(&self) -> bool {
        self.open && !self.waiting && self.review.is_none() && self.text.is_some()
    }
    pub fn paste(&mut self, pasted: &str) {
        if !self.accepts_paste() {
            return;
        }
        let limit = if Kind::of(FIELDS[self.selected]) == Kind::PathList {
            32768
        } else {
            2048
        };
        if let Some(text) = &mut self.text {
            for c in pasted.chars().filter(|c| !crate::display_control(*c)) {
                if text.len() + c.len_utf8() <= limit {
                    text.push(c);
                }
            }
        }
        self.hide();
    }
    fn clear_draft(&mut self) {
        self.toggle = None;
        self.text = None;
    }
    pub fn update(&mut self, r: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.scroll = 0;
        self.review_seen = 0;
        self.note.clear();
        match r {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None;
                self.clear_draft();
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Saved(s)) => {
                let timing = self.review_timing(&s);
                self.note = format!("Saved and verified on disk. Takes effect {timing}.");
                self.data = Some(s);
                self.review = None;
                self.clear_draft();
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.clear_draft();
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    fn review_timing(&self, saved: &Snapshot) -> &'static str {
        FIELDS
            .get(self.selected)
            .and_then(|key| saved.fields.get(*key))
            .map_or("later", |f| when(&f.timing))
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        let typed = key.modifiers.difference(KeyModifiers::SHIFT).is_empty();
        if key.kind != KeyEventKind::Press || !typed || self.waiting {
            return None;
        }
        if key.code == KeyCode::Esc {
            if self.review.take().is_some() || self.toggle.is_some() || self.text.is_some() {
                self.clear_draft();
                self.scroll = 0;
                self.hide()
            } else {
                *self = Self::default()
            }
            return None;
        }
        if matches!(key.code, KeyCode::PageDown | KeyCode::PageUp) {
            self.scroll = if key.code == KeyCode::PageDown {
                self.scroll
                    .saturating_add(self.height)
                    .min(self.lines.saturating_sub(self.height))
            } else {
                self.scroll.saturating_sub(self.height)
            };
            self.hide();
            return None;
        }
        let field = FIELDS[self.selected];
        let kind = Kind::of(field);
        let limit = if kind == Kind::PathList { 32768 } else { 2048 };
        if self.review.is_none()
            && let Some(text) = &mut self.text
        {
            match key.code {
                KeyCode::Char(c) if !crate::display_control(c) => {
                    if text.len() + c.len_utf8() <= limit {
                        text.push(c);
                    }
                    self.note.clear();
                    self.hide();
                    return None;
                }
                KeyCode::Backspace => {
                    text.pop();
                    self.note.clear();
                    self.hide();
                    return None;
                }
                KeyCode::Enter if self.visible => {
                    let before = self.data.clone()?;
                    let row = &before.fields[field];
                    return match parse_draft(kind, row.minimum, row.maximum, text) {
                        Ok(value) if value != row.saved => {
                            self.waiting = true;
                            self.scroll = 0;
                            self.hide();
                            Some(Request::Review(Edit {
                                before,
                                key: field.into(),
                                value,
                            }))
                        }
                        Ok(_) => {
                            self.note = "Choose a different value.".into();
                            None
                        }
                        Err(why) => {
                            self.note = why.into();
                            None
                        }
                    };
                }
                _ => return None,
            }
        }
        let req = match key.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                self.selected = if key.code == KeyCode::Up {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(FIELDS.len() - 1)
                };
                self.clear_draft();
                self.note.clear();
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Char('e') if self.review.is_none() && kind != Kind::Bool => {
                let row = self.data.as_ref()?.fields.get(field)?;
                if row.editable {
                    self.text = Some(draft_of(kind, &row.saved));
                    self.note = if kind == Kind::PathList {
                        "Enter a JSON list of folders; [] clears it. Enter reviews. Esc cancels."
                    } else {
                        "Type the new value. Enter reviews. Esc cancels."
                    }
                    .into();
                    self.hide();
                }
                return None;
            }
            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ')
                if self.review.is_none() && kind == Kind::Bool =>
            {
                let row = self.data.as_ref()?.fields.get(field)?;
                if row.editable {
                    let current = self.toggle.or(row.saved.as_bool())?;
                    self.toggle = Some(!current);
                    self.scroll = 0;
                    self.hide();
                }
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Enter if self.visible && self.toggle.is_some() => {
                let before = self.data.clone()?;
                let value = json!(self.toggle?);
                if value == before.fields[field].saved {
                    self.note = "Choose a different value.".into();
                    return None;
                }
                Request::Review(Edit {
                    before,
                    key: field.into(),
                    value,
                })
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(req)
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Connection and local-server preferences".to_owned()];
        if let Some(review) = &self.review {
            let key = review.edit.key.as_str();
            let kind = Kind::of(key);
            let row = &review.edit.before.fields[key];
            lines.extend([
                format!("Save {}?", label(key)),
                format!("Scope: {} · Applies: {}", row.scope, when(&row.timing)),
                format!("In effect now: {}", show(kind, &row.effective)),
                format!("Saved on disk: {}", show(kind, &row.saved)),
                format!("Requested: {}", show(kind, &review.edit.value)),
                row.effect.clone(),
                "Enter save   Esc cancel".into(),
            ]);
        } else if let Some(s) = &self.data {
            lines.push(
                "Saving records a preference only: it never reconnects, loads a model, contacts a server or starts a process."
                    .into(),
            );
            for (i, key) in FIELDS
                .iter()
                .enumerate()
                .skip(self.selected.saturating_sub(3))
                .take(7)
            {
                let row = &s.fields[*key];
                let kind = Kind::of(key);
                let mut line = format!(
                    "{} {}: {}",
                    if i == self.selected { ">" } else { " " },
                    label(key),
                    show(kind, &row.effective)
                );
                if row.pending() {
                    line.push_str(&format!(
                        " (saved {}; applies {})",
                        show(kind, &row.saved),
                        when(&row.timing)
                    ));
                }
                if !row.editable {
                    line.push_str(" (locked)");
                }
                lines.push(line);
            }
            let key = FIELDS[self.selected];
            let kind = Kind::of(key);
            let row = &s.fields[key];
            lines.push(format!(
                "Field {}/{} · {} · applies {}",
                self.selected + 1,
                FIELDS.len(),
                row.scope,
                when(&row.timing)
            ));
            lines.push(row.effect.clone());
            if let Some(reason) = &row.reason {
                lines.push(reason.clone());
            }
            if let Some(toggle) = self.toggle {
                lines.push(format!(
                    "{}: {}",
                    label(key),
                    if toggle { "on" } else { "off" }
                ));
            } else if let Some(text) = &self.text {
                lines.push(format!(
                    "{}: {}_",
                    label(key),
                    if kind.urls() {
                        redact_url(text)
                    } else {
                        crate::ui::display_safe(text)
                    }
                ));
            }
            lines.push(format!(
                "Free-tier API key settings ({}) are never read or shown here, and only the variable name of the custom key is editable. Edit the keys in the Python /settings dialog or set each provider's environment variable: {}.",
                KEY_SETTINGS.len(),
                KEY_SETTINGS.join(", ")
            ));
            lines.push(if kind == Kind::Bool {
                "↑/↓ field   ←/→ or Space toggle   Enter review   F5 refresh   Esc back".into()
            } else {
                "↑/↓ field   E edit   Enter review   F5 refresh   Esc back".into()
            });
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone())
        }
        if self.waiting {
            lines.push("Waiting for the source…".into())
        }
        if self.data.is_none() && !self.waiting && self.review.is_none() {
            lines.push("F5 refresh   Esc back".into())
        }
        let safe = lines
            .iter()
            .map(|s| crate::ui::display_safe(s))
            .collect::<Vec<_>>()
            .join("\n");
        let wrapped = crate::ui::wrap(&safe, usize::from(area.width.saturating_sub(2)));
        self.height = usize::from(area.height.saturating_sub(2));
        self.lines = wrapped.len();
        self.scroll = self.scroll.min(self.lines.saturating_sub(self.height));
        let drawable = area.width >= 36 && self.height >= 5;
        if self.review_width != area.width {
            self.review_width = area.width;
            self.review_seen = 0;
        }
        if self.review.is_some() && drawable && self.scroll <= self.review_seen {
            self.review_seen = self
                .review_seen
                .max(self.scroll + self.height)
                .min(self.lines);
        }
        // Enter is accepted only after every line of the review has been painted.
        self.visible = drawable && (self.review.is_none() || self.review_seen == self.lines);
        let shown = if !drawable {
            "Resize to read the review. Esc back.".into()
        } else {
            wrapped
                .iter()
                .skip(self.scroll)
                .take(self.height)
                .cloned()
                .collect::<Vec<_>>()
                .join("\n")
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(shown)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Connection ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn identity() -> Identity {
        Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        }
    }
    fn value_of(key: &str) -> Value {
        match key {
            "lm_host" => json!("http://localhost:1234"),
            "pin_default_model" => json!(false),
            "default_context_length" => Value::Null,
            "custom_base_url" => json!(""),
            "custom_api_key_env" => json!(""),
            "custom_context_length" => json!(0),
            "claude_executable" | "llama_executable" => json!(""),
            "llama_host" => json!("http://localhost:7470"),
            "llama_attach_hosts" => json!(["http://localhost:8088"]),
            "llama_models_max" => json!(2),
            "llama_scan_litesuite" | "llama_scan_lmstudio" | "llama_scan_hf_cache" => json!(true),
            "llama_models_dirs" => json!([]),
            _ => json!(600),
        }
    }
    fn timing(key: &str) -> &'static str {
        match key {
            "default_context_length" => "reload",
            "llama_executable" | "llama_attach_hosts" | "llama_models_max" => "restart",
            "lms_load_timeout_s" => "next_turn",
            "llama_scan_litesuite"
            | "llama_scan_lmstudio"
            | "llama_scan_hf_cache"
            | "llama_models_dirs" => "immediate",
            _ => "reconnect",
        }
    }
    fn fixture() -> (Identity, Value) {
        let id = identity();
        let mut root = id.request();
        for (key, value) in [
            ("kind", json!("connection_preferences")),
            ("scope", json!("mixed")),
            ("work_instance", json!("i")),
            ("revision", json!("1".repeat(64))),
            (
                "key_settings",
                json!({"names": KEY_SETTINGS, "readable": false}),
            ),
        ] {
            root[key] = value;
        }
        for key in FIELDS {
            let kind = Kind::of(key);
            let ranged = matches!(kind, Kind::Int | Kind::OptionalInt);
            let min = match key {
                "custom_context_length" => 0,
                "lms_load_timeout_s" => 30,
                _ => 1,
            };
            root["fields"][key] = json!({
                "scope": if key == "claude_executable" || timing(key) == "immediate" { "device" } else { "conversation" },
                "timing": timing(key), "kind": kind.name(),
                "minimum": if ranged { json!(min) } else { Value::Null },
                "maximum": if ranged { json!(MAX_INT) } else { Value::Null },
                "saved": value_of(key), "effective": value_of(key),
                "editable": true, "reason": null,
                "effect": "Saves it. Saving never reconnects, contacts a server, loads a model or starts a process.",
            });
        }
        (id, root)
    }
    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn screen(view: &mut View) -> String {
        let theme = crate::theme::builtin_themes().remove(0);
        let mut terminal = Terminal::new(TestBackend::new(110, 40)).unwrap();
        terminal
            .draw(|frame| view.draw(frame, frame.area(), &theme))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content()
            .chunks(110)
            .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }
    fn opened(doc: &Value, id: &Identity) -> View {
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(Snapshot::parse(doc, id, "i").unwrap())));
        view
    }

    #[test]
    fn folder_list_edit_keeps_commas_unicode_and_windows_separators() {
        let paths = json!([r"C:\models, local", "D:/models/\u{96ea}"]);
        let draft = draft_of(Kind::PathList, &paths);
        assert_eq!(
            parse_draft(Kind::PathList, None, None, &draft),
            Ok(paths.clone())
        );
        assert_eq!(show(Kind::PathList, &paths), paths.to_string());
        assert_eq!(parse_draft(Kind::PathList, None, None, "[]"), Ok(json!([])));
        for invalid in [
            json!([""]),
            json!(["x", "x"]),
            json!([" padded "]),
            json!(["bad\npath"]),
            json!(vec!["x"; 17]),
            json!(["x".repeat(1025)]),
        ] {
            assert!(parse_draft(Kind::PathList, None, None, &invalid.to_string()).is_err());
        }
    }

    #[test]
    fn snapshot_checks_binding_shape_scope_and_the_secret_name_list() {
        let (id, doc) = fixture();
        assert!(Snapshot::parse(&doc, &id, "i").is_ok());
        for (path, value) in [
            ("/work_instance", json!("old")),
            ("/version", json!(2)),
            ("/scope", json!("device")),
            ("/fields/lm_host/kind", json!("path")),
            ("/fields/lm_host/scope", json!("elsewhere")),
            ("/fields/lm_host/timing", json!("soon")),
            ("/fields/lm_host/reason", json!("no reason while editable")),
            ("/fields/lm_host/editable", json!(false)),
            ("/fields/lm_host/effect", json!("")),
            ("/fields/pin_default_model/saved", json!(1)),
            ("/fields/llama_models_max/maximum", Value::Null),
            ("/fields/lm_host/minimum", json!(1)),
            ("/fields/custom_api_key_env/saved", json!("sk-not-a-name")),
            ("/key_settings/readable", json!(true)),
            ("/key_settings/names/0", json!("groq_api_key")),
        ] {
            let mut drift = doc.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{path}");
        }
        let mut leaked = doc.clone();
        leaked["key_settings"]["groq_api_key"] = json!("gsk_secret");
        assert!(Snapshot::parse(&leaked, &id, "i").is_err());
        let mut extra = doc;
        extra["fields"]["groq_api_key"] = json!({"saved": "gsk_secret"});
        assert!(Snapshot::parse(&extra, &id, "i").is_err());
    }

    #[test]
    fn credentials_are_refused_when_editable_and_redacted_when_locked() {
        let (id, doc) = fixture();
        for bad_url in [
            "http://user:pw@example.test",
            "http://user@example.test",
            "http://example.test/?token=abc",
            "http://example.test/#frag",
            "ftp://example.test",
            " http://example.test",
        ] {
            let mut drift = doc.clone();
            drift["fields"]["lm_host"]["saved"] = json!(bad_url);
            drift["fields"]["lm_host"]["effective"] = json!(bad_url);
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{bad_url}");
            // A locked row may carry only the source's redaction marker, never a credential.
            drift["fields"]["lm_host"]["editable"] = json!(false);
            drift["fields"]["lm_host"]["reason"] = json!("locked");
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{bad_url}");
        }
        let mut locked = doc;
        locked["fields"]["lm_host"]["saved"] = json!("http://***@example.test:1234/");
        locked["fields"]["lm_host"]["effective"] = json!("http://***@example.test:1234/");
        locked["fields"]["lm_host"]["editable"] = json!(false);
        locked["fields"]["lm_host"]["reason"] =
            json!("The stored value cannot be shown safely; edit it outside the native TUI");
        locked["fields"]["llama_attach_hosts"]["saved"] =
            json!(["http://a.test", "https://***@b.test"]);
        locked["fields"]["llama_attach_hosts"]["effective"] =
            json!(["http://a.test", "https://***@b.test"]);
        locked["fields"]["llama_attach_hosts"]["editable"] = json!(false);
        locked["fields"]["llama_attach_hosts"]["reason"] = json!("locked");
        let mut view = opened(&locked, &id);
        let painted = screen(&mut view);
        assert!(painted.contains("http://***@example.test:1234/") && painted.contains("(locked)"));
        assert!(!painted.contains("hunter2"));
        // Defense in depth: even a value that slipped past parsing is redacted when painted.
        assert_eq!(
            redact_url("https://user:hunter2@example.test:8443/p?key=hunter2#hunter2"),
            "https://***@example.test:8443/p"
        );
        assert_eq!(
            redact_url("http://example.test/v1"),
            "http://example.test/v1"
        );
        assert!(
            !show(
                Kind::UrlList,
                &json!(["http://u:hunter2@a.test", "http://b.test"])
            )
            .contains("hunter2")
        );
        // The editor never prefills or paints a typed credential either.
        assert!(parse_draft(Kind::Url, None, None, "http://u:hunter2@example.test").is_err());
        assert!(parse_draft(Kind::UrlOrBlank, None, None, "http://example.test/?k=v").is_err());
        assert!(parse_draft(Kind::UrlList, None, None, "http://a.test, http://a.test").is_err());
    }

    #[test]
    fn key_fields_accept_only_a_variable_name() {
        for ok in ["", "MY_SERVER_KEY", "_x9", "lower_name"] {
            assert!(parse_draft(Kind::EnvName, None, None, ok).is_ok(), "{ok}");
        }
        for bad_name in [
            "sk-abcdef",
            "has space",
            "9LEADING",
            "dot.name",
            &"X".repeat(65),
        ] {
            assert!(
                parse_draft(Kind::EnvName, None, None, bad_name).is_err(),
                "{bad_name}"
            );
        }
        // The key settings themselves are not editable fields at all.
        assert!(KEY_SETTINGS.iter().all(|k| !FIELDS.contains(k)));
        let (id, doc) = fixture();
        let view = opened(&doc, &id);
        let mut view = view;
        let painted = screen(&mut view);
        let flat = painted.replace(
            "│
│", "",
        );
        assert!(flat.contains("never read or shown here"));
        for name in KEY_SETTINGS {
            assert!(
                flat.contains(name),
                "{name}
{painted}"
            );
        }
        assert!(!painted.contains("gsk_"));
    }

    #[test]
    fn draft_parsing_follows_each_kind() {
        assert_eq!(
            parse_draft(Kind::Int, Some(30), Some(MAX_INT), "900"),
            Ok(json!(900))
        );
        assert!(parse_draft(Kind::Int, Some(30), Some(MAX_INT), "29").is_err());
        assert!(parse_draft(Kind::Int, Some(30), Some(MAX_INT), "-5").is_err());
        assert!(parse_draft(Kind::Int, Some(30), Some(MAX_INT), "").is_err());
        assert!(parse_draft(Kind::Int, Some(0), Some(MAX_INT), "99999999999999999999").is_err());
        assert_eq!(
            parse_draft(Kind::OptionalInt, Some(1), Some(MAX_INT), " "),
            Ok(Value::Null)
        );
        assert_eq!(
            parse_draft(
                Kind::UrlList,
                None,
                None,
                "http://a.test, https://b.test:8443"
            ),
            Ok(json!(["http://a.test", "https://b.test:8443"]))
        );
        assert_eq!(parse_draft(Kind::UrlList, None, None, ""), Ok(json!([])));
        assert_eq!(parse_draft(Kind::UrlOrBlank, None, None, ""), Ok(json!("")));
        assert!(parse_draft(Kind::Url, None, None, "").is_err());
        assert!(parse_draft(Kind::Path, None, None, "bad\nline").is_err());
        assert_eq!(draft_of(Kind::UrlList, &json!(["a", "b"])), "a, b");
        assert_eq!(draft_of(Kind::OptionalInt, &Value::Null), "");
    }

    #[test]
    fn review_names_the_timing_and_enter_waits_for_the_whole_review() {
        let (id, doc) = fixture();
        let mut view = opened(&doc, &id);
        screen(&mut view);
        // lm_host is selected first: edit it.
        assert!(view.key(press(KeyCode::Char('e'))).is_none());
        for _ in 0..21 {
            view.key(press(KeyCode::Backspace));
        }
        for c in "http://127.0.0.1:1235".chars() {
            view.key(press(KeyCode::Char(c)));
        }
        screen(&mut view);
        let request = view.key(press(KeyCode::Enter)).expect("review request");
        let command = request.command(&id, "i").unwrap();
        assert_eq!(command["type"], "gui.connection.native_review");
        assert_eq!(command["field"], "lm_host");
        assert_eq!(command["value"], "http://127.0.0.1:1235");
        // The request alone mutates nothing.
        assert_eq!(
            view.data.as_ref().unwrap().fields["lm_host"].saved,
            json!("http://localhost:1234")
        );
        let Reply::Update(Update::Review(review)) = request
            .response(
                &receipt(
                    &id,
                    &doc,
                    "lm_host",
                    json!("http://127.0.0.1:1235"),
                    "connection_review",
                    None,
                    None,
                ),
                &id,
                "i",
                "r",
            )
            .unwrap()
        else {
            panic!("expected a review")
        };
        view.waiting = true;
        view.update(Ok(Update::Review(review)));
        // Not yet painted: Enter must not confirm.
        assert!(view.key(press(KeyCode::Enter)).is_none());
        let painted = screen(&mut view);
        assert!(
            painted.contains("Applies: the next reconnect or relaunch"),
            "{painted}"
        );
        assert!(
            painted.contains("never reconnects, contacts a server"),
            "{painted}"
        );
        assert!(
            painted.contains("Requested: http://127.0.0.1:1235"),
            "{painted}"
        );
        let apply = view.key(press(KeyCode::Enter)).expect("apply request");
        assert_eq!(
            apply.command(&id, "i").unwrap()["type"],
            "gui.connection.native_apply"
        );
    }

    fn receipt(
        id: &Identity,
        doc: &Value,
        key: &str,
        value: Value,
        kind: &str,
        after: Option<&str>,
        preferences: Option<Value>,
    ) -> Value {
        let row = &doc["fields"][key];
        let mut v = id.request();
        for (name, val) in [
            ("kind", json!(kind)),
            ("scope", row["scope"].clone()),
            ("work_instance", json!("i")),
            ("token", json!("a".repeat(64))),
            ("field", json!(key)),
            ("value", value),
            ("timing", row["timing"].clone()),
            ("effect", row["effect"].clone()),
            ("before", doc["revision"].clone()),
            ("after", after.map_or(Value::Null, |a| json!(a))),
            (
                "apply_id",
                match kind {
                    "connection_review" => Value::Null,
                    _ => json!("apply"),
                },
            ),
        ] {
            v[name] = val;
        }
        if let Some(p) = preferences {
            v["preferences"] = p;
        }
        v
    }

    #[test]
    fn readback_keeps_a_pending_field_pending_and_rejects_drift() {
        let (id, doc) = fixture();
        let before = Snapshot::parse(&doc, &id, "i").unwrap();
        for (key, value, applied_now) in [
            ("lm_host", json!("http://127.0.0.1:1235"), false),
            ("lms_load_timeout_s", json!(900), true),
            ("default_context_length", json!(32768), false),
        ] {
            let review = Review {
                edit: Edit {
                    before: before.clone(),
                    key: key.into(),
                    value: value.clone(),
                },
                token: "a".repeat(64),
            };
            let after = "b".repeat(64);
            let mut saved = doc.clone();
            saved["revision"] = json!(after);
            saved["fields"][key]["saved"] = value.clone();
            if applied_now {
                saved["fields"][key]["effective"] = value.clone();
            }
            let response = receipt(
                &id,
                &doc,
                key,
                value.clone(),
                "connection_saved",
                Some(after.as_str()),
                Some(saved),
            );
            let request = Request::Readback(review, "apply".into(), after);
            let Ok(Reply::Update(Update::Saved(snapshot))) =
                request.clone().response(&response, &id, "i", "readback")
            else {
                panic!("{key} readback was refused");
            };
            assert_eq!(snapshot.fields[key].pending(), !applied_now, "{key}");
            for (path, drifted) in [
                ("/field", json!("llama_host")),
                ("/apply_id", json!("replay")),
                ("/timing", json!("immediate")),
                ("/effect", json!("Saving restarts the world.")),
                ("/preferences/fields/llama_models_max/saved", json!(9)),
                (
                    "/preferences/fields/pin_default_model/effective",
                    json!(true),
                ),
            ] {
                let mut drift = response.clone();
                *drift.pointer_mut(path).unwrap() = drifted;
                assert!(
                    request
                        .clone()
                        .response(&drift, &id, "i", "readback")
                        .is_err(),
                    "{key} {path}"
                );
            }
            // A reconnect field must not claim to have applied; an immediate one must.
            let mut wrong = response.clone();
            wrong["preferences"]["fields"][key]["effective"] = if applied_now {
                doc["fields"][key]["effective"].clone()
            } else {
                value.clone()
            };
            assert!(
                request
                    .clone()
                    .response(&wrong, &id, "i", "readback")
                    .is_err(),
                "{key}"
            );
        }
    }

    #[test]
    fn requests_refuse_locked_unchanged_and_out_of_range_edits() {
        let (id, mut doc) = fixture();
        doc["fields"]["claude_executable"]["editable"] = json!(false);
        doc["fields"]["claude_executable"]["reason"] =
            json!("A launch option owns this value for this process");
        let before = Snapshot::parse(&doc, &id, "i").unwrap();
        let edit = |key: &str, value: Value| {
            Request::Review(Edit {
                before: before.clone(),
                key: key.into(),
                value,
            })
            .command(&id, "i")
        };
        assert!(edit("claude_executable", json!("C:/x/claude.exe")).is_err());
        assert!(edit("lm_host", json!("http://localhost:1234")).is_err());
        assert!(edit("lm_host", json!("http://u:p@h.test")).is_err());
        assert!(edit("lms_load_timeout_s", json!(10)).is_err());
        assert!(edit("pin_default_model", json!("true")).is_err());
        assert!(edit("groq_api_key", json!("x")).is_err());
        assert!(edit("lms_load_timeout_s", json!(1200)).is_ok());
        assert!(edit("pin_default_model", json!(true)).is_ok());
    }

    #[test]
    fn locked_rows_and_toggles_behave_and_esc_keeps_the_composer_flow() {
        let (id, mut doc) = fixture();
        doc["fields"]["pin_default_model"]["editable"] = json!(false);
        doc["fields"]["pin_default_model"]["reason"] =
            json!("Environment variable LITETUI_X owns this value");
        let mut view = opened(&doc, &id);
        screen(&mut view);
        view.key(press(KeyCode::Down));
        screen(&mut view);
        // Locked: arrows do not start a toggle, and E is ignored for booleans.
        assert!(view.key(press(KeyCode::Right)).is_none());
        assert!(view.toggle.is_none());
        let painted = screen(&mut view);
        assert!(painted.contains("LITETUI_X"), "{painted}");
        // Escape from the list closes the panel; a draft is cancelled first.
        view.key(press(KeyCode::Up));
        view.key(press(KeyCode::Char('e')));
        assert!(view.text.is_some());
        view.key(press(KeyCode::Esc));
        assert!(view.text.is_none() && view.open);
        view.key(press(KeyCode::Esc));
        assert!(!view.open);
    }
}
