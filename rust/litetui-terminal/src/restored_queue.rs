//! Source-owned held-input inspection and explicit cancel/continue intents.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::Value;
use std::{collections::HashSet, io};

pub const RECONCILE: &str = "gui.restored_input.reconcile";
pub const READ: &str = "gui.restored_input.list";
pub const CONTINUE: &str = "gui.restored_input.continue";
pub const SETTLED: &str = "gui.restored_input.settled";
pub const CANCEL: &str = "gui.restored_input.cancel";
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Open,
    Cancel { revision: u64, entry: String },
    Continue { revision: u64, entry: String },
    Reconcile { revision: u64, entry: String },
}
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub id: String,
    pub provider: String,
    pub segment: String,
    pub workspace: String,
    pub uncertain_from: String,
    pub state: String,
    pub content: String,
    pub source: String,
    pub profile: String,
    pub thread: String,
    pub turn: String,
    pub revision: u64,
    pub can_cancel: bool,
    pub can_continue: bool,
    pub can_reconcile: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub revision: u64,
    pub token: String,
    pub session: String,
    pub backend: String,
    pub model: String,
    pub entries: Vec<Entry>,
    pub message: Option<String>,
    /// Set only after a verified mutation receipt and independent queue readback.
    pub recovered: Option<Box<Entry>>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Snapshot(Snapshot),
    Rejected(String),
    ContinuationRejected(String),
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text(v: &Value, key: &str, limit: usize, empty: bool) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| s.len() <= limit && (empty || !s.is_empty()))
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid recovered input {key}")))
}
impl Snapshot {
    pub fn parse(v: &Value, revision: u64) -> io::Result<Self> {
        if serde_json::to_vec(v)?.len() > 262400 {
            return Err(fail("Recovered input budget exceeded"));
        }
        if v["policy"] != "hold" {
            return Err(fail("Recovered input policy changed"));
        }
        let token = text(v, "snapshot", 64, false)?;
        if token.len() != 64
            || !token
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(fail("Invalid recovered input snapshot token"));
        }
        let rows = v["entries"]
            .as_array()
            .filter(|rows| rows.len() <= 1000)
            .ok_or_else(|| fail("Invalid recovered input entries"))?;
        if v["held"].as_u64() != Some(rows.len() as u64) {
            return Err(fail("Recovered input count mismatch"));
        }
        let mut seen = HashSet::new();
        let mut entries = Vec::new();
        for row in rows {
            let id = text(row, "id", 128, false)?;
            if !seen.insert(id.clone()) {
                return Err(fail("Duplicate recovered input identity"));
            }
            let provider = match row.get("provider") {
                None => "codex".to_owned(),
                Some(_) => text(row, "provider", 32, false)?,
            };
            let optional = |key, limit| {
                if row.get(key).is_some() {
                    text(row, key, limit, true)
                } else {
                    Ok(String::new())
                }
            };
            let segment = optional("segment_id", 128)?;
            let workspace = optional("workspace", 4096)?;
            let uncertain_from = optional("uncertain_from", 32)?;
            let state = text(row, "state", 32, false)?;
            let valid = match provider.as_str() {
                "codex" => matches!(
                    state.as_str(),
                    "queued"
                        | "next_turn"
                        | "admitting"
                        | "admitted"
                        | "sending"
                        | "uncertain"
                        | "accepted"
                ),
                "claude" => {
                    !segment.is_empty() && matches!(state.as_str(), "prepared" | "uncertain")
                }
                _ => false,
            };
            if !valid {
                return Err(fail("Unknown recovered input state"));
            }
            let can_cancel = row["can_cancel"]
                .as_bool()
                .ok_or_else(|| fail("Missing cancellation policy"))?;
            if can_cancel != matches!(state.as_str(), "queued" | "next_turn" | "prepared") {
                return Err(fail(
                    "Recovered input cancellation policy disagrees with delivery evidence",
                ));
            }
            let can_continue = match row.get("can_continue") {
                None => false,
                Some(v) => v
                    .as_bool()
                    .ok_or_else(|| fail("Invalid continuation policy"))?,
            };
            if can_continue
                && (!can_cancel
                    || !entries.is_empty()
                    || v["backend"] != "codex"
                    || provider != "codex")
            {
                return Err(fail(
                    "Continuation policy disagrees with queue order or delivery evidence",
                ));
            }
            let can_reconcile = match row.get("can_reconcile") {
                None => false,
                Some(v) => v
                    .as_bool()
                    .ok_or_else(|| fail("Invalid reconciliation policy"))?,
            };
            if can_reconcile
                && (can_cancel
                    || provider != "codex"
                    || v["backend"] != "codex"
                    || row["thread_id"].as_str().is_none_or(str::is_empty)
                    || row["turn_id"].as_str().is_none_or(str::is_empty))
            {
                return Err(fail(
                    "Reconciliation policy disagrees with provider identity",
                ));
            }
            entries.push(Entry {
                id,
                provider,
                segment,
                workspace,
                uncertain_from,
                state,
                can_cancel,
                can_continue,
                can_reconcile,
                revision: row["revision"]
                    .as_u64()
                    .filter(|v| *v < (1u64 << 53))
                    .ok_or_else(|| fail("Invalid recovered input revision"))?,
                content: text(row, "content", 65536, true)?,
                source: text(row, "source", 128, true)?,
                profile: text(row, "tool_profile", 128, true)?,
                thread: text(row, "thread_id", 128, true)?,
                turn: text(row, "turn_id", 128, true)?,
            });
        }
        Ok(Self {
            revision,
            token,
            session: text(v, "session_id", 128, false)?,
            backend: text(v, "backend", 64, false)?,
            model: text(v, "model", 1024, false)?,
            entries,
            message: None,
            recovered: None,
        })
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Action {
    Cancel,
    Continue,
    Reconcile,
}
#[derive(Default)]
pub struct View {
    open: bool,
    waiting: bool,
    snapshot: Option<Snapshot>,
    cursor: usize,
    scroll: usize,
    max_scroll: usize,
    intent: Option<Intent>,
    visible: bool,
    geometry: Option<Rect>,
    armed: Option<Action>,
    confirmation_visible: bool,
    pub notice: String,
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.snapshot = None;
        self.cursor = 0;
        self.scroll = 0;
        self.intent = Some(Intent::Open);
        self.notice = "Reading held input…".into();
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn selection_text(&self, intent: &Intent) -> io::Result<&str> {
        if let Intent::Continue { revision, entry } | Intent::Reconcile { revision, entry } = intent
            && let Some(snapshot) = &self.snapshot
            && snapshot.revision == *revision
            && let Some(row) = snapshot.entries.iter().find(|e| {
                e.id == *entry
                    && if matches!(intent, Intent::Continue { .. }) {
                        e.can_continue
                    } else {
                        e.can_reconcile
                    }
            })
        {
            return Ok(&row.content);
        }
        Err(fail("Continuation selection is unavailable"))
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
        self.snapshot = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.armed = None;
        self.confirmation_visible = false;
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.cursor = 0;
        self.scroll = 0;
        match update {
            Update::Snapshot(snapshot) => {
                self.notice = snapshot
                    .message
                    .clone()
                    .unwrap_or_else(|| "Saved queue verified. Nothing replayed.".into());
                self.snapshot = Some(snapshot);
            }
            Update::Rejected(message) | Update::ContinuationRejected(message) => {
                self.snapshot = None;
                self.notice = message;
            }
        }
    }
    fn disarm(&mut self) {
        self.armed = None;
        self.confirmation_visible = false;
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !key.modifiers.is_empty() {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.armed.is_some() {
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
            KeyCode::Up | KeyCode::Down => {
                self.disarm();
                self.cursor = if key.code == KeyCode::Up {
                    self.cursor.saturating_sub(1)
                } else {
                    (self.cursor + 1).min(
                        self.snapshot
                            .as_ref()
                            .map_or(0, |s| s.entries.len().saturating_sub(1)),
                    )
                };
                self.scroll = 0;
            }
            KeyCode::PageUp | KeyCode::PageDown => {
                self.disarm();
                self.scroll = if key.code == KeyCode::PageUp {
                    self.scroll.saturating_sub(5)
                } else {
                    self.scroll.saturating_add(5).min(self.max_scroll)
                };
            }
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => self.open(),
            KeyCode::Delete if key.kind == KeyEventKind::Press => {
                self.disarm();
                if self
                    .snapshot
                    .as_ref()
                    .and_then(|s| s.entries.get(self.cursor))
                    .is_some_and(|e| e.can_cancel)
                {
                    self.armed = Some(Action::Cancel);
                }
            }
            KeyCode::Char('c') if key.kind == KeyEventKind::Press => {
                self.disarm();
                if self
                    .snapshot
                    .as_ref()
                    .and_then(|s| s.entries.get(self.cursor))
                    .is_some_and(|e| e.can_continue)
                {
                    self.armed = Some(Action::Continue);
                }
            }
            KeyCode::Char('h') if key.kind == KeyEventKind::Press => {
                self.disarm();
                if self
                    .snapshot
                    .as_ref()
                    .and_then(|s| s.entries.get(self.cursor))
                    .is_some_and(|e| e.can_reconcile)
                {
                    self.armed = Some(Action::Reconcile);
                }
            }
            KeyCode::Enter
                if key.kind == KeyEventKind::Press
                    && self.armed.is_some()
                    && self.confirmation_visible =>
            {
                if let Some(snapshot) = &self.snapshot
                    && let Some(entry) = snapshot.entries.get(self.cursor)
                {
                    let intent = match self.armed {
                        Some(Action::Cancel) if entry.can_cancel => Intent::Cancel {
                            revision: snapshot.revision,
                            entry: entry.id.clone(),
                        },
                        Some(Action::Continue) if entry.can_continue => Intent::Continue {
                            revision: snapshot.revision,
                            entry: entry.id.clone(),
                        },
                        Some(Action::Reconcile) if entry.can_reconcile => Intent::Reconcile {
                            revision: snapshot.revision,
                            entry: entry.id.clone(),
                        },
                        _ => return,
                    };
                    self.intent = Some(intent);
                    self.waiting = true;
                    self.notice = "Waiting for runtime verification…".into();
                    self.hide();
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        self.disarm();
        if !self.visible || self.waiting || !event.modifiers.is_empty() {
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(3),
            MouseEventKind::ScrollDown => {
                self.scroll = self.scroll.saturating_add(3).min(self.max_scroll)
            }
            _ => {}
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() || area.width < 36 || area.height < 14 {
            self.hide();
            return;
        }
        let width = area.width.saturating_sub(2).min(100);
        let height = area.height.saturating_sub(2).min(32);
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
        frame.render_widget(Clear, rect);
        let block = Block::default()
            .title(" Held input ")
            .borders(Borders::ALL)
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let count = self.snapshot.as_ref().map_or(0, |s| s.entries.len());
        let selected = self
            .snapshot
            .as_ref()
            .and_then(|s| s.entries.get(self.cursor));
        let body = match selected {
            Some(e) => format!(
                "Entry {}/{} | {} | revision {}\nID: {}\nProvider: {}\nSource: {} | profile: {}\nThread: {}\nTurn: {}\nSegment: {}\nWorkspace: {}\nUncertain from: {}\n{}\n\n{}",
                self.cursor + 1,
                count,
                e.state,
                e.revision,
                e.id,
                e.provider,
                e.source,
                e.profile,
                e.thread,
                e.turn,
                e.segment,
                e.workspace,
                e.uncertain_from,
                if e.can_continue {
                    "Continuation or cancellation available."
                } else if e.can_cancel {
                    "Cancellation available; no provider submission."
                } else if e.can_reconcile {
                    "Check provider history; no input will be sent."
                } else {
                    "Delivery evidence must be reconciled. Cancellation unavailable."
                },
                e.content
            ),
            None => {
                if self.waiting {
                    "Waiting for runtime…".into()
                } else if self.snapshot.is_some() {
                    "No held inputs.".into()
                } else {
                    "Held input unavailable.".into()
                }
            }
        };
        // Include complete source diagnostics in the scrollable body as well.
        let body = format!("{body}\n\nStatus: {}", self.notice);
        // Escape before wrapping; source control bytes never reach the terminal.
        let lines = crate::ui::wrap(
            &terminal_text(&body).replace('\t', "\\t"),
            inner.width as usize,
        );
        let body_height = inner.height.saturating_sub(4);
        self.max_scroll = lines.len().saturating_sub(body_height as usize);
        self.scroll = self.scroll.min(self.max_scroll);
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(self.scroll)
                    .take(body_height as usize)
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            Rect::new(inner.x, inner.y, inner.width, body_height),
        );
        let help = if self.armed == Some(Action::Continue) {
            "Enter: confirm continuation | Esc: keep held"
        } else if self.armed == Some(Action::Reconcile) {
            "Enter: check history | Esc: keep held"
        } else if self.armed.is_some() {
            "Enter: confirm cancellation | Esc: keep input"
        } else {
            "Up/Down: entry | PgUp/PgDn: inspect"
        };
        frame.render_widget(
            Paragraph::new(help),
            Rect::new(inner.x, inner.bottom() - 4, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(if self.armed == Some(Action::Continue) {
                "Run this saved input with its source authority?"
            } else if self.armed == Some(Action::Reconcile) {
                "Read history; never resend?"
            } else if self.armed.is_some() {
                "Cancel this saved entry?"
            } else {
                "C: continue | H: history | Del: cancel | R: refresh"
            }),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(terminal_text(&self.notice)),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
        );
        self.visible = self.open && !self.waiting;
        self.confirmation_visible = self.visible && self.armed.is_some();
    }
}
