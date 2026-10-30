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
use unicode_width::UnicodeWidthStr;

pub const LIST: &str = "gui.backends.list";
pub const SET: &str = "gui.backend.set";
pub const MODEL_BYTES: usize = 1024;
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub name: String,
    pub label: String,
    pub selected: bool,
    pub owned_engine_controls: bool,
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
    Select {
        revision: u64,
        name: String,
        model: String,
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
            entries.push(Entry {
                name,
                label: text(row, "label", 1024)?,
                selected: current,
                owned_engine_controls: boolean(row, "owned_engine_controls")?,
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
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub catalog: Option<Catalog>,
    editor: Composer,
    selected: usize,
    editing: bool,
    intent: Option<Intent>,
    pending: Option<(String, String)>,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    armed: bool,
    confirmation_visible: bool,
    review_visible: bool,
    confirm_offset: usize,
    confirm_page: usize,
}
impl Default for View {
    fn default() -> Self {
        Self {
            open: false,
            waiting: false,
            notice: String::new(),
            catalog: None,
            editor: Composer::new(MODEL_BYTES, 0),
            selected: 0,
            editing: false,
            intent: None,
            pending: None,
            visible: false,
            geometry: None,
            hits: Vec::new(),
            pressed: None,
            armed: false,
            confirmation_visible: false,
            review_visible: false,
            confirm_offset: 0,
            confirm_page: 1,
        }
    }
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.catalog = None;
        self.intent = Some(Intent::Read);
        self.pending = None;
        self.notice = "Reading source backend metadata…".into();
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
        self.editing = false;
        self.editor.clear();
    }
    fn disarm(&mut self) {
        self.armed = false;
        self.confirmation_visible = false;
        self.review_visible = false;
        self.confirm_offset = 0;
        self.pressed = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.disarm();
        self.hits.clear();
    }
    pub fn model_text(&self) -> &str {
        self.editor.text()
    }
    pub fn editing(&self) -> bool {
        self.open && self.visible && !self.waiting && self.editing && !self.armed
    }
    pub fn paste(&mut self, text: &str) {
        self.disarm();
        if self.editing() {
            self.insert(text);
        }
    }
    fn insert(&mut self, text: &str) {
        if text.chars().any(char::is_control)
            || self
                .editor
                .text()
                .len()
                .saturating_sub(self.editor.selected_text().len())
                .saturating_add(text.len())
                > MODEL_BYTES
        {
            self.notice = "Model must be a single line of at most 1024 UTF-8 bytes.".into();
            return;
        }
        self.editor.insert(text);
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if let Update::Changed { backend, model } = &update
            && (!self.waiting || self.pending.as_ref() != Some(&(backend.clone(), model.clone())))
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
                self.catalog = Some(catalog);
                self.editing = false;
                self.notice = "Metadata only; choose a backend and enter an explicit model.".into();
            }
            Update::Rejected(error) => {
                self.pending = None;
                self.catalog = None;
                self.editing = false;
                self.notice = error;
            }
            Update::Changed { .. } => {
                self.finish();
                self.notice = "Backend, model, history and held input verified.".into();
            }
        }
        Ok(())
    }
    fn choose(&mut self, index: usize) {
        self.disarm();
        if self.selected != index {
            self.editor.clear();
        }
        self.selected = index;
        self.editing = false;
    }
    fn arm(&mut self) {
        self.disarm();
        if let Some(c) = &self.catalog
            && let Some(r) = c.entries.get(self.selected)
        {
            match c.selection(c.revision, &r.name, self.editor.text()) {
                Ok(()) => self.armed = true,
                Err(e) => self.notice = e.to_string(),
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.armed {
                self.disarm();
            } else if self.editing {
                self.editing = false;
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if self.armed {
            if !key.modifiers.is_empty() {
                self.disarm();
                return;
            }
            match key.code {
                KeyCode::PageDown if key.kind == KeyEventKind::Press && self.review_visible => {
                    self.confirm_offset = self.confirm_offset.saturating_add(self.confirm_page);
                    self.confirmation_visible = false;
                    self.review_visible = false;
                }
                KeyCode::PageUp if key.kind == KeyEventKind::Press && self.review_visible => {
                    self.confirm_offset = self.confirm_offset.saturating_sub(self.confirm_page);
                    self.confirmation_visible = false;
                    self.review_visible = false;
                }
                KeyCode::Enter if key.kind == KeyEventKind::Press && self.confirmation_visible => {
                    if let Some(c) = &self.catalog
                        && let Some(r) = c.entries.get(self.selected)
                    {
                        let name = r.name.clone();
                        let model = self.editor.text().to_owned();
                        self.intent = Some(Intent::Select {
                            revision: c.revision,
                            name: name.clone(),
                            model: model.clone(),
                        });
                        self.pending = Some((name, model));
                        self.waiting = true;
                        self.notice =
                            "Connecting; verifying source state and preserved conversation…".into();
                        self.hide();
                    }
                }
                KeyCode::Enter | KeyCode::PageDown | KeyCode::PageUp => {}
                _ => self.disarm(),
            }
            return;
        }
        if self.editing {
            let shift = key.modifiers.contains(KeyModifiers::SHIFT);
            let control = key.modifiers.contains(KeyModifiers::CONTROL);
            if key
                .modifiers
                .intersects(KeyModifiers::ALT | KeyModifiers::SUPER)
            {
                return;
            }
            match key.code {
                KeyCode::Enter if key.kind == KeyEventKind::Press => self.arm(),
                KeyCode::Char('a') if control => self.editor.select_all(),
                KeyCode::Char(c) if !control => self.insert(&c.to_string()),
                KeyCode::Left => self.editor.move_cursor(
                    if control {
                        Motion::WordLeft
                    } else {
                        Motion::Left
                    },
                    shift,
                ),
                KeyCode::Right => self.editor.move_cursor(
                    if control {
                        Motion::WordRight
                    } else {
                        Motion::Right
                    },
                    shift,
                ),
                KeyCode::Home => self.editor.move_cursor(Motion::Home, shift),
                KeyCode::End => self.editor.move_cursor(Motion::End, shift),
                KeyCode::Backspace => self.editor.delete(if control {
                    Delete::WordLeft
                } else {
                    Delete::Left
                }),
                KeyCode::Delete => self.editor.delete(if control {
                    Delete::WordRight
                } else {
                    Delete::Right
                }),
                KeyCode::Tab if key.kind == KeyEventKind::Press => self.editing = false,
                _ => {}
            }
            return;
        }
        if !key.modifiers.is_empty() {
            self.disarm();
            return;
        }
        let last = self
            .catalog
            .as_ref()
            .map_or(0, |c| c.entries.len().saturating_sub(1));
        match key.code {
            KeyCode::Up => self.choose(self.selected.saturating_sub(1)),
            KeyCode::Down => self.choose((self.selected + 1).min(last)),
            KeyCode::Home => self.choose(0),
            KeyCode::End => self.choose(last),
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => self.open(),
            KeyCode::Enter | KeyCode::Tab
                if key.kind == KeyEventKind::Press && self.catalog.is_some() =>
            {
                self.editing = true;
                self.notice =
                    "Type the exact model. Enter reviews; no fallback model is chosen.".into();
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        self.armed = false;
        self.confirmation_visible = false;
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
                if self.pressed.take().is_some_and(|p| Some(p) == hit) {
                    self.choose(hit.expect("paired hit"));
                }
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown if hit.is_some() => {
                let last = self
                    .catalog
                    .as_ref()
                    .map_or(0, |c| c.entries.len().saturating_sub(1));
                self.choose(if event.kind == MouseEventKind::ScrollUp {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(last)
                });
            }
            _ => self.pressed = None,
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, t: &Theme) {
        self.visible = false;
        if !self.owns_input() || area.width < 36 || area.height < 14 {
            self.hide();
            return;
        }
        let width = area.width.saturating_sub(2).min(100);
        let height = area.height.saturating_sub(2).min(28);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(rect) {
            self.disarm();
            self.geometry = Some(rect);
        }
        self.hits.clear();
        frame.render_widget(Clear, rect);
        let block = Block::default()
            .title(" Backend and model ")
            .borders(Borders::ALL)
            .style(Style::default().fg(t.foreground).bg(t.background))
            .border_style(Style::default().fg(t.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        if self.armed {
            let name = self
                .catalog
                .as_ref()
                .and_then(|c| c.entries.get(self.selected))
                .map_or("", |r| r.name.as_str());
            let lines = crate::ui::wrap(
                &format!(
                    "Backend: {}\nModel: {}",
                    display(name, 0),
                    display(self.editor.text(), 0)
                ),
                inner.width as usize,
            );
            let page = usize::from(inner.height.saturating_sub(4)).max(1);
            self.confirm_page = page;
            self.confirm_offset = self
                .confirm_offset
                .min(lines.len().saturating_sub(1) / page * page);
            frame.render_widget(
                Paragraph::new("Apply this backend and model?"),
                Rect::new(inner.x, inner.y, inner.width, 1),
            );
            frame.render_widget(
                Paragraph::new(
                    lines
                        .iter()
                        .skip(self.confirm_offset)
                        .take(page)
                        .cloned()
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                Rect::new(inner.x, inner.y + 1, inner.width, page as u16),
            );
            let full = self.confirm_offset + page >= lines.len();
            frame.render_widget(
                Paragraph::new(if full {
                    "Enter: apply | Esc: cancel"
                } else {
                    "PgDn: review remaining model"
                }),
                Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
            );
            frame.render_widget(
                Paragraph::new("Connect only; no start or load."),
                Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
            );
            self.confirmation_visible = self.open && !self.waiting && full;
            self.review_visible = self.open && !self.waiting;
        } else {
            let active = self
                .catalog
                .as_ref()
                .map(|c| format!("Active: {} / {}", c.selected, c.model))
                .unwrap_or_else(|| "Catalog unavailable".into());
            let configured = self
                .catalog
                .as_ref()
                .map(|c| {
                    format!(
                        "Configured: {} | Codex req:{} eff:{}",
                        c.configured,
                        if c.requested_native { "native" } else { "http" },
                        c.effective_engine.as_deref().unwrap_or("n/a")
                    )
                })
                .unwrap_or_default();
            for (dy, text) in [
                active,
                configured,
                "Support metadata; readiness unchecked".into(),
            ]
            .into_iter()
            .enumerate()
            {
                frame.render_widget(
                    Paragraph::new(display(&text, 0)),
                    Rect::new(inner.x, inner.y + dy as u16, inner.width, 1),
                );
            }
            let list = Rect::new(
                inner.x,
                inner.y + 3,
                inner.width,
                inner.height.saturating_sub(6),
            );
            if let Some(c) = &self.catalog {
                let start = self
                    .selected
                    .saturating_sub(usize::from(list.height).saturating_sub(1));
                for (i, r) in c
                    .entries
                    .iter()
                    .enumerate()
                    .skip(start)
                    .take(list.height as usize)
                {
                    let hit = Rect::new(list.x, list.y + (i - start) as u16, list.width, 1);
                    let style = Style::default()
                        .fg(if i == self.selected {
                            t.title
                        } else {
                            t.foreground
                        })
                        .add_modifier(if i == self.selected {
                            Modifier::BOLD
                        } else {
                            Modifier::empty()
                        });
                    frame.render_widget(
                        Paragraph::new(format!(
                            "{}{} {} [{}]",
                            if i == self.selected { ">" } else { " " },
                            if r.selected { "●" } else { " " },
                            display(&r.label, 0),
                            display(&r.name, 0)
                        ))
                        .style(style),
                        hit,
                    );
                    self.hits.push((hit, i));
                }
            }
            let model_line = if self.editing {
                let before = display(self.editor.before_cursor(), 0);
                let available = usize::from(inner.width.saturating_sub(9));
                let mut used = 0;
                let mut tail: Vec<&str> = before
                    .graphemes(true)
                    .rev()
                    .take_while(|glyph| {
                        used += glyph.width();
                        used <= available
                    })
                    .collect();
                tail.reverse();
                format!(
                    "Model: {}▏{}",
                    tail.concat(),
                    display(&self.editor.text()[self.editor.before_cursor().len()..], 0)
                )
            } else {
                format!("Model: {}", display(self.editor.text(), 0))
            };
            frame.render_widget(
                Paragraph::new(model_line),
                Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
            );
            frame.render_widget(
                Paragraph::new(if self.editing {
                    "Enter review | Tab list | Esc back"
                } else {
                    "↑↓ choose | Enter model | R refresh"
                }),
                Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
            );
        }
        frame.render_widget(
            Paragraph::new(display(&self.notice, 0)),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
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
            session: session.into(),
            messages: Vec::new(),
            hidden_system_messages: 0,
            bytes: 0,
            units: 0,
        });
    }
    crate::resume::History::parse(value, session)
}
