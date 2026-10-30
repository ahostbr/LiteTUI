//! Owned conversation catalogue and native selection UI. No filesystem/provider I/O.
//! An open response is only a connection/history barrier: the management owner
//! must verify fresh runtime state and held input before publishing Opened.
use crate::{live::terminal_text, resume::History, theme::Theme};
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

pub const PAGE: &str = "gui.conversations.page";
pub const OPEN: &str = "gui.conversations.open";
pub const PREVIEW: &str = "gui.conversations.preview";
pub const PAGE_SIZE: usize = 50;
pub const CATALOG_LIMIT: usize = 10_000;
pub const PAGE_BYTES: usize = 256 * 1024;

#[derive(Debug, PartialEq)]
pub struct Opened {
    pub snapshot: crate::management::Snapshot,
    pub history: History,
    pub queue: crate::restored_queue::Snapshot,
    pub policy: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub session: String,
    pub agent: String,
    pub agent_name: String,
    pub title: String,
    pub message_count: u64,
    pub updated_at: f64,
    pub current: bool,
    pub can_open: bool,
    pub reason: String,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    pub revision: u64,
    pub session: String,
    pub agent: String,
    pub snapshot: String,
    pub offset: usize,
    pub total: usize,
    pub entries: Vec<Entry>,
    pub previous: Option<String>,
    pub next: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Refresh,
    Page { revision: u64, cursor: String },
    Open { revision: u64, session: String },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Page(Page),
    /// Only after correlated open + owned state + held-input verification.
    Opened {
        session: String,
    },
    Rejected(String),
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text(value: &Value, key: &str, limit: usize, empty: bool) -> io::Result<String> {
    value[key]
        .as_str()
        .filter(|s| s.len() <= limit && (empty || !s.is_empty()))
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid conversation catalogue {key}")))
}
fn identity(value: &Value, key: &str) -> io::Result<String> {
    let id = text(value, key, 36, false)?;
    // Storage accepts every canonical UUID version; execution IDs use uuid4.
    if id.len() != 36
        || !id.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
    {
        return Err(fail(
            "Conversation catalogue has a noncanonical storage identity",
        ));
    }
    Ok(id)
}
fn count(value: &Value, key: &str, limit: u64) -> io::Result<u64> {
    value[key]
        .as_u64()
        .filter(|n| *n <= limit)
        .ok_or_else(|| fail(&format!("Invalid conversation catalogue {key}")))
}
fn boolean(value: &Value, key: &str) -> io::Result<bool> {
    value[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid conversation catalogue {key}")))
}
fn cursor(value: &Value, key: &str) -> io::Result<Option<String>> {
    match value.get(key) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if !s.is_empty() && s.len() <= 1024 => Ok(Some(s.clone())),
        _ => Err(fail("Invalid or missing catalogue page cursor")),
    }
}
impl Page {
    pub fn parse(value: &Value, revision: u64, session: &str) -> io::Result<Self> {
        if serde_json::to_vec(value)?.len() > PAGE_BYTES {
            return Err(fail("Conversation page exceeds the display budget"));
        }
        let current = identity(value, "session_id")?;
        let agent = identity(value, "agent_id")?;
        if current != session {
            return Err(fail("Conversation page belongs to another active session"));
        }
        let snapshot = text(value, "snapshot", 64, false)?;
        if snapshot.len() != 64
            || !snapshot
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(fail("Invalid conversation catalogue snapshot"));
        }
        let offset = count(value, "offset", CATALOG_LIMIT as u64)? as usize;
        let total = count(value, "total", CATALOG_LIMIT as u64)? as usize;
        let rows = value["entries"]
            .as_array()
            .filter(|rows| rows.len() <= PAGE_SIZE)
            .ok_or_else(|| fail("Invalid conversation page entries"))?;
        if offset > total
            || !offset.is_multiple_of(PAGE_SIZE)
            || (offset > 0 && offset == total)
            || rows.len() != total.saturating_sub(offset).min(PAGE_SIZE)
        {
            return Err(fail("Conversation page count or offset is inconsistent"));
        }
        let previous = cursor(value, "previous_cursor")?;
        let next = cursor(value, "next_cursor")?;
        if previous.is_some() != (offset > 0)
            || next.is_some() != (offset + rows.len() < total)
            || previous.as_ref().is_some_and(|p| Some(p) == next.as_ref())
        {
            return Err(fail(
                "Conversation page navigation disagrees with its bounds",
            ));
        }
        let mut seen = HashSet::new();
        let mut entries = Vec::new();
        for row in rows {
            let id = identity(row, "session_id")?;
            let owner = identity(row, "agent_id")?;
            let current_row = boolean(row, "current")?;
            let can_open = boolean(row, "can_open")?;
            if !seen.insert(id.clone())
                || owner != agent
                || current_row != (id == current)
                || (current_row && can_open)
            {
                return Err(fail("Ambiguous or foreign conversation catalogue entry"));
            }
            let updated_at = row["updated_at"]
                .as_f64()
                .filter(|n| n.is_finite() && *n >= 0.0 && *n <= (1u64 << 53) as f64)
                .ok_or_else(|| fail("Invalid conversation update time"))?;
            let reason = text(row, "reason", 4096, true)?;
            if !can_open && reason.trim().is_empty() {
                return Err(fail("Unavailable conversation lacks a source reason"));
            }
            entries.push(Entry {
                session: id,
                agent: owner,
                agent_name: text(row, "agent_name", 1024, false)?,
                title: text(row, "title", 4096, false)?,
                message_count: count(row, "message_count", 1u64 << 53)?,
                updated_at,
                current: current_row,
                can_open,
                reason,
            });
        }
        Ok(Self {
            revision,
            session: current,
            agent,
            snapshot,
            offset,
            total,
            entries,
            previous,
            next,
        })
    }
    /// A page may replace its predecessor only for that offered cursor/snapshot.
    pub fn follows(&self, previous: &Self, requested: &str) -> io::Result<()> {
        let offset = if previous.next.as_deref() == Some(requested) {
            previous.offset + previous.entries.len()
        } else if previous.previous.as_deref() == Some(requested) {
            previous.offset.saturating_sub(PAGE_SIZE)
        } else {
            return Err(fail("Catalogue cursor was not offered by the current page"));
        };
        if self.session != previous.session
            || self.agent != previous.agent
            || self.snapshot != previous.snapshot
            || self.total != previous.total
            || self.offset != offset
        {
            return Err(fail(
                "Conversation catalogue changed while paging; refresh it",
            ));
        }
        Ok(())
    }
    pub fn selection(&self, revision: u64, session: &str) -> io::Result<&Entry> {
        if revision != self.revision {
            return Err(fail("Conversation selection changed; refresh the browser"));
        }
        self.entries
            .iter()
            .find(|row| row.session == session && row.can_open && !row.current)
            .ok_or_else(|| fail("Conversation was not offered for opening"))
    }
}

