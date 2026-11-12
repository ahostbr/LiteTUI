//! Source-owned backend selection. Metadata does not establish service readiness.
use crate::{
    composer::{Composer, Delete, Motion},
    live::terminal_text,
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
use serde_json::Value;
use std::{collections::HashSet, io};
use unicode_segmentation::UnicodeSegmentation;

pub const LIST: &str = "gui.backends.list";
pub const SET: &str = "gui.backend.set";
pub const MODELS: &str = "gui.backends.native_models";
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
    /// List the models the chosen backend serves (Python's /model after /backend).
    Models {
        revision: u64,
        name: String,
    },
    Select {
        revision: u64,
        name: String,
        model: String,
        /// The context-length answer for a local backend; None is "Keep current".
        context: Option<u64>,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Catalog(Catalog),
    Models(ModelList),
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
        let model = text(value, "selected_model", MODEL_BYTES)?;
        if !valid_model(&model) {
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
    pub fn selection(&self, revision: u64, name: &str, model: &str) -> io::Result<()> {
        if self.revision != revision
            || !self.entries.iter().any(|r| r.name == name)
            || !valid_model(model)
        {
            return Err(fail(
                "Backend selection changed or model is invalid; refresh and select explicitly",
            ));
        }
        if self.selected == name && self.model == model {
            return Err(fail("That backend and model are already active"));
        }
        Ok(())
    }
}
fn display(text: &str, offset: usize) -> String {
    terminal_text(text)
        .replace('\n', "\\n")
        .replace('\t', "\\t")
        .graphemes(true)
        .skip(offset)
        .collect()
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
pub struct ModelRow {
    pub key: String,
    pub label: String,
    pub loaded: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct ContextFacts {
    pub applies_on_engine_start: bool,
    pub suggestions: std::collections::HashMap<String, u64>,
    pub remembered: std::collections::HashMap<String, u64>,
}
/// The models another backend serves, listed the way Python's /model lists them.
#[derive(Clone, Debug, PartialEq)]
pub struct ModelList {
    pub revision: u64,
    pub name: String,
    pub rows: Vec<ModelRow>,
    pub context: Option<ContextFacts>,
}
fn positive(value: &Value) -> io::Result<Option<u64>> {
    match value {
        Value::Null => Ok(None),
        v => v
            .as_u64()
            .filter(|n| *n > 0)
            .map(Some)
            .ok_or_else(|| fail("Invalid context suggestion")),
    }
}
impl ModelList {
    pub fn parse(value: &Value, catalog: &Catalog, name: &str) -> io::Result<Self> {
        if serde_json::to_vec(value)?.len() > 256 * 1024
            || value["version"].as_u64() != Some(1)
            || value["kind"] != "backend_models"
            || value["session_id"] != catalog.session.as_str()
            || value["agent_id"] != catalog.agent.as_str()
            || value["snapshot"] != catalog.snapshot.as_str()
            || value["name"] != name
        {
            return Err(fail(
                "Backend model list does not match the selected backend",
            ));
        }
        let mut seen = HashSet::new();
        let mut rows = Vec::new();
        for row in value["models"]
            .as_array()
            .filter(|rows| rows.len() <= 512)
            .ok_or_else(|| fail("Invalid backend model list"))?
        {
            let key = text(row, "key", MODEL_BYTES)?;
            if !valid_model(&key) || !seen.insert(key.clone()) {
                return Err(fail("Invalid or duplicate backend model"));
            }
            rows.push(ModelRow {
                key,
                label: text(row, "label", 4096)?,
                loaded: boolean(row, "loaded")?,
            });
        }
        let context = match &value["context"] {
            Value::Null => None,
            facts => {
                let mut suggestions = std::collections::HashMap::new();
                let mut remembered = std::collections::HashMap::new();
                for row in &rows {
                    if let Some(n) = positive(&facts["suggestions"][&row.key])? {
                        suggestions.insert(row.key.clone(), n);
                    }
                    if let Some(n) = positive(&facts["remembered"][&row.key])? {
                        remembered.insert(row.key.clone(), n);
                    }
                }
                Some(ContextFacts {
                    applies_on_engine_start: boolean(facts, "applies_on_engine_start")?,
                    suggestions,
                    remembered,
                })
            }
        };
        Ok(Self {
            revision: catalog.revision,
            name: name.to_owned(),
            rows,
            context,
        })
    }
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    Engines,
    Models,
    Context,
}
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub catalog: Option<Catalog>,
    pub models: Option<ModelList>,
    stage: Stage,
    editor: Composer,
    selected: usize,
    model_selected: usize,
    context_error: String,
    intent: Option<Intent>,
    pending: Option<String>,
    visible: bool,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    preselect: Option<String>,
}
impl Default for View {
    fn default() -> Self {
        Self {
            open: false,
            waiting: false,
            notice: String::new(),
            catalog: None,
            models: None,
            stage: Stage::Engines,
            editor: Composer::new(64, 0),
            selected: 0,
            model_selected: 0,
            context_error: String::new(),
            intent: None,
            pending: None,
            visible: false,
            hits: Vec::new(),
            pressed: None,
            preselect: None,
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
        self.models = None;
        self.stage = Stage::Engines;
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
        self.models = None;
        self.stage = Stage::Engines;
        self.editor.clear();
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
        self.hits.clear();
    }
    /// The context field's text (the only free text in this flow).
    pub fn model_text(&self) -> &str {
        self.editor.text()
    }
    pub fn editing(&self) -> bool {
        self.open && self.visible && !self.waiting && self.stage == Stage::Context
    }
    pub fn paste(&mut self, text: &str) {
        if self.editing() && !text.chars().any(char::is_control) {
            self.editor.insert(text);
        }
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
                            // `/backend NAME` switches at once in Python: go straight to its models.
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
                self.models = None;
                self.stage = Stage::Engines;
                if picked {
                    self.open = true;
                    self.pick();
                }
            }
            Update::Models(list) => {
                // Python opens /model with nothing chosen after a backend switch: the first row.
                self.model_selected = 0;
                self.notice = if list.rows.is_empty() {
                    format!("No models discovered on {}.", list.name)
                } else {
                    String::new()
                };
                self.stage = if list.rows.is_empty() {
                    Stage::Engines
                } else {
                    Stage::Models
                };
                self.models = Some(list);
            }
            Update::Rejected(error) => {
                self.preselect = None;
                self.pending = None;
                if self.stage == Stage::Engines {
                    self.catalog = None;
                } else {
                    self.stage = Stage::Models;
                }
                self.notice = error;
            }
            Update::Changed { .. } => {
                self.finish();
            }
        }
        Ok(())
    }
    fn rows(&self) -> usize {
        match self.stage {
            Stage::Engines => self.catalog.as_ref().map_or(0, |c| c.entries.len()),
            Stage::Models => self.models.as_ref().map_or(0, |m| m.rows.len()),
            Stage::Context => 0,
        }
    }
    fn cursor(&mut self) -> &mut usize {
        if self.stage == Stage::Models {
            &mut self.model_selected
        } else {
            &mut self.selected
        }
    }
    fn move_to(&mut self, index: usize) {
        let last = self.rows().saturating_sub(1);
        *self.cursor() = index.min(last);
    }
    fn pick(&mut self) {
        match self.stage {
            Stage::Engines => {
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
                self.intent = Some(Intent::Models {
                    revision: c.revision,
                    name: r.name.clone(),
                });
                self.waiting = true;
                self.notice = format!("Listing {} models…", r.label);
                self.hide();
            }
            Stage::Models => {
                let Some(m) = &self.models else { return };
                let Some(row) = m.rows.get(self.model_selected) else {
                    return;
                };
                if let Some(facts) = &m.context {
                    let value = facts.suggestions.get(&row.key).copied();
                    self.editor.clear();
                    if let Some(value) = value {
                        self.editor.insert(&value.to_string());
                    }
                    self.context_error.clear();
                    self.stage = Stage::Context;
                } else {
                    self.select(None);
                }
            }
            Stage::Context => match parse_context(self.editor.text()) {
                Ok(tokens) => self.select(Some(tokens)),
                Err(error) => self.context_error = error,
            },
        }
    }
    fn select(&mut self, context: Option<u64>) {
        let Some(m) = &self.models else { return };
        let Some(row) = m.rows.get(self.model_selected) else {
            return;
        };
        self.intent = Some(Intent::Select {
            revision: m.revision,
            name: m.name.clone(),
            model: row.key.clone(),
            context,
        });
        self.pending = Some(m.name.clone());
        self.waiting = true;
        self.notice = format!("Backend → {}; reconnecting…", m.name);
        self.hide();
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            match self.stage {
                // context_choice: Esc is "Keep current"; the pick itself still happens.
                Stage::Context if self.open && self.visible && !self.waiting => self.select(None),
                Stage::Models if !self.waiting => {
                    self.stage = Stage::Engines;
                    self.models = None;
                    self.notice.clear();
                }
                _ => self.close(),
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if self.stage == Stage::Context {
            let control = key.modifiers.contains(KeyModifiers::CONTROL);
            match key.code {
                KeyCode::Enter if key.kind == KeyEventKind::Press => self.pick(),
                KeyCode::Char('a') if control => self.editor.select_all(),
                KeyCode::Char(c) if !control && !c.is_control() => {
                    self.editor.insert(&c.to_string())
                }
                KeyCode::Left => self.editor.move_cursor(Motion::Left, false),
                KeyCode::Right => self.editor.move_cursor(Motion::Right, false),
                KeyCode::Home => self.editor.move_cursor(Motion::Home, false),
                KeyCode::End => self.editor.move_cursor(Motion::End, false),
                KeyCode::Backspace => self.editor.delete(Delete::Left),
                KeyCode::Delete => self.editor.delete(Delete::Right),
                _ => {}
            }
            return;
        }
        if !key.modifiers.is_empty() {
            return;
        }
        let current = *self.cursor();
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
                let current = *self.cursor();
                self.move_to(if event.kind == MouseEventKind::ScrollUp {
                    current.saturating_sub(1)
                } else {
                    current + 1
                });
            }
            _ => self.pressed = None,
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, t: &Theme) {
        self.visible = false;
        if !self.owns_input() || area.width < 36 || area.height < 10 {
            self.hide();
            return;
        }
        self.hits.clear();
        let (title, labels, cursor, current): (String, Vec<String>, usize, Option<usize>) =
            match self.stage {
                Stage::Engines => (
                    ENGINE_TITLE.into(),
                    self.catalog.as_ref().map_or_else(Vec::new, |c| {
                        c.entries
                            .iter()
                            .map(|r| match &r.mark {
                                Some(mark) => format!("{}  · {mark}", r.label),
                                None => r.label.clone(),
                            })
                            .collect()
                    }),
                    self.selected,
                    self.catalog
                        .as_ref()
                        .and_then(|c| c.entries.iter().position(|r| r.selected)),
                ),
                Stage::Models | Stage::Context => (
                    MODEL_TITLE.into(),
                    self.models.as_ref().map_or_else(Vec::new, |m| {
                        m.rows.iter().map(|r| r.label.clone()).collect()
                    }),
                    self.model_selected,
                    None,
                ),
            };
        let width = area.width.saturating_sub(4).min(100);
        let list_height = (labels.len() as u16).clamp(1, area.height.saturating_sub(8));
        let context_rows = if self.stage == Stage::Context { 5 } else { 0 };
        let height = (list_height + 5 + context_rows).min(area.height.saturating_sub(2));
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        frame.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .style(Style::default().fg(t.foreground).bg(t.background))
            .border_style(Style::default().fg(t.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        frame.render_widget(
            Paragraph::new(display(&title, 0)).style(Style::default().add_modifier(Modifier::BOLD)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let list = Rect::new(
            inner.x,
            inner.y + 1,
            inner.width,
            inner.height.saturating_sub(3 + context_rows),
        );
        let start = cursor.saturating_sub(usize::from(list.height).saturating_sub(1));
        for (i, label) in labels
            .iter()
            .enumerate()
            .skip(start)
            .take(list.height as usize)
        {
            let hit = Rect::new(list.x, list.y + (i - start) as u16, list.width, 1);
            let highlighted = i == cursor;
            let style = if highlighted {
                Style::default()
                    .fg(t.background)
                    .bg(t.title)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(t.foreground)
            };
            let marker = if current == Some(i) { "▸ " } else { "" };
            frame.render_widget(
                Paragraph::new(format!("{marker}{}", display(label, 0))).style(style),
                hit,
            );
            if self.stage != Stage::Context {
                self.hits.push((hit, i));
            }
        }
        if self.stage == Stage::Context {
            let model = self
                .models
                .as_ref()
                .and_then(|m| m.rows.get(self.model_selected));
            let key = model.map_or("", |r| r.key.as_str());
            let facts = self.models.as_ref().and_then(|m| m.context.as_ref());
            let mut hint = if facts.is_some_and(|f| f.applies_on_engine_start) {
                "Applies on the next /engine start (stop a running engine first).".to_owned()
            } else {
                "The model is loaded at this size.".to_owned()
            };
            if let Some(last) = facts.and_then(|f| f.remembered.get(key)) {
                hint.push_str(&format!(" Last used for this model: {}.", group(*last)));
            }
            let y = list.bottom();
            for (dy, line) in [
                format!("Context length for {} (tokens)", display(key, 0)),
                hint,
                format!("{}▏", display(self.editor.text(), 0)),
                self.context_error.clone(),
                "Enter: Set context · Esc: Keep current".into(),
            ]
            .into_iter()
            .enumerate()
            {
                frame.render_widget(
                    Paragraph::new(line),
                    Rect::new(inner.x, y + dy as u16, inner.width, 1),
                );
            }
        }
        frame.render_widget(
            Paragraph::new(PICKER_HINT).style(Style::default().fg(t.foreground)),
            Rect::new(inner.x, inner.bottom().saturating_sub(2), inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(display(&self.notice, 0)),
            Rect::new(inner.x, inner.bottom().saturating_sub(1), inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
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
