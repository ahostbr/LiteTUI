//! Source-derived model/thinking choices and a native selector. No runtime I/O.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::Value;
use std::{collections::HashSet, io};
use unicode_segmentation::UnicodeSegmentation;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Model,
    Thinking,
}
impl Kind {
    pub fn read(self) -> &'static str {
        match self {
            Self::Model => "gui.models.list",
            Self::Thinking => "gui.models.capabilities",
        }
    }
    pub fn apply(self) -> &'static str {
        match self {
            Self::Model => "gui.models.select",
            Self::Thinking => "gui.thinking.set",
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Open(Kind),
    Select { revision: u64, value: String },
}
#[derive(Clone, Debug, PartialEq)]
pub struct Choice {
    pub value: String,
    pub label: String,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Choices {
    pub revision: u64,
    pub kind: Kind,
    pub session: String,
    pub backend: String,
    pub model: String,
    pub current: String,
    pub rows: Vec<Choice>,
    pub note: String,
    pub message: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Choices(Choices),
    Rejected(String),
}
fn fail(text: &str) -> io::Error {
    io::Error::other(text.to_owned())
}
fn text<'a>(v: &'a Value, k: &str, limit: usize) -> io::Result<&'a str> {
    v.get(k)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty() && v.len() <= limit)
        .ok_or_else(|| fail(&format!("Invalid model control {k}")))
}
impl Choices {
    pub fn parse(kind: Kind, value: &Value, revision: u64, session: &str) -> io::Result<Self> {
        let backend = text(value, "backend", 64)?.to_owned();
        let model = text(value, "model", 1024)?.to_owned();
        let (current, rows, note) = match kind {
            Kind::Model => {
                let values = value
                    .get("models")
                    .and_then(Value::as_array)
                    .filter(|v| v.len() <= 512)
                    .ok_or_else(|| fail("Invalid model catalogue"))?;
                let mut rows = Vec::new();
                for row in values {
                    let slug = text(row, "slug", 1024)?;
                    let name = text(row, "name", 1024)?;
                    if row.get("current").and_then(Value::as_bool) != Some(slug == model) {
                        return Err(fail(
                            "Model catalogue current flag disagrees with selection",
                        ));
                    }
                    if !row
                        .get("loaded")
                        .is_some_and(|v| v.is_null() || v.is_boolean())
                    {
                        return Err(fail("Model loaded state must be nullable boolean"));
                    }
                    rows.push(Choice {
                        value: slug.to_owned(),
                        label: if name == slug {
                            slug.to_owned()
                        } else {
                            format!("{name} · {slug}")
                        },
                    });
                }
                (
                    model.clone(),
                    rows,
                    format!("Connected catalogue: {backend}. Enter selects; Escape closes."),
                )
            }
            Kind::Thinking => {
                let thinking = &value["thinking"];
                let values = thinking
                    .get("levels")
                    .and_then(Value::as_array)
                    .filter(|v| !v.is_empty() && v.len() <= 32)
                    .ok_or_else(|| fail("Invalid thinking choices"))?;
                let current = match thinking.get("level") {
                    Some(Value::Null) => "default".to_owned(),
                    Some(Value::String(s)) if !s.is_empty() && s.len() <= 64 => s.clone(),
                    _ => {
                        return Err(fail(
                            "Runtime lacks current thinking selection; update Python source",
                        ));
                    }
                };
                let rows = values
                    .iter()
                    .map(|v| {
                        let s = v
                            .as_str()
                            .filter(|s| !s.is_empty() && s.len() <= 64)
                            .ok_or_else(|| fail("Invalid thinking level"))?;
                        Ok(Choice {
                            value: s.to_owned(),
                            label: s.to_owned(),
                        })
                    })
                    .collect::<io::Result<Vec<_>>>()?;
                let source = text(thinking, "source", 1024)?;
                let note = thinking
                    .get("note")
                    .and_then(Value::as_str)
                    .filter(|s| s.len() <= 4096)
                    .ok_or_else(|| fail("Invalid thinking note"))?;
                (current, rows, format!("{source}: {note}"))
            }
        };
        let mut seen = HashSet::new();
        let mut budget = note.len() + current.len() + session.len() + backend.len() + model.len();
        for row in &rows {
            if !seen.insert(&row.value) {
                return Err(fail("Duplicate model/thinking choice"));
            }
            budget = budget.saturating_add(row.label.len() + row.value.len());
        }
        if budget > 65536 {
            return Err(fail("Model controls text budget exceeded"));
        }
        Ok(Self {
            revision,
            kind,
            session: session.to_owned(),
            backend,
            model,
            current,
            rows,
            note,
            message: None,
        })
    }
}

