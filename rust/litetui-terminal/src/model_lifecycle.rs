//! Explicit current-model operations with canonical source settlement and readback.
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
    "gui.models.native_lifecycle_snapshot",
    "gui.models.native_lifecycle_review",
    "gui.models.native_lifecycle_apply",
    "gui.models.native_lifecycle_readback",
];
const ACTIONS: [&str; 3] = ["load", "unload", "reconnect"];
const ID: [&str; 7] = [
    "version",
    "kind",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "work_instance",
];
fn bad() -> io::Error {
    io::Error::other("Model lifecycle response differs from the reviewed runtime")
}
fn text(v: &Value, limit: usize) -> io::Result<String> {
    v.as_str()
        .filter(|s| !s.is_empty() && s.len() <= limit && !s.chars().any(crate::display_control))
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn number(v: &Value) -> io::Result<Option<u64>> {
    if v.is_null() {
        Ok(None)
    } else {
        v.as_u64()
            .filter(|n| (1..=2_147_483_647).contains(n))
            .map(Some)
            .ok_or_else(bad)
    }
}
fn binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
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
pub struct Snapshot {
    revision: String,
    model: String,
    target: String,
    selected: String,
    models: Vec<String>,
    choice: ContextChoice,
    inherited: Option<u64>,
    preset: Option<u64>,
    backend: String,
    endpoint: Option<String>,
    lifetime: String,
    advertised: bool,
    loaded: Option<bool>,
    context: Option<u64>,
    load_context: Option<u64>,
    other_load: u64,
    connected: bool,
    actions: BTreeMap<String, Option<String>>,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        binding(v, id, instance, "model_lifecycle")?;
        keys(
            v,
            &[
                ID.as_slice(),
                &[
                    "revision",
                    "endpoint",
                    "lifetime",
                    "advertised",
                    "reported_loaded",
                    "context_length",
                    "context_source",
                    "load_context",
                    "target",
                    "selected_model",
                    "models",
                    "context_mode",
                    "context_value",
                    "inherited_context",
                    "preset_context",
                    "other_load_fields",
                    "connected",
                    "actions",
                ],
            ]
            .concat(),
        )?;
        let supported = ["custom", "lmstudio", "llamacpp"].contains(&id.backend.as_str());
        let endpoint = if supported {
            let s = text(&v["endpoint"], 2048)?;
            if !s.starts_with("http://") && !s.starts_with("https://")
                || s.contains('@')
                || s.contains('?')
                || s.contains('#')
            {
                return Err(bad());
            }
            Some(s)
        } else if v["endpoint"].is_null() {
            None
        } else {
            return Err(bad());
        };
        let lifetime = text(&v["lifetime"], 16)?;
        if !["external", "unmanaged"].contains(&lifetime.as_str()) {
            return Err(bad());
        }
        let loaded = match v["reported_loaded"] {
            Value::Null => None,
            Value::Bool(b) => Some(b),
            _ => return Err(bad()),
        };
        if id.backend == "custom" && loaded.is_some() {
            return Err(bad());
        }
        if v["context_source"]
            != if id.backend == "custom" {
                "client_declared"
            } else {
                "backend_reported"
            }
        {
            return Err(bad());
        }
        let advertised = v["advertised"].as_bool().ok_or_else(bad)?;
        keys(&v["actions"], &ACTIONS)?;
        let mut actions = BTreeMap::new();
        for a in ACTIONS {
            let row = &v["actions"][a];
            keys(row, &["eligible", "reason"])?;
            let eligible = row["eligible"].as_bool().ok_or_else(bad)?;
            let reason = if eligible && row["reason"].is_null() {
                None
            } else if !eligible {
                Some(text(&row["reason"], 1024)?)
            } else {
                return Err(bad());
            };
            if eligible
                && (!supported
                    || !advertised
                    || lifetime != "external"
                    || a != "reconnect" && id.backend == "custom"
                    || a == "load" && loaded != Some(false)
                    || a == "unload" && loaded != Some(true))
            {
                return Err(bad());
            }
            actions.insert(a.into(), reason);
        }
        let target = text(&v["target"], 1024)?;
        let selected = text(&v["selected_model"], 1024)?;
        let models: Vec<String> = v["models"]
            .as_array()
            .filter(|rows| rows.len() <= 4096)
            .ok_or_else(bad)?
            .iter()
            .map(|row| text(row, 1024))
            .collect::<io::Result<_>>()?;
        if models
            .iter()
            .collect::<std::collections::HashSet<_>>()
            .len()
            != models.len()
            || advertised && !models.contains(&target)
        {
            return Err(bad());
        }
        let choice = ContextChoice::parse(v)?;
        let inherited = number(&v["inherited_context"])?;
        let load_context = number(&v["load_context"])?;
        if load_context != choice.effective(inherited) {
            return Err(bad());
        }
        Ok(Self {
            revision: hash(v, "revision")?,
            target,
            selected,
            models,
            choice,
            inherited,
            preset: number(&v["preset_context"])?,
            model: id.model.clone(),
            backend: id.backend.clone(),
            endpoint,
            lifetime,
            advertised,
            loaded,
            context: number(&v["context_length"])?,
            load_context: number(&v["load_context"])?,
            other_load: v["other_load_fields"]
                .as_u64()
                .filter(|n| *n <= 128)
                .ok_or_else(bad)?,
            connected: v["connected"].as_bool().ok_or_else(bad)?,
            actions,
        })
    }
}
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum ContextChoice {
    #[default]
    Inherit,
    Default,
    Value(u64),
}
impl ContextChoice {
    fn parse(v: &Value) -> io::Result<Self> {
        match (v["context_mode"].as_str(), number(&v["context_value"])?) {
            (Some("inherit"), None) => Ok(Self::Inherit),
            (Some("default"), None) => Ok(Self::Default),
            (Some("value"), Some(n)) => Ok(Self::Value(n)),
            _ => Err(bad()),
        }
    }
    fn effective(&self, inherited: Option<u64>) -> Option<u64> {
        match self {
            Self::Inherit => inherited,
            Self::Default => None,
            Self::Value(n) => Some(*n),
        }
    }
    fn command(&self, c: &mut Value) {
        c["context_mode"] = json!(match self {
            Self::Inherit => "inherit",
            Self::Default => "default",
            Self::Value(_) => "value",
        });
        c["context_value"] = json!(if let Self::Value(n) = self {
            Some(*n)
        } else {
            None
        });
    }
}
impl Update {
    pub fn selected_model(&self) -> Option<&str> {
        if let Self::Settled(s) = self {
            Some(&s.selected)
        } else {
            None
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    before: Snapshot,
    action: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    selection: Selection,
    token: String,
    retirement: Option<Value>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Inspect(String, ContextChoice),
    RefreshReview(String, ContextChoice, String),
    Review(Selection),
    Apply(Review),
    Readback(Review, String, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Review),
    Settled(Snapshot),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
    pub fn transition_target(&self) -> Option<&str> {
        let r = match self {
            Self::Apply(r) | Self::Readback(r, ..) => r,
            _ => return None,
        };
        (r.selection.action == "load" && r.selection.before.target != r.selection.before.model)
            .then_some(r.selection.before.target.as_str())
    }
    pub fn retained_readback(&self) -> bool {
        matches!(self, Self::Readback(r, ..) if r.retirement.is_some())
            && self.transition_target().is_some()
    }
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut c = id.request();
        c["work_instance"] = json!(instance);
        c["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Inspect(target, choice) | Self::RefreshReview(target, choice, _) => {
                c["target"] = json!(target);
                choice.command(&mut c);
                OPERATIONS[0]
            }
            Self::Review(s) => {
                if !matches!(s.before.actions.get(&s.action), Some(None)) {
                    return Err(bad());
                }
                c["revision"] = json!(s.before.revision);
                c["action"] = json!(s.action);
                c["target"] = json!(s.before.target);
                s.before.choice.command(&mut c);
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
        let (kind, s, token, applied) = match &self {
            Self::List | Self::Inspect(..) | Self::RefreshReview(..) => {
                let parsed = Snapshot::parse(v, id, instance)?;
                if parsed.selected != id.model
                    || matches!(&self, Self::Inspect(target, choice) | Self::RefreshReview(target, choice, _)
                    if &parsed.target != target || &parsed.choice != choice)
                {
                    return Err(bad());
                }
                if let Self::RefreshReview(_, _, action) = self {
                    if !matches!(parsed.actions.get(&action), Some(None)) {
                        return Err(bad());
                    }
                    return Ok(Reply::Next(Self::Review(Selection {
                        before: parsed,
                        action,
                    })));
                }
                return Ok(Reply::Update(Update::List(parsed)));
            }
            Self::Review(s) => ("model_lifecycle_review", s, None, None),
            Self::Apply(r) => (
                "model_lifecycle_applied",
                &r.selection,
                Some(&r.token),
                Some(command_id),
            ),
            Self::Readback(r, a, _) => (
                "model_lifecycle_settled",
                &r.selection,
                Some(&r.token),
                Some(a.as_str()),
            ),
        };
        binding(v, id, instance, kind)?;
        let mut fields = [
            ID.as_slice(),
            &["token", "action", "before", "after", "apply_id", "target"],
        ]
        .concat();
        if matches!(self, Self::Readback(..)) {
            fields.push("state");
        }
        let mut retirement = serde_json::Map::new();
        if self.transition_target().is_some() {
            for key in [
                "work_shutdown",
                "work_recapture_required",
                "work_execution_shutdown",
                "runtime_controls_shutdown",
                "runtime_controls_historical_shutdown",
            ] {
                if let Some(value) = v.get(key) {
                    fields.push(key);
                    retirement.insert(key.into(), value.clone());
                }
            }
            if v["work_recapture_required"] != true || !v["work_shutdown"].is_object() {
                return Err(bad());
            }
        }
        keys(v, &fields)?;
        let actual_token = hash(v, "token")?;
        if v["target"] != s.before.target
            || v["action"] != s.action
            || v["before"] != s.before.revision
            || token.is_some_and(|t| t != &actual_token)
        {
            return Err(bad());
        }
        if let Some(a) = applied {
            if v["apply_id"] != a {
                return Err(bad());
            }
            hash(v, "after")?;
        } else if !v["apply_id"].is_null() || !v["after"].is_null() {
            return Err(bad());
        }
        match self {
            Self::Review(selection) => Ok(Reply::Update(Update::Review(Review {
                selection,
                token: actual_token,
                retirement: None,
            }))),
            Self::Apply(mut r) => {
                r.retirement = (!retirement.is_empty()).then_some(Value::Object(retirement));
                Ok(Reply::Next(Self::Readback(
                    r,
                    command_id.into(),
                    hash(v, "after")?,
                )))
            }
            Self::Readback(r, _, revision) => {
                let actual = Snapshot::parse(&v["state"], id, instance)?;
                if r.retirement.as_ref()
                    != (!retirement.is_empty()).then_some(&Value::Object(retirement))
                {
                    return Err(bad());
                }
                let before = r.selection.before;
                if actual.revision != revision
                    || actual.revision != hash(v, "after")?
                    || !actual.connected
                    || !actual.advertised
                    || actual.endpoint != before.endpoint
                    || actual.lifetime != before.lifetime
                    || actual.target != before.target
                    || actual.selected
                        != if r.selection.action == "load" {
                            before.target.clone()
                        } else {
                            before.model.clone()
                        }
                    || actual.choice != before.choice
                    || actual.load_context != before.load_context
                    || actual.other_load != before.other_load
                    || r.selection.action == "load"
                        && before.backend == "lmstudio"
                        && before.load_context.is_some()
                        && actual.context != before.load_context
                    || r.selection.action == "load" && actual.loaded != Some(true)
                    || r.selection.action == "unload" && actual.loaded != Some(false)
                {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Settled(actual)))
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
    review: Option<Review>,
    note: String,
    scroll: usize,
    height: usize,
    lines: usize,
    context_draft: Option<String>,
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
        match result {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None;
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Settled(s)) => {
                self.data = Some(s);
                self.review = None;
                self.note = "Operation and connection verified by independent readback.".into();
            }
            Err(e) => {
                self.data = None;
                self.review = None;
                self.note = crate::live::terminal_text(&e);
            }
        }
    }
    pub(crate) fn review_action(&mut self, action: &str, model: &str) -> Option<Request> {
        if !self.open || self.waiting || self.review.is_some() {
            return None;
        }
        let before = self.data.clone()?;
        if !model.is_empty() && model != before.target {
            self.note = format!(
                "Requested model {model} is no longer selected; resolve the command again."
            );
            return None;
        }
        if let Some(reason) = before.actions.get(action)? {
            self.note = reason.clone();
            return None;
        }
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(Request::Review(Selection {
            before,
            action: action.into(),
        }))
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        if let Some(draft) = self.context_draft.as_mut() {
            match key.code {
                KeyCode::Esc => {
                    self.context_draft = None;
                }
                KeyCode::Char(c) if c.is_ascii_digit() && draft.len() < 10 => draft.push(c),
                KeyCode::Backspace => {
                    draft.pop();
                }
                KeyCode::Enter => {
                    let Some(value) = draft
                        .parse::<u64>()
                        .ok()
                        .filter(|n| (1..=2_147_483_647).contains(n))
                    else {
                        self.note =
                            "Context must be a positive whole number up to 2147483647.".into();
                        return None;
                    };
                    let target = self.data.as_ref()?.target.clone();
                    self.context_draft = None;
                    self.waiting = true;
                    self.hide();
                    return Some(Request::Inspect(target, ContextChoice::Value(value)));
                }
                _ => {}
            }
            self.hide();
            return None;
        }
        if key.code == KeyCode::Esc {
            if self.review.take().is_some() {
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
        let request = match key.code {
            KeyCode::F(5) if self.review.is_none() => {
                self.data.as_ref().map_or(Request::List, |s| {
                    Request::Inspect(s.target.clone(), s.choice.clone())
                })
            }
            KeyCode::Up | KeyCode::Down if self.visible && self.review.is_none() => {
                let s = self.data.as_ref()?;
                let index = s.models.iter().position(|x| x == &s.target).unwrap_or(0);
                let next = if key.code == KeyCode::Up {
                    index.saturating_sub(1)
                } else {
                    (index + 1).min(s.models.len().saturating_sub(1))
                };
                Request::Inspect(s.models.get(next)?.clone(), s.choice.clone())
            }
            KeyCode::Char('h' | 'd') if self.visible && self.review.is_none() => Request::Inspect(
                self.data.as_ref()?.target.clone(),
                if key.code == KeyCode::Char('h') {
                    ContextChoice::Inherit
                } else {
                    ContextChoice::Default
                },
            ),
            KeyCode::Char('c') if self.visible && self.review.is_none() => {
                self.context_draft = Some(
                    self.data
                        .as_ref()?
                        .load_context
                        .map(|n| n.to_string())
                        .unwrap_or_default(),
                );
                self.hide();
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Char(c)
                if self.visible && self.review.is_none() && ['l', 'u', 'r'].contains(&c) =>
            {
                let action = match c {
                    'l' => "load",
                    'u' => "unload",
                    _ => "reconnect",
                };
                let before = self.data.clone()?;
                if let Some(reason) = before.actions.get(action)? {
                    self.note = reason.clone();
                    return None;
                }
                if before.model != before.selected {
                    Request::RefreshReview(before.target, before.choice, action.into())
                } else {
                    Request::Review(Selection {
                        before,
                        action: action.into(),
                    })
                }
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(request)
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Model lifecycle".to_owned()];
        if let Some(s) = &self.data {
            lines.push(format!("Target: {} · {}", s.target, s.backend));
            lines.push(format!(
                "Selected model: {} · {} advertised targets",
                s.selected,
                s.models.len()
            ));
            lines.push(format!(
                "Endpoint: {}",
                s.endpoint.as_deref().unwrap_or("provider owned")
            ));
            lines.push(format!(
                "Runtime lifetime: {} · Connection: {}",
                s.lifetime,
                if s.connected { "ready" } else { "not ready" }
            ));
            lines.push(format!(
                "Reported loaded: {} · Context: {} ({})",
                s.loaded
                    .map(|b| if b { "yes" } else { "no" })
                    .unwrap_or("unknown"),
                s.context
                    .map(|n| n.to_string())
                    .unwrap_or_else(|| "unknown".into()),
                if s.backend == "custom" {
                    "client declared"
                } else {
                    "backend reported"
                }
            ));
            lines.push(format!(
                "One-use load context: {} · Other load fields: {}",
                s.load_context
                    .map(|n| n.to_string())
                    .unwrap_or_else(|| "server default".into()),
                s.other_load
            ));
            lines.push(format!(
                "Context choice: {} · Inherited default: {:?} · Saved local preset (not applied): {:?}",
                match s.choice { ContextChoice::Inherit => "inherit", ContextChoice::Default => "server default/preset", ContextChoice::Value(_) => "one-use value" }, s.inherited, s.preset
            ));
        }
        if let Some(r) = &self.review {
            lines.push(format!(
                "Review {} for the model and endpoint above.",
                r.selection.action
            ));
            if r.selection.action == "load" {
                lines.push(
                    "After success, the loaded target becomes this conversation's selected model."
                        .into(),
                );
                lines
                    .push("Loads model weights and may evict models used by other clients.".into());
                lines.push(if r.selection.before.backend=="lmstudio"{"Uses the reviewed one-use context; adopts this model and records the actual load in this conversation."}
                    else{"Uses the server's existing model preset; other saved load fields are not applied."}.into());
            } else if r.selection.action == "unload" {
                lines.push("Unloads this target for every shared client; selection and invocation stay unchanged.".into());
            } else {
                lines.push(
                    "Refreshes the current client/backend binding at this exact endpoint.".into(),
                );
                lines.push("Deferred endpoint/settings changes are not applied here.".into());
            }
            lines.push(
                "The server process remains externally owned. Partial effects are not rolled back."
                    .into(),
            );
            lines.push("Enter apply once · Esc cancel".into());
        } else if let Some(s) = &self.data {
            for a in ACTIONS {
                lines.push(format!(
                    "{}: {}",
                    a,
                    s.actions[a].as_deref().unwrap_or("available")
                ));
            }
            lines.push(
                "Up/Down target · H inherit · D server default/preset · C context value".into(),
            );
            lines.push("L load · U unload · R reconnect · F5 refresh · Esc back".into());
        }
        if let Some(draft) = &self.context_draft {
            lines.push(format!(
                "Context tokens: {draft} · Enter inspect · Esc discard"
            ));
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone());
        }
        if self.waiting {
            lines.push("Waiting for the source; in-flight work must settle…".into());
        }
        if self.data.is_none() && !self.waiting {
            lines.push("F5 refresh · Esc back".into());
        }
        let safe = lines
            .iter()
            .map(|s| crate::ui::display_safe(s))
            .collect::<Vec<_>>()
            .join(
                "
",
            );
        let wrapped = crate::ui::wrap(&safe, usize::from(area.width.saturating_sub(2)));
        self.height = usize::from(area.height.saturating_sub(2));
        self.lines = wrapped.len();
        self.scroll = self.scroll.min(self.lines.saturating_sub(self.height));
        self.visible = area.width >= 36
            && self.height >= 5
            && (self.review.is_none() || self.lines <= self.height);
        let shown = if !self.visible {
            "Resize to see the full review. Esc back.".into()
        } else {
            wrapped
                .iter()
                .skip(self.scroll)
                .take(self.height)
                .cloned()
                .collect::<Vec<_>>()
                .join(
                    "
",
                )
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(shown)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Model lifecycle ")),
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
            backend: "lmstudio".into(),
            model: "chosen".into(),
        };
        let mut v = id.request();
        v.as_object_mut().unwrap().extend(json!({"kind":"model_lifecycle","work_instance":"a".repeat(64),
            "revision":"b".repeat(64),"endpoint":"http://127.0.0.1:54321/v1","lifetime":"external",
            "advertised":true,"reported_loaded":false,"context_length":8192,"context_source":"backend_reported",
            "target":"chosen","selected_model":"chosen","models":["chosen","other"],"context_mode":"inherit","context_value":null,"inherited_context":4096,"preset_context":null,
            "load_context":4096,"other_load_fields":0,"connected":true,
            "actions":{"load":{"eligible":true,"reason":null},"unload":{"eligible":false,"reason":"Not loaded"},
                "reconnect":{"eligible":true,"reason":null}}}).as_object().unwrap().clone());
        (id, v)
    }
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    #[test]
    fn eligibility_and_loaded_observations_are_strict() {
        let (id, v) = fixture();
        assert!(Snapshot::parse(&v, &id, &"a".repeat(64)).is_ok());
        for (field, value) in [
            ("reported_loaded", json!(true)),
            ("endpoint", json!("http://user:secret@host/v1")),
            ("lifetime", json!("owned")),
            ("unexpected", json!(true)),
        ] {
            let mut bad = v.clone();
            bad[field] = value;
            assert!(Snapshot::parse(&bad, &id, &"a".repeat(64)).is_err());
        }
    }
    #[test]
    fn applied_is_only_followed_by_exact_independent_settlement() {
        let (id, v) = fixture();
        let before = Snapshot::parse(&v, &id, &"a".repeat(64)).unwrap();
        let review = Review {
            selection: Selection {
                before,
                action: "load".into(),
            },
            token: "c".repeat(64),
            retirement: None,
        };
        let mut applied = id.request();
        applied.as_object_mut().unwrap().extend(
            json!({"kind":"model_lifecycle_applied","work_instance":"a".repeat(64),
            "token":"c".repeat(64),"action":"load","before":"b".repeat(64),"after":"d".repeat(64),
            "apply_id":"command","target":"chosen"})
            .as_object()
            .unwrap()
            .clone(),
        );
        let Reply::Next(next) = Request::Apply(review)
            .response(&applied, &id, &"a".repeat(64), "command")
            .unwrap()
        else {
            panic!("applied is not settled")
        };
        let mut state = v.clone();
        state["revision"] = json!("d".repeat(64));
        state["reported_loaded"] = json!(true);
        state["context_length"] = json!(4096);
        state["actions"]["load"] = json!({"eligible":false,"reason":"Already loaded"});
        state["actions"]["unload"] = json!({"eligible":true,"reason":null});
        applied["kind"] = json!("model_lifecycle_settled");
        applied["state"] = state;
        assert!(matches!(
            next.clone()
                .response(&applied, &id, &"a".repeat(64), "read"),
            Ok(Reply::Update(Update::Settled(_)))
        ));
        applied["state"]["endpoint"] = json!("http://127.0.0.1:54322/v1");
        assert!(
            next.response(&applied, &id, &"a".repeat(64), "read")
                .is_err()
        );
    }
    #[test]
    fn full_review_must_be_visible_and_cancel_consumes_no_action() {
        let (id, v) = fixture();
        let before = Snapshot::parse(&v, &id, &"a".repeat(64)).unwrap();
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(before.clone())));
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(matches!(
            view.key(key(KeyCode::Char('l'))),
            Some(Request::Review(_))
        ));
        view.update(Ok(Update::Review(Review {
            selection: Selection {
                before,
                action: "load".into(),
            },
            token: "c".repeat(64),
            retirement: None,
        })));
        assert!(view.key(key(KeyCode::Enter)).is_none());
        let mut small = Terminal::new(TestBackend::new(40, 8)).unwrap();
        small
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(view.key(key(KeyCode::Enter)).is_none());
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(view.key(key(KeyCode::Esc)).is_none());
        assert!(view.open && view.review.is_none());
        assert!(view.key(key(KeyCode::Enter)).is_none());
    }
    #[test]
    fn target_and_one_use_context_fields_are_strict() {
        let (id, v) = fixture();
        for (field, value) in [
            ("context_mode", json!("default")),
            ("context_value", json!(true)),
            ("target", json!("missing")),
            ("models", json!(["chosen", "chosen"])),
            ("selected_model", json!("")),
        ] {
            let mut changed = v.clone();
            changed[field] = value;
            assert!(
                Snapshot::parse(&changed, &id, &"a".repeat(64)).is_err(),
                "{field}"
            );
        }
        let choice = Request::Inspect("other".into(), ContextChoice::Default)
            .command(&id, &"a".repeat(64))
            .unwrap();
        assert_eq!(choice["context_mode"], "default");
        assert!(choice["context_value"].is_null());
        assert_eq!(choice["target"], "other");
    }

    #[test]
    fn next_action_after_target_adoption_reads_a_fresh_epoch_before_review() {
        let (id, mut v) = fixture();
        v["target"] = json!("other");
        v["selected_model"] = json!("other");
        v["reported_loaded"] = json!(true);
        v["actions"]["load"] = json!({"eligible":false,"reason":"Loaded"});
        v["actions"]["unload"] = json!({"eligible":true,"reason":null});
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::Settled(
            Snapshot::parse(&v, &id, &"a".repeat(64)).unwrap(),
        )));
        let mut terminal = Terminal::new(TestBackend::new(130, 45)).unwrap();
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        let request = view.key(key(KeyCode::Char('u'))).unwrap();
        assert!(matches!(request, Request::RefreshReview(..)));
        let mut current = id;
        current.model = "other".into();
        v["model"] = json!("other");
        v["work_instance"] = json!("e".repeat(64));
        let Reply::Next(Request::Review(selection)) = request
            .response(&v, &current, &"e".repeat(64), "read")
            .unwrap()
        else {
            panic!("fresh read must lead only to review");
        };
        assert_eq!(selection.before.model, "other");
        assert_eq!(selection.action, "unload");
    }
}
