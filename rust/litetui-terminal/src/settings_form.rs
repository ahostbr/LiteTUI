//! `/settings`: Python's own settings dialog, drawn natively.
//!
//! Python mounts the real Textual settings screen headless and sends its tabs and
//! rows as data (src/litetui/native_settings_form.py): section headers, labels,
//! inputs, switches, selects, help lines and buttons, in screen order. This view
//! draws them the way Textual does and keeps the edits as a local draft. Save
//! sends only the edited controls; Python sets them on a fresh copy of its screen
//! and presses its own Save, so parsing, validation, persistence and the messages
//! are Python's. Secrets never arrive here: a password input says only whether a
//! value is set.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
    widgets::Clear,
};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    io,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub const READ: &str = "gui.settings.native_form";
pub const SAVE: &str = "gui.settings.native_form_save";
pub const RESTORE: &str = "gui.settings.native_form_restore";
pub const MAX_BYTES: usize = 1024 * 1024;
const MAX_TEXT: usize = 64 * 1024;
const MAX_ITEMS: usize = 4096;

fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn string(v: &Value, key: &str) -> io::Result<String> {
    match &v[key] {
        Value::String(s) if s.len() <= MAX_TEXT => Ok(s.clone()),
        _ => Err(fail("Invalid settings form text")),
    }
}
fn optional(v: &Value, key: &str) -> io::Result<Option<String>> {
    match &v[key] {
        Value::Null => Ok(None),
        _ => string(v, key).map(Some),
    }
}
fn flag(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail("Invalid settings form flag"))
}
fn id(v: &Value) -> io::Result<String> {
    let id = string(v, "id")?;
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(fail("Invalid settings control id"));
    }
    Ok(id)
}
/// Everything shown comes from Python; never let it drive the terminal.
fn shown(text: &str) -> String {
    terminal_text(text).replace('\t', "    ")
}

