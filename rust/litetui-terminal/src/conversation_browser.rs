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
    /// The row Python's /resume picker shows (conversation_page._picker_label), else the title.
    pub label: String,
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
    /// /resume's first picker: every ready owned agent (id, name).
    pub agents: Vec<(String, String)>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    ConfirmOpen { snapshot: String, action_id: String },
    Action(crate::conversation_actions::Intent),
    Refresh,
    Page { revision: u64, cursor: String },
    Open { revision: u64, session: String },
    Inspect { revision: u64, session: String },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Preview(Box<crate::history::Snapshot>),
    Action(crate::conversation_actions::Update),
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
            let title = text(row, "title", 4096, false)?;
            let label = match row.get("picker_label") {
                None => title.clone(),
                Some(_) => text(row, "picker_label", 4096, false)?,
            };
            entries.push(Entry {
                session: id,
                agent: owner,
                agent_name: text(row, "agent_name", 1024, false)?,
                label,
                title,
                message_count: count(row, "message_count", 1u64 << 53)?,
                updated_at,
                current: current_row,
                can_open,
                reason,
            });
        }
        let agents = match value.get("agents") {
            None => Vec::new(),
            Some(list) => list
                .as_array()
                .filter(|l| l.len() <= 256)
                .ok_or_else(|| fail("Invalid agent list"))?
                .iter()
                .map(|pair| match pair.as_array().map(Vec::as_slice) {
                    Some([Value::String(id), Value::String(name)])
                        if !name.is_empty() && name.len() <= 1024 =>
                    {
                        identity(&serde_json::json!({ "id": id }), "id")
                            .map(|id| (id, name.clone()))
                    }
                    _ => Err(fail("Invalid agent list entry")),
                })
                .collect::<io::Result<Vec<_>>>()?,
        };
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
            agents,
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
    /// Inspection proves row membership only; it never grants Open authority.
    pub fn inspection(&self, revision: u64, session: &str) -> io::Result<&Entry> {
        if revision != self.revision {
            return Err(fail("Conversation selection changed; refresh the browser"));
        }
        self.entries
            .iter()
            .find(|row| row.session == session)
            .ok_or_else(|| fail("Conversation was not offered for inspection"))
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
    pub actions: crate::conversation_actions::View,
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub page: Option<Page>,
    selected: usize,
    horizontal: usize,
    intent: Option<Intent>,
    inspection_pending: bool,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    armed: bool,
    confirmation_visible: bool,
    pending_open: Option<String>,
    preview: Option<Box<crate::history::Snapshot>>,
    preview_page: usize,
    preview_pages: usize,
    preview_seen: Vec<bool>,
    preselect: Option<String>,
    /// Python's two pickers: an agent, then one of its conversations.
    stage: Stage,
    agent_selected: usize,
    /// The picker's first visible wrapped line (picker_box keeps the cursor in view).
    scroll: usize,
    /// A page read for PgUp/PgDn keeps the conversation picker.
    paging: bool,
    /// Why a pick did nothing, shown in the picker.
    line: Option<String>,
    /// Another agent was picked: the window relaunches as it.
    switch_to: Option<String>,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum Stage {
    #[default]
    Agents,
    Conversations,
}
impl View {
    /// A conversation id prefix from a command only moves the row cursor on the
    /// first page. Opening still needs `o`, the history review and Enter.
    pub fn preselect(&mut self, prefix: &str) {
        if self.open && self.waiting && !prefix.trim().is_empty() {
            self.preselect = Some(prefix.trim().to_owned());
        }
    }
    /// Python's `/resume <id>` (_cmd_resume): the first conversation whose id starts with the
    /// argument is opened; none says so. True when the caller must open the selection.
    fn apply_preselection(&mut self, page: &Page) -> bool {
        let Some(prefix) = self.preselect.take() else {
            return false;
        };
        let Some(index) = page
            .entries
            .iter()
            .position(|row| row.session.starts_with(&prefix))
        else {
            self.line = Some(format!(
                "No conversation matches '{prefix}'. Run /resume with no argument to pick one."
            ));
            return false;
        };
        self.selected = index;
        let row = &page.entries[index];
        if row.can_open && !row.current {
            return true;
        }
        self.line = Some(row.reason.clone());
        false
    }
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.preselect = None;
        self.inspection_pending = false;
        self.waiting = true;
        self.page = None;
        self.intent = Some(Intent::Refresh);
        self.pending_open = None;
        self.preview = None;
        self.notice = "Reading owned conversations…".into();
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting || self.actions.owns_input()
    }
    /// The agent the /resume picker chose to switch this window to, once.
    pub fn take_switch(&mut self) -> Option<String> {
        self.switch_to.take()
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.actions
            .take_intent()
            .map(Intent::Action)
            .or_else(|| self.intent.take())
    }
    pub fn paste(&mut self, text: &str) {
        self.actions.paste(text);
    }
    pub fn action_completed(
        &mut self,
        changed: &crate::conversation_actions::Changed,
    ) -> io::Result<()> {
        self.actions
            .complete(changed.action, &changed.snapshot.session)?;
        self.page = Some(changed.page.clone());
        self.selected = self
            .page
            .as_ref()
            .unwrap()
            .entries
            .iter()
            .position(|e| e.current)
            .unwrap_or(0);
        self.pending_open = None;
        self.waiting = false;
        self.hide();
        if changed.action == crate::conversation_actions::Action::New {
            self.finish();
        } else {
            self.notice = format!("Conversation renamed: {}", changed.current.title);
        }
        Ok(())
    }
    pub fn close(&mut self) {
        self.preselect = None;
        self.inspection_pending = false;
        self.actions.close();
        self.open = false;
        self.preview = None;
        self.hide();
        if self.intent.take().is_some() {
            // Unsent intent can be discarded; sent commands still own input.
            self.waiting = false;
            self.pending_open = None;
        }
    }
    pub fn finish(&mut self) {
        self.actions.finish();
        self.close();
        self.waiting = false;
        self.pending_open = None;
        self.page = None;
    }
    /// Preserve the catalogue and selection underneath the exclusive inspector.
    pub fn suspend_for_inspection(&mut self) {
        if !self.inspection_pending {
            self.hide();
            return;
        }
        self.inspection_pending = false;
        self.intent = None;
        self.waiting = false;
        self.pending_open = None;
        self.preview = None;
        self.hide();
    }
    fn disarm(&mut self) {
        // A new selection or arming clears the last pick's reason.
        self.line = None;
        self.preview_page = 0;
        self.preview_seen.clear();
        self.armed = false;
        self.confirmation_visible = false;
        self.pressed = None;
    }
    pub fn hide(&mut self) {
        self.actions.hide();
        self.visible = false;
        self.disarm();
        self.hits.clear();
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        if let Update::Action(update) = update {
            self.actions.observe(update);
            self.notice = self.actions.notice.clone();
            return Ok(());
        }
        if let Update::Opened { session } = &update
            && (!self.waiting || self.pending_open.as_ref() != Some(session))
        {
            return Err(fail("Unexpected conversation-open verification"));
        }
        if matches!(update, Update::Page(_)) && self.pending_open.is_some() {
            return Err(fail("Catalogue read cannot complete a conversation open"));
        }
        self.waiting = false;
        self.inspection_pending = false;
        self.intent = None;
        self.hide();
        self.horizontal = 0;
        match update {
            Update::Preview(preview) => {
                if self.open {
                    if self.pending_open.as_ref() != Some(&preview.identity.session) {
                        return Err(fail("Preview changed the selected conversation"));
                    }
                    if preview.read_only {
                        // Refused by the source's check: say why, open nothing.
                        self.pending_open = None;
                        self.line =
                            Some("Saved history is read-only; opening is unavailable.".into());
                        self.notice =
                            "Saved history is read-only; opening is unavailable. Current conversation is unchanged."
                                .into();
                    } else {
                        // Python opens on the pick: the reviewed open is confirmed at once.
                        self.intent = Some(Intent::ConfirmOpen {
                            snapshot: preview.token.clone(),
                            action_id: preview.open_action_id.clone().ok_or_else(|| {
                                fail("Conversation preview lacks its open action")
                            })?,
                        });
                        self.waiting = true;
                        self.notice =
                            "Opening reviewed conversation; waiting for lifecycle and owned readback…"
                                .into();
                    }
                } else {
                    self.pending_open = None;
                }
            }
            Update::Action(_) => unreachable!("handled action"),
            Update::Page(page) => {
                self.selected = page.entries.iter().position(|row| row.current).unwrap_or(0);
                // `/resume <id>` names a conversation of this agent: Python skips the agent picker.
                let named = self.preselect.is_some();
                if !std::mem::take(&mut self.paging) && !named {
                    self.stage = if page.agents.is_empty() {
                        Stage::Conversations
                    } else {
                        Stage::Agents
                    };
                    self.agent_selected = page
                        .agents
                        .iter()
                        .position(|(id, _)| *id == page.agent)
                        .unwrap_or(0);
                }
                if named {
                    self.stage = Stage::Conversations;
                }
                self.line = None;
                self.notice = "Choose a saved conversation. Nothing has been opened.".into();
                let open = self.apply_preselection(&page);
                self.page = Some(page);
                if open {
                    self.open_selected();
                }
            }
            Update::Opened { .. } => {
                self.finish();
                self.notice = "Conversation and held input verified.".into();
            }
            Update::Rejected(error) => {
                self.preselect = None;
                self.pending_open = None;
                self.preview = None;
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
            self.paging = true;
            self.waiting = true;
            self.notice = "Reading the selected catalogue page…".into();
            self.hide();
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if self.actions.owns_input() {
            self.actions.key(key);
            return;
        }
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !key.modifiers.is_empty() {
            self.disarm();
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if !self.waiting && self.preview.is_some() {
                self.preview = None;
                self.pending_open = None;
                self.disarm();
            } else if self.armed {
                self.disarm();
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if let Some(preview) = &self.preview {
            match key.code {
                KeyCode::PageDown if self.preview_seen.get(self.preview_page) == Some(&true) => {
                    self.preview_page =
                        (self.preview_page + 1).min(self.preview_pages.saturating_sub(1));
                    self.visible = false;
                    self.confirmation_visible = false;
                }
                KeyCode::PageUp => {
                    self.preview_page = self.preview_page.saturating_sub(1);
                    self.visible = false;
                    self.confirmation_visible = false;
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && self.confirmation_visible
                        && !preview.read_only =>
                {
                    self.intent = Some(Intent::ConfirmOpen {
                        snapshot: preview.token.clone(),
                        action_id: preview.open_action_id.clone().expect("preview action"),
                    });
                    self.waiting = true;
                    self.notice =
                        "Opening reviewed conversation; waiting for lifecycle and owned readback…"
                            .into();
                    self.hide();
                }
                _ => self.disarm(),
            }
            return;
        }
        if self.stage == Stage::Agents {
            let last = self
                .page
                .as_ref()
                .map_or(0, |p| p.agents.len().saturating_sub(1));
            match key.code {
                KeyCode::Up => self.agent_selected = self.agent_selected.saturating_sub(1),
                KeyCode::Down => self.agent_selected = (self.agent_selected + 1).min(last),
                KeyCode::Home => self.agent_selected = 0,
                KeyCode::End => self.agent_selected = last,
                KeyCode::Enter if key.kind == KeyEventKind::Press => self.agent_enter(),
                KeyCode::Char('r') if key.kind == KeyEventKind::Press => self.open(),
                _ => {}
            }
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
            KeyCode::Char('n' | 'e') if key.kind == KeyEventKind::Press => {
                self.disarm();
                self.actions.open(if key.code == KeyCode::Char('n') {
                    crate::conversation_actions::Action::New
                } else {
                    crate::conversation_actions::Action::Rename
                });
            }
            KeyCode::Char('i') if key.kind == KeyEventKind::Press => {
                self.disarm();
                if let Some(page) = &self.page
                    && let Some(row) = page.entries.get(self.selected)
                {
                    self.inspection_pending = true;
                    self.intent = Some(Intent::Inspect {
                        revision: page.revision,
                        session: row.session.clone(),
                    });
                    self.waiting = true;
                    self.notice =
                        "Inspecting saved display; the current conversation stays unchanged."
                            .into();
                    self.hide();
                }
            }
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
                    self.notice = "Inspecting the selected history before opening…".into();
                    self.hide();
                }
            }
            // "Enter or click to select": Python opens the picked conversation at once
            // (Sentinel 5eb26818: 1:1, no question, no review screen).
            KeyCode::Enter if key.kind == KeyEventKind::Press && !self.armed => {
                self.open_selected()
            }
            _ => {}
        }
    }
    /// The agent picker's pick: this agent's conversations, as Python's second picker.
    fn agent_enter(&mut self) {
        let Some(page) = &self.page else {
            return;
        };
        let Some((id, name)) = page.agents.get(self.agent_selected) else {
            return;
        };
        if *id != page.agent {
            // Python relaunches the window as that agent (convo._switch_window_to).
            self.switch_to = Some(name.clone());
            self.close();
            return;
        }
        if page.entries.is_empty() {
            self.line = Some("Nothing to resume.".into());
            return;
        }
        self.selected = page.entries.iter().position(|e| e.current).unwrap_or(0);
        self.stage = Stage::Conversations;
        self.line = None;
    }
    /// Open the highlighted conversation. The source's checks still run (the open is refused
    /// with its reason when they fail) and its history review is confirmed when it arrives.
    fn open_selected(&mut self) {
        let Some(page) = &self.page else {
            return;
        };
        let Some(row) = page.entries.get(self.selected) else {
            return;
        };
        if !row.can_open || row.current {
            self.line = Some(row.reason.clone());
            return;
        }
        self.intent = Some(Intent::Open {
            revision: page.revision,
            session: row.session.clone(),
        });
        self.pending_open = Some(row.session.clone());
        self.waiting = true;
        self.notice = "Opening the selected conversation…".into();
        self.hide();
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if self.actions.owns_input() {
            self.actions.mouse(event);
            return;
        }
        self.armed = false;
        self.confirmation_visible = false;
        if self.preview.is_some() {
            self.disarm();
            return;
        }
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
                    let index = hit.expect("paired hit");
                    self.horizontal = 0;
                    if self.stage == Stage::Agents {
                        // Choosing an agent opens nothing, so a click picks it as in Python.
                        self.agent_selected = index;
                        self.agent_enter();
                    } else {
                        // "Enter or click to select": a click opens, as in Python.
                        self.selected = index;
                        self.open_selected();
                    }
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
    /// Python's /resume pickers (picker_box): "Choose an agent", then "Resume a conversation".
    fn draw_picker(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        // Python opens each picker once its rows exist.
        if !self.owns_input() || self.waiting {
            self.hide();
            return;
        }
        let (title, rows, highlighted): (&str, Vec<String>, usize) = match (&self.page, self.stage)
        {
            (Some(page), Stage::Agents) => (
                "Choose an agent",
                page.agents.iter().map(|(_, name)| name.clone()).collect(),
                self.agent_selected,
            ),
            (Some(page), Stage::Conversations) => (
                "Resume a conversation",
                // Titles are source text: control characters stay literal.
                page.entries.iter().map(|e| line(&e.label, 0)).collect(),
                self.selected,
            ),
            (None, _) => ("Choose an agent", Vec::new(), 0),
        };
        let mut extra = Vec::new();
        let why = self
            .line
            .clone()
            .or_else(|| self.page.is_none().then(|| self.notice.clone()))
            .filter(|l| !l.is_empty());
        if let Some(why) = why {
            extra.push((why, Style::default().fg(theme.error)));
        }
        let question = self
            .armed
            .then(|| self.selected().map(|row| row.session.clone()))
            .flatten();
        if let Some(id) = &question {
            let plain = Style::default().fg(theme.foreground);
            extra.push((String::new(), plain));
            extra.push((
                "Open this conversation?".into(),
                plain.add_modifier(Modifier::BOLD),
            ));
            extra.push((id.clone(), plain));
            extra.push(("Enter: open | Esc: cancel".into(), plain));
        }
        let drawn = crate::picker_box::draw(
            frame,
            area,
            theme,
            title,
            &rows,
            highlighted,
            &mut self.scroll,
            &extra,
            crate::picker_box::HINT,
        );
        if self.geometry != Some(area) {
            self.disarm();
            self.geometry = Some(area);
        }
        self.hits = drawn.hits;
        self.visible = self.open && !self.waiting && drawn.rect.width > 0;
        self.confirmation_visible = self.visible && question.is_some();
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if self.actions.owns_input() {
            self.actions.draw(frame, area, theme);
            return;
        }
        if self.preview.is_none() {
            return self.draw_picker(frame, area, theme);
        }
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
        if let Some(preview) = &self.preview {
            let effects = preview.open_effects.as_ref().expect("preview effects");
            let text = if preview.read_only {
                format!(
                    "Saved conversation: {}\n{} saved messages.\n\nThis saved history is read-only; opening is unavailable.\n\nYour current conversation and composer draft remain unchanged.\nPress Esc to return to the catalogue.",
                    line(&preview.title, 0),
                    preview.counts.messages
                )
            } else {
                format!(
                    "Open saved conversation: {}\n{} saved messages; no prompt will be sent.\n\nLifecycle hooks: {}\nProvider cleanup: {}\nNo new conversation is created; restored input stays held\n\nHook tools still require existing source authorization; this confirmation grants none.\nThe composer draft stays intact.\n\nRead every page, then press Enter to open once.",
                    line(&preview.title, 0),
                    preview.counts.messages,
                    if effects.lifecycle_hooks {
                        "configured leave/resume hooks will run"
                    } else {
                        "none configured"
                    },
                    if effects.provider_cleanup {
                        "outgoing provider session will be closed"
                    } else {
                        "not required"
                    }
                )
            };
            let lines = crate::ui::wrap(&text, inner.width as usize);
            let rows = usize::from(inner.height.saturating_sub(3)).max(1);
            self.preview_pages = lines.len().div_ceil(rows).max(1);
            if self.preview_seen.len() != self.preview_pages {
                self.preview_seen = vec![false; self.preview_pages];
                self.preview_page = 0;
            }
            self.preview_page = self.preview_page.min(self.preview_pages - 1);
            self.preview_seen[self.preview_page] = true;
            frame.render_widget(
                Paragraph::new(
                    lines
                        .iter()
                        .skip(self.preview_page * rows)
                        .take(rows)
                        .cloned()
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                Rect::new(inner.x, inner.y, inner.width, rows as u16),
            );
            self.confirmation_visible = self.open
                && !preview.read_only
                && !self.waiting
                && self.preview_page + 1 == self.preview_pages
                && self.preview_seen.iter().all(|v| *v);
            frame.render_widget(
                Paragraph::new(format!(
                    "Page {}/{} · PgUp/PgDn\n{} · Esc cancels",
                    self.preview_page + 1,
                    self.preview_pages,
                    if preview.read_only {
                        "Read-only; Open unavailable"
                    } else if self.confirmation_visible {
                        "Enter: open once"
                    } else {
                        "Read all pages"
                    }
                )),
                Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
            );
            self.visible = self.open && !self.waiting;
            return;
        }
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
            Paragraph::new("Current: ● | ←→ row details"),
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
                "↑↓ choose | I inspect".into(),
                "PgUp/PgDn page | R refresh".into(),
                "O open | N new | E rename | Esc".into(),
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