/// This checks only the source open barrier and owned history. The caller must
/// still read current state and restored input before committing the UI switch.
pub fn connected_history(value: &Value, session: &str) -> io::Result<History> {
    if value["connected"] != true {
        return Err(fail("Conversation connection has not completed"));
    }
    History::parse(value, session)
}
fn line(value: &str, offset: usize) -> String {
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
    pub notice: String,
    pub page: Option<Page>,
    selected: usize,
    horizontal: usize,
    intent: Option<Intent>,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    armed: bool,
    confirmation_visible: bool,
    pending_open: Option<String>,
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.page = None;
        self.intent = Some(Intent::Refresh);
        self.pending_open = None;
        self.notice = "Reading owned conversations…".into();
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
            // Unsent intent can be discarded; sent commands still own input.
            self.waiting = false;
            self.pending_open = None;
        }
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.pending_open = None;
        self.page = None;
    }
    fn disarm(&mut self) {
        self.armed = false;
        self.confirmation_visible = false;
        self.pressed = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.disarm();
        self.hits.clear();
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if let Update::Opened { session } = &update
            && (!self.waiting || self.pending_open.as_ref() != Some(session))
        {
            return Err(fail("Unexpected conversation-open verification"));
        }
        if matches!(update, Update::Page(_)) && self.pending_open.is_some() {
            return Err(fail("Catalogue read cannot complete a conversation open"));
        }
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.horizontal = 0;
        match update {
            Update::Page(page) => {
                self.selected = page.entries.iter().position(|row| row.current).unwrap_or(0);
                self.notice = "Choose a saved conversation. Nothing has been opened.".into();
                self.page = Some(page);
            }
            Update::Opened { .. } => {
                self.finish();
                self.notice = "Conversation and held input verified.".into();
            }
            Update::Rejected(error) => {
                self.pending_open = None;
                self.page = None;
                self.notice = error;
            }
        }
        Ok(())
    }
    fn selected(&self) -> Option<&Entry> {
        self.page
            .as_ref()
            .and_then(|page| page.entries.get(self.selected))
    }
    fn details(&self) -> String {
        self.selected()
            .map(|row| {
                format!(
                    "{} | {} | {} messages | updated {} | {}",
                    row.session,
                    row.agent_name,
                    row.message_count,
                    row.updated_at,
                    if row.can_open {
                        "Open available"
                    } else {
                        row.reason.as_str()
                    }
                )
            })
            .unwrap_or_default()
    }
    fn inspection_end(&self) -> usize {
        let details = self.details();
        let title = self.selected().map_or("", |row| row.title.as_str());
        [details.as_str(), title, self.notice.as_str()]
            .into_iter()
            .map(|text| line(text, 0).graphemes(true).count().saturating_sub(1))
            .max()
            .unwrap_or(0)
    }
    fn request_page(&mut self, next: bool) {
        self.disarm();
        if let Some(page) = &self.page
            && let Some(cursor) = if next { &page.next } else { &page.previous }
        {
            self.intent = Some(Intent::Page {
                revision: page.revision,
                cursor: cursor.clone(),
            });
            self.waiting = true;
            self.notice = "Reading the selected catalogue page…".into();
            self.hide();
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !key.modifiers.is_empty() {
            self.disarm();
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.armed {
                self.disarm();
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        match key.code {
            KeyCode::Up | KeyCode::Down | KeyCode::Home | KeyCode::End => {
                self.disarm();
                let last = self
                    .page
                    .as_ref()
                    .map_or(0, |p| p.entries.len().saturating_sub(1));
                self.selected = match key.code {
                    KeyCode::Up => self.selected.saturating_sub(1),
                    KeyCode::Down => (self.selected + 1).min(last),
                    KeyCode::Home => 0,
                    _ => last,
                };
                self.horizontal = 0;
            }
            KeyCode::Left | KeyCode::Right => {
                self.disarm();
                self.horizontal = if key.code == KeyCode::Left {
                    self.horizontal.saturating_sub(10)
                } else {
                    self.horizontal
                        .saturating_add(10)
                        .min(self.inspection_end())
                };
            }
            KeyCode::PageUp if key.kind == KeyEventKind::Press => self.request_page(false),
            KeyCode::PageDown if key.kind == KeyEventKind::Press => self.request_page(true),
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => self.open(),
            KeyCode::Char('o') if key.kind == KeyEventKind::Press => {
                self.disarm();
                self.armed = self
                    .selected()
                    .is_some_and(|row| row.can_open && !row.current);
                if !self.armed
                    && let Some(row) = self.selected()
                {
                    self.notice = row.reason.clone();
                }
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press && self.confirmation_visible => {
                if let Some(page) = &self.page
                    && let Some(row) = page.entries.get(self.selected)
                    && row.can_open
                    && !row.current
                {
                    self.intent = Some(Intent::Open {
                        revision: page.revision,
                        session: row.session.clone(),
                    });
                    self.pending_open = Some(row.session.clone());
                    self.waiting = true;
                    self.notice =
                        "Opening; verifying connection, history, ownership and held input…".into();
                    self.hide();
                }
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
            .map(|(_, index)| *index);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if self
                    .pressed
                    .take()
                    .is_some_and(|pressed| Some(pressed) == hit)
                {
                    self.selected = hit.expect("paired hit");
                    self.horizontal = 0;
                }
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.pressed = None;
                if hit.is_some() {
                    let last = self
                        .page
                        .as_ref()
                        .map_or(0, |p| p.entries.len().saturating_sub(1));
                    self.selected = if event.kind == MouseEventKind::ScrollUp {
                        self.selected.saturating_sub(3)
                    } else {
                        self.selected.saturating_add(3).min(last)
                    };
                    self.horizontal = 0;
                }
            }
            _ => self.pressed = None,
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() || area.width < 36 || area.height < 14 {
            self.hide();
            return;
        }
        let width = area.width.saturating_sub(2).min(100);
        let height = area.height.saturating_sub(2).min(30);
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
            .title(" Conversations ")
            .borders(Borders::ALL)
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let page_label = self
            .page
            .as_ref()
            .map(|page| {
                if page.total == 0 {
                    "No saved conversations.".into()
                } else {
                    format!(
                        "{}–{} of {} · saved conversations",
                        page.offset + 1,
                        page.offset + page.entries.len(),
                        page.total
                    )
                }
            })
            .unwrap_or_else(|| {
                if self.waiting {
                    "Reading runtime…".into()
                } else {
                    "Catalogue unavailable.".into()
                }
            });
        frame.render_widget(
            Paragraph::new(page_label),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new("Current: ●   Opening never sends input"),
            Rect::new(inner.x, inner.y + 1, inner.width, 1),
        );
        let list = Rect::new(
            inner.x,
            inner.y + 2,
            inner.width,
            inner.height.saturating_sub(7),
        );
        if let Some(page) = &self.page {
            let start = self
                .selected
                .saturating_sub(usize::from(list.height).saturating_sub(1));
            for (index, row) in page
                .entries
                .iter()
                .enumerate()
                .skip(start)
                .take(usize::from(list.height))
            {
                let selected = index == self.selected;
                let text = format!(
                    "{}{} {}",
                    if selected { ">" } else { " " },
                    if row.current { "●" } else { " " },
                    line(&row.title, if selected { self.horizontal } else { 0 })
                );
                let style = Style::default()
                    .fg(if selected {
                        theme.title
                    } else {
                        theme.foreground
                    })
                    .add_modifier(if selected {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    });
                let hit = Rect::new(list.x, list.y + (index - start) as u16, list.width, 1);
                frame.render_widget(Paragraph::new(Line::styled(text, style)), hit);
                self.hits.push((hit, index));
            }
        }
        let footer = inner.bottom() - 5;
        if self.armed {
            let id = self
                .selected()
                .map(|row| row.session.as_str())
                .unwrap_or("");
            let ids = crate::ui::wrap(id, inner.width as usize);
            frame.render_widget(
                Paragraph::new("Open this conversation?"),
                Rect::new(inner.x, footer, inner.width, 1),
            );
            frame.render_widget(
                Paragraph::new(ids.join("\n")),
                Rect::new(inner.x, footer + 1, inner.width, 2),
            );
            frame.render_widget(
                Paragraph::new("Enter: open | Esc: cancel"),
                Rect::new(inner.x, footer + 3, inner.width, 1),
            );
            self.confirmation_visible =
                self.open && !self.waiting && !id.is_empty() && ids.len() <= 2;
        } else {
            for (offset, text) in [
                line(&self.details(), self.horizontal),
                "↑↓ choose | ←→ inspect".into(),
                "PgUp/PgDn page | R refresh".into(),
                "O open | Esc close".into(),
            ]
            .into_iter()
            .enumerate()
            {
                frame.render_widget(
                    Paragraph::new(text),
                    Rect::new(inner.x, footer + offset as u16, inner.width, 1),
                );
            }
        }
        frame.render_widget(
            Paragraph::new(line(&self.notice, self.horizontal)),
            Rect::new(inner.x, footer + 4, inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
    }
}
