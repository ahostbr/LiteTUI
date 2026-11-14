//! Source-owned backend selection. Metadata does not establish service readiness.
use crate::theme::Theme;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{Frame, layout::Rect, style::Style};
use serde_json::Value;
use std::{collections::HashSet, io};

pub const LIST: &str = "gui.backends.list";
pub const SET: &str = "gui.backend.set";
pub const MODEL_BYTES: usize = 1024;
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub name: String,
    pub label: String,
    pub selected: bool,
    pub owned_engine_controls: bool,
    /// Display-only readiness mark (Python's "Label  · mark"); never a readiness claim.
    pub mark: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Catalog {
    pub revision: u64,
    pub session: String,
    pub agent: String,
    pub agent_name: String,
    pub snapshot: String,
    pub selected: String,
    pub configured: String,
    pub model: String,
    pub requested_native: bool,
    pub effective_engine: Option<String>,
    pub entries: Vec<Entry>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Read,
    /// Python's /backend: switch and save the backend at once, the model unchosen (Ryan
    /// 2026-11-12: "/backend switches backend only /model displays models that backend provides").
    Switch {
        revision: u64,
        name: String,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Catalog(Catalog),
    Rejected(String),
    Changed { backend: String, model: String },
}
#[derive(Debug, PartialEq)]
pub struct Changed {
    pub snapshot: crate::management::Snapshot,
    pub catalog: Catalog,
    pub history: crate::resume::History,
    pub queue: crate::restored_queue::Snapshot,
    pub policy: Value,
    /// Python's App._offer_unchosen_model: open /model now (a backend-only switch whose backend
    /// lists models, once a session).
    pub offer_models: bool,
}
fn fail(text: &str) -> io::Error {
    io::Error::other(text.to_owned())
}
fn text(value: &Value, key: &str, limit: usize) -> io::Result<String> {
    value[key]
        .as_str()
        .filter(|s| !s.trim().is_empty() && s.len() <= limit && !s.chars().any(char::is_control))
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid backend metadata {key}")))
}
fn identity(value: &Value, key: &str) -> io::Result<String> {
    let id = text(value, key, 36)?;
    if id.len() != 36
        || !id.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
    {
        return Err(fail("Noncanonical backend ownership identity"));
    }
    Ok(id)
}
fn boolean(value: &Value, key: &str) -> io::Result<bool> {
    value[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid backend metadata {key}")))
}
pub fn valid_model(value: &str) -> bool {
    !value.trim().is_empty()
        && value.trim() == value
        && value.len() <= MODEL_BYTES
        && !value.chars().any(char::is_control)
}
impl Catalog {
    pub fn parse(value: &Value, revision: u64, session: &str, agent: &str) -> io::Result<Self> {
        if serde_json::to_vec(value)?.len() > 65536
            || value["version"].as_u64() != Some(1)
            || value["kind"].as_str() != Some("support_metadata")
            || value["readiness_checked"].as_bool() != Some(false)
        {
            return Err(fail("Unsupported or oversized backend metadata"));
        }
        let actual_session = identity(value, "session_id")?;
        let actual_agent = identity(value, "agent_id")?;
        if actual_session != session || actual_agent != agent {
            return Err(fail("Backend catalog belongs to another owned session"));
        }
        let snapshot = text(value, "snapshot", 64)?;
        if snapshot.len() != 64
            || !snapshot
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(fail("Invalid backend catalog snapshot"));
        }
        let selected = text(value, "selected_backend", 128)?;
        let configured = text(value, "configured_backend", 128)?;
        // Empty after /backend: Python leaves the model unchosen until /model picks one.
        let model = value["selected_model"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| fail("Invalid backend metadata selected_model"))?;
        if !model.is_empty() && !valid_model(&model) {
            return Err(fail("Invalid current backend model"));
        }
        let engine = &value["codex_engine"];
        let requested_native = boolean(engine, "requested_native")?;
        let effective_engine = match engine.get("effective") {
            Some(Value::Null) if selected != "codex" => None,
            Some(Value::String(s))
                if selected == "codex" && ["native", "http"].contains(&s.as_str()) =>
            {
                Some(s.clone())
            }
            _ => return Err(fail("Effective Codex engine disagrees with active backend")),
        };
        let rows = value["entries"]
            .as_array()
            .filter(|rows| !rows.is_empty() && rows.len() <= 32)
            .ok_or_else(|| fail("Invalid backend catalog entries"))?;
        let mut seen = HashSet::new();
        let mut entries = Vec::new();
        for row in rows {
            let name = text(row, "name", 128)?;
            if !seen.insert(name.clone()) {
                return Err(fail("Duplicate backend catalog entry"));
            }
            let current = boolean(row, "selected")?;
            if current != (name == selected) {
                return Err(fail("Backend selection flags disagree"));
            }
            let mark = value["marks"]
                .get(&name)
                .and_then(Value::as_str)
                .filter(|m| !m.is_empty() && m.len() <= 1024 && !m.chars().any(char::is_control))
                .map(str::to_owned);
            entries.push(Entry {
                name,
                label: text(row, "label", 1024)?,
                selected: current,
                owned_engine_controls: boolean(row, "owned_engine_controls")?,
                mark,
            });
        }
        if !seen.contains(&selected) || !seen.contains(&configured) {
            return Err(fail(
                "Active or configured backend absent from source catalog",
            ));
        }
        Ok(Self {
            revision,
            session: actual_session,
            agent: actual_agent,
            agent_name: text(value, "agent_name", 1024)?,
            snapshot,
            selected,
            configured,
            model,
            requested_native,
            effective_engine,
            entries,
        })
    }
}
/// Python LiteTUI's picker hint (litetui/picker.py DEFAULT_HINT).
pub const PICKER_HINT: &str = "↑↓ move · Enter or click to select · Esc to cancel";
/// litetui/plugins/model_switch._cmd_backend title.
pub const ENGINE_TITLE: &str = "Select the engine for this conversation";
/// litetui/plugins/model_switch._cmd_model title.
pub const MODEL_TITLE: &str = "Select a model";
/// litetui/context_choice CTX_FLOOR / CTX_CEILING (a typo guard, not a VRAM model).
pub const CTX_FLOOR: u64 = 512;
pub const CTX_CEILING: u64 = 1 << 20;

#[derive(Clone, Debug, PartialEq)]
pub struct ContextFacts {
    pub applies_on_engine_start: bool,
    pub suggestions: std::collections::HashMap<String, u64>,
    pub remembered: std::collections::HashMap<String, u64>,
}
/// The "Type a number of tokens" rule of litetui/context_choice.parse_context.
pub fn parse_context(text: &str) -> Result<u64, String> {
    let raw: String = text
        .trim()
        .chars()
        .filter(|c| !matches!(c, ',' | '_' | ' '))
        .collect();
    if raw.is_empty() || !raw.chars().all(|c| c.is_ascii_digit()) {
        return Err("Type a number of tokens, for example 100000 or 131072.".into());
    }
    let value: u64 = raw
        .parse()
        .map_err(|_| "Type a number of tokens, for example 100000 or 131072.".to_owned())?;
    if !(CTX_FLOOR..=CTX_CEILING).contains(&value) {
        return Err(format!(
            "{} is not a plausible context length ({} to {} tokens).",
            group(value),
            group(CTX_FLOOR),
            group(CTX_CEILING)
        ));
    }
    Ok(value)
}
fn group(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub catalog: Option<Catalog>,
    selected: usize,
    intent: Option<Intent>,
    pending: Option<String>,
    visible: bool,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    preselect: Option<String>,
    /// The picker's first visible wrapped line (picker_box keeps the cursor in view).
    scroll: usize,
}
impl Default for View {
    fn default() -> Self {
        Self {
            open: false,
            waiting: false,
            notice: String::new(),
            catalog: None,
            selected: 0,
            intent: None,
            pending: None,
            visible: false,
            hits: Vec::new(),
            pressed: None,
            preselect: None,
            scroll: 0,
        }
    }
}
impl View {
    /// A command argument only moves the row cursor once the catalogue arrives.
    pub fn preselect(&mut self, wanted: &str) {
        if self.open && self.waiting && !wanted.trim().is_empty() {
            self.preselect = Some(wanted.trim().to_ascii_lowercase());
        }
    }
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.preselect = None;
        self.catalog = None;
        self.intent = Some(Intent::Read);
        self.pending = None;
        self.notice = "Reading the engines…".into();
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn close(&mut self) {
        self.open = false;
        self.preselect = None;
        self.hide();
        if self.intent.take().is_some() {
            self.waiting = false;
            self.pending = None;
        }
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.pending = None;
        self.catalog = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
        self.hits.clear();
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if let Update::Changed { backend, .. } = &update
            && (!self.waiting || self.pending.as_ref() != Some(backend))
        {
            return Err(fail("Unexpected backend transition verification"));
        }
        if matches!(update, Update::Catalog(_)) && self.pending.is_some() {
            return Err(fail("Catalog cannot complete a backend transition"));
        }
        self.waiting = false;
        self.intent = None;
        self.hide();
        match update {
            Update::Catalog(catalog) => {
                self.selected = catalog.entries.iter().position(|r| r.selected).unwrap_or(0);
                self.notice = String::new();
                let mut picked = false;
                if let Some(wanted) = self.preselect.take() {
                    match catalog.entries.iter().position(|r| r.name == wanted) {
                        Some(index) => {
                            // `/backend NAME` switches at once in Python.
                            self.selected = index;
                            picked = true;
                        }
                        None => {
                            self.notice = format!(
                                "Unknown backend {wanted:?} — {}",
                                catalog
                                    .entries
                                    .iter()
                                    .map(|r| r.name.as_str())
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            )
                        }
                    }
                }
                self.catalog = Some(catalog);
                if picked {
                    self.open = true;
                    self.pick();
                }
            }
            Update::Rejected(error) => {
                self.preselect = None;
                self.pending = None;
                // Python closes its picker and says why in the chat. Keeping this open on an
                // emptied list left nothing for the arrows or Enter to act on (Ryan 2026-11-12).
                self.catalog = None;
                self.close();
                self.notice = error;
            }
            Update::Changed { .. } => {
                self.finish();
            }
        }
        Ok(())
    }
    fn rows(&self) -> usize {
        self.catalog.as_ref().map_or(0, |c| c.entries.len())
    }
    fn move_to(&mut self, index: usize) {
        self.selected = index.min(self.rows().saturating_sub(1));
    }
    fn pick(&mut self) {
        let Some(c) = &self.catalog else { return };
        let Some(r) = c.entries.get(self.selected) else {
            return;
        };
        if r.selected {
            // plugins/model_switch._switch_backend
            self.notice = format!("Already on {}", r.name);
            self.close();
            return;
        }
        // App.apply_backend_change: the switch happens now; /model lists its models.
        // Python's own lines ("Backend → NAME; reconnecting…") arrive as system lines.
        self.intent = Some(Intent::Switch {
            revision: c.revision,
            name: r.name.clone(),
        });
        self.pending = Some(r.name.clone());
        self.waiting = true;
        self.notice.clear();
        self.hide();
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if !key.modifiers.is_empty() {
            return;
        }
        let current = self.selected;
        let last = self.rows().saturating_sub(1);
        match key.code {
            KeyCode::Up => self.move_to(current.saturating_sub(1)),
            KeyCode::Down => self.move_to(current + 1),
            KeyCode::PageUp => self.move_to(current.saturating_sub(10)),
            KeyCode::PageDown => self.move_to(current + 10),
            KeyCode::Home => self.move_to(0),
            KeyCode::End => self.move_to(last),
            KeyCode::Enter if key.kind == KeyEventKind::Press => self.pick(),
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting || event.modifiers != KeyModifiers::NONE {
            self.pressed = None;
            return;
        }
        let hit = self
            .hits
            .iter()
            .find(|(rect, _)| rect.contains((event.column, event.row).into()))
            .map(|(_, i)| *i);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                // "Enter or click to select"
                if let Some(index) = self.pressed.take().filter(|p| Some(*p) == hit) {
                    self.move_to(index);
                    self.pick();
                }
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown if hit.is_some() => {
                let current = self.selected;
                self.move_to(if event.kind == MouseEventKind::ScrollUp {
                    current.saturating_sub(1)
                } else {
                    current + 1
                });
            }
            _ => self.pressed = None,
        }
    }
    /// Python's picker (picker_box): the engine list.
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, t: &Theme) {
        self.visible = false;
        // Python closes the picker on a pick and opens the next one when its rows exist.
        if !self.owns_input() || self.waiting {
            self.hide();
            return;
        }
        let labels: Vec<String> = self.catalog.as_ref().map_or_else(Vec::new, |c| {
            c.entries
                .iter()
                .map(|r| match &r.mark {
                    Some(mark) => format!("{}  · {mark}", r.label),
                    None => r.label.clone(),
                })
                .collect()
        });
        let mut extra: Vec<(String, Style)> = Vec::new();
        if !self.notice.is_empty() {
            // A refused pick says why where the next attempt is made.
            extra.push((self.notice.clone(), Style::default().fg(t.error)));
        }
        let drawn = crate::picker_box::draw(
            frame,
            area,
            t,
            ENGINE_TITLE,
            &labels,
            self.selected,
            &mut self.scroll,
            &extra,
            PICKER_HINT,
        );
        self.hits = drawn.hits;
        self.visible = self.open && !self.waiting && drawn.rect.width > 0;
    }
}

/// Backend changes preserve the current conversation, including a fresh empty one.
pub fn history(value: &Value, session: &str) -> io::Result<crate::resume::History> {
    if value["messages"].as_array().is_some_and(Vec::is_empty) {
        if value["session_id"] != session
            || value["read_only"] != false
            || !value["meta"].is_object()
            || value["meta"].get("id").is_some_and(|id| id != session)
        {
            return Err(fail("Invalid empty owned conversation history"));
        }
        return Ok(crate::resume::History {
            descriptor: None,
            page: None,
            session: session.into(),
            messages: Vec::new(),
            hidden_system_messages: 0,
            bytes: 0,
            units: 0,
        });
    }
    crate::resume::History::parse(value, session)
}
