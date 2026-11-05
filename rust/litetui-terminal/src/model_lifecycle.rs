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
        Ok(Self {
            revision: hash(v, "revision")?,
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
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    before: Snapshot,
    action: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    selection: Selection,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
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
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut c = id.request();
        c["work_instance"] = json!(instance);
        c["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Review(s) => {
                if !matches!(s.before.actions.get(&s.action), Some(None)) {
                    return Err(bad());
                }
                c["revision"] = json!(s.before.revision);
                c["action"] = json!(s.action);
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
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance,
                )?)));
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
            &["token", "action", "before", "after", "apply_id"],
        ]
        .concat();
        if matches!(self, Self::Readback(..)) {
            fields.push("state");
        }
        keys(v, &fields)?;
        let actual_token = hash(v, "token")?;
        if v["action"] != s.action
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
            }))),
            Self::Apply(r) => Ok(Reply::Next(Self::Readback(
                r,
                command_id.into(),
                hash(v, "after")?,
            ))),
            Self::Readback(r, _, revision) => {
                let actual = Snapshot::parse(&v["state"], id, instance)?;
                let before = r.selection.before;
                if actual.revision != revision
                    || actual.revision != hash(v, "after")?
                    || !actual.connected
                    || !actual.advertised
                    || actual.endpoint != before.endpoint
                    || actual.lifetime != before.lifetime
                    || actual.load_context != before.load_context
                    || actual.other_load != before.other_load
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
        if !model.is_empty() && model != before.model {
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
            KeyCode::F(5) if self.review.is_none() => Request::List,
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
                Request::Review(Selection {
                    before,
                    action: action.into(),
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
        let mut lines = vec!["Current model lifecycle".to_owned()];
        if let Some(s) = &self.data {
            lines.push(format!("Requested model: {} · {}", s.model, s.backend));
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
                "Saved load context: {} · Other load fields: {}",
                s.load_context
                    .map(|n| n.to_string())
                    .unwrap_or_else(|| "server default".into()),
                s.other_load
            ));
        }
        if let Some(r) = &self.review {
            lines.push(format!(
                "Review {} for the model and endpoint above.",
                r.selection.action
            ));
            if r.selection.action == "load" {
                lines
                    .push("Loads model weights and may evict models used by other clients.".into());
                lines.push(if r.selection.before.backend=="lmstudio"{"Uses saved context length; records the observed load choice in this conversation."}
                    else{"Uses the server's existing model preset; other saved load fields are not applied."}.into());
            } else if r.selection.action == "unload" {
                lines.push("Unloads this model for every client sharing this server.".into());
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
            lines.push("L load · U unload · R reconnect · F5 refresh · Esc back".into());
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
        };
        let mut applied = id.request();
        applied.as_object_mut().unwrap().extend(
            json!({"kind":"model_lifecycle_applied","work_instance":"a".repeat(64),
            "token":"c".repeat(64),"action":"load","before":"b".repeat(64),"after":"d".repeat(64),
            "apply_id":"command"})
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
}