#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    Input {
        value: Option<String>,
        set: bool,
        placeholder: String,
        password: bool,
    },
    Switch(bool),
    Select {
        value: String,
        options: Vec<(String, String)>,
    },
    TextArea(String),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Control {
    pub id: String,
    pub kind: Kind,
    pub disabled: bool,
    pub reason: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Item {
    Section {
        id: String,
        label: String,
        open: bool,
        /// Python's search text for the section, and for each of its fields by name.
        search: String,
        fields: Vec<(String, String)>,
    },
    Label {
        text: String,
        inline: bool,
    },
    Control(Control),
    Text {
        text: String,
        help: bool,
        subhead: bool,
    },
    Button {
        id: String,
        label: String,
        disabled: bool,
    },
    Line(Vec<Item>),
    Row {
        items: Vec<Item>,
        indent: bool,
    },
    HooksEditor,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Tab {
    pub id: String,
    pub label: String,
    pub items: Vec<Item>,
    /// Rows in a scroll region (padding and scrollbar columns), or the full width.
    pub scroll: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Button {
    pub id: String,
    pub label: String,
    pub disabled: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Form {
    pub token: String,
    pub title: String,
    pub subtitle: String,
    pub search_placeholder: String,
    pub search_status: String,
    pub tabs: Vec<Tab>,
    pub buttons: Vec<Button>,
}

fn item(v: &Value, depth: usize, count: &mut usize, ids: &mut HashSet<String>) -> io::Result<Item> {
    *count += 1;
    if *count > MAX_ITEMS || depth > 4 {
        return Err(fail("Settings form is too large"));
    }
    let children = |v: &Value, count: &mut usize, ids: &mut HashSet<String>| {
        v["items"]
            .as_array()
            .ok_or_else(|| fail("Invalid settings form group"))?
            .iter()
            .map(|child| item(child, depth + 1, count, ids))
            .collect::<io::Result<Vec<_>>>()
    };
    let control = |v: &Value, kind: Kind, ids: &mut HashSet<String>| -> io::Result<Item> {
        let id = id(v)?;
        if !ids.insert(id.clone()) {
            return Err(fail("Duplicate settings control id"));
        }
        Ok(Item::Control(Control {
            id,
            kind,
            disabled: flag(v, "disabled")?,
            reason: optional(v, "reason")?,
        }))
    };
    Ok(match v["kind"].as_str() {
        Some("section") => Item::Section {
            id: id(v)?,
            label: string(v, "label")?,
            open: flag(v, "open")?,
            search: string(v, "search")?,
            fields: v["fields"]
                .as_object()
                .filter(|f| f.len() <= 256)
                .ok_or_else(|| fail("Invalid settings section fields"))?
                .iter()
                .map(|(name, text)| match text {
                    Value::String(t) if t.len() <= MAX_TEXT => Ok((name.clone(), t.clone())),
                    _ => Err(fail("Invalid settings section field text")),
                })
                .collect::<io::Result<_>>()?,
        },
        Some("label") => Item::Label {
            text: string(v, "text")?,
            inline: flag(v, "inline")?,
        },
        Some("text") => {
            let classes = v["classes"]
                .as_array()
                .ok_or_else(|| fail("Invalid settings text classes"))?;
            let has = |name: &str| classes.iter().any(|c| c == name);
            Item::Text {
                text: string(v, "text")?,
                help: has("set-help"),
                subhead: has("set-subhead"),
            }
        }
        Some("button") => Item::Button {
            id: id(v)?,
            label: string(v, "label")?,
            disabled: flag(v, "disabled")?,
        },
        Some("line") => Item::Line(children(v, count, ids)?),
        Some("row") => Item::Row {
            items: children(v, count, ids)?,
            indent: flag(v, "indent")?,
        },
        Some("hooks_editor") => Item::HooksEditor,
        Some("switch") => control(v, Kind::Switch(flag(v, "value")?), ids)?,
        Some("textarea") => control(v, Kind::TextArea(string(v, "value")?), ids)?,
        Some("select") => {
            let options = v["options"]
                .as_array()
                .filter(|o| o.len() <= 1024)
                .ok_or_else(|| fail("Invalid settings select options"))?
                .iter()
                .map(|o| match o.as_array().map(Vec::as_slice) {
                    Some([Value::String(label), Value::String(value)])
                        if label.len() <= 4096 && value.len() <= 4096 =>
                    {
                        Ok((label.clone(), value.clone()))
                    }
                    _ => Err(fail("Invalid settings select option")),
                })
                .collect::<io::Result<Vec<_>>>()?;
            control(
                v,
                Kind::Select {
                    value: string(v, "value")?,
                    options,
                },
                ids,
            )?
        }
        Some("input") => {
            let password = flag(v, "password")?;
            let kind = Kind::Input {
                value: if password {
                    if v.get("value").is_some() {
                        return Err(fail("A secret value reached the native settings form"));
                    }
                    None
                } else {
                    Some(string(v, "value")?)
                },
                set: password && flag(v, "set")?,
                placeholder: string(v, "placeholder")?,
                password,
            };
            control(v, kind, ids)?
        }
        _ => return Err(fail("Unknown settings form row")),
    })
}
impl Form {
    /// Parse a `settings_form` / `settings_form_save` result bound to this session.
    pub fn parse(
        v: &Value,
        kind: &str,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        if serde_json::to_vec(v)?.len() > MAX_BYTES
            || v["version"] != 1
            || v["kind"] != kind
            || v["session_id"] != session
            || v["agent_id"] != agent
            || v["backend"] != backend
            || v["model"] != model
        {
            return Err(fail("Settings form identity, version or size changed"));
        }
        let token = string(v, "form")?;
        if token.len() != 64 || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(fail("Invalid settings form token"));
        }
        let mut count = 0;
        let mut ids = HashSet::new();
        let tabs = v["tabs"]
            .as_array()
            .filter(|t| !t.is_empty() && t.len() <= 32)
            .ok_or_else(|| fail("Invalid settings form tabs"))?
            .iter()
            .map(|tab| {
                Ok(Tab {
                    id: id(tab)?,
                    label: string(tab, "label")?,
                    scroll: flag(tab, "scroll")?,
                    items: tab["items"]
                        .as_array()
                        .ok_or_else(|| fail("Invalid settings form tab"))?
                        .iter()
                        .map(|i| item(i, 0, &mut count, &mut ids))
                        .collect::<io::Result<_>>()?,
                })
            })
            .collect::<io::Result<_>>()?;
        let buttons = v["buttons"]
            .as_array()
            .filter(|b| b.len() <= 16)
            .ok_or_else(|| fail("Invalid settings form buttons"))?
            .iter()
            .map(|b| {
                Ok(Button {
                    id: id(b)?,
                    label: string(b, "label")?,
                    disabled: flag(b, "disabled")?,
                })
            })
            .collect::<io::Result<_>>()?;
        Ok(Self {
            token,
            title: string(v, "title")?,
            subtitle: string(v, "subtitle")?,
            search_placeholder: string(v, "search_placeholder")?,
            search_status: string(v, "search_status")?,
            tabs,
            buttons,
        })
    }
    fn control(&self, id: &str) -> Option<&Control> {
        fn find<'a>(items: &'a [Item], id: &str) -> Option<&'a Control> {
            items.iter().find_map(|item| match item {
                Item::Control(c) if c.id == id => Some(c),
                Item::Line(items) | Item::Row { items, .. } => find(items, id),
                _ => None,
            })
        }
        self.tabs.iter().find_map(|tab| find(&tab.items, id))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Read,
    Save {
        form: String,
        values: Map<String, Value>,
    },
    /// Python's confirmed Restore defaults.
    Restore {
        form: String,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Form(Box<Form>),
    Saved {
        status: String,
        message: String,
        form: Box<Form>,
        /// Interface preferences read back after the save, so the chat redraws with them.
        presentation: Option<Box<crate::presentation::Presentation>>,
    },
    Rejected(String),
}
impl Update {
    /// A save result: Python's status and message, plus the form as it now stands.
    pub fn saved(
        v: &Value,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        let form = Form::parse(v, "settings_form_save", session, agent, backend, model)?;
        let status = string(v, "status")?;
        if !matches!(
            status.as_str(),
            "saved" | "partial" | "refused" | "conflict"
        ) {
            return Err(fail("Invalid settings save status"));
        }
        Ok(Self::Saved {
            status,
            message: string(v, "message")?,
            form: Box::new(form),
            presentation: None,
        })
    }
}

/// What a focusable row is.
#[derive(Clone, Debug, PartialEq)]
enum Target {
    Section(String),
    Control(String),
    Button(String),
    /// The Hooks tab's editor: opens the native hooks screen (/hooks).
    Hooks,
}
/// Python's focus order: the search box, Clear, the tab bar, the tab's rows, the buttons.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
enum Focus {
    #[default]
    Search,
    Clear,
    Tabs,
    Row(usize),
    Button(usize),
}
#[derive(Clone, Debug)]
enum Hit {
    Tab(usize),
    Row(usize),
    Button(usize),
    Option(usize),
    Confirm(usize),
    Search,
    Clear,
}
const CONFIRM: [&str; 3] = ["Save", "Discard", "Keep editing"];
const RESTORE_CONFIRM: [&str; 2] = ["Restore defaults", "Keep editing"];

#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    form: Option<Form>,
    tab: usize,
    scroll: usize,
    focus: Focus,
    edits: BTreeMap<String, Value>,
    sections: HashMap<String, bool>,
    cursor: usize,
    popup: Option<usize>,
    confirm: Option<usize>,
    /// The open confirmation is Python's restore one, not the unsaved-changes one.
    restoring: bool,
    /// Python's `#set-error` line: the last save's message.
    message: String,
    /// The search box (Python's #set-search) and its caret.
    query: String,
    query_cursor: usize,
    searched: bool,
    /// Keys pressed before the form arrived; Textual delivers these once its screen mounts.
    early: Vec<KeyEvent>,
    /// A screen the dialog hands over to (the hooks screen, the voice actions panel).
    route: Option<Route>,
    /// Capture key: the next key pressed becomes the record hotkey (Python's on_key).
    capturing: bool,
    intent: Option<Intent>,
    hits: Vec<(Rect, Hit)>,
    visible: bool,
}

impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        *self = Self {
            open: true,
            waiting: true,
            intent: Some(Intent::Read),
            notice: "Reading settings…".into(),
            ..Self::default()
        };
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.hits.clear();
    }
    pub fn close(&mut self) {
        let waiting = self.waiting && self.intent.is_none();
        *self = Self {
            waiting,
            ..Self::default()
        };
    }
    pub fn finish(&mut self) {
        *self = Self::default();
    }
    /// The screen the dialog handed over to, once.
    pub fn take_route(&mut self) -> Option<Route> {
        self.route.take()
    }
    /// Kept for the Hooks tab's callers.
    pub fn take_hooks(&mut self) -> bool {
        if self.route == Some(Route::Hooks) {
            self.route = None;
            return true;
        }
        false
    }
    fn hand_over(&mut self, route: Route, what: &str) {
        if self.dirty() {
            self.message = format!("Save or discard the settings draft before opening {what}.");
        } else {
            self.close();
            self.route = Some(route);
        }
    }
    pub fn dirty(&self) -> bool {
        !self.edits.is_empty()
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.intent = None;
        match update {
            Update::Form(form) => {
                if !self.open {
                    return;
                }
                self.tab = self.tab.min(form.tabs.len().saturating_sub(1));
                self.form = Some(*form);
                self.notice.clear();
                for key in std::mem::take(&mut self.early) {
                    self.key(key);
                }
            }
            Update::Saved {
                status,
                message,
                form,
                ..
            } => match status.as_str() {
                // A complete save closes the dialog, as Python's does.
                "saved" => {
                    self.close();
                    self.notice = message;
                }
                "refused" => {
                    // Nothing was saved: keep the draft and show Python's reason.
                    self.message = message;
                }
                "conflict" => {
                    let form = *form;
                    // Keep each edit whose control is still there and editable.
                    self.edits
                        .retain(|id, _| form.control(id).is_some_and(|c| !c.disabled));
                    self.form = Some(form);
                    self.message = message;
                }
                _ => {
                    self.edits.clear();
                    self.form = Some(*form);
                    self.message = message;
                }
            },
            Update::Rejected(reason) => {
                if self.form.is_none() {
                    self.close();
                    self.notice = reason;
                } else {
                    self.message = reason;
                }
            }
        }
        self.clamp_focus();
    }
    fn tab(&self) -> Option<&Tab> {
        self.form.as_ref().and_then(|f| f.tabs.get(self.tab))
    }
    fn section_open(&self, id: &str, default: bool) -> bool {
        self.sections.get(id).copied().unwrap_or(default)
    }
    fn terms(&self) -> Vec<String> {
        self.query
            .to_lowercase()
            .split_whitespace()
            .map(str::to_owned)
            .collect()
    }
    /// settings_ui_model.search_settings: a section matches when it holds every term; its
    /// matching fields are those whose own text holds every term.
    fn section_hit(&self, search: &str, fields: &[(String, String)]) -> Option<HashSet<String>> {
        let terms = self.terms();
        if terms.is_empty() || !terms.iter().all(|t| search.contains(t.as_str())) {
            return None;
        }
        Some(
            fields
                .iter()
                .filter(|(_, text)| terms.iter().all(|t| text.contains(t.as_str())))
                .map(|(name, _)| name.clone())
                .collect(),
        )
    }
    /// SettingsSectionHeader.apply_search: is this member of a matching section shown?
    fn member_shown(item: &Item, wanted: &HashSet<String>) -> bool {
        fn ids(item: &Item, out: &mut Vec<String>) {
            match item {
                Item::Control(c) => {
                    if let Some(name) = c.id.strip_prefix("f-") {
                        out.push(name.to_owned());
                    }
                }
                Item::Line(items) | Item::Row { items, .. } => {
                    items.iter().for_each(|i| ids(i, out))
                }
                _ => {}
            }
        }
        let mut found = Vec::new();
        ids(item, &mut found);
        wanted.is_empty() || found.is_empty() || found.iter().any(|f| wanted.contains(f))
    }
    /// Python's _apply_search, after each change to the search box.
    fn search(&mut self) {
        let Some(form) = self.form.clone() else {
            return;
        };
        self.searched = true;
        if self.terms().is_empty() {
            self.sections.clear();
            return;
        }
        // The first matching section, and its first matching field in the section's own order.
        let first = form.tabs.iter().enumerate().find_map(|(t, tab)| {
            tab.items.iter().find_map(|item| match item {
                Item::Section {
                    search, fields: f, ..
                } => self.section_hit(search, f).map(|hit| {
                    let field = f.iter().map(|(n, _)| n).find(|n| hit.contains(*n)).cloned();
                    (t, field)
                }),
                _ => None,
            })
        });
        if let Some((tab, field)) = first {
            self.tab = tab;
            self.scroll = 0;
            if let Some(field) = field
                && let Some(i) = self
                    .targets()
                    .iter()
                    .position(|t| *t == Target::Control(format!("f-{field}")))
            {
                self.focus = Focus::Row(i);
                self.cursor = self
                    .focused_control()
                    .map_or(0, |c| self.text(c).chars().count());
            }
        }
    }
    fn search_status(&self) -> String {
        let Some(form) = &self.form else {
            return String::new();
        };
        if self.terms().is_empty() {
            return if self.searched {
                "Search labels, help, scopes, and section names".into()
            } else {
                form.search_status.clone()
            };
        }
        let (mut sections, mut fields) = (0usize, 0usize);
        for tab in &form.tabs {
            for item in &tab.items {
                if let Item::Section {
                    search, fields: f, ..
                } = item
                    && let Some(hit) = self.section_hit(search, f)
                {
                    sections += 1;
                    fields += hit.len();
                }
            }
        }
        if sections == 0 {
            return "No settings match".into();
        }
        format!(
            "{sections} section{} · {fields} direct field match{}",
            if sections == 1 { "" } else { "s" },
            if fields == 1 { "" } else { "es" }
        )
    }
    /// The focusable rows of the active tab, in order, skipping folded sections.
    fn targets(&self) -> Vec<Target> {
        let mut out = Vec::new();
        let Some(tab) = self.tab() else {
            return out;
        };
        let mut folded = false;
        let mut wanted: Option<HashSet<String>> = None;
        let searching = !self.terms().is_empty();
        fn visit(items: &[Item], out: &mut Vec<Target>) {
            for item in items {
                match item {
                    Item::HooksEditor => out.push(Target::Hooks),
                    Item::Control(c) => out.push(Target::Control(c.id.clone())),
                    Item::Button { id, .. } => out.push(Target::Button(id.clone())),
                    Item::Line(items) | Item::Row { items, .. } => visit(items, out),
                    _ => {}
                }
            }
        }
        for item in &tab.items {
            if let Item::Section {
                id,
                open,
                search,
                fields,
                ..
            } = item
            {
                if searching {
                    wanted = self.section_hit(search, fields);
                    folded = wanted.is_none();
                    if wanted.is_some() {
                        out.push(Target::Section(id.clone()));
                    }
                } else {
                    folded = !self.section_open(id, *open);
                    out.push(Target::Section(id.clone()));
                }
            } else if !folded && wanted.as_ref().is_none_or(|w| Self::member_shown(item, w)) {
                visit(std::slice::from_ref(item), &mut out);
            }
        }
        out
    }
    fn clamp_focus(&mut self) {
        let rows = self.targets().len();
        let buttons = self.form.as_ref().map_or(0, |f| f.buttons.len());
        self.focus = match self.focus {
            Focus::Row(i) if i >= rows => {
                if rows > 0 {
                    Focus::Row(rows - 1)
                } else {
                    Focus::Tabs
                }
            }
            Focus::Button(i) if i >= buttons => Focus::Tabs,
            focus => focus,
        };
    }
    fn focused(&self) -> Option<Target> {
        match self.focus {
            Focus::Row(i) => self.targets().get(i).cloned(),
            _ => None,
        }
    }
    fn focused_control(&self) -> Option<&Control> {
        match self.focused()? {
            Target::Control(id) => self.form.as_ref()?.control(&id),
            _ => None,
        }
    }
    /// The draft text of an input or text area (the edit, or Python's value).
    fn text(&self, control: &Control) -> String {
        match self.edits.get(&control.id) {
            Some(Value::String(s)) => s.clone(),
            _ => match &control.kind {
                Kind::Input { value, .. } => value.clone().unwrap_or_default(),
                Kind::TextArea(value) => value.clone(),
                Kind::Select { value, .. } => value.clone(),
                Kind::Switch(_) => String::new(),
            },
        }
    }
    fn switch(&self, control: &Control) -> bool {
        match (self.edits.get(&control.id), &control.kind) {
            (Some(Value::Bool(b)), _) => *b,
            (_, Kind::Switch(b)) => *b,
            _ => false,
        }
    }
    /// Record an edit; going back to Python's value drops it, so Esc asks only when needed.
    fn set(&mut self, control: &Control, value: Value) {
        let original = match &control.kind {
            Kind::Switch(b) => Some(json!(b)),
            Kind::Select { value, .. } => Some(json!(value)),
            Kind::TextArea(value) => Some(json!(value)),
            Kind::Input { value, .. } => value.as_ref().map(|v| json!(v)),
        };
        if original.as_ref() == Some(&value) {
            self.edits.remove(&control.id);
        } else {
            self.edits.insert(control.id.clone(), value);
        }
        self.message.clear();
    }
    fn editing_text(&self) -> bool {
        self.popup.is_none()
            && self.confirm.is_none()
            && self.focused_control().is_some_and(|c| {
                !c.disabled && matches!(c.kind, Kind::Input { .. } | Kind::TextArea(_))
            })
    }
    pub fn accepts_paste(&self) -> bool {
        self.open && !self.waiting && (self.editing_text() || self.focus == Focus::Search)
    }
    pub fn paste(&mut self, text: &str) {
        if !self.accepts_paste() {
            return;
        }
        if self.focus == Focus::Search {
            let insert: String = text.chars().filter(|c| !c.is_control()).collect();
            let at = byte(&self.query, self.query_cursor);
            self.query.insert_str(at, &insert);
            self.query_cursor += insert.chars().count();
            self.search();
            return;
        }
        let control = self.focused_control().expect("editing").clone();
        let multiline = matches!(control.kind, Kind::TextArea(_));
        let insert: String = text
            .chars()
            .filter(|c| !c.is_control() || (multiline && *c == '\n'))
            .collect();
        let mut value = self.text(&control);
        if value.chars().count() + insert.chars().count() > MAX_TEXT / 4 {
            self.message = "That is longer than a settings value can be.".into();
            return;
        }
        let at = byte(&value, self.cursor);
        value.insert_str(at, &insert);
        self.cursor += insert.chars().count();
        self.set(&control, json!(value));
    }
    fn save(&mut self) {
        let Some(form) = &self.form else {
            return;
        };
        if self.edits.is_empty() {
            // Python's Save with nothing changed closes the dialog.
            self.close();
            return;
        }
        self.intent = Some(Intent::Save {
            form: form.token.clone(),
            values: self.edits.clone().into_iter().collect(),
        });
        self.waiting = true;
        self.confirm = None;
        self.message = "Saving…".into();
    }
    fn end_confirm(&mut self) {
        self.confirm = None;
        self.restoring = false;
    }
    fn restore(&mut self) {
        let Some(form) = &self.form else {
            return;
        };
        self.intent = Some(Intent::Restore {
            form: form.token.clone(),
        });
        self.edits.clear();
        self.waiting = true;
        self.end_confirm();
        self.message = "Restoring defaults…".into();
    }
    fn cancel(&mut self) {
        if self.dirty() {
            self.confirm = Some(0);
        } else {
            self.close();
        }
    }
    fn press(&mut self, id: &str) {
        match id {
            "set-save" => self.save(),
            "set-cancel" => self.cancel(),
            "set-defaults" => {
                self.restoring = true;
                self.confirm = Some(0);
            }
            // Python's Voice tab actions run in the native voice actions panel.
            "voice-test" | "voice-install-edge" | "voice-dl-stt" => {
                self.hand_over(Route::Voice, "the voice actions")
            }
            "voice-capture" => {
                self.capturing = true;
                self.message = "Press the key combination…".into();
            }
            _ => {
                let label = self
                    .form
                    .as_ref()
                    .and_then(|f| {
                        f.buttons
                            .iter()
                            .find(|b| b.id == id)
                            .map(|b| b.label.clone())
                    })
                    .unwrap_or_else(|| id.to_owned());
                self.message = format!("{label}: not available in this frontend yet.");
            }
        }
    }
    fn activate(&mut self) {
        match self.focus {
            Focus::Button(i) => {
                if let Some(b) = self.form.as_ref().and_then(|f| f.buttons.get(i)).cloned()
                    && !b.disabled
                {
                    self.press(&b.id);
                }
            }
            Focus::Row(_) => match self.focused() {
                Some(Target::Section(id)) => {
                    let default = self
                        .tab()
                        .and_then(|t| {
                            t.items.iter().find_map(|i| match i {
                                Item::Section { id: s, open, .. } if *s == id => Some(*open),
                                _ => None,
                            })
                        })
                        .unwrap_or(false);
                    let now = !self.section_open(&id, default);
                    self.sections.insert(id, now);
                }
                Some(Target::Button(id)) => self.press(&id),
                Some(Target::Hooks) => self.hand_over(Route::Hooks, "hooks"),
                Some(Target::Control(_)) => {
                    let Some(control) = self.focused_control().cloned() else {
                        return;
                    };
                    if control.disabled {
                        self.message = control
                            .reason
                            .clone()
                            .unwrap_or_else(|| "This setting is locked here.".into());
                        return;
                    }
                    match &control.kind {
                        Kind::Switch(_) => {
                            let now = !self.switch(&control);
                            self.set(&control, json!(now));
                        }
                        Kind::Select { options, .. } => {
                            let current = self.text(&control);
                            self.popup =
                                Some(options.iter().position(|o| o.1 == current).unwrap_or(0));
                        }
                        Kind::TextArea(_) => self.paste("\n"),
                        Kind::Input { .. } => self.step(1),
                    }
                }
                None => {}
            },
            Focus::Clear => {
                self.query.clear();
                self.query_cursor = 0;
                self.search();
                self.focus = Focus::Search;
            }
            Focus::Tabs | Focus::Search => {}
        }
    }
    /// Move focus like Textual's Tab/Shift+Tab: tab bar, the tab's rows, then the buttons.
    fn step(&mut self, delta: isize) {
        let rows = self.targets().len();
        let buttons = self.form.as_ref().map_or(0, |f| f.buttons.len());
        let total = 3 + rows + buttons;
        let index = match self.focus {
            Focus::Search => 0,
            Focus::Clear => 1,
            Focus::Tabs => 2,
            Focus::Row(i) => 3 + i,
            Focus::Button(i) => 3 + rows + i,
        } as isize;
        let next = (index + delta).rem_euclid(total as isize) as usize;
        self.focus = match next {
            0 => Focus::Search,
            1 => Focus::Clear,
            2 => Focus::Tabs,
            n if n < 3 + rows => Focus::Row(n - 3),
            n => Focus::Button(n - 3 - rows),
        };
        self.cursor = self
            .focused_control()
            .map_or(0, |c| self.text(c).chars().count());
    }
    fn change_tab(&mut self, delta: isize) {
        let count = self.form.as_ref().map_or(0, |f| f.tabs.len());
        if count == 0 {
            return;
        }
        self.tab = (self.tab as isize + delta).rem_euclid(count as isize) as usize;
        self.scroll = 0;
        self.focus = Focus::Tabs;
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !self.open {
            return;
        }
        if self.waiting {
            if self.form.is_none() && self.early.len() < 64 {
                self.early.push(key);
            }
            return;
        }
        if let Some(choice) = self.confirm {
            let count = if self.restoring { 2 } else { 3 };
            match key.code {
                KeyCode::Left | KeyCode::BackTab => {
                    self.confirm = Some((choice + count - 1) % count)
                }
                KeyCode::Right | KeyCode::Tab => self.confirm = Some((choice + 1) % count),
                KeyCode::Esc => self.end_confirm(),
                KeyCode::Enter => match (self.restoring, choice) {
                    (true, 0) => self.restore(),
                    (false, 0) => self.save(),
                    (false, 1) => self.close(),
                    _ => self.end_confirm(),
                },
                _ => {}
            }
            return;
        }
        if let Some(choice) = self.popup {
            let Some(control) = self.focused_control().cloned() else {
                self.popup = None;
                return;
            };
            let Kind::Select { options, .. } = &control.kind else {
                self.popup = None;
                return;
            };
            match key.code {
                KeyCode::Up => self.popup = Some(choice.saturating_sub(1)),
                KeyCode::Down => {
                    self.popup = Some((choice + 1).min(options.len().saturating_sub(1)))
                }
                KeyCode::Home => self.popup = Some(0),
                KeyCode::End => self.popup = Some(options.len().saturating_sub(1)),
                KeyCode::Esc => self.popup = None,
                KeyCode::Enter => {
                    if let Some((_, value)) = options.get(choice) {
                        self.set(&control, json!(value));
                    }
                    self.popup = None;
                }
                _ => {}
            }
            return;
        }
        if self.capturing {
            // SettingsBody.on_key: the key itself, in Textual's name, into the hotkey box.
            if let Some(name) = textual_key(key)
                && let Some(control) = self
                    .form
                    .as_ref()
                    .and_then(|f| f.control("f-stt_hotkey"))
                    .cloned()
            {
                self.capturing = false;
                self.set(&control, json!(name));
                self.message = format!("Record hotkey set to {name}. Save to apply.");
            }
            return;
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        if ctrl && matches!(key.code, KeyCode::Char('s') | KeyCode::Char('S')) {
            self.save();
            return;
        }
        match key.code {
            KeyCode::Esc => return self.cancel(),
            KeyCode::Tab => return self.step(1),
            KeyCode::BackTab => return self.step(-1),
            KeyCode::PageDown => {
                self.scroll = self.scroll.saturating_add(10);
                return;
            }
            KeyCode::PageUp => {
                self.scroll = self.scroll.saturating_sub(10);
                return;
            }
            _ => {}
        }
        if self.focus == Focus::Search {
            let len = self.query.chars().count();
            let before = self.query.clone();
            match key.code {
                KeyCode::Char(c) if !ctrl => {
                    let at = byte(&self.query, self.query_cursor);
                    self.query.insert(at, c);
                    self.query_cursor += 1;
                }
                KeyCode::Backspace if self.query_cursor > 0 => {
                    self.query_cursor -= 1;
                    let at = byte(&self.query, self.query_cursor);
                    self.query.remove(at);
                }
                KeyCode::Delete if self.query_cursor < len => {
                    let at = byte(&self.query, self.query_cursor);
                    self.query.remove(at);
                }
                KeyCode::Left => self.query_cursor = self.query_cursor.saturating_sub(1),
                KeyCode::Right => self.query_cursor = (self.query_cursor + 1).min(len),
                KeyCode::Home => self.query_cursor = 0,
                KeyCode::End => self.query_cursor = len,
                KeyCode::Down => self.step(1),
                KeyCode::Up => self.step(-1),
                _ => {}
            }
            if self.query != before {
                self.search();
            }
            return;
        }
        if self.editing_text() {
            let control = self.focused_control().expect("editing").clone();
            let mut value = self.text(&control);
            let len = value.chars().count();
            self.cursor = self.cursor.min(len);
            match key.code {
                KeyCode::Char(c) if !ctrl => {
                    let at = byte(&value, self.cursor);
                    value.insert(at, c);
                    self.cursor += 1;
                    self.set(&control, json!(value));
                }
                KeyCode::Backspace if self.cursor > 0 => {
                    self.cursor -= 1;
                    let at = byte(&value, self.cursor);
                    value.remove(at);
                    self.set(&control, json!(value));
                }
                KeyCode::Delete if self.cursor < len => {
                    let at = byte(&value, self.cursor);
                    value.remove(at);
                    self.set(&control, json!(value));
                }
                KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
                KeyCode::Right => self.cursor = (self.cursor + 1).min(len),
                KeyCode::Home => self.cursor = 0,
                KeyCode::End => self.cursor = len,
                KeyCode::Up => self.step(-1),
                KeyCode::Down => self.step(1),
                KeyCode::Enter => self.activate(),
                _ => {}
            }
            return;
        }
        // Left/Right change tab on the tab bar only, as Textual's Tabs do.
        match key.code {
            KeyCode::Left if self.focus == Focus::Tabs => self.change_tab(-1),
            KeyCode::Right if self.focus == Focus::Tabs => self.change_tab(1),
            KeyCode::Up => self.step(-1),
            KeyCode::Down => self.step(1),
            KeyCode::Enter | KeyCode::Char(' ') => self.activate(),
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        match event.kind {
            MouseEventKind::ScrollDown => self.scroll = self.scroll.saturating_add(3),
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(3),
            MouseEventKind::Down(MouseButton::Left) => {
                let at = Position::new(event.column, event.row);
                let Some(hit) = self
                    .hits
                    .iter()
                    .rev()
                    .find(|(rect, _)| rect.contains(at))
                    .map(|(_, hit)| hit.clone())
                else {
                    self.popup = None;
                    return;
                };
                match hit {
                    Hit::Tab(i) => {
                        self.tab = i;
                        self.scroll = 0;
                        self.focus = Focus::Tabs;
                    }
                    Hit::Row(i) => {
                        // A click focuses the row; on anything but a text box it also acts.
                        self.focus = Focus::Row(i);
                        self.cursor = self
                            .focused_control()
                            .map_or(0, |c| self.text(c).chars().count());
                        if !self.editing_text() {
                            self.activate();
                        }
                    }
                    Hit::Button(i) => {
                        self.focus = Focus::Button(i);
                        self.activate();
                    }
                    Hit::Option(i) => {
                        self.popup = Some(i);
                        self.key(KeyEvent::from(KeyCode::Enter));
                    }
                    Hit::Confirm(i) => {
                        self.confirm = Some(i);
                        self.key(KeyEvent::from(KeyCode::Enter));
                    }
                    Hit::Search => {
                        self.focus = Focus::Search;
                        self.query_cursor = self.query.chars().count();
                    }
                    Hit::Clear => {
                        self.focus = Focus::Clear;
                        self.activate();
                    }
                }
            }
            _ => {}
        }
    }
}
/// Where the settings dialog hands over.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Hooks,
    Voice,
}

/// A key as Textual names it ("ctrl+space", "f9", "a", "A"); None for a bare modifier.
pub fn textual_key(key: KeyEvent) -> Option<String> {
    let name = match key.code {
        KeyCode::Char(' ') => "space".to_owned(),
        KeyCode::Char(c) => c.to_string(),
        KeyCode::F(n) => format!("f{n}"),
        KeyCode::Enter => "enter".into(),
        KeyCode::Tab => "tab".into(),
        KeyCode::BackTab => "shift+tab".into(),
        KeyCode::Esc => "escape".into(),
        KeyCode::Backspace => "backspace".into(),
        KeyCode::Delete => "delete".into(),
        KeyCode::Insert => "insert".into(),
        KeyCode::Home => "home".into(),
        KeyCode::End => "end".into(),
        KeyCode::PageUp => "pageup".into(),
        KeyCode::PageDown => "pagedown".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        _ => return None,
    };
    let mut prefix = String::new();
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        prefix.push_str("ctrl+");
    }
    if key.modifiers.contains(KeyModifiers::ALT) {
        prefix.push_str("alt+");
    }
    // A shifted letter is its capital, as Textual reports it; other keys name shift.
    if key.modifiers.contains(KeyModifiers::SHIFT)
        && !matches!(key.code, KeyCode::Char(c) if c != ' ')
        && key.code != KeyCode::BackTab
    {
        prefix.push_str("shift+");
    }
    Some(format!("{prefix}{name}"))
}

