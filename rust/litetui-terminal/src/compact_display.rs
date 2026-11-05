//! Local compact display. All transcript contents and source controls remain owned by the chat.
use crate::{
    live_chat::{Chat, ReplyState},
    native_activity::ResultState,
};
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ThinkingHit {
    pub row: usize,
    pub card: usize,
}

#[derive(Default)]
pub struct Profile {
    active: bool,
    initialized: bool,
    override_mode: Option<bool>,
    viewport: Rect,
    hits: Vec<(u16, ThinkingHit)>,
    pressed: Option<(u16, u16, ThinkingHit)>,
    activity: String,
    observed_at: Option<Instant>,
}
impl Profile {
    pub fn active(&self) -> bool {
        self.active
    }
    pub fn resize(&mut self, width: u16) {
        self.active = self.override_mode.unwrap_or({
            if !self.initialized {
                width < 100
            } else if self.active {
                width < 101
            } else {
                width <= 93
            }
        });
        self.initialized = true;
    }
    pub fn toggle(&mut self) {
        self.active = !self.active;
        self.override_mode = Some(self.active);
        self.clear_hits();
    }
    pub fn clear_hits(&mut self) {
        self.hits.clear();
        self.pressed = None;
    }
    pub(crate) fn set_hits(&mut self, rect: Rect, scroll: usize, headers: &[(usize, ThinkingHit)]) {
        if self.viewport != rect {
            self.pressed = None;
        }
        self.viewport = rect;
        self.hits.clear();
        for (line, hit) in headers {
            if let Some(y) = line.checked_sub(scroll)
                && y < usize::from(rect.height)
            {
                self.hits.push((rect.y + y as u16, *hit));
            }
        }
    }
    pub fn click(&mut self, event: MouseEvent) -> Option<ThinkingHit> {
        if !event.modifiers.is_empty() {
            self.pressed = None;
            return None;
        }
        let hit = self
            .hits
            .iter()
            .find(|(y, _)| {
                *y == event.row
                    && event.column >= self.viewport.x
                    && event.column < self.viewport.right()
            })
            .map(|(_, hit)| (event.column, event.row, *hit));
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                let start = self.pressed.take();
                if start.is_some() && start == hit {
                    return hit.map(|(_, _, id)| id);
                }
            }
            MouseEventKind::Drag(_) | MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.pressed = None
            }
            _ => {}
        }
        None
    }
    pub(crate) fn now(&mut self, chat: &Chat) -> [String; 3] {
        let wait = if !chat.approvals.is_empty() {
            Some("WAITING ON you · approval")
        } else if !chat.questions.is_empty() {
            Some("WAITING ON you · answer")
        } else {
            None
        };
        let active = chat.rows.iter().rev().find(|r| {
            matches!(
                r.state,
                ReplyState::Sending
                    | ReplyState::Streaming
                    | ReplyState::Cancelling
                    | ReplyState::Maintenance
                    | ReplyState::AwaitingRetirement
            )
        });
        let current = if let Some(row) = active {
            if let Some(tool) = row
                .tools
                .iter()
                .rev()
                .find(|t| matches!(t.result, ResultState::Pending))
            {
                summary(tool.name.as_deref().unwrap_or(&tool.id), 72)
            } else if row.state == ReplyState::Cancelling {
                "stopping".into()
            } else if row.state == ReplyState::Maintenance {
                "compacting conversation".into()
            } else if row.state == ReplyState::AwaitingRetirement {
                "awaiting worker retirement".into()
            } else if row
                .cards
                .last()
                .is_some_and(|c| !c.reasoning.is_empty() && c.answer.is_empty())
            {
                "thinking".into()
            } else {
                "responding".into()
            }
        } else if chat.closed {
            "disconnected".into()
        } else if chat.busy {
            "source work".into()
        } else if !chat.ready {
            "connecting".into()
        } else {
            "idle".into()
        };
        let observed = wait.unwrap_or(&current);
        if self.activity != observed || self.observed_at.is_none() {
            self.activity = observed.into();
            self.observed_at = Some(Instant::now());
        }
        let seconds = self.observed_at.map_or(0, |t| t.elapsed().as_secs());
        let top = wait.map_or_else(
            || "Task claim unavailable".to_owned(),
            |reason| format!("{reason} · {}:{:02}", seconds / 60, seconds % 60),
        );
        let now = if current == "idle" || wait.is_some() {
            format!("now  {current}")
        } else {
            format!("now  {current}  {seconds}s observed")
        };
        [
            top,
            now,
            format!(
                "last {}",
                chat.last_activity_summary.as_deref().unwrap_or("—")
            ),
        ]
    }
}
/// Keep summaries bounded even for large retained text and escape controls before presentation.
pub(crate) fn summary(text: &str, cap: usize) -> String {
    let raw: String = text.chars().take(cap).collect();
    crate::ui::display_safe(&raw)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
