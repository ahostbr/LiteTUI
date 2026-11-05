//! Reviewed inference preferences for the current requested model.
use crate::{
    theme::Theme,
    work_lifecycle::{hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, io};

pub const OPERATIONS: [&str; 4] = [
    "gui.models.native_preferences_snapshot",
    "gui.models.native_preferences_review",
    "gui.models.native_preferences_apply",
    "gui.models.native_preferences_readback",
];
const FIELDS: [&str; 36] = [
    "temperature",
    "top_p",
    "max_tokens",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "reasoning_effort",
    "enable_thinking",
    "json_schema",
    "ctx",
    "ngl",
    "threads",
    "batch",
    "ubatch",
    "parallel",
    "flash_attn",
    "cache_k",
    "cache_v",
    "kv_offload",
    "kv_unified",
    "mlock",
    "mmap",
    "context_shift",
    "seed",
    "rope_base",
    "rope_scale",
    "spec_type",
    "draft_model",
    "draft_max",
    "draft_min",
    "draft_p_min",
    "mmproj",
    "chat_template_file",
];
fn load_field(key: &str) -> bool {
    FIELDS[12..].contains(&key)
}
fn kind(key: &str) -> &'static str {
    match key {
        "max_tokens" | "top_k" | "ctx" | "ngl" | "threads" | "batch" | "ubatch" | "parallel"
        | "seed" | "draft_max" | "draft_min" => "int",
        "enable_thinking" | "kv_offload" | "kv_unified" | "mlock" | "mmap" | "context_shift" => {
            "tri"
        }
        "stop" => "strings",
        "json_schema" => "json",
        "reasoning_effort" | "flash_attn" | "cache_k" | "cache_v" | "spec_type" => "select",
        "draft_model" | "mmproj" | "chat_template_file" => "text",
        _ => "float",
    }
}

