//! Local compact display. All transcript contents and source controls remain owned by the chat.
use crate::{
    live_chat::{Chat, ReplyState},
    native_activity::ResultState,
};
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;
use std::time::Instant;

/// What a click on a transcript header row acts on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    /// The "Thinking" block of one assistant card.
    Reasoning(usize),
    /// The title row of one assistant card: fold or unfold it.
    Card(usize),
    /// The first line of the prompt: fold or unfold it.
    Prompt,
    /// "Speak" on a folded, completed assistant card: opens the reviewed saved history.
    Speak,
    /// The attached-image line: opens the reviewed saved history.
    Image,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HeaderHit {
    pub row: usize,
    pub target: Target,
}
/// A clickable transcript line. `span` narrows the hit to columns (relative to the transcript
/// viewport) inside the line; without it the whole line is the target.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Header {
    pub line: usize,
    pub span: Option<(u16, u16)>,
    pub hit: HeaderHit,
}

/// A header hit placed on a screen row; `span` is in absolute screen columns.
struct Placed {
    row: u16,
    span: Option<(u16, u16)>,
    hit: HeaderHit,
}
#[derive(Default)]
pub struct Profile {
    active: bool,
    initialized: bool,
    override_mode: Option<bool>,
    viewport: Rect,
    hits: Vec<Placed>,
    pressed: Option<(u16, u16, HeaderHit)>,
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
    pub(crate) fn set_hits(&mut self, rect: Rect, scroll: usize, headers: &[Header]) {
        if self.viewport != rect {
            self.pressed = None;
        }
        self.viewport = rect;
        self.hits.clear();
        for header in headers {
            if let Some(y) = header.line.checked_sub(scroll)
                && y < usize::from(rect.height)
            {
                let span = header
                    .span
                    .map(|(from, to)| (rect.x.saturating_add(from), rect.x.saturating_add(to)));
                self.hits.push(Placed {
                    row: rect.y + y as u16,
                    span,
                    hit: header.hit,
                });
            }
        }
    }
    pub fn click(&mut self, event: MouseEvent) -> Option<HeaderHit> {
        if !event.modifiers.is_empty() {
            self.pressed = None;
            return None;
        }
        // Narrow hits are pushed before the line's own hit, so the first match is the most specific.
        let hit = self
            .hits
            .iter()
            .find(|placed| {
                placed.row == event.row
                    && event.column >= self.viewport.x
                    && event.column < self.viewport.right()
                    && placed
                        .span
                        .is_none_or(|(from, to)| event.column >= from && event.column < to)
            })
            .map(|placed| (event.column, event.row, placed.hit));
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
impl crate::demo::Demo {
    /// A completed click on a transcript header. Folding is local presentation; Speak and image
    /// hits only open the reviewed saved-history control (S reviews speech, I previews an image),
    /// so nothing is played or fetched from here. False when the row no longer exists.
    pub fn activate_header(&mut self, hit: HeaderHit) -> bool {
        let compact = self.compact_display.active();
        let Some(row) = self.live.as_mut().and_then(|c| c.rows.get_mut(hit.row)) else {
            return false;
        };
        match hit.target {
            Target::Reasoning(card) => match row.cards.get_mut(card) {
                Some(card) => card.toggle_reasoning(compact),
                None => return false,
            },
            Target::Card(card) => match row.cards.get_mut(card) {
                Some(card) => card.fold.toggle(),
                None => return false,
            },
            Target::Prompt => row.prompt_fold.toggle(),
            Target::Speak | Target::Image => {
                self.transcript_selection.clear();
                self.open_history();
                if self
                    .live
                    .as_ref()
                    .is_some_and(|c| c.saved_history.is_open())
                {
                    self.notice = if hit.target == Target::Speak {
                        "Opened Saved History: S reviews speaking an answer. Nothing was played."
                            .into()
                    } else {
                        "Opened Saved History: I previews an embedded image. Nothing was fetched."
                            .into()
                    };
                }
                return true;
            }
        }
        self.transcript_selection.clear();
        true
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

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn at(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }
    fn tap(profile: &mut Profile, column: u16, row: u16) -> Option<HeaderHit> {
        profile.click(at(MouseEventKind::Down(MouseButton::Left), column, row));
        profile.click(at(MouseEventKind::Up(MouseButton::Left), column, row))
    }
    fn hit(target: Target) -> HeaderHit {
        HeaderHit { row: 0, target }
    }
    fn header(line: usize, span: Option<(u16, u16)>, target: Target) -> Header {
        Header {
            line,
            span,
            hit: hit(target),
        }
    }

    #[test]
    fn a_narrow_hit_wins_inside_its_columns_and_the_title_hit_covers_the_rest() {
        let mut profile = Profile::default();
        profile.set_hits(
            Rect::new(1, 2, 60, 10),
            0,
            &[
                header(3, Some((5, 12)), Target::Speak),
                header(3, None, Target::Card(0)),
            ],
        );
        // viewport.x = 1, so the span is columns 6..13 on screen row 5.
        assert_eq!(tap(&mut profile, 6, 5), Some(hit(Target::Speak)));
        assert_eq!(tap(&mut profile, 12, 5), Some(hit(Target::Speak)));
        assert_eq!(tap(&mut profile, 13, 5), Some(hit(Target::Card(0))));
        assert_eq!(tap(&mut profile, 40, 5), Some(hit(Target::Card(0))));
        assert_eq!(tap(&mut profile, 40, 6), None);
        assert_eq!(tap(&mut profile, 0, 5), None);
        assert_eq!(tap(&mut profile, 61, 5), None);
    }

    #[test]
    fn lines_scrolled_out_of_the_viewport_have_no_hit_and_a_moved_release_is_no_click() {
        let mut profile = Profile::default();
        profile.set_hits(
            Rect::new(0, 0, 40, 4),
            10,
            &[
                header(9, None, Target::Prompt),
                header(11, None, Target::Prompt),
                header(14, None, Target::Image),
            ],
        );
        assert_eq!(tap(&mut profile, 3, 1), Some(hit(Target::Prompt)));
        assert_eq!(tap(&mut profile, 3, 0), None);
        assert_eq!(tap(&mut profile, 3, 4), None);
        profile.click(at(MouseEventKind::Down(MouseButton::Left), 3, 1));
        assert_eq!(
            profile.click(at(MouseEventKind::Up(MouseButton::Left), 4, 1)),
            None
        );
        profile.click(at(MouseEventKind::Down(MouseButton::Left), 3, 1));
        profile.click(at(MouseEventKind::ScrollDown, 3, 1));
        assert_eq!(
            profile.click(at(MouseEventKind::Up(MouseButton::Left), 3, 1)),
            None
        );
        let mut shifted = at(MouseEventKind::Down(MouseButton::Left), 3, 1);
        shifted.modifiers = KeyModifiers::SHIFT;
        assert_eq!(profile.click(shifted), None);
    }

    #[test]
    fn auto_fold_latches_once_but_a_click_toggle_always_works() {
        use crate::live_chat::Fold;
        let mut fold = Fold::default();
        assert!(fold.autofold() && fold.folded);
        assert!(!fold.autofold());
        fold.toggle();
        assert!(!fold.folded);
        assert!(
            !fold.autofold() && !fold.folded,
            "a reopened card stays open"
        );
        fold.toggle();
        assert!(fold.folded);
        assert!(!Fold::default().folded);
    }
}
