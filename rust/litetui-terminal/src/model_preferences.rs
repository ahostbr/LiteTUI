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
const FIELDS: [&str; 3] = ["temperature", "top_p", "max_tokens"];

fn bad() -> io::Error {
    io::Error::other("Model preference response differs from the reviewed conversation")
}
fn value_ok(key: &str, v: &Value) -> bool {
    v.is_null()
        || if key == "max_tokens" {
            v.as_u64().is_some_and(|n| (1..=2_147_483_647).contains(&n))
        } else {
            v.as_f64().is_some_and(f64::is_finite)
        }
}
fn show(v: &Value) -> String {
    if v.is_null() {
        "server default".into()
    } else {
        v.to_string()
    }
}
fn label(key: &str) -> &'static str {
    match key {
        "temperature" => "Temperature",
        "top_p" => "Top P",
        _ => "Response token limit",
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
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    revision: String,
    fields: BTreeMap<String, Field>,
    supported: bool,
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
            ],
        )?;
        keys(&v["fields"], &FIELDS)?;
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
                ],
            )?;
            let present = r["present"].as_bool().ok_or_else(bad)?;
            let active_present = r["active_present"].as_bool().ok_or_else(bad)?;
            let editable = r["editable"].as_bool().ok_or_else(bad)?;
            let overridden = r["overridden"].as_bool().ok_or_else(bad)?;
            if ["saved", "active", "next_request", "inherited"]
                .iter()
                .any(|f| !value_ok(key, &r[*f]))
                || !present && !r["saved"].is_null()
                || !active_present && !r["active"].is_null()
                || editable
                    && (!supported
                        || overridden
                        || present != active_present
                        || r["saved"] != r["active"])
                || overridden && key != "max_tokens"
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
                },
            );
        }
        Ok(Self {
            revision: hash(v, "revision")?,
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
                        && (e.value.is_null()
                            || !value_ok(&e.field, &e.value)
                            || row.present && row.saved == e.value)
                {
                    return Err(bad());
                }
                c["revision"] = json!(e.before.revision);
                c["field"] = json!(e.field);
                c["mode"] = json!(if e.inherit { "inherit" } else { "value" });
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
            || v["mode"] != if edit.inherit { "inherit" } else { "value" }
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
                row.active_present = row.present;
                row.active = row.saved.clone();
                row.next = if r.edit.inherit {
                    row.inherited.clone()
                } else {
                    r.edit.value
                };
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
                self.draft = None;
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Saved(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
                self.note = "Saved and verified for the next request.".into();
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
                KeyCode::Char(c) if c.is_ascii_digit() || ".+-eE".contains(c) => {
                    if text.len() < 48 {
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
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Char('i') | KeyCode::Char('e') if self.review.is_none() => {
                let row = self.data.as_ref()?.fields.get(field)?;
                if row.editable {
                    self.draft = Some(String::new());
                    self.note = if key.code == KeyCode::Char('i') {
                        "Inherit this conversation's default. Enter reviews.".into()
                    } else {
                        "Type a number. Empty input inherits the conversation default.".into()
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
                let value = if text.is_empty() {
                    Value::Null
                } else {
                    match serde_json::from_str::<Value>(text) {
                        Ok(v) if !v.is_null() && value_ok(field, &v) => v,
                        _ => {
                            self.note = "Enter a valid finite number; the token limit requires a positive integer.".into();
                            return None;
                        }
                    }
                };
                let before = self.data.clone()?;
                let row = &before.fields[field];
                if !row.editable
                    || text.is_empty() && !row.present
                    || !text.is_empty() && row.present && row.saved == value
                {
                    self.note = "Choose a different value.".into();
                    return None;
                }
                Request::Review(Edit {
                    before,
                    field: field.into(),
                    value,
                    inherit: text.is_empty(),
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
                format!("Save {} for {}?", label(&r.edit.field), r.edit.before.model),
                format!("Current next request: {}", show(&row.next)),
                format!(
                    "Requested next request: {}",
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
                "Enter save · Esc cancel".into(),
            ]);
        } else if let Some(s) = &self.data {
            if let Some(reason) = &s.reason {
                lines.push(reason.clone());
            }
            for (i, key) in FIELDS.iter().enumerate() {
                let r = &s.fields[*key];
                lines.push(format!(
                    "{} {} · saved {} · next request {}{}",
                    if i == self.selected { ">" } else { " " },
                    label(key),
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
            lines.push(format!("Other saved load fields: {}.", s.deferred_load));
            if let Some(draft) = &self.draft {
                lines.push(format!(
                    "{}: {}",
                    label(FIELDS[self.selected]),
                    if draft.is_empty() { "inherit" } else { draft }
                ));
            }
            lines.push(
                "↑/↓ field · E type value · I inherit · Enter review · F5 refresh · Esc back"
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
                "active":null,"next_request":default,"inherited":default,"editable":true,"overridden":false});
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
            (
                "mode",
                json!(if r.edit.inherit { "inherit" } else { "value" }),
            ),
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
}
