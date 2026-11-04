//! Human runtime choices. A key creates a fresh Review, never changes authority.
use crate::{runtime_controls as controls, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};

pub use controls::Field;
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Read(Field),
    Review {
        snapshot: Box<controls::Snapshot>,
        change: controls::Change,
    },
    Apply(Box<controls::Review>),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Snapshot {
        field: Field,
        snapshot: Box<controls::Snapshot>,
    },
    Review(Box<controls::Review>),
    Settled(Box<controls::Readback>),
    Rejected(String),
}
#[derive(Default)]
pub struct View {
    open: bool,
    waiting: bool,
    selected_apply: bool,
    painted: bool,
    painted_until: usize,
    painted_width: u16,
    page_height: usize,
    wrapped_review: Vec<String>,
    field: Option<Field>,
    review: Option<controls::Review>,
    intent: Option<Intent>,
    scroll: usize,
    max_scroll: usize,
    pub notice: String,
}
impl View {
    pub fn owns_input(&self) -> bool {
        self.open
    }
    pub fn pending(&self) -> bool {
        self.waiting || self.intent.is_some()
    }
    pub fn open(&mut self, field: Field) {
        if self.pending() {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.field = Some(field);
        self.review = None;
        self.scroll = 0;
        self.painted_until = 0;
        self.selected_apply = false;
        self.painted = false;
        self.notice = "Reading the current runtime choice…".into();
        self.intent = Some(Intent::Read(field));
    }
    /// Hiding a panel does not discard its sent action or readback obligation.
    pub fn close(&mut self) {
        self.open = false;
        self.painted = false;
        self.selected_apply = false;
        if !self.waiting {
            self.review = None;
        }
    }
    pub fn hide(&mut self) {
        self.painted = false;
        self.painted_until = 0;
        self.selected_apply = false;
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn apply(&mut self, update: Update) {
        self.waiting = false;
        self.wrapped_review.clear();
        self.painted = false;
        self.selected_apply = false;
        self.scroll = 0;
        self.painted_until = 0;
        match update {
            Update::Snapshot { field, snapshot } => {
                if !self.open || self.field != Some(field) {
                    return;
                }
                if let Some(reason) = snapshot.editable.get(&field).and_then(Option::as_deref) {
                    self.notice = reason.to_owned();
                    return;
                }
                let change = match field {
                    Field::Tools => controls::Change::Tools(!snapshot.facts.tools_enabled),
                    Field::Profile => controls::Change::Profile(snapshot.next_profile),
                    Field::Plan => controls::Change::Plan(!snapshot.facts.plan_mode),
                };
                self.waiting = true;
                self.notice = "Preparing the exact change for review…".into();
                self.intent = Some(Intent::Review { snapshot, change });
            }
            Update::Review(review) => {
                if self.open && self.field == Some(review.change.field()) {
                    self.review = Some(*review);
                    self.notice =
                        "Review this choice. Tab selects Apply; Enter confirms it once.".into();
                }
            }
            Update::Settled(readback) => {
                self.review = None;
                self.notice = match readback.release {
                    controls::Release::Install => match readback.status {
                        controls::Status::SessionOnly => format!(
                            "Applied for this session; saving failed: {}",
                            readback.wire["reason"].as_str().unwrap_or("unknown reason")
                        ),
                        _ => "Applied and verified.".into(),
                    },
                    controls::Release::Unchanged => format!(
                        "Not applied: {}",
                        readback.wire["reason"]
                            .as_str()
                            .unwrap_or("source refused the change")
                    ),
                    controls::Release::Close => {
                        "The change could not be verified. The runtime is closing.".into()
                    }
                };
            }
            Update::Rejected(reason) => {
                self.review = None;
                self.notice = reason;
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if !self.open || key.kind != KeyEventKind::Press {
            return;
        }
        if key.code == KeyCode::BackTab && key.modifiers == KeyModifiers::SHIFT {
            if !self.waiting && self.review.is_some() && self.painted {
                self.selected_apply = !self.selected_apply;
            }
            return;
        }
        if !key.modifiers.is_empty() {
            return;
        }
        match key.code {
            KeyCode::Esc => self.close(),
            KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
            KeyCode::Down => self.scroll = (self.scroll + 1).min(self.max_scroll),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(self.page_height.max(1)),
            KeyCode::PageDown => {
                self.scroll = self
                    .scroll
                    .saturating_add(self.page_height.max(1))
                    .min(self.max_scroll)
            }
            KeyCode::Home => self.scroll = 0,
            KeyCode::End => self.scroll = self.max_scroll,
            KeyCode::Tab | KeyCode::Left | KeyCode::Right
                if !self.waiting && self.review.is_some() && self.painted =>
            {
                self.selected_apply = !self.selected_apply
            }
            KeyCode::Enter if !self.waiting && self.painted => {
                if self.selected_apply {
                    if let Some(review) = self.review.take() {
                        self.waiting = true;
                        self.painted = false;
                        self.selected_apply = false;
                        self.notice = "Applying this choice and verifying the readback…".into();
                        self.intent = Some(Intent::Apply(Box::new(review)));
                    }
                } else {
                    self.close();
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open {
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(3),
            MouseEventKind::ScrollDown => self.scroll = (self.scroll + 3).min(self.max_scroll),
            _ => {}
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open || area.width < 24 || area.height < 8 {
            self.hide();
            return;
        }
        let rect = Rect::new(
            area.x + 1,
            area.y + 1,
            area.width.saturating_sub(2),
            area.height.saturating_sub(2),
        );
        let title = match self.field {
            Some(Field::Tools) => " Tools ",
            Some(Field::Profile) => " Authority ",
            Some(Field::Plan) => " Plan mode ",
            None => " Runtime choice ",
        };
        let block = Block::default()
            .borders(Borders::ALL)
            .title(title)
            .style(Style::default().fg(theme.foreground).bg(theme.panel));
        let inner = block.inner(rect);
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);
        if self.review.is_none() {
            self.wrapped_review.clear();
        }
        if let Some(review) = &self.review
            && (self.wrapped_review.is_empty() || self.painted_width != inner.width)
        {
            self.wrapped_review = review
                .detail_lines()
                .into_iter()
                .flat_map(|line| {
                    crate::ui::wrap(&crate::live::terminal_text(&line), inner.width as usize)
                })
                .collect();
        }
        let review_lines = self.wrapped_review.len();
        let notice = crate::ui::wrap(
            &crate::live::terminal_text(&self.notice),
            inner.width as usize,
        );
        let height = inner.height.saturating_sub(2) as usize;
        self.page_height = height;
        if self.painted_width != inner.width {
            self.painted_width = inner.width;
            self.painted_until = 0;
        }
        self.max_scroll = (review_lines + 1 + notice.len()).saturating_sub(height);
        self.scroll = self.scroll.min(self.max_scroll);
        // Slice before drawing: review text is bounded but can exceed u16 scroll.
        if self.scroll <= self.painted_until {
            self.painted_until = self
                .painted_until
                .max(self.scroll.saturating_add(height).min(review_lines));
        }
        self.painted = self.review.is_none() || self.painted_until >= review_lines;
        let text = self
            .wrapped_review
            .iter()
            .map(String::as_str)
            .chain(std::iter::once(""))
            .chain(notice.iter().map(String::as_str))
            .skip(self.scroll)
            .take(height)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(
            Paragraph::new(text),
            Rect::new(inner.x, inner.y, inner.width, height as u16),
        );
        let buttons = if self.waiting {
            "Waiting · Esc hides · Ctrl+Esc Stop · Ctrl+Q Quit"
        } else if self.review.is_some() && !self.painted {
            "Read all pages with ↑↓ / PgDn to enable Apply · Esc cancels"
        } else if self.review.is_some() && self.selected_apply {
            " Cancel    [Apply] · Enter confirms · Esc cancels"
        } else if self.review.is_some() {
            "[Cancel]    Apply  · Tab selects · ↑↓ scroll"
        } else {
            "Enter or Esc closes"
        };
        frame.render_widget(
            Paragraph::new(buttons).style(Style::default().fg(theme.title)),
            Rect::new(
                inner.x,
                inner.y + inner.height.saturating_sub(1),
                inner.width,
                1,
            ),
        );
    }
}