fn bad() -> io::Error {
    io::Error::other("Model preference response differs from the reviewed conversation")
}
fn value_ok(key: &str, v: &Value) -> bool {
    v.is_null()
        || match kind(key) {
            "int" => v.as_i64().is_some_and(|n| key != "max_tokens" || n > 0),
            "float" => v.as_f64().is_some_and(f64::is_finite),
            "tri" => v.is_boolean(),
            "strings" => v.as_array().is_some_and(|a| {
                a.len() <= 256
                    && a.iter()
                        .all(|v| v.as_str().is_some_and(|s| s.len() <= 4096))
            }),
            "json" => v
                .as_str()
                .is_some_and(|s| s.len() <= 65536 && serde_json::from_str::<Value>(s).is_ok()),
            _ => v.as_str().is_some_and(|s| s.len() <= 65536),
        }
}
fn wire_ok(key: &str, v: &Value) -> bool {
    if key == "json_schema" {
        v.is_null() || v.is_object()
    } else {
        value_ok(key, v)
    }
}
fn mode(edit: &Edit) -> &'static str {
    if edit.inherit {
        "inherit"
    } else if edit.value.is_null() {
        "server"
    } else {
        "value"
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Spec {
    label: String,
    group: String,
    kind: String,
    choices: Vec<String>,
    minimum: Value,
    maximum: Value,
    help: String,
    editable: bool,
    reason: Option<String>,
    wire_choices: Value,
}
impl Spec {
    fn parse(key: &str, v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "label",
                "group",
                "kind",
                "choices",
                "minimum",
                "maximum",
                "help",
                "editable",
                "reason",
                "wire_choices",
            ],
        )?;
        let text = |name: &str| {
            v[name]
                .as_str()
                .filter(|s| s.len() <= 4096)
                .map(str::to_owned)
                .ok_or_else(bad)
        };
        let group = text("group")?;
        if group != if load_field(key) { "load" } else { "inference" } || v["kind"] != kind(key) {
            return Err(bad());
        }
        let choices = v["choices"]
            .as_array()
            .filter(|a| a.len() <= 64)
            .ok_or_else(bad)?
            .iter()
            .map(|v| {
                v.as_str()
                    .filter(|s| s.len() <= 256)
                    .map(str::to_owned)
                    .ok_or_else(bad)
            })
            .collect::<io::Result<Vec<_>>>()?;
        for bound in ["minimum", "maximum"] {
            if !v[bound].is_null() && !v[bound].as_f64().is_some_and(f64::is_finite) {
                return Err(bad());
            }
        }
        let editable = v["editable"].as_bool().ok_or_else(bad)?;
        let reason = match &v["reason"] {
            Value::Null if editable => None,
            Value::String(s) if !editable && !s.is_empty() && s.len() <= 4096 => Some(s.clone()),
            _ => return Err(bad()),
        };
        let wires = v["wire_choices"].as_object().ok_or_else(bad)?;
        if key == "reasoning_effort" {
            if wires.len() != choices.len()
                || choices
                    .iter()
                    .any(|c| !wires.contains_key(c) || !wire_ok(key, &wires[c]))
            {
                return Err(bad());
            }
        } else if !wires.is_empty() {
            return Err(bad());
        }
        Ok(Self {
            label: text("label")?,
            group,
            kind: text("kind")?,
            choices,
            minimum: v["minimum"].clone(),
            maximum: v["maximum"].clone(),
            help: text("help")?,
            editable,
            reason,
            wire_choices: v["wire_choices"].clone(),
        })
    }
    fn accepts(&self, key: &str, value: &Value) -> bool {
        value_ok(key, value)
            && (value.is_null()
                || (self.kind != "select"
                    || value
                        .as_str()
                        .is_some_and(|s| self.choices.iter().any(|c| c == s)))
                    && (!value.is_number()
                        || value.as_f64().is_some_and(|n| {
                            self.minimum.as_f64().is_none_or(|lo| n >= lo)
                                && self.maximum.as_f64().is_none_or(|hi| n <= hi)
                        })))
    }
    fn wire(&self, key: &str, value: &Value) -> io::Result<Value> {
        if key == "reasoning_effort" && !value.is_null() {
            return self
                .wire_choices
                .get(value.as_str().ok_or_else(bad)?)
                .cloned()
                .ok_or_else(bad);
        }
        if key == "json_schema" && !value.is_null() {
            return Ok(
                json!({"type":"json_schema", "json_schema":{"name":"litetui_schema", "schema":serde_json::from_str::<Value>(value.as_str().ok_or_else(bad)?).map_err(|_| bad())?}}),
            );
        }
        Ok(value.clone())
    }
}
fn show(v: &Value) -> String {
    if v.is_null() {
        "server default".into()
    } else {
        v.to_string()
    }
}
fn binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
        || v["scope"] != "conversation"
        || v["work_instance"] != instance
        || !id
            .request()
            .as_object()
            .expect("identity")
            .iter()
            .all(|(k, x)| v[k] == *x)
    {
        return Err(bad());
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Field {
    present: bool,
    saved: Value,
    active_present: bool,
    active: Value,
    next: Value,
    inherited: Value,
    editable: bool,
    overridden: bool,
    server_default: Value,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    revision: String,
    fields: BTreeMap<String, Field>,
    supported: bool,
    catalog: BTreeMap<String, Spec>,
    raw: Value,
    reason: Option<String>,
    loaded: Option<bool>,
    context: Option<u64>,
    declared: bool,
    deferred_load: u64,
    model: String,
    backend: String,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        binding(v, id, instance, "model_preferences")?;
        keys(
            v,
            &[
                "version",
                "kind",
                "scope",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "revision",
                "fields",
                "supported",
                "reason",
                "reported_loaded",
                "context_length",
                "context_source",
                "deferred_load_fields",
                "catalog",
                "raw",
            ],
        )?;
        keys(&v["fields"], &FIELDS)?;
        keys(&v["catalog"], &FIELDS)?;
        keys(&v["raw"], &["inference", "load"])?;
        for group in ["inference", "load"] {
            keys(&v["raw"][group], &["saved", "active"])?;
            for state in ["saved", "active"] {
                if !v["raw"][group][state].is_object() {
                    return Err(bad());
                }
            }
        }
        let catalog = FIELDS
            .iter()
            .map(|k| Ok(((*k).to_owned(), Spec::parse(k, &v["catalog"][*k])?)))
            .collect::<io::Result<BTreeMap<_, _>>>()?;
        let supported = v["supported"].as_bool().ok_or_else(bad)?;
        let expected =
            ["lmstudio", "llamacpp", "custom", "ninfer", "strata"].contains(&id.backend.as_str());
        if supported != expected {
            return Err(bad());
        }
        let reason = match &v["reason"] {
            Value::Null if supported => None,
            Value::String(s)
                if !supported
                    && !s.is_empty()
                    && s.len() <= 1024
                    && !s.chars().any(crate::display_control) =>
            {
                Some(s.clone())
            }
            _ => return Err(bad()),
        };
        let loaded = match v["reported_loaded"] {
            Value::Null => None,
            Value::Bool(b) => Some(b),
            _ => return Err(bad()),
        };
        let context = if v["context_length"].is_null() {
            None
        } else {
            Some(
                v["context_length"]
                    .as_u64()
                    .filter(|n| (1..=2_147_483_647).contains(n))
                    .ok_or_else(bad)?,
            )
        };
        let declared = id.backend == "custom";
        if v["context_source"]
            != if declared {
                "client_declared"
            } else {
                "backend_reported"
            }
        {
            return Err(bad());
        }
        let deferred_load = v["deferred_load_fields"]
            .as_u64()
            .filter(|n| *n <= 4096)
            .ok_or_else(bad)?;
        let mut fields = BTreeMap::new();
        for key in FIELDS {
            let r = &v["fields"][key];
            keys(
                r,
                &[
                    "present",
                    "saved",
                    "active_present",
                    "active",
                    "next_request",
                    "inherited",
                    "editable",
                    "overridden",
                    "server_default",
                ],
            )?;
            let present = r["present"].as_bool().ok_or_else(bad)?;
            let active_present = r["active_present"].as_bool().ok_or_else(bad)?;
            let editable = r["editable"].as_bool().ok_or_else(bad)?;
            let overridden = r["overridden"].as_bool().ok_or_else(bad)?;
            let meta = &catalog[key];
            let raw = &v["raw"][&meta.group];
            if ["saved", "active"].iter().any(|f| !value_ok(key, &r[*f]))
                || ["next_request", "inherited", "server_default"]
                    .iter()
                    .any(|f| !wire_ok(key, &r[*f]))
                || present != raw["saved"].get(key).is_some()
                || active_present != raw["active"].get(key).is_some()
                || present && raw["saved"][key] != r["saved"]
                || active_present && raw["active"][key] != r["active"]
                || !present && !r["saved"].is_null()
                || !active_present && !r["active"].is_null()
                || editable
                    && (!supported
                        || !meta.editable
                        || overridden
                        || !load_field(key)
                            && (present != active_present || r["saved"] != r["active"]))
                || overridden && !["max_tokens", "reasoning_effort"].contains(&key)
            {
                return Err(bad());
            }
            fields.insert(
                key.into(),
                Field {
                    present,
                    saved: r["saved"].clone(),
                    active_present,
                    active: r["active"].clone(),
                    next: r["next_request"].clone(),
                    inherited: r["inherited"].clone(),
                    editable,
                    overridden,
                    server_default: r["server_default"].clone(),
                },
            );
        }
        Ok(Self {
            revision: hash(v, "revision")?,
            catalog,
            raw: v["raw"].clone(),
            fields,
            supported,
            reason,
            loaded,
            context,
            declared,
            deferred_load,
            model: id.model.clone(),
            backend: id.backend.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edit {
    before: Snapshot,
    field: String,
    value: Value,
    inherit: bool,
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
        let mut c = id.request();
        c["work_instance"] = json!(instance);
        c["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Review(e) => {
                let row = e
                    .before
                    .fields
                    .get(&e.field)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                if e.inherit && (!e.value.is_null() || !row.present)
                    || !e.inherit
                        && (e.value.is_null() && load_field(&e.field)
                            || !e.before.catalog[&e.field].accepts(&e.field, &e.value)
                            || row.present && row.saved == e.value)
                {
                    return Err(bad());
                }
                c["revision"] = json!(e.before.revision);
                c["field"] = json!(e.field);
                c["mode"] = json!(mode(e));
                c["value"] = e.value.clone();
                OPERATIONS[1]
            }
            Self::Apply(r) => {
                c["token"] = json!(r.token);
                OPERATIONS[2]
            }
            Self::Readback(r, a, _) => {
                c["token"] = json!(r.token);
                c["apply_id"] = json!(a);
                OPERATIONS[3]
            }
        });
        Ok(c)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command_id: &str,
    ) -> io::Result<Reply> {
        let (kind, edit, token, applied) = match &self {
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance,
                )?)));
            }
            Self::Review(e) => ("model_preferences_review", e, None, None),
            Self::Apply(r) => (
                "model_preferences_applied",
                &r.edit,
                Some(&r.token),
                Some(command_id),
            ),
            Self::Readback(r, a, _) => (
                "model_preferences_saved",
                &r.edit,
                Some(&r.token),
                Some(a.as_str()),
            ),
        };
        binding(v, id, instance, kind)?;
        let mut fields = vec![
            "version",
            "kind",
            "scope",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "token",
            "field",
            "mode",
            "value",
            "before",
            "after",
            "apply_id",
        ];
        if matches!(self, Self::Readback(..)) {
            fields.push("preferences");
        }
        keys(v, &fields)?;
        let actual_token = hash(v, "token")?;
        if v["field"] != edit.field
            || v["before"] != edit.before.revision
            || v["value"] != edit.value
            || v["mode"] != mode(edit)
            || token.is_some_and(|x| x != &actual_token)
        {
            return Err(bad());
        }
        if let Some(a) = applied {
            if v["apply_id"] != a || hash(v, "after")? == edit.before.revision {
                return Err(bad());
            }
        } else if !v["after"].is_null() || !v["apply_id"].is_null() {
            return Err(bad());
        }
        match self {
            Self::Review(edit) => Ok(Reply::Update(Update::Review(Review {
                edit,
                token: actual_token,
            }))),
            Self::Apply(r) => Ok(Reply::Next(Self::Readback(
                r,
                command_id.into(),
                hash(v, "after")?,
            ))),
            Self::Readback(r, _, revision) => {
                let actual = Snapshot::parse(&v["preferences"], id, instance)?;
                let mut expected = r.edit.before;
                expected.revision = revision;
                let row = expected.fields.get_mut(&r.edit.field).ok_or_else(bad)?;
                row.present = !r.edit.inherit;
                row.saved = r.edit.value.clone();
                let group = expected.catalog[&r.edit.field].group.clone();
                if !load_field(&r.edit.field) {
                    row.active_present = row.present;
                    row.active = row.saved.clone();
                    row.next = if r.edit.inherit {
                        row.inherited.clone()
                    } else if r.edit.value.is_null() {
                        row.server_default.clone()
                    } else {
                        expected.catalog[&r.edit.field].wire(&r.edit.field, &r.edit.value)?
                    };
                }
                for state in if load_field(&r.edit.field) {
                    &["saved"][..]
                } else {
                    &["saved", "active"][..]
                } {
                    let raw = expected.raw[&group][*state]
                        .as_object_mut()
                        .ok_or_else(bad)?;
                    if r.edit.inherit {
                        raw.remove(&r.edit.field);
                    } else {
                        raw.insert(r.edit.field.clone(), r.edit.value.clone());
                    }
                }
                if actual != expected || actual.revision != hash(v, "after")? {
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
    draft: Option<String>,
    server: bool,
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
        };
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.scroll = 0;
        self.note.clear();
        self.review_seen = 0;
        match result {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Saved(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
                self.note = "Saved and verified. Inference: next request. Load: reload/reopen pending; running model unchanged.".into();
            }
            Err(e) => {
                self.data = None;
                self.review = None;
                self.draft = None;
                self.note = crate::live::terminal_text(&e);
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        if key.code == KeyCode::Esc {
            if self.review.take().is_some() || self.draft.take().is_some() {
                self.draft = None;
                self.hide();
            } else {
                *self = Self::default();
            }
            return None;
        }
        if matches!(key.code, KeyCode::PageUp | KeyCode::PageDown) {
            self.scroll = if key.code == KeyCode::PageUp {
                self.scroll.saturating_sub(self.height)
            } else {
                self.scroll
                    .saturating_add(self.height)
                    .min(self.lines.saturating_sub(self.height))
            };
            self.hide();
            return None;
        }
        let field = FIELDS[self.selected];
        if self.review.is_none()
            && let Some(text) = &mut self.draft
        {
            match key.code {
                KeyCode::Char(c) if !crate::display_control(c) => {
                    self.server = false;
                    if text.len() < 65536 {
                        text.push(c);
                    }
                    self.hide();
                    return None;
                }
                KeyCode::Backspace => {
                    text.pop();
                    self.hide();
                    return None;
                }
                _ => {}
            }
        }
        let request = match key.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                self.selected = if key.code == KeyCode::Up {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(FIELDS.len() - 1)
                };
                self.draft = None;
                self.server = false;
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Char('i') | KeyCode::Char('e') | KeyCode::Char('d')
                if self.review.is_none() =>
            {
                let row = self.data.as_ref()?.fields.get(field)?;
                if row.editable && (key.code != KeyCode::Char('d') || !load_field(field)) {
                    self.server = key.code == KeyCode::Char('d');
                    self.draft = Some(String::new());
                    self.note = if self.server {
                        "Explicit server default: remove the inherited request override. Enter reviews.".into()
                    } else if key.code == KeyCode::Char('i') {
                        "Inherit this conversation's default. Enter reviews.".into()
                    } else {
                        "Type a value (true/false for booleans; comma-separated stop strings). Empty input inherits.".into()
                    };
                    self.hide();
                }
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Enter if self.visible => {
                let text = self.draft.as_ref()?;
                let meta = &self.data.as_ref()?.catalog[field];
                let value = if text.is_empty() || self.server {
                    Value::Null
                } else {
                    let parsed = match meta.kind.as_str() {
                        "text" | "select" | "json" => Ok(json!(text)),
                        "strings" if text == "[]" => Ok(json!([])),
                        "strings" => Ok(json!(
                            text.split(',')
                                .map(str::trim)
                                .filter(|s| !s.is_empty())
                                .collect::<Vec<_>>()
                        )),
                        _ => serde_json::from_str::<Value>(text),
                    };
                    match parsed {
                        Ok(v) if !v.is_null() && meta.accepts(field, &v) => v,
                        _ => {
                            self.note =
                                "Value does not match the source field type, bounds or choices."
                                    .into();
                            self.hide();
                            return None;
                        }
                    }
                };
                let before = self.data.clone()?;
                let row = &before.fields[field];
                if !row.editable
                    || text.is_empty() && !self.server && !row.present
                    || (!text.is_empty() || self.server) && row.present && row.saved == value
                {
                    self.note = "Choose a different value.".into();
                    return None;
                }
                Request::Review(Edit {
                    before,
                    field: field.into(),
                    value,
                    inherit: text.is_empty() && !self.server,
                })
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(request)
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Model preferences for this conversation".to_owned()];
        if let Some(s) = &self.data {
            lines.push(format!("Requested model: {} · {}", s.model, s.backend));
            lines.push(format!(
                "Loaded according to adapter: {} · Context: {} ({})",
                s.loaded
                    .map(|v| if v { "yes" } else { "no" })
                    .unwrap_or("unknown"),
                s.context
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "unknown".into()),
                if s.declared {
                    "client declared"
                } else {
                    "backend reported"
                }
            ));
        }
        if let Some(r) = &self.review {
            let row = &r.edit.before.fields[&r.edit.field];
            lines.extend([
                format!("Save {} for {}?", r.edit.before.catalog[&r.edit.field].label, r.edit.before.model),
                format!("Current {}: {}", if load_field(&r.edit.field) { "active load preference" } else { "next request" }, show(&row.next)),
                format!(
                    "Requested preference: {}",
                    show(if r.edit.inherit {
                        &row.inherited
                    } else {
                        &r.edit.value
                    })
                ),
                if r.edit.inherit {
                    "Remove this model override and inherit the conversation default.".into()
                } else {
                    "Save this model override in the current conversation.".into()
                },
                if load_field(&r.edit.field) { "Save only. Running settings remain unchanged; reload/reopen is required.".into() }
                else if r.edit.value.is_null() && !r.edit.inherit { "Explicit null suppresses the global override; request-constructor defaults still apply.".into() }
                else { "Inference takes effect in the next request.".into() },
                "Enter save · Esc cancel".into(),
            ]);
        } else if let Some(s) = &self.data {
            if let Some(reason) = &s.reason {
                lines.push(reason.clone());
            }
            for (i, key) in FIELDS
                .iter()
                .enumerate()
                .skip(self.selected.saturating_sub(3))
                .take(7)
            {
                let r = &s.fields[*key];
                lines.push(format!(
                    "{} {} · saved {} · active/next {}{}",
                    if i == self.selected { ">" } else { " " },
                    s.catalog[*key].label,
                    if r.present {
                        show(&r.saved)
                    } else {
                        "inherit".into()
                    },
                    show(&r.next),
                    if r.overridden {
                        " (launch override)"
                    } else {
                        ""
                    }
                ));
            }
            let key = FIELDS[self.selected];
            let meta = &s.catalog[key];
            let row = &s.fields[key];
            lines.push(format!(
                "Field {}/{} · {} · {}",
                self.selected + 1,
                FIELDS.len(),
                meta.group,
                meta.help
            ));
            if !meta.choices.is_empty() {
                lines.push(format!("Choices: {}", meta.choices.join(", ")));
            }
            if let Some(why) = &meta.reason {
                lines.push(why.clone());
            }
            if load_field(key) {
                lines.push("Load values are saved only. Reload/reopen required; active settings and running model are unchanged.".into());
                if row.present != row.active_present || row.saved != row.active {
                    lines.push(
                        "Saved load preference differs from active settings (pending).".into(),
                    );
                }
            } else {
                lines.push(format!(
                    "Inherited request value: {} · Explicit default: {}",
                    show(&row.inherited),
                    show(&row.server_default)
                ));
            }
            if let Some(draft) = &self.draft {
                lines.push(format!(
                    "{}: {}",
                    s.catalog[FIELDS[self.selected]].label,
                    if draft.is_empty() { "inherit" } else { draft }
                ));
            }
            lines.push(
                "↑/↓ field · E edit · I inherit · D server default · Enter review · PgUp/PgDn · F5 refresh · Esc back"
                    .into(),
            );
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone());
        }
        if self.waiting {
            lines.push("Waiting for the source…".into());
        }
        if self.data.is_none() && !self.waiting {
            lines.push("F5 refresh · Esc back".into());
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
                .block(Block::bordered().title(" Model preferences ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};

    fn fixture() -> (Identity, Value) {
        let id = Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "requested".into(),
        };
        let mut v = id.request();
        for (k, x) in [
            ("kind", json!("model_preferences")),
            ("scope", json!("conversation")),
            ("work_instance", json!("instance")),
            ("revision", json!("a".repeat(64))),
            ("supported", json!(true)),
            ("reason", Value::Null),
            ("reported_loaded", json!(false)),
            ("context_length", json!(8192)),
            ("context_source", json!("client_declared")),
            ("deferred_load_fields", json!(0)),
            (
                "catalog",
                serde_json::from_str(include_str!(
                    "../tests/fixtures/model_preferences_catalog.json"
                ))
                .unwrap(),
            ),
            (
                "raw",
                json!({"inference":{"saved":{},"active":{}},"load":{"saved":{},"active":{}}}),
            ),
        ] {
            v[k] = x;
        }
        for k in FIELDS {
            let default = if k == "max_tokens" {
                json!(4096)
            } else {
                Value::Null
            };
            v["fields"][k] = json!({"present":false,"saved":null,"active_present":false,
                "active":null,"next_request":default,"inherited":default,"editable":v["catalog"][k]["editable"],"overridden":false,"server_default":default});
        }
        (id, v)
    }
    fn receipt(id: &Identity, r: &Review, kind: &str, after: Value, apply: Value) -> Value {
        let mut v = id.request();
        for (k, x) in [
            ("kind", json!(kind)),
            ("scope", json!("conversation")),
            ("work_instance", json!("instance")),
            ("token", json!(r.token)),
            ("field", json!(r.edit.field)),
            ("mode", json!(mode(&r.edit))),
            ("value", r.edit.value.clone()),
            ("before", json!(r.edit.before.revision)),
            ("after", after),
            ("apply_id", apply),
        ] {
            v[k] = x;
        }
        v
    }
    #[test]
    fn model_selection_residency_and_request_fields_remain_distinct() {
        let (id, v) = fixture();
        let s = Snapshot::parse(&v, &id, "instance").unwrap();
        assert_eq!(s.model, "requested");
        assert_eq!(s.loaded, Some(false));
        for (pointer, x) in [
            ("/work_instance", json!("old")),
            ("/version", json!(2)),
            ("/model", json!("different")),
            ("/reported_loaded", json!("false")),
            ("/context_source", json!("backend_reported")),
            ("/fields/temperature/next_request", json!("0.5")),
            ("/fields/max_tokens/inherited", json!(-1)),
            ("/fields/top_p/overridden", json!(true)),
        ] {
            let mut invalid = v.clone();
            *invalid.pointer_mut(pointer).unwrap() = x;
            assert!(
                Snapshot::parse(&invalid, &id, "instance").is_err(),
                "{pointer}"
            );
        }
    }
    #[test]
    fn saved_request_requires_exact_review_application_and_independent_values() {
        let (id, v) = fixture();
        let r = Review {
            edit: Edit {
                before: Snapshot::parse(&v, &id, "instance").unwrap(),
                field: "temperature".into(),
                value: json!(0.35),
                inherit: false,
            },
            token: "b".repeat(64),
        };
        let revision = "c".repeat(64);
        let applied = receipt(
            &id,
            &r,
            "model_preferences_applied",
            json!(revision),
            json!("apply-1"),
        );
        let Reply::Next(request) = Request::Apply(r.clone())
            .response(&applied, &id, "instance", "apply-1")
            .unwrap()
        else {
            panic!("readback required");
        };
        assert_eq!(
            request.command(&id, "instance").unwrap()["apply_id"],
            "apply-1"
        );
        let mut saved = receipt(
            &id,
            &r,
            "model_preferences_saved",
            json!(revision),
            json!("apply-1"),
        );
        let mut snapshot = v;
        snapshot["revision"] = json!(revision);
        let row = &mut snapshot["fields"]["temperature"];
        row["present"] = json!(true);
        row["active_present"] = json!(true);
        row["saved"] = json!(0.35);
        row["active"] = json!(0.35);
        row["next_request"] = json!(0.35);
        snapshot["raw"]["inference"]["saved"]["temperature"] = json!(0.35);
        snapshot["raw"]["inference"]["active"]["temperature"] = json!(0.35);
        saved["preferences"] = snapshot;
        assert!(matches!(
            request
                .clone()
                .response(&saved, &id, "instance", "read-2")
                .unwrap(),
            Reply::Update(Update::Saved(_))
        ));
        for (pointer, x) in [
            ("/apply_id", json!("other")),
            ("/token", json!("d".repeat(64))),
            ("/preferences/fields/temperature/next_request", json!(0.6)),
            ("/preferences/fields/top_p/inherited", json!(0.8)),
            ("/preferences/reported_loaded", json!(true)),
        ] {
            let mut drift = saved.clone();
            *drift.pointer_mut(pointer).unwrap() = x;
            assert!(
                request
                    .clone()
                    .response(&drift, &id, "instance", "read-2")
                    .is_err(),
                "{pointer}"
            );
        }
    }
    #[test]
    fn an_entire_review_must_be_visible_before_enter_can_apply() {
        let (id, v) = fixture();
        let snapshot = Snapshot::parse(&v, &id, "instance").unwrap();
        let r = Review {
            edit: Edit {
                before: snapshot,
                field: "max_tokens".into(),
                value: json!(321),
                inherit: false,
            },
            token: "b".repeat(64),
        };
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::Review(r)));
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        assert!(view.key(enter).is_none());
        let mut small = Terminal::new(TestBackend::new(30, 5)).unwrap();
        small
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(view.key(enter).is_none());
        let mut large = Terminal::new(TestBackend::new(100, 30)).unwrap();
        large
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(matches!(view.key(enter), Some(Request::Apply(_))));
        assert!(view.key(enter).is_none());
    }
    #[test]
    fn load_readback_keeps_active_configuration_and_unknown_keys_exact() {
        let (mut id, mut value) = fixture();
        id.backend = "lmstudio".into();
        value["backend"] = json!("lmstudio");
        value["context_source"] = json!("backend_reported");
        value["catalog"]["ctx"]["editable"] = json!(true);
        value["catalog"]["ctx"]["reason"] = Value::Null;
        value["fields"]["ctx"]["editable"] = json!(true);
        value["raw"]["load"]["saved"]["future"] = json!({"keep":true});
        value["raw"]["load"]["active"]["future"] = json!({"keep":true});
        let review = Review {
            edit: Edit {
                before: Snapshot::parse(&value, &id, "instance").unwrap(),
                field: "ctx".into(),
                value: json!(4096),
                inherit: false,
            },
            token: "b".repeat(64),
        };
        let revision = "c".repeat(64);
        let mut saved = receipt(
            &id,
            &review,
            "model_preferences_saved",
            json!(revision),
            json!("apply"),
        );
        value["revision"] = json!(revision);
        value["fields"]["ctx"]["present"] = json!(true);
        value["fields"]["ctx"]["saved"] = json!(4096);
        value["raw"]["load"]["saved"]["ctx"] = json!(4096);
        saved["preferences"] = value;
        let request = Request::Readback(review, "apply".into(), revision);
        assert!(
            request
                .clone()
                .response(&saved, &id, "instance", "read")
                .is_ok()
        );
        for (path, mutation) in [
            ("/preferences/raw/load/active/future/keep", json!(false)),
            ("/preferences/fields/ctx/active", json!(4096)),
            ("/preferences/fields/ctx/next_request", json!(4096)),
            ("/preferences/raw/load/saved/future/keep", json!(false)),
        ] {
            let mut changed = saved.clone();
            *changed.pointer_mut(path).unwrap() = mutation;
            assert!(
                request
                    .clone()
                    .response(&changed, &id, "instance", "read")
                    .is_err(),
                "{path}"
            );
        }
    }
    #[test]
    fn long_json_review_requires_every_page_before_confirming() {
        let (id, value) = fixture();
        let review = Review {
            edit: Edit {
                before: Snapshot::parse(&value, &id, "instance").unwrap(),
                field: "json_schema".into(),
                value: json!(format!(r#"{{"description":"{}"}}"#, "x".repeat(4000))),
                inherit: false,
            },
            token: "b".repeat(64),
        };
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::Review(review)));
        let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(view.key(enter).is_none());
        for _ in 0..20 {
            view.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            terminal
                .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
                .unwrap();
        }
        assert!(matches!(view.key(enter), Some(Request::Apply(_))));
    }
}