fn byte(text: &str, chars: usize) -> usize {
    text.char_indices()
        .nth(chars)
        .map_or(text.len(), |(i, _)| i)
}

// ── Drawing: Textual's widgets, cell for cell ────────────────────────────────

struct Paint {
    background: Color,
    surface: Color,
    panel: Color,
    text: Color,
    muted: Color,
    accent: Color,
    border: Color,
    error: Color,
}
impl Paint {
    fn of(t: &Theme) -> Self {
        Self {
            background: t.background,
            surface: t.surface,
            panel: t.panel,
            text: t.foreground,
            muted: t.thinking_text,
            accent: t.primary,
            border: t.thinking_border,
            error: t.error,
        }
    }
}
/// One drawn row of the active tab: cells to paint and the target it focuses.
/// The three terminal rows of one Textual tall widget, as positioned spans.
type Rows3 = [Vec<(u16, String, Style)>; 3];
#[derive(Default)]
struct Row {
    spans: Vec<(u16, String, Style)>,
    target: Option<usize>,
    /// Column and target of an input's caret, when this row holds the text line.
    caret: Option<u16>,
}
fn clip(text: &str, cells: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|c| {
            used += c.width().unwrap_or(0);
            used <= cells
        })
        .collect()
}
fn pad(text: &str, cells: usize) -> String {
    let text = clip(text, cells);
    let fill = cells.saturating_sub(text.width());
    format!("{text}{}", " ".repeat(fill))
}
fn centered(text: &str, cells: usize) -> String {
    let text = clip(text, cells);
    let left = cells.saturating_sub(text.width()) / 2;
    pad(&format!("{}{text}", " ".repeat(left)), cells)
}
fn wrap(text: &str, cells: usize) -> Vec<String> {
    let cells = cells.max(1);
    let mut out = Vec::new();
    for paragraph in text.split('\n') {
        let mut line = String::new();
        for word in paragraph.split(' ').filter(|w| !w.is_empty()) {
            let fits = if line.is_empty() {
                word.width() <= cells
            } else {
                line.width() + 1 + word.width() <= cells
            };
            if !fits && !line.is_empty() {
                out.push(std::mem::take(&mut line));
            }
            if word.width() > cells {
                // A word longer than the line is split, as Rich folds it.
                for piece in crate::ui::wrap(word, cells) {
                    if !line.is_empty() {
                        out.push(std::mem::take(&mut line));
                    }
                    line = piece;
                }
                continue;
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line.push_str(word);
        }
        out.push(line);
    }
    if out.is_empty() {
        out.push(String::new());
    }
    out
}

struct Layout<'a> {
    view: &'a View,
    paint: &'a Paint,
    targets: Vec<Target>,
    rows: Vec<Row>,
    /// The last row pushed is a margin, which Textual collapses with the next margin.
    margin: bool,
}
impl Layout<'_> {
    fn target(&self, wanted: &Target) -> Option<usize> {
        self.targets.iter().position(|t| t == wanted)
    }
    fn focused(&self, index: Option<usize>) -> bool {
        index.is_some() && self.view.focus == Focus::Row(index.unwrap())
    }
    /// A vertical margin; adjacent margins collapse to the larger, as in Textual.
    fn margin(&mut self) {
        if !self.margin {
            self.rows.push(Row::default());
            self.margin = true;
        }
    }
    fn line(&mut self, x: u16, text: String, style: Style) {
        self.margin = false;
        self.rows.push(Row {
            spans: vec![(x, text, style)],
            ..Row::default()
        });
    }
    /// Textual's tall box (Input, Select): ▊▔…▎ / ▊ text ▎ / ▊▁…▎.
    fn tall(
        &self,
        x: u16,
        width: u16,
        text: &str,
        right: &str,
        style: Style,
        edge: Style,
    ) -> Rows3 {
        let inner = usize::from(width.saturating_sub(2));
        let body = inner.saturating_sub(4 + right.width());
        [
            vec![
                (x, "▊".into(), edge),
                (x + 1, "▔".repeat(inner), edge),
                (x + 1 + inner as u16, "▎".into(), edge),
            ],
            vec![
                (x, "▊".into(), edge),
                (x + 1, format!("  {}{right}  ", pad(text, body)), style),
                (x + 1 + inner as u16, "▎".into(), edge),
            ],
            vec![
                (x, "▊".into(), edge),
                (x + 1, "▁".repeat(inner), edge),
                (x + 1 + inner as u16, "▎".into(), edge),
            ],
        ]
    }
    /// The three rows of one control at `x`, `width` wide, and its caret column.
    fn control(
        &self,
        control: &Control,
        x: u16,
        width: u16,
        index: Option<usize>,
    ) -> (Rows3, Option<u16>) {
        let p = self.paint;
        let focused = self.focused(index);
        let dim = control.disabled;
        let edge = Style::default()
            .fg(if focused { p.accent } else { p.border })
            .bg(p.surface);
        let text_style = Style::default()
            .fg(if dim { p.muted } else { p.text })
            .bg(p.surface);
        let muted = Style::default().fg(p.muted).bg(p.surface);
        match &control.kind {
            Kind::Switch(_) => {
                let on = self.view.switch(control);
                let mut rows = self.tall(x, 10, "", "", text_style, edge);
                let slider = Style::default().bg(if on { p.accent } else { p.border });
                let track = Style::default().bg(p.surface);
                rows[1] = vec![
                    (x, "▊".into(), edge),
                    (x + 1, "        ".into(), track),
                    (x + if on { 5 } else { 3 }, "  ".into(), slider),
                    (x + 9, "▎".into(), edge),
                ];
                (rows, None)
            }
            Kind::Select { options, .. } => {
                let value = self.view.text(control);
                let label = options
                    .iter()
                    .find(|o| o.1 == value)
                    .map_or(value.clone(), |o| o.0.clone());
                (
                    self.tall(x, width, &shown(&label), "▼", text_style, edge),
                    None,
                )
            }
            Kind::TextArea(_) | Kind::Input { .. } => {
                let value = self.view.text(control);
                let (shown_text, style) = match &control.kind {
                    Kind::Input {
                        password: true,
                        set,
                        ..
                    } => {
                        if self.view.edits.contains_key(&control.id) {
                            ("•".repeat(value.chars().count()), text_style)
                        } else if *set {
                            ("••••••••".into(), text_style)
                        } else if let Kind::Input { placeholder, .. } = &control.kind {
                            (shown(placeholder), muted)
                        } else {
                            unreachable!()
                        }
                    }
                    Kind::Input { placeholder, .. } if value.is_empty() => {
                        (shown(placeholder), muted)
                    }
                    _ => (shown(&value).replace('\n', "⏎"), text_style),
                };
                let body = usize::from(width.saturating_sub(6));
                let caret = self.view.cursor.min(shown_text.chars().count());
                let start = caret.saturating_sub(body.saturating_sub(1));
                let visible: String = shown_text.chars().skip(start).collect();
                let caret_x = (focused && !dim && self.view.popup.is_none())
                    .then(|| x + 3 + (caret - start) as u16);
                (self.tall(x, width, &visible, "", style, edge), caret_x)
            }
        }
    }
    fn button_width(label: &str) -> u16 {
        (label.width() as u16 + 2).max(16)
    }
    fn button(&self, label: &str, x: u16, width: u16, focused: bool, disabled: bool) -> Rows3 {
        let p = self.paint;
        let face = Style::default()
            .fg(if disabled { p.muted } else { p.text })
            .bg(if focused { p.accent } else { p.panel })
            .add_modifier(Modifier::BOLD);
        let top = Style::default()
            .fg(p.border)
            .bg(if focused { p.accent } else { p.panel });
        let w = usize::from(width);
        [
            vec![(x, "▔".repeat(w), top)],
            vec![(x, centered(&shown(label), w), face)],
            vec![(x, "▁".repeat(w), top)],
        ]
    }
    fn push3(&mut self, rows: Rows3, target: Option<usize>, caret: Option<u16>) {
        self.margin = false;
        for (i, spans) in rows.into_iter().enumerate() {
            self.rows.push(Row {
                spans,
                target,
                caret: if i == 1 { caret } else { None },
            });
        }
    }
    fn items(&mut self, items: &[Item], x: u16, width: u16) {
        let p = self.paint;
        let mut folded = false;
        let mut wanted: Option<HashSet<String>> = None;
        let searching = !self.view.terms().is_empty();
        for item in items {
            if let Item::Section {
                id,
                label,
                open,
                search,
                fields,
            } = item
            {
                if searching {
                    wanted = self.view.section_hit(search, fields);
                    if wanted.is_none() {
                        folded = true;
                        continue;
                    }
                    folded = false;
                } else {
                    folded = !self.view.section_open(id, *open);
                }
                let index = self.target(&Target::Section(id.clone()));
                let focused = self.focused(index);
                let arrow = if folded { "▸" } else { "▾" };
                let label = label
                    .strip_prefix('▾')
                    .or_else(|| label.strip_prefix('▸'))
                    .map_or(label.clone(), |rest| format!("{arrow}{rest}"));
                let bg = if focused { p.accent } else { p.panel };
                let edge = Style::default().fg(p.border).bg(bg);
                let face = Style::default()
                    .fg(if focused { p.background } else { p.accent })
                    .bg(bg);
                self.margin();
                let w = usize::from(width);
                self.push3(
                    [
                        vec![(x, "▔".repeat(w), edge)],
                        vec![(
                            x,
                            format!(" {} ", centered(&shown(&label), w.saturating_sub(2))),
                            face,
                        )],
                        vec![(x, "▁".repeat(w), edge)],
                    ],
                    index,
                    None,
                );
                continue;
            }
            if folded
                || wanted
                    .as_ref()
                    .is_some_and(|w| !View::member_shown(item, w))
            {
                continue;
            }
            self.item(item, x, width);
        }
    }
    fn item(&mut self, item: &Item, x: u16, width: u16) {
        let p = self.paint;
        match item {
            Item::Section { .. } => {}
            Item::Label { text, .. } => self.line(
                x + 1,
                clip(&shown(text), usize::from(width.saturating_sub(1))),
                Style::default()
                    .fg(p.text)
                    .bg(p.surface)
                    .add_modifier(Modifier::BOLD),
            ),
            Item::Text {
                text,
                help,
                subhead,
            } => {
                if *subhead {
                    self.margin();
                }
                let (dx, style) = if *help {
                    (1, Style::default().fg(p.muted).bg(p.surface))
                } else if *subhead {
                    (
                        0,
                        Style::default()
                            .fg(p.muted)
                            .bg(p.surface)
                            .add_modifier(Modifier::BOLD),
                    )
                } else {
                    (0, Style::default().fg(p.text).bg(p.surface))
                };
                for line in wrap(&shown(text), usize::from(width.saturating_sub(dx))) {
                    self.line(x + dx, line, style);
                }
            }
            Item::Control(control) => {
                let index = self.target(&Target::Control(control.id.clone()));
                let width = if matches!(control.kind, Kind::Switch(_)) {
                    10
                } else {
                    width
                };
                let (rows, caret) = self.control(control, x, width, index);
                self.push3(rows, index, caret);
            }
            Item::Button {
                id,
                label,
                disabled,
            } => {
                let index = self.target(&Target::Button(id.clone()));
                let w = Self::button_width(label);
                let rows = self.button(label, x, w, self.focused(index), *disabled);
                self.push3(rows, index, None);
            }
            Item::HooksEditor => {
                let index = self.target(&Target::Hooks);
                let focused = self.focused(index);
                let rows = self.button(
                    "Open the hooks editor (/hooks)",
                    x,
                    Self::button_width("Open the hooks editor (/hooks)"),
                    focused,
                    false,
                );
                self.push3(rows, index, None);
            }
            Item::Row { items, indent } => {
                let dx = if *indent { 4 } else { 0 };
                self.items(items, x + dx, width.saturating_sub(dx));
                if !indent {
                    self.margin();
                }
            }
            Item::Line(items) => {
                // Fixed widths first (switch 10, buttons, inline labels); inputs share the rest.
                let fixed: u16 = items
                    .iter()
                    .map(|i| match i {
                        Item::Control(Control {
                            kind: Kind::Switch(_),
                            ..
                        }) => 10,
                        Item::Button { label, .. } => Self::button_width(label),
                        Item::Label { text, .. } => text.width() as u16 + 1,
                        _ => 0,
                    })
                    .sum();
                let flexible = items
                    .iter()
                    .filter(|i| {
                        matches!(
                            i,
                            Item::Control(Control {
                                kind: Kind::Input { .. } | Kind::Select { .. } | Kind::TextArea(_),
                                ..
                            })
                        )
                    })
                    .count()
                    .max(1) as u16;
                let share = width.saturating_sub(fixed) / flexible;
                let mut block = [Vec::new(), Vec::new(), Vec::new()];
                let mut target = None;
                let mut caret = None;
                let mut cx = x;
                for item in items {
                    match item {
                        Item::Label { text, .. } => {
                            block[0].push((
                                cx + 1,
                                shown(text),
                                Style::default()
                                    .fg(p.text)
                                    .bg(p.surface)
                                    .add_modifier(Modifier::BOLD),
                            ));
                            cx += text.width() as u16 + 1;
                        }
                        Item::Control(control) => {
                            let index = self.target(&Target::Control(control.id.clone()));
                            let w = if matches!(control.kind, Kind::Switch(_)) {
                                10
                            } else {
                                share
                            };
                            let (rows, c) = self.control(control, cx, w, index);
                            for (row, spans) in block.iter_mut().zip(rows) {
                                row.extend(spans);
                            }
                            if self.focused(index) || target.is_none() {
                                target = index;
                            }
                            caret = caret.or(c);
                            cx += w;
                        }
                        Item::Button {
                            id,
                            label,
                            disabled,
                        } => {
                            let index = self.target(&Target::Button(id.clone()));
                            let w = Self::button_width(label);
                            let rows = self.button(label, cx, w, self.focused(index), *disabled);
                            for (row, spans) in block.iter_mut().zip(rows) {
                                row.extend(spans);
                            }
                            if self.focused(index) || target.is_none() {
                                target = index;
                            }
                            cx += w;
                        }
                        other => self.item(other, cx, width.saturating_sub(cx - x)),
                    }
                }
                self.push3(block, target, caret);
            }
        }
    }
}

