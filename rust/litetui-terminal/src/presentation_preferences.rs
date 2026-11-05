//! Review and verify device presentation preferences (transcript, dialogs, footer).
//! Source settings own every value; this view drafts one change, shows the exact
//! before and after, and publishes only what the source read back from disk.
use crate::{
    presentation::{FIELDS, FOOTER_ORDER_DEFAULT, Presentation, valid_order},
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
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, io};

pub const OPERATIONS: [&str; 4] = [
    "gui.presentation.native_preferences",
    "gui.presentation.native_review",
    "gui.presentation.native_apply",
    "gui.presentation.native_readback",
];
/// Shown for inspection, never offered: this frontend has no machine-meter surface.
const READ_ONLY: [&str; 1] = ["footer_task_manager"];
const GROUPS: [(&str, usize); 3] = [("Transcript", 0), ("Dialogs", 5), ("Footer", 7)];

fn bad() -> io::Error {
    io::Error::other("Presentation preference response differs from its reviewed device settings")
}
fn is_flag(key: &str) -> bool {
    !matches!(
        key,
        "error_message_style" | "dialog_style" | "dialog_side" | "footer_order"
    )
}
fn offered(key: &str) -> bool {
    !READ_ONLY.contains(&key)
}
fn choices(key: &str) -> Vec<Value> {
    match key {
        "error_message_style" => vec![json!("plain"), json!("detail")],
        "dialog_style" => vec![json!("modal"), json!("sidebar")],
        "dialog_side" => vec![json!("right"), json!("left")],
        _ if is_flag(key) => vec![json!(false), json!(true)],
        _ => vec![],
    }
}
fn label(key: &str) -> &'static str {
    match key {
        "show_thinking" => "Show thinking blocks",
        "show_stop_line" => "Show turn stop line",
        "show_stop_time" => "Show local completion time",
        "autoscroll" => "Follow newest output",
        "error_message_style" => "Error messages",
        "dialog_style" => "Dialog style",
        "dialog_side" => "Sidebar side",
        "footer_show_seat" => "Agent name",
        "footer_show_thinking" => "Thinking level",
        "footer_show_bg" => "Background processes",
        "footer_show_subagents" => "Subagents",
        "footer_show_convo" => "Conversation id",
        "footer_show_context" => "Context used / max",
        "footer_show_context_pct" => "Context percent",
        "footer_show_tps" => "Tokens per second",
        "footer_show_cache" => "Prompt cache",
        "footer_task_manager" => "Machine meters",
        "footer_show_key_hints" => "Keyboard hints",
        "footer_order" => "Footer order",
        _ => "Presentation preference",
    }
}
fn display(key: &str, v: &Value) -> String {
    match (key, v) {
        (_, Value::Bool(true)) => "on".into(),
        (_, Value::Bool(false)) => "off".into(),
        ("error_message_style", Value::String(s)) => match s.as_str() {
            "plain" => "Plain Talk".into(),
            _ => "Full Detail".into(),
        },
        (_, Value::String(s)) => s.clone(),
        (_, Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(" "),
        _ => v.to_string(),
    }
}
fn verify_binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
        || v["scope"] != "device"
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
    saved: Value,
    effective: Value,
    editable: bool,
    overridden: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    fields: BTreeMap<String, Field>,
    revision: String,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        verify_binding(v, id, instance, "presentation_preferences")?;
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
            ],
        )?;
        keys(&v["fields"], &FIELDS)?;
        let mut fields = BTreeMap::new();
        for key in FIELDS {
            let raw = &v["fields"][key];
            keys(raw, &["saved", "effective", "editable", "overridden"])?;
            let saved = raw["saved"].clone();
            let effective = raw["effective"].clone();
            for value in [&saved, &effective] {
                let valid = match (key, value) {
                    ("footer_order", Value::Array(items)) => valid_order(items),
                    ("footer_order", _) => false,
                    (_, Value::Bool(_)) if is_flag(key) => true,
                    (_, value) => choices(key).contains(value),
                };
                if !valid {
                    return Err(bad());
                }
            }
            let editable = raw["editable"].as_bool().ok_or_else(bad)?;
            let overridden = raw["overridden"].as_bool().ok_or_else(bad)?;
            if editable != (offered(key) && !overridden && saved == effective) {
                return Err(bad());
            }
            fields.insert(
                key.into(),
                Field {
                    saved,
                    effective,
                    editable,
                    overridden,
                },
            );
        }
        Ok(Self {
            fields,
            revision: hash(v, "revision")?,
        })
    }
    /// What the renderer should honor right now.
    pub fn effective(&self) -> Presentation {
        let map: Map<String, Value> = self
            .fields
            .iter()
            .map(|(key, field)| (key.clone(), field.effective.clone()))
            .collect();
        Presentation::from_effective(&map).expect("validated presentation snapshot")
    }
    fn shown(&self, key: &str) -> String {
        let field = &self.fields[key];
        let mut s = display(key, &field.effective);
        if field.saved != field.effective {
            s.push_str(&format!(" (saved {})", display(key, &field.saved)));
        }
        if field.overridden {
            s.push_str(" (override)");
        }
        if !offered(key) {
            s.push_str(" (not shown in this frontend)");
        }
        s
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
impl Update {
    /// The effective preferences the renderer should adopt after this update.
    pub fn effective(&self) -> Option<Presentation> {
        match self {
            Self::List(snapshot) | Self::Saved(snapshot) => Some(snapshot.effective()),
            Self::Review(_) => None,
        }
    }
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
                let r = e
                    .before
                    .fields
                    .get(&e.key)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                let allowed = match &e.value {
                    Value::Array(items) => e.key == "footer_order" && valid_order(items),
                    value => choices(&e.key).contains(value),
                };
                if r.effective == e.value || !allowed {
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
            Self::Readback(r, id, _) => {
                cmd["token"] = json!(r.token);
                cmd["apply_id"] = json!(id);
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
            Self::Review(e) => ("presentation_review", e, None, None),
            Self::Apply(r) => (
                "presentation_applied",
                &r.edit,
                Some(&r.token),
                Some(cmd_id),
            ),
            Self::Readback(r, apply, _) => (
                "presentation_saved",
                &r.edit,
                Some(&r.token),
                Some(apply.as_str()),
            ),
        };
        verify_binding(v, id, instance, kind)?;
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
            "before",
            "after",
            "apply_id",
        ];
        if kind == "presentation_saved" {
            members.push("preferences")
        }
        keys(v, &members)?;
        let actual = hash(v, "token")?;
        if v["field"] != e.key
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
                field.saved = edit.value.clone();
                field.effective = edit.value;
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
    draft: Option<Value>,
    /// Footer-order item under the cursor while a new order is being drafted.
    cursor: usize,
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
        }
    }
    /// Open with the cursor on one named field (the `Alt+S` scroll-lock shortcut).
    pub fn open_field(&mut self, key: &str) {
        self.open();
        self.selected = FIELDS.iter().position(|k| *k == key).unwrap_or(0);
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn update(&mut self, r: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.note.clear();
        match r {
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
                self.note = "Saved and verified on disk.".into();
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.draft = None;
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    fn draft_order(&self, row: &Field) -> Vec<Value> {
        match self.draft.as_ref().unwrap_or(&row.effective) {
            Value::Array(items) => items.clone(),
            _ => FOOTER_ORDER_DEFAULT.iter().map(|id| json!(id)).collect(),
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        if key.code == KeyCode::Esc {
            if self.review.take().is_some() || self.draft.take().is_some() {
                self.hide()
            } else {
                *self = Self::default()
            }
            return None;
        }
        let field = FIELDS[self.selected];
        let req = match key.code {
            KeyCode::PageDown | KeyCode::PageUp => {
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
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                self.selected = if key.code == KeyCode::Up {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(FIELDS.len() - 1)
                };
                self.draft = None;
                self.cursor = 0;
                self.hide();
                return None;
            }
            KeyCode::Left | KeyCode::Right if self.review.is_none() => {
                let row = self.data.as_ref()?.fields.get(field)?.clone();
                if !row.editable {
                    return None;
                }
                if field == "footer_order" {
                    let step = FOOTER_ORDER_DEFAULT.len();
                    self.cursor = if key.code == KeyCode::Right {
                        (self.cursor + 1) % step
                    } else {
                        (self.cursor + step - 1) % step
                    };
                    self.hide();
                    return None;
                }
                let options = choices(field);
                let old = self.draft.as_ref().unwrap_or(&row.effective);
                let pos = options.iter().position(|v| v == old)?;
                let step = if key.code == KeyCode::Right {
                    1
                } else {
                    options.len() - 1
                };
                self.draft = Some(options[(pos + step) % options.len()].clone());
                self.hide();
                return None;
            }
            KeyCode::Char(c @ ('[' | ']')) if self.review.is_none() && field == "footer_order" => {
                let row = self.data.as_ref()?.fields.get(field)?.clone();
                if !row.editable {
                    return None;
                }
                let mut order = self.draft_order(&row);
                let target = if c == ']' {
                    self.cursor + 1
                } else {
                    self.cursor.checked_sub(1)?
                };
                if target >= order.len() {
                    return None;
                }
                order.swap(self.cursor, target);
                self.cursor = target;
                self.draft = (order != *row.effective.as_array().ok_or(()).ok()?)
                    .then_some(Value::Array(order));
                self.hide();
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Enter if self.visible => {
                let before = self.data.clone()?;
                let value = self.draft.clone()?;
                if value == before.fields[field].effective {
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
    fn row_text(&self, s: &Snapshot, key: &str, selected: bool) -> String {
        let marker = if selected { ">" } else { " " };
        let shown = match (&self.draft, selected, key) {
            (Some(Value::Array(items)), true, "footer_order") => {
                let rendered: Vec<String> = items
                    .iter()
                    .filter_map(Value::as_str)
                    .enumerate()
                    .map(|(i, id)| {
                        if i == self.cursor {
                            format!("[{id}]")
                        } else {
                            id.to_owned()
                        }
                    })
                    .collect();
                format!("{} (draft)", rendered.join(" "))
            }
            (Some(value), true, _) => format!("{} (draft)", display(key, value)),
            (None, true, "footer_order") if s.fields[key].editable => {
                let rendered: Vec<String> = s.fields[key]
                    .effective
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .enumerate()
                    .map(|(i, id)| {
                        if i == self.cursor {
                            format!("[{id}]")
                        } else {
                            id.to_owned()
                        }
                    })
                    .collect();
                rendered.join(" ")
            }
            _ => s.shown(key),
        };
        format!("{marker} {}: {shown}", label(key))
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Interface preferences on this device".to_owned()];
        let mut selected_line = 0;
        let width = usize::from(area.width.saturating_sub(2));
        if let Some(review) = &self.review {
            lines.extend([
                format!("Save {}?", label(&review.edit.key)),
                format!(
                    "Current: {}",
                    review.edit.before.shown(&review.edit.key)
                ),
                format!(
                    "Requested: {}",
                    display(&review.edit.key, &review.edit.value)
                ),
                "Device setting. It applies immediately and is shared with other LiteTUI hosts on this device.".into(),
                "Enter save   Esc cancel".into(),
            ]);
        } else if let Some(s) = self.data.clone() {
            for (index, key) in FIELDS.iter().enumerate() {
                if let Some((title, _)) = GROUPS.iter().find(|(_, first)| *first == index) {
                    lines.push(format!("{title}:"));
                }
                if index == self.selected {
                    selected_line = lines.len();
                }
                lines.push(self.row_text(&s, key, index == self.selected));
            }
            let key = FIELDS[self.selected];
            lines.push(if key == "footer_order" && s.fields[key].editable {
                "↑/↓ field   ←/→ pick item   [ ] move item   Enter review   F5 refresh   Esc back"
            } else if s.fields[key].editable {
                "↑/↓ field   ←/→ choose   Enter review   F5 refresh   Esc back"
            } else {
                "Current field is read-only   ↑/↓ field   F5 refresh   Esc back"
            }
            .into());
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
        let mut wrapped = Vec::new();
        let mut selected_wrapped = 0;
        for (index, line) in lines.iter().enumerate() {
            if index == selected_line && self.review.is_none() && self.data.is_some() {
                selected_wrapped = wrapped.len();
            }
            wrapped.extend(crate::ui::wrap(line, width));
        }
        self.height = usize::from(area.height.saturating_sub(2));
        self.lines = wrapped.len();
        if self.review.is_none() && self.data.is_some() && self.height > 0 {
            if selected_wrapped < self.scroll {
                self.scroll = selected_wrapped;
            } else if selected_wrapped >= self.scroll + self.height {
                self.scroll = selected_wrapped + 1 - self.height;
            }
        }
        self.scroll = self.scroll.min(self.lines.saturating_sub(self.height));
        self.visible = area.width >= 36
            && self.height >= 5
            && (self.review.is_none() || self.lines <= self.height);
        let text = if area.width < 36 || self.height < 5 || self.review.is_some() && !self.visible {
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
            Paragraph::new(text)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Interface ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn fixture() -> (Identity, Value) {
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let mut root = id.request();
        for (key, value) in [
            ("kind", json!("presentation_preferences")),
            ("scope", json!("device")),
            ("work_instance", json!("i")),
            ("revision", json!("1".repeat(64))),
        ] {
            root[key] = value;
        }
        for key in FIELDS {
            let val = match key {
                "error_message_style" => json!("plain"),
                "dialog_style" => json!("modal"),
                "dialog_side" => json!("right"),
                "footer_order" => json!(FOOTER_ORDER_DEFAULT),
                "show_stop_time" | "footer_task_manager" | "footer_show_key_hints" => json!(false),
                _ => json!(true),
            };
            root["fields"][key] =
                json!({"effective":val,"saved":val,"editable":offered(key),"overridden":false});
        }
        (id, root)
    }
    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn painted(view: &mut View) -> String {
        use ratatui::{Terminal, backend::TestBackend};
        let theme = crate::theme::builtin_themes().remove(0);
        let mut terminal = Terminal::new(TestBackend::new(110, 34)).unwrap();
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

    #[test]
    fn snapshot_checks_binding_edit_eligibility_vocabulary_and_order() {
        let (id, doc) = fixture();
        let parsed = Snapshot::parse(&doc, &id, "i").unwrap();
        assert!(!parsed.fields["footer_task_manager"].editable);
        assert!(parsed.effective().show_thinking);
        for (path, value) in [
            ("/work_instance", json!("old")),
            ("/version", json!(2)),
            ("/fields/show_thinking/editable", json!(false)),
            ("/fields/footer_task_manager/editable", json!(true)),
            ("/fields/autoscroll/saved", json!(1)),
            ("/fields/dialog_style/effective", json!("popup")),
            ("/fields/footer_order/effective", json!(["pct"])),
        ] {
            let mut drift = doc.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{path}");
        }
    }

    #[test]
    fn choosing_a_value_requires_a_painted_review_and_does_not_touch_effective_data() {
        let (id, doc) = fixture();
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(Snapshot::parse(&doc, &id, "i").unwrap())));
        painted(&mut view);
        assert!(view.key(press(KeyCode::Right)).is_none());
        assert!(view.key(press(KeyCode::Enter)).is_none());
        painted(&mut view);
        let request = view.key(press(KeyCode::Enter)).unwrap();
        let command = request.command(&id, "i").unwrap();
        assert_eq!(command["type"], "gui.presentation.native_review");
        assert_eq!(command["field"], "show_thinking");
        assert_eq!(command["value"], false);
        assert_eq!(
            view.data.as_ref().unwrap().fields["show_thinking"].effective,
            true
        );
    }

    #[test]
    fn footer_order_is_drafted_by_moving_items_and_reviewed_as_a_full_order() {
        let (id, doc) = fixture();
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(Snapshot::parse(&doc, &id, "i").unwrap())));
        for _ in 0..FIELDS.len() {
            view.key(press(KeyCode::Down));
        }
        assert_eq!(FIELDS[view.selected], "footer_order");
        assert!(view.key(press(KeyCode::Char('['))).is_none());
        for _ in 0..7 {
            view.key(press(KeyCode::Right));
        }
        view.key(press(KeyCode::Char('[')));
        let screen = painted(&mut view);
        assert!(screen.contains("(draft)"), "{screen}");
        assert!(screen.contains("[tps] cache"), "{screen}");
        painted(&mut view);
        let request = view.key(press(KeyCode::Enter)).unwrap();
        let command = request.command(&id, "i").unwrap();
        assert_eq!(command["field"], "footer_order");
        assert_eq!(command["value"][6], "tps");
        assert_eq!(command["value"][7], "cache");
        assert_eq!(command["value"].as_array().unwrap().len(), 13);
        // Moving the item back restores the original order, so there is nothing to review.
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(Snapshot::parse(&doc, &id, "i").unwrap())));
        for _ in 0..FIELDS.len() {
            view.key(press(KeyCode::Down));
        }
        view.key(press(KeyCode::Right));
        view.key(press(KeyCode::Char(']')));
        assert!(view.draft.is_some());
        view.key(press(KeyCode::Char('[')));
        assert!(view.draft.is_none());
    }

    #[test]
    fn read_only_rows_cannot_be_drafted_and_the_list_scrolls_to_the_selection() {
        let (id, doc) = fixture();
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(Snapshot::parse(&doc, &id, "i").unwrap())));
        for _ in 0..16 {
            view.key(press(KeyCode::Down));
        }
        assert_eq!(FIELDS[view.selected], "footer_task_manager");
        let screen = painted(&mut view);
        assert!(screen.contains("not shown in this frontend"), "{screen}");
        assert!(view.key(press(KeyCode::Right)).is_none() && view.draft.is_none());
        assert!(view.key(press(KeyCode::Enter)).is_none());
        let mut small = View::default();
        small.open();
        small.update(Ok(Update::List(Snapshot::parse(&doc, &id, "i").unwrap())));
        for _ in 0..FIELDS.len() {
            small.key(press(KeyCode::Down));
        }
        use ratatui::{Terminal, backend::TestBackend};
        let theme = crate::theme::builtin_themes().remove(0);
        let mut terminal = Terminal::new(TestBackend::new(80, 12)).unwrap();
        terminal
            .draw(|frame| small.draw(frame, frame.area(), &theme))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("Footer order"), "{text}");
    }

    #[test]
    fn readback_rejects_substituted_setting_and_unrelated_effective_drift() {
        let (id, doc) = fixture();
        let before = Snapshot::parse(&doc, &id, "i").unwrap();
        let review = Review {
            edit: Edit {
                before,
                key: "dialog_side".into(),
                value: json!("left"),
            },
            token: "a".repeat(64),
        };
        let after = "b".repeat(64);
        let mut saved = doc.clone();
        saved["revision"] = json!(after);
        saved["fields"]["dialog_side"]["effective"] = json!("left");
        saved["fields"]["dialog_side"]["saved"] = json!("left");
        let mut response = id.request();
        for (key, val) in [
            ("kind", json!("presentation_saved")),
            ("scope", json!("device")),
            ("work_instance", json!("i")),
            ("token", json!(review.token)),
            ("field", json!("dialog_side")),
            ("value", json!("left")),
            ("before", json!(review.edit.before.revision)),
            ("after", json!(after)),
            ("apply_id", json!("apply")),
            ("preferences", saved),
        ] {
            response[key] = val;
        }
        let request = Request::Readback(review, "apply".into(), after);
        let saved = request.clone().response(&response, &id, "i", "readback");
        match saved {
            Ok(Reply::Update(update)) => {
                assert!(!update.effective().unwrap().show_stop_time);
                assert!(update.effective().unwrap().dialogs_left);
            }
            _ => panic!("expected verified readback"),
        }
        for (path, val) in [
            ("/field", json!("dialog_style")),
            ("/apply_id", json!("replay")),
            ("/preferences/fields/autoscroll/effective", json!(false)),
            (
                "/preferences/fields/footer_order/effective",
                json!(FOOTER_ORDER_DEFAULT.iter().rev().collect::<Vec<_>>()),
            ),
        ] {
            let mut drift = response.clone();
            *drift.pointer_mut(path).unwrap() = val;
            assert!(
                request
                    .clone()
                    .response(&drift, &id, "i", "readback")
                    .is_err(),
                "{path}"
            );
        }
    }
}