fn display_line(value: &str, offset: usize) -> String {
    terminal_text(value)
        .replace('\n', "\\n")
        .replace('\t', "\\t")
        .graphemes(true)
        .skip(offset)
        .collect()
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub menu: Option<Choices>,
    pub notice: String,
    kind: Option<Kind>,
    cursor: usize,
    horizontal: usize,
    intent: Option<Intent>,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
}
impl View {
    pub fn open(&mut self, kind: Kind) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.menu = None;
        self.horizontal = 0;
        self.kind = Some(kind);
        self.intent = Some(Intent::Open(kind));
        self.notice = "Reading runtime choices…".into();
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
        }
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.menu = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
        self.hits.clear();
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.horizontal = 0;
        match update {
            Update::Choices(menu) => {
                self.kind = Some(menu.kind);
                self.cursor = menu
                    .rows
                    .iter()
                    .position(|r| r.value == menu.current)
                    .unwrap_or(0);
                self.notice = menu
                    .message
                    .clone()
                    .unwrap_or_else(|| "Runtime selection verified.".into());
                self.menu = Some(menu);
            }
            Update::Rejected(error) => {
                self.menu = None;
                self.notice = error;
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !key.modifiers.is_empty() {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        match key.code {
            KeyCode::Tab if key.kind == KeyEventKind::Press => {
                self.open(if self.kind == Some(Kind::Model) {
                    Kind::Thinking
                } else {
                    Kind::Model
                });
            }
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => {
                self.open(self.kind.unwrap_or(Kind::Model))
            }
            KeyCode::Up => {
                self.cursor = self.cursor.saturating_sub(1);
                self.horizontal = 0;
                self.pressed = None;
            }
            KeyCode::Down => {
                self.cursor = (self.cursor + 1).min(
                    self.menu
                        .as_ref()
                        .map_or(0, |m| m.rows.len().saturating_sub(1)),
                );
                self.horizontal = 0;
                self.pressed = None;
            }
            KeyCode::Left => self.horizontal = self.horizontal.saturating_sub(10),
            KeyCode::Right => {
                self.horizontal = self
                    .horizontal
                    .saturating_add(10)
                    .min(self.inspection_end())
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                if let Some(menu) = &self.menu
                    && let Some(row) = menu.rows.get(self.cursor)
                {
                    self.intent = Some(Intent::Select {
                        revision: menu.revision,
                        value: row.value.clone(),
                    });
                    self.waiting = true;
                    self.notice = "Applying selection; waiting for runtime readback…".into();
                    self.hide();
                }
            }
            _ => {}
        }
    }
    fn current_label(&self) -> String {
        self.menu
            .as_ref()
            .map(|m| format!("{} · current: {}", m.backend, m.current))
            .unwrap_or_default()
    }
    fn inspection_end(&self) -> usize {
        let current = self.current_label();
        let mut longest = [current.as_str(), self.notice.as_str()]
            .into_iter()
            .map(|s| display_line(s, 0).graphemes(true).count())
            .max()
            .unwrap_or(0);
        if let Some(menu) = &self.menu {
            longest = longest.max(display_line(&menu.note, 0).graphemes(true).count());
            if let Some(row) = menu.rows.get(self.cursor) {
                longest = longest.max(display_line(&row.label, 0).graphemes(true).count());
            }
        }
        longest.saturating_sub(1)
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting || event.modifiers != KeyModifiers::NONE {
            return;
        }
        let hit = self
            .hits
            .iter()
            .find(|(r, _)| r.contains((event.column, event.row).into()))
            .map(|(_, i)| *i);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take().is_some_and(|p| Some(p) == hit) {
                    self.cursor = hit.unwrap();
                    self.horizontal = 0;
                }
            }
            _ => {}
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, t: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            self.hide();
            return;
        }
        if area.width < 36 || area.height < 14 {
            self.hide();
            return;
        }
        let width = area.width.saturating_sub(2).min(92);
        let height = area.height.saturating_sub(2).min(25);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(rect) {
            self.pressed = None;
            self.geometry = Some(rect);
        }
        self.hits.clear();
        frame.render_widget(Clear, rect);
        let title = if self.kind == Some(Kind::Thinking) {
            " Thinking "
        } else {
            " Models "
        };
        let block = Block::default()
            .title(title)
            .borders(Borders::ALL)
            .style(Style::default().fg(t.foreground).bg(t.background))
            .border_style(Style::default().fg(t.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let current = self.current_label();
        frame.render_widget(
            Paragraph::new(display_line(&current, self.horizontal)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        if let Some(menu) = &self.menu {
            let note = display_line(&menu.note, self.horizontal);
            frame.render_widget(
                Paragraph::new(note),
                Rect::new(inner.x, inner.y + 1, inner.width, 1),
            );
        }
        let list = Rect::new(
            inner.x,
            inner.y + 2,
            inner.width,
            inner.height.saturating_sub(6),
        );
        if let Some(menu) = &self.menu {
            let start = self
                .cursor
                .saturating_sub(usize::from(list.height).saturating_sub(1));
            for (index, row) in menu
                .rows
                .iter()
                .enumerate()
                .skip(start)
                .take(usize::from(list.height))
            {
                let selected = index == self.cursor;
                let shown = display_line(&row.label, if selected { self.horizontal } else { 0 });
                let line = Line::styled(
                    format!(
                        "{} {}{}",
                        if selected { ">" } else { " " },
                        if row.value == menu.current {
                            "● "
                        } else {
                            ""
                        },
                        shown
                    ),
                    Style::default()
                        .fg(if selected { t.title } else { t.foreground })
                        .bg(t.background)
                        .add_modifier(if selected {
                            Modifier::BOLD
                        } else {
                            Modifier::empty()
                        }),
                );
                let hit = Rect::new(list.x, list.y + (index - start) as u16, list.width, 1);
                frame.render_widget(Paragraph::new(line), hit);
                self.hits.push((hit, index));
            }
        }
        let status = display_line(&self.notice, self.horizontal);
        frame.render_widget(
            Paragraph::new(status),
            Rect::new(inner.x, inner.bottom().saturating_sub(3), inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new("↑↓ choose · ←→ inspect · Enter apply"),
            Rect::new(inner.x, inner.bottom().saturating_sub(2), inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new("Tab models/thinking · R refresh · Esc close"),
            Rect::new(inner.x, inner.bottom().saturating_sub(1), inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
    }
}