impl View {
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        self.hits.clear();
        if !self.owns_input() {
            return;
        }
        let p = Paint::of(theme);
        // Python's ModalScreen covers the whole screen: nothing of the chat shows around the box.
        frame.render_widget(Clear, area);
        frame
            .buffer_mut()
            .set_style(area, Style::default().fg(p.text).bg(p.background));
        // Python's #set-box: 92 wide (or the screen), 88% tall, centered; thick border, padding 1 2.
        let width = area.width.min(92);
        let height = (u32::from(area.height) * 88 / 100) as u16;
        if width < 40 || height < 16 {
            return;
        }
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        frame.render_widget(Clear, rect);
        let surface = Style::default().fg(p.text).bg(p.surface);
        let edge = Style::default().fg(p.accent).bg(p.surface);
        let buf = frame.buffer_mut();
        for y in rect.top()..rect.bottom() {
            let row = if y == rect.top() {
                format!("█{}█", "▀".repeat(usize::from(width - 2)))
            } else if y + 1 == rect.bottom() {
                format!("█{}█", "▄".repeat(usize::from(width - 2)))
            } else {
                format!("█{}█", " ".repeat(usize::from(width - 2)))
            };
            buf.set_string(rect.x, y, row, edge);
            if y != rect.top() && y + 1 != rect.bottom() {
                buf.set_string(rect.x + 1, y, " ".repeat(usize::from(width - 2)), surface);
            }
        }
        let x0 = rect.x + 3;
        let w = width - 6;
        let top = rect.y + 2;
        let bottom = rect.bottom() - 3; // last content row
        let Some(form) = self.form.clone() else {
            buf.set_string(x0, top, "Settings", surface.add_modifier(Modifier::BOLD));
            buf.set_string(
                x0,
                top + 1,
                shown(&self.notice),
                Style::default().fg(p.muted).bg(p.surface),
            );
            return;
        };
        let muted = Style::default().fg(p.muted).bg(p.surface);
        buf.set_string(
            x0,
            top,
            clip(&shown(&form.title), w.into()),
            Style::default()
                .fg(p.accent)
                .bg(p.surface)
                .add_modifier(Modifier::BOLD),
        );
        buf.set_string(x0, top + 1, clip(&shown(&form.subtitle), w.into()), muted);
        // Search row: input (1fr), status (1fr), Clear button (16, margin 1).
        let search = (w.saturating_sub(17)) / 2;
        let probe = Layout {
            view: self,
            paint: &p,
            targets: Vec::new(),
            rows: Vec::new(),
            margin: false,
        };
        let (query, query_style) = if self.query.is_empty() {
            (shown(&form.search_placeholder), muted)
        } else {
            (shown(&self.query), surface)
        };
        let body = usize::from(search.saturating_sub(6));
        let start = self.query_cursor.saturating_sub(body.saturating_sub(1));
        let query: String = query
            .chars()
            .skip(if self.query.is_empty() { 0 } else { start })
            .collect();
        let search_edge = Style::default()
            .fg(if self.focus == Focus::Search {
                p.accent
            } else {
                p.border
            })
            .bg(p.surface);
        let search_rows = probe.tall(x0, search, &query, "", query_style, search_edge);
        let clear = probe.button("Clear", x0 + w - 16, 16, self.focus == Focus::Clear, false);
        for (i, (a, b)) in search_rows.iter().zip(clear.iter()).enumerate() {
            for (x, text, style) in a.iter().chain(b.iter()) {
                buf.set_string(*x, top + 3 + i as u16, text, *style);
            }
        }
        let status = self.search_status();
        let status_width = usize::from(w.saturating_sub(search + 17).saturating_sub(1));
        let lines: Vec<String> = if status.is_empty() {
            Vec::new()
        } else {
            wrap(&shown(&status), status_width)
        };
        let first = top + 3 + (3 - lines.len().min(3) as u16) / 2;
        for (i, line) in lines.iter().take(3).enumerate() {
            buf.set_string(x0 + search + 1, first + i as u16, line, muted);
        }
        self.hits
            .push((Rect::new(x0, top + 3, search, 3), Hit::Search));
        self.hits
            .push((Rect::new(x0 + w - 16, top + 3, 16, 3), Hit::Clear));
        let search_caret = (self.focus == Focus::Search).then(|| {
            (
                x0 + 3 + (self.query_cursor - start.min(self.query_cursor)) as u16,
                top + 4,
            )
        });
        // Tabs: " label " each; the underline marks the active one (Textual's Underline bar).
        let tabs_y = top + 7;
        let mut cx = x0;
        let mut spans = Vec::new();
        for (i, tab) in form.tabs.iter().enumerate() {
            let label = format!(" {} ", shown(&tab.label));
            let cells = label.width() as u16;
            spans.push((i, cx, label, cells));
            cx += cells;
        }
        // Textual's Tabs scroll the active tab to the centre, within the strip's extent.
        let (_, ax, _, acells) = &spans[self.tab.min(spans.len() - 1)];
        let strip = cx - x0;
        let centre = f64::from(ax - x0) + f64::from(*acells) / 2.0 - f64::from(w) / 2.0;
        let shift = (centre.max(0.0) as u16).min(strip.saturating_sub(w));
        let underline = Style::default().fg(p.border).bg(p.surface);
        buf.set_string(x0, tabs_y + 1, "━".repeat(w.into()), underline);
        for (i, sx, label, cells) in &spans {
            let start = sx.saturating_sub(shift);
            let hidden = usize::from(x0.saturating_sub(start));
            if hidden >= usize::from(*cells) {
                continue;
            }
            let sx = start.max(x0);
            if sx >= x0 + w {
                break;
            }
            let tail: String = label.chars().skip(hidden).collect();
            let cells = &(*cells - hidden as u16);
            let visible = clip(&tail, usize::from(x0 + w - sx));
            let active = *i == self.tab;
            let style = if active {
                Style::default()
                    .fg(p.text)
                    .bg(p.surface)
                    .add_modifier(Modifier::BOLD)
            } else {
                muted
            };
            buf.set_string(sx, tabs_y, &visible, style);
            self.hits.push((
                Rect::new(sx, tabs_y, (*cells).min(x0 + w - sx), 2),
                Hit::Tab(*i),
            ));
            if active {
                let accent = Style::default()
                    .fg(if self.focus == Focus::Tabs {
                        p.accent
                    } else {
                        p.text
                    })
                    .bg(p.surface);
                buf.set_string(sx, tabs_y + 1, "╸", underline);
                if *cells > 2 {
                    buf.set_string(
                        sx + 1,
                        tabs_y + 1,
                        "━".repeat(usize::from(cells - 2)),
                        accent,
                    );
                }
                if sx + cells - 1 < x0 + w {
                    buf.set_string(sx + cells - 1, tabs_y + 1, "╺", underline);
                }
            }
        }
        // Buttons along the bottom, left to right with Textual's margin-left 2 (they clip, as there).
        let buttons_y = bottom - 2;
        let mut bx = x0 + 2;
        let probe = Layout {
            view: self,
            paint: &p,
            targets: Vec::new(),
            rows: Vec::new(),
            margin: false,
        };
        let mut button_hits = Vec::new();
        let mut painted = Vec::new();
        for (i, b) in form.buttons.iter().enumerate() {
            let bw = Layout::button_width(&b.label);
            if bx >= x0 + w {
                break;
            }
            let shown_w = bw.min(x0 + w - bx);
            let rows = probe.button(&b.label, bx, bw, self.focus == Focus::Button(i), b.disabled);
            painted.push((rows, shown_w));
            button_hits.push((Rect::new(bx, buttons_y, shown_w, 3), Hit::Button(i)));
            bx += bw + 2;
        }
        for (rows, shown_w) in painted {
            for (r, spans) in rows.iter().enumerate() {
                for (x, text, style) in spans {
                    buf.set_string(*x, buttons_y + r as u16, clip(text, shown_w.into()), *style);
                }
            }
        }
        self.hits.extend(button_hits);
        // Python's #set-error line sits above the buttons when it has something to say.
        let message = if self.message.is_empty() {
            &self.notice
        } else {
            &self.message
        };
        let lines = if message.is_empty() {
            vec![String::new()]
        } else {
            wrap(&shown(message), usize::from(w - 2))
        };
        let lines: Vec<String> = lines.into_iter().take(3).collect();
        // Above the buttons' padding-top row; padding 0 1 like Python's.
        let view_bottom = buttons_y - 1 - lines.len() as u16;
        for (i, line) in lines.iter().enumerate() {
            buf.set_string(
                x0 + 1,
                view_bottom + i as u16,
                line,
                Style::default().fg(p.error).bg(p.surface),
            );
        }
        // The active tab's rows, in a scroll region (content 2 narrower: padding-right and scrollbar).
        let content_top = tabs_y + 2;
        let height = view_bottom.saturating_sub(content_top);
        let targets = self.targets();
        // Textual's widths: a scroll pane keeps a padding column, and a scrollbar column only
        // while its rows overflow; the Hooks pane has neither.
        let lay = |width: u16| {
            let mut layout = Layout {
                view: &*self,
                paint: &p,
                targets: targets.clone(),
                rows: Vec::new(),
                margin: false,
            };
            if let Some(tab) = form.tabs.get(self.tab) {
                layout.items(&tab.items, x0, width);
            }
            layout.rows
        };
        let scrolls = form.tabs.get(self.tab).is_some_and(|t| t.scroll);
        let (cw, rows) = if !scrolls {
            (w, lay(w))
        } else {
            let fitted = lay(w - 1);
            if fitted.len() > usize::from(height) {
                (w - 2, lay(w - 2))
            } else {
                (w - 1, fitted)
            }
        };
        let max_scroll = rows.len().saturating_sub(usize::from(height));
        // Keep the focused row in view.
        if let Focus::Row(i) = self.focus
            && let (Some(first), Some(last)) = (
                rows.iter().position(|r| r.target == Some(i)),
                rows.iter().rposition(|r| r.target == Some(i)),
            )
        {
            if first < self.scroll {
                self.scroll = first.saturating_sub(1);
            } else if last >= self.scroll + usize::from(height) {
                self.scroll = last + 1 - usize::from(height);
            }
        }
        self.scroll = self.scroll.min(max_scroll);
        let buf = frame.buffer_mut();
        let mut caret = None;
        for (dy, row) in rows
            .iter()
            .skip(self.scroll)
            .take(height.into())
            .enumerate()
        {
            let y = content_top + dy as u16;
            for (x, text, style) in &row.spans {
                let room = usize::from((x0 + cw).saturating_sub(*x));
                buf.set_string(*x, y, clip(text, room), *style);
            }
            if let Some(target) = row.target {
                self.hits.push((Rect::new(x0, y, cw, 1), Hit::Row(target)));
            }
            if let Some(cx) = row.caret {
                caret = Some((cx, y));
            }
        }
        if max_scroll > 0 {
            // Textual's ScrollBarRender.render_bar, vertical, in eighths of a cell.
            const BARS: [&str; 8] = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", " "];
            let size = f64::from(height);
            let virtual_size = rows.len() as f64;
            let thumb = (size / (virtual_size / size)).max(1.0);
            let position = (size - thumb) * (self.scroll as f64 / (virtual_size - size));
            let start = (position * 8.0) as usize;
            let end = start + (thumb * 8.0).ceil() as usize;
            let (start_index, start_bar) = (start / 8, start % 8);
            let (end_index, end_bar) = (end / 8, end % 8);
            let column = x0 + cw;
            let track = Style::default().fg(p.border).bg(p.surface);
            let solid = Style::default().fg(p.surface).bg(p.border);
            for i in start_index..end_index.min(usize::from(height)) {
                buf.set_string(column, content_top + i as u16, " ", solid);
            }
            if start_index < usize::from(height) && BARS[7 - start_bar] != " " {
                buf.set_string(
                    column,
                    content_top + start_index as u16,
                    BARS[7 - start_bar],
                    track,
                );
            }
            if end_index < usize::from(height) && BARS[7 - end_bar] != " " {
                buf.set_string(
                    column,
                    content_top + end_index as u16,
                    BARS[7 - end_bar],
                    solid,
                );
            }
        }
        // A select's choices open over the rows below it.
        if let Some(choice) = self.popup
            && let Some(Kind::Select { options, .. }) =
                self.focused_control().map(|c| c.kind.clone())
        {
            let anchor = rows
                .iter()
                .skip(self.scroll)
                .take(height.into())
                .rposition(|r| self.focus == Focus::Row(r.target.unwrap_or(usize::MAX)))
                .map_or(content_top, |i| content_top + i as u16 + 1);
            let shown_rows = options
                .len()
                .min(usize::from(bottom.saturating_sub(anchor)).max(3))
                .min(12);
            let first = choice.saturating_sub(shown_rows.saturating_sub(1));
            let box_rect = Rect::new(
                x0,
                anchor.min(bottom.saturating_sub(shown_rows as u16 + 2)),
                cw,
                shown_rows as u16 + 2,
            );
            frame.render_widget(Clear, box_rect);
            let edge = Style::default().fg(p.accent).bg(p.panel);
            let buf = frame.buffer_mut();
            let inner = usize::from(box_rect.width - 2);
            buf.set_string(
                box_rect.x,
                box_rect.y,
                format!("▊{}▎", "▔".repeat(inner)),
                edge,
            );
            for (i, (label, _)) in options.iter().enumerate().skip(first).take(shown_rows) {
                let y = box_rect.y + 1 + (i - first) as u16;
                let style = if i == choice {
                    Style::default().fg(p.background).bg(p.accent)
                } else {
                    Style::default().fg(p.text).bg(p.panel)
                };
                buf.set_string(box_rect.x, y, "▊", edge);
                buf.set_string(
                    box_rect.x + 1,
                    y,
                    pad(&format!(" {}", shown(label)), inner),
                    style,
                );
                buf.set_string(box_rect.x + 1 + inner as u16, y, "▎", edge);
                self.hits.push((
                    Rect::new(box_rect.x + 1, y, inner as u16, 1),
                    Hit::Option(i),
                ));
            }
            buf.set_string(
                box_rect.x,
                box_rect.bottom() - 1,
                format!("▊{}▎", "▁".repeat(inner)),
                edge,
            );
            caret = None;
        }
        // Python's SettingsExitConfirm: Save / Discard / Keep editing.
        if let Some(choice) = self.confirm {
            let cw = width.min(76).saturating_sub(4).max(40);
            let crect = Rect::new(
                area.x + (area.width - cw) / 2,
                area.y + area.height / 2 - 5,
                cw,
                10,
            );
            frame.render_widget(Clear, crect);
            let buf = frame.buffer_mut();
            let panel = Style::default().fg(p.text).bg(p.panel);
            let edge = Style::default().fg(p.accent).bg(p.panel);
            let inner = usize::from(cw - 2);
            for y in crect.top()..crect.bottom() {
                let (l, m, r) = if y == crect.top() {
                    ("▊", "▔".repeat(inner), "▎")
                } else if y + 1 == crect.bottom() {
                    ("▊", "▁".repeat(inner), "▎")
                } else {
                    ("▊", " ".repeat(inner), "▎")
                };
                buf.set_string(crect.x, y, l, edge);
                buf.set_string(
                    crect.x + 1,
                    y,
                    m,
                    if y == crect.top() || y + 1 == crect.bottom() {
                        edge
                    } else {
                        panel
                    },
                );
                buf.set_string(crect.right() - 1, y, r, edge);
            }
            // SettingsExitConfirm's two forms: unsaved changes, or restore defaults.
            let (title, sub, labels): (&str, &str, &[&str]) = if self.restoring {
                (
                    "Restore all settings to defaults?",
                    "This replaces the current draft and applies only after you confirm.",
                    &RESTORE_CONFIRM,
                )
            } else {
                (
                    "Unsaved settings changes",
                    "Save the draft, discard it, or return to the settings panel.",
                    &CONFIRM,
                )
            };
            buf.set_string(
                crect.x + 3,
                crect.y + 2,
                title,
                panel.add_modifier(Modifier::BOLD),
            );
            buf.set_string(
                crect.x + 3,
                crect.y + 3,
                clip(sub, inner - 4),
                Style::default().fg(p.muted).bg(p.panel),
            );
            let probe = Layout {
                view: self,
                paint: &p,
                targets: Vec::new(),
                rows: Vec::new(),
                margin: false,
            };
            let total: u16 = labels
                .iter()
                .map(|l| Layout::button_width(l) + 2)
                .sum::<u16>();
            let mut bx = crect.x + (cw.saturating_sub(total)) / 2 + 1;
            let mut hits = Vec::new();
            for (i, label) in labels.iter().enumerate() {
                let bw = Layout::button_width(label);
                let rows = probe.button(label, bx, bw, i == choice, false);
                for (r, spans) in rows.iter().enumerate() {
                    for (x, text, style) in spans {
                        buf.set_string(*x, crect.y + 5 + r as u16, text, *style);
                    }
                }
                hits.push((Rect::new(bx, crect.y + 5, bw, 3), Hit::Confirm(i)));
                bx += bw + 2;
            }
            self.hits.extend(hits);
            caret = None;
        }
        if caret.is_none() && self.popup.is_none() && self.confirm.is_none() {
            caret = search_caret;
        }
        if self.waiting {
            caret = None;
        }
        if let Some(position) = caret {
            frame.set_cursor_position(position);
        }
        self.visible = true;
    }
}
