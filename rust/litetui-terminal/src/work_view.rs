//! Work inspection stays separate from the ordinary composer and execution authority.
use crate::{live::terminal_text, theme::Theme, work_protocol as protocol};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent, MouseEventKind};
use protocol::{Calendar, Page, PauseReview, Row, Section, Snapshot, StopReview, Tail};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    Snapshot,
    Page {
        section: Section,
        offset: usize,
    },
    Tail(Row),
    Calendar {
        month: String,
        date: Option<String>,
        offset: usize,
    },
    PauseValidate,
    PauseApply(Box<PauseReview>),
    StopValidate(Row),
    StopApply(Box<StopReview>),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Intent {
    pub generation: u64,
    pub request: Request,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Data {
    Snapshot(Box<Snapshot>),
    Page(Page),
    Tail(Tail),
    Calendar(Calendar),
    Review(Box<PauseReview>),
    Paused,
    StopReview(Box<StopReview>),
    Stopped(Row),
    Rejected(String),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Update {
    pub generation: u64,
    pub data: Data,
}
#[derive(Clone)]
enum Review {
    Pause(PauseReview),
    Stop(StopReview),
}
impl Review {
    fn request(&self) -> Request {
        match self {
            Self::Pause(review) => Request::PauseApply(Box::new(review.clone())),
            Self::Stop(review) => Request::StopApply(Box::new(review.clone())),
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub snapshot: Option<Snapshot>,
    pub page: Option<Page>,
    pub calendar: Option<Calendar>,
    pub tail: Option<Tail>,
    section: usize,
    selected: usize,
    page_back: Vec<usize>,
    calendar_back: Vec<usize>,
    details: bool,
    error: Option<String>,
    scroll: usize,
    max_scroll: usize,
    month: String,
    review: Option<Review>,
    stopped: Option<Row>,
    review_page: usize,
    seen: Vec<bool>,
    visible: bool,
    geometry: Option<Rect>,
    generation: u64,
    pending: Option<Request>,
    intent: Option<Intent>,
}
impl View {
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.refresh();
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.hide();
        // A sent read still owns its correlated reply. Retain input ownership
        // until it drains, so closing the panel cannot race a fresh prompt.
        let dispatched = self.pending.is_some() && self.intent.is_none();
        if !dispatched {
            self.pending = None;
            self.intent = None;
            self.waiting = false;
            self.review = None;
            self.seen.clear();
        }
    }
    pub fn finish(&mut self) {
        let generation = self.generation;
        *self = Self::default();
        self.generation = generation;
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn pending_generation(&self) -> Option<u64> {
        self.pending.as_ref().map(|_| self.generation)
    }
    fn request(&mut self, request: Request) {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice = "Work request limit reached; reopen the runtime.".into();
            return;
        };
        self.generation = generation;
        self.error = None;
        self.pending = Some(request.clone());
        self.intent = Some(Intent {
            generation,
            request,
        });
        self.waiting = true;
        self.hide();
    }
    fn refresh(&mut self) {
        self.review = None;
        self.stopped = None;
        self.tail = None;
        self.page = None;
        self.calendar = None;
        self.details = false;
        self.scroll = 0;
        self.notice = "Reading Work…".into();
        self.request(Request::Snapshot);
    }
    fn section_request(&mut self) {
        self.page_back.clear();
        self.calendar_back.clear();
        self.selected = 0;
        self.scroll = 0;
        self.details = false;
        self.tail = None;
        self.page = None;
        self.calendar = None;
        let Some(snapshot) = &self.snapshot else {
            return;
        };
        if self.section < 4 {
            let section = Section::ALL[self.section];
            if !snapshot.source(section).available {
                self.notice = snapshot.source(section).reason.clone().unwrap_or_default();
                self.hide();
                return;
            }
            self.request(Request::Page { section, offset: 0 });
        } else if self.section == 5 {
            if !snapshot.source(Section::Jobs).available {
                self.notice = snapshot
                    .source(Section::Jobs)
                    .reason
                    .clone()
                    .unwrap_or_default();
                self.hide();
                return;
            }
            self.request(Request::Calendar {
                month: self.month.clone(),
                date: None,
                offset: 0,
            });
        } else {
            self.notice = "Current conversation goal · F5 refresh".into();
            self.hide();
        }
    }
    pub fn observe(&mut self, update: Update) {
        if self.pending.is_none() || update.generation != self.generation {
            return;
        }
        let pending = self.pending.take().expect("pending");
        let compatible = matches!(
            (&pending, &update.data),
            (Request::Snapshot, Data::Snapshot(_))
                | (Request::Page { .. }, Data::Page(_))
                | (Request::Tail(_), Data::Tail(_))
                | (Request::Calendar { .. }, Data::Calendar(_))
                | (Request::PauseValidate, Data::Review(_))
                | (Request::PauseApply(_), Data::Paused)
                | (Request::StopValidate(_), Data::StopReview(_))
                | (Request::StopApply(_), Data::Stopped(_))
                | (_, Data::Rejected(_))
        );
        if !compatible {
            self.pending = Some(pending);
            return;
        }
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.scroll = 0;
        match update.data {
            Data::Snapshot(snapshot) => {
                self.month = snapshot.calendar_month.clone();
                self.snapshot = Some(*snapshot);
                self.notice = "Captured Work facts · Tab categories · Enter details".into();
                if self.open {
                    self.section_request();
                }
            }
            Data::Page(page) => {
                if let Some(old) = &self.page {
                    if old.section == page.section {
                        update_back(&mut self.page_back, old.offset, page.offset);
                    } else {
                        self.page_back.clear();
                    }
                } else {
                    self.page_back.clear();
                }
                self.page = Some(page);
                self.selected = 0;
                self.details = false;
                self.notice =
                    "Captured rows · Enter details · N/PgDn next page · B/PgUp back".into();
            }
            Data::Tail(tail) => {
                self.tail = Some(tail);
                self.details = true;
                self.notice = "Task output · PgUp/PgDn scroll · Esc returns".into();
            }
            Data::Calendar(calendar) => {
                if let Some(old) = &self.calendar {
                    if old.month == calendar.month && old.date == calendar.date {
                        update_back(&mut self.calendar_back, old.offset, calendar.offset);
                    } else {
                        self.calendar_back.clear();
                    }
                } else {
                    self.calendar_back.clear();
                }
                self.calendar = Some(calendar);
                self.selected = 0;
                self.notice =
                    "Calendar · Enter opens the selected day · Left/Right change month".into();
            }
            Data::Review(review) => {
                self.review = Some(Review::Pause(*review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page; confirm only on the last visible page.".into();
            }
            Data::Paused => {
                if let Some(s) = self.snapshot.as_mut() {
                    s.paused = true;
                    s.can_pause = false;
                    s.pause_reason = Some("Future scheduled prompts are already paused.".into());
                }
                self.review = None;
                self.notice =
                    "Future scheduled prompts paused. Active work and saved jobs are unchanged."
                        .into();
            }
            Data::StopReview(review) => {
                self.review = Some(Review::Stop(*review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page before confirming this task Stop.".into();
            }
            Data::Stopped(row) => {
                self.notice = format!(
                    "Task Stop verified · final state {} · F5 refresh Work",
                    row.status
                );
                self.stopped = Some(row);
                self.review = None;
                self.snapshot = None;
                self.page = None;
                self.tail = None;
                self.calendar = None;
                self.details = true;
            }
            Data::Rejected(reason) => {
                self.review = None;
                self.error = Some(reason);
                self.details = true;
                self.notice = "Work request failed · PgUp/PgDn details · F5 refresh".into();
            }
        }
    }
    fn count(&self) -> usize {
        if let Some(c) = &self.calendar {
            if c.date.is_some() {
                c.rows.len()
            } else {
                c.days.iter().filter(|d| d.date.is_some()).count()
            }
        } else {
            self.page.as_ref().map_or(0, |p| p.rows.len())
        }
    }
    fn current_row(&self) -> Option<&Row> {
        self.page.as_ref().and_then(|p| p.rows.get(self.selected))
    }
    fn page_by(&mut self, forward: bool) {
        if let Some(c) = &self.calendar
            && let Some(date) = &c.date
        {
            let offset = if forward {
                c.next
            } else {
                self.calendar_back.last().copied()
            };
            if let Some(offset) = offset.filter(|o| *o != c.offset) {
                self.request(Request::Calendar {
                    month: c.month.clone(),
                    date: Some(date.clone()),
                    offset,
                });
            }
            return;
        }
        if let Some(p) = &self.page {
            let offset = if forward {
                p.next
            } else {
                self.page_back.last().copied()
            };
            if let Some(offset) = offset.filter(|o| *o != p.offset) {
                self.request(Request::Page {
                    section: p.section,
                    offset,
                });
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        let back_tab =
            key.code == KeyCode::BackTab && key.modifiers == crossterm::event::KeyModifiers::SHIFT;
        if key.kind == KeyEventKind::Release || (!key.modifiers.is_empty() && !back_tab) {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.review.is_some() && !self.waiting {
                self.review = None;
                self.seen.clear();
                self.hide();
            } else if self.tail.is_some() {
                self.tail = None;
                self.details = false;
                self.scroll = 0;
                self.hide();
            } else if self.details {
                self.details = false;
                self.scroll = 0;
                self.hide();
            } else if self.calendar.as_ref().is_some_and(|c| c.date.is_some()) && !self.waiting {
                self.request(Request::Calendar {
                    month: self.month.clone(),
                    date: None,
                    offset: 0,
                });
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if let Some(review) = &self.review {
            if key.kind != KeyEventKind::Press {
                return;
            }
            match key.code {
                KeyCode::PageUp | KeyCode::Left => {
                    self.review_page = self.review_page.saturating_sub(1);
                    self.hide();
                }
                KeyCode::PageDown | KeyCode::Right => {
                    self.review_page =
                        (self.review_page + 1).min(self.seen.len().saturating_sub(1));
                    self.hide();
                }
                KeyCode::Enter => {
                    if self.review_page + 1 < self.seen.len() {
                        self.review_page += 1;
                        self.hide();
                    } else if !self.seen.is_empty() && self.seen.iter().all(|seen| *seen) {
                        self.request(review.request());
                    }
                }
                _ => {}
            }
            return;
        }
        if key.kind == KeyEventKind::Press {
            match key.code {
                KeyCode::F(5) => {
                    self.refresh();
                    return;
                }
                KeyCode::Tab => {
                    self.section = (self.section + 1) % 6;
                    self.section_request();
                    return;
                }
                KeyCode::BackTab => {
                    self.section = (self.section + 5) % 6;
                    self.section_request();
                    return;
                }
                KeyCode::Char('k') => {
                    if let Some(row) = self.current_row().filter(|row| row.can_stop())
                        && self.snapshot.as_ref().is_some_and(|s| !s.quitting)
                    {
                        self.request(Request::StopValidate(row.clone()));
                    } else {
                        self.notice =
                            "Select a current owned running host task to review Stop.".into();
                    }
                    return;
                }
                KeyCode::Char('p') => {
                    if let Some(snapshot) = &self.snapshot {
                        if snapshot.can_pause {
                            self.request(Request::PauseValidate);
                        } else {
                            self.notice = snapshot.pause_reason.clone().unwrap_or_default();
                        }
                    }
                    return;
                }
                _ => {}
            }
        }
        if self.details || self.section == 4 {
            match key.code {
                KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
                KeyCode::Down => self.scroll = (self.scroll + 1).min(self.max_scroll),
                KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(8),
                KeyCode::PageDown => self.scroll = (self.scroll + 8).min(self.max_scroll),
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => {
                self.selected = (self.selected + 1).min(self.count().saturating_sub(1))
            }
            KeyCode::Home => self.selected = 0,
            KeyCode::End => self.selected = self.count().saturating_sub(1),
            KeyCode::PageDown | KeyCode::Char('n') => self.page_by(true),
            KeyCode::PageUp | KeyCode::Char('b') => self.page_by(false),
            KeyCode::Left | KeyCode::Right
                if self.section == 5 && key.kind == KeyEventKind::Press =>
            {
                self.month = protocol::shift_month(&self.month, key.code == KeyCode::Right);
                self.request(Request::Calendar {
                    month: self.month.clone(),
                    date: None,
                    offset: 0,
                });
            }
            KeyCode::Char('t') if key.kind == KeyEventKind::Press => {
                if let Some(row) = self.current_row() {
                    if row.tail_available {
                        self.request(Request::Tail(row.clone()));
                    } else {
                        self.notice = "Output is unavailable for this selected row.".into();
                    }
                }
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                if let Some(c) = &self.calendar {
                    if c.date.is_none() {
                        if let Some(day) = c
                            .days
                            .iter()
                            .filter_map(|d| d.date.as_ref())
                            .nth(self.selected)
                        {
                            self.request(Request::Calendar {
                                month: self.month.clone(),
                                date: Some(day.clone()),
                                offset: 0,
                            });
                        }
                    } else {
                        self.details = true;
                        self.scroll = 0;
                        self.hide();
                    }
                } else if self.current_row().is_some()
                    || self.section < 4
                        && self
                            .snapshot
                            .as_ref()
                            .is_some_and(|s| !s.source(Section::ALL[self.section]).available)
                {
                    self.error = None;
                    self.details = true;
                    self.scroll = 0;
                    self.hide();
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting || !event.modifiers.is_empty() {
            return;
        }
        if self.review.is_some() {
            self.seen.fill(false);
            self.hide();
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => {
                if self.details || self.section == 4 {
                    self.scroll = self.scroll.saturating_sub(3);
                } else {
                    self.selected = self.selected.saturating_sub(1);
                }
            }
            MouseEventKind::ScrollDown => {
                if self.details || self.section == 4 {
                    self.scroll = (self.scroll + 3).min(self.max_scroll);
                } else {
                    self.selected = (self.selected + 1).min(self.count().saturating_sub(1));
                }
            }
            _ => {}
        }
    }
    fn review_lines(&self) -> Vec<String> {
        let r = match self.review.as_ref().unwrap() {
            Review::Pause(review) => review,
            Review::Stop(review) => {
                let mut lines = vec![
                    "Stop this owned background task".into(),
                    protocol::STOP_CONSEQUENCE.into(),
                    format!("Conversation: {}", review.snapshot.identity.session),
                    format!("Agent: {}", review.snapshot.identity.agent),
                    format!("Backend: {}", review.snapshot.identity.backend),
                    format!("Model: {}", review.snapshot.identity.model),
                    "Exact selected task:".into(),
                    serde_json::to_string_pretty(&review.row.raw).expect("validated task row"),
                    "Source Stop authority:".into(),
                ];
                for key in [
                    "stop_mode",
                    "process_id",
                    "owner_pid",
                    "owner_instance",
                    "owner_created",
                    "producer_id",
                    "worker_id",
                    "lifecycle_instance",
                    "action_id",
                ] {
                    lines.push(format!("{key}: {}", review.wire[key]));
                }
                lines.push("Enter on the last visible page confirms once. Esc cancels.".into());
                return lines;
            }
        };
        vec![
            "Pause future scheduled prompts".into(),
            protocol::CONSEQUENCE.into(),
            format!("Conversation: {}", r.snapshot.identity.session),
            format!("Agent: {}", r.snapshot.identity.agent),
            format!("Backend: {}", r.snapshot.identity.backend),
            format!("Model: {}", r.snapshot.identity.model),
            "Applies only to this runtime. Saved schedules remain enabled.".into(),
            "This does not stop a task, pause a goal, clear input, or close the runtime.".into(),
            "Enter on the last visible page confirms once. Esc cancels.".into(),
        ]
    }
    fn detail_lines(&self) -> Vec<String> {
        let mut lines = Vec::new();
        if let Some(notice) = self
            .snapshot
            .as_ref()
            .and_then(|s| s.scheduler_notice.as_ref())
        {
            lines.push(format!("Schedule held: {notice}"));
            lines.push(String::new());
        }
        lines.extend(self.detail_content());
        lines
    }
    fn detail_content(&self) -> Vec<String> {
        if let Some(row) = &self.stopped {
            return vec![
                "Task Stop independently verified. Workers, callbacks and process retired.".into(),
                serde_json::to_string_pretty(&row.raw).expect("validated task row"),
                "F5 refreshes the Work capture before another task action.".into(),
            ];
        }
        if let Some(error) = &self.error {
            return vec![error.clone()];
        }
        if self.section < 4
            && let Some(reason) = self
                .snapshot
                .as_ref()
                .and_then(|s| s.source(Section::ALL[self.section]).reason.as_ref())
        {
            return vec![reason.clone()];
        }
        if let Some(t) = &self.tail {
            let mut rows = vec![
                format!("Task {} · {}", t.task_id, t.status),
                format!("Output: {}", t.path),
                format!(
                    "Bytes {}..{}{}",
                    t.offset,
                    t.total,
                    if t.lossy {
                        " · invalid UTF-8 replaced"
                    } else {
                        ""
                    }
                ),
            ];
            if let Some(r) = &t.reason {
                rows.push(r.clone());
            }
            rows.extend(t.text.lines().map(str::to_owned));
            return rows;
        }
        if self.section == 4 {
            let Some(s) = &self.snapshot else {
                return vec![];
            };
            let g = &s.goal;
            if g["status"] != "present" {
                return vec![
                    g["reason"]
                        .as_str()
                        .unwrap_or("No saved goal for this conversation.")
                        .into(),
                ];
            }
            let g = &g["value"];
            let mut rows = Vec::new();
            for (key, label) in [
                ("objective", "Objective"),
                ("status", "Status"),
                ("evaluated_turns", "Evaluated turns"),
                ("max_turns", "Maximum turns (0 means unlimited)"),
                ("last_verdict", "Last verdict"),
                ("last_reason", "Last reason"),
                ("no_progress_count", "No-progress turns"),
                ("tool_profile", "Recorded tool profile"),
                ("started_by", "Started by"),
                ("created", "Created"),
                ("updated", "Updated"),
            ] {
                let v = &g[key];
                rows.push(format!(
                    "{label}: {}",
                    v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string())
                ));
            }
            if let Some(evidence) = g["missing_evidence"].as_array() {
                for e in evidence {
                    rows.push(format!(
                        "Missing evidence: {}",
                        e.as_str().unwrap_or_default()
                    ));
                }
            }
            return rows;
        }
        if let Some(c) = &self.calendar
            && let Some(row) = c.rows.get(self.selected)
        {
            return vec![
                row.summary(),
                format!("Job: {}", row.job),
                format!("Firings on this day: {}", row.total),
            ];
        }
        self.current_row().map_or_else(Vec::new, Row::details)
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            return;
        }
        if area.width < 48 || area.height < 18 {
            self.seen.clear();
            self.review_page = 0;
            frame.render_widget(Clear, area);
            frame.render_widget(
                Paragraph::new("Work needs 48×18. Resize, or Esc to close.")
                    .block(Block::bordered().title(" Work ")),
                area,
            );
            return;
        }
        let width = area.width.saturating_sub(2).min(110);
        let height = area.height.saturating_sub(2).min(36);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(rect) {
            self.geometry = Some(rect);
            self.seen.clear();
            self.review_page = 0;
        }
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(" Work ")
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let notice_lines = self
            .snapshot
            .as_ref()
            .and_then(|s| s.scheduler_notice.as_ref())
            .map(|s| {
                crate::ui::wrap(
                    &terminal_text(&format!("Schedule held: {s}")),
                    inner.width as usize,
                )
            })
            .unwrap_or_default();
        let notice_height = notice_lines.len().min(4) as u16;
        if notice_height > 0 {
            frame.render_widget(
                Paragraph::new(
                    notice_lines
                        .into_iter()
                        .take(4)
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                Rect::new(inner.x, inner.y + 1, inner.width, notice_height),
            );
        }
        let body = Rect::new(
            inner.x,
            inner.y + 2 + notice_height,
            inner.width,
            inner.height.saturating_sub(5 + notice_height),
        );
        let label = if self.section < 4 {
            Section::ALL[self.section].label()
        } else if self.section == 4 {
            "Goal"
        } else {
            "Calendar"
        };
        let header = format!(
            "{} · {}{}",
            label,
            self.snapshot.as_ref().map_or("Reading", |s| if s.paused {
                "Schedules paused"
            } else {
                "Schedules active"
            }),
            if self.snapshot.as_ref().is_some_and(|s| s.busy) {
                " · Turn active"
            } else {
                ""
            }
        );
        frame.render_widget(
            Paragraph::new(terminal_text(&header)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let wrap = |lines: Vec<String>| -> Vec<String> {
            lines
                .into_iter()
                .flat_map(|line| crate::ui::wrap(&terminal_text(&line), body.width as usize))
                .collect()
        };
        let lines = if self.review.is_some() {
            let lines = wrap(self.review_lines());
            let per = body.height.max(1) as usize;
            let pages = lines.len().div_ceil(per).max(1);
            if self.seen.len() != pages {
                self.seen = vec![false; pages];
            }
            self.review_page = self.review_page.min(pages - 1);
            self.seen[self.review_page] = true;
            self.notice = format!(
                "Review {}/{} · PgUp/PgDn · Enter {} · Esc cancel",
                self.review_page + 1,
                pages,
                if self.review_page + 1 == pages {
                    "confirms"
                } else {
                    "next page"
                }
            );
            lines
                .into_iter()
                .skip(self.review_page * per)
                .take(per)
                .collect::<Vec<_>>()
        } else if self.details || self.section == 4 {
            let lines = wrap(self.detail_lines());
            self.max_scroll = lines.len().saturating_sub(body.height as usize);
            self.scroll = self.scroll.min(self.max_scroll);
            lines
                .into_iter()
                .skip(self.scroll)
                .take(body.height as usize)
                .collect()
        } else {
            let mut labels = Vec::new();
            if let Some(c) = &self.calendar {
                if c.date.is_some() {
                    labels.extend(c.rows.iter().map(|entry| entry.summary()));
                } else {
                    labels.extend(c.days.iter().filter(|d| d.date.is_some()).map(|d| {
                        format!(
                            "{} · {} job{}{}",
                            d.date.as_deref().unwrap(),
                            d.total,
                            if d.total == 1 { "" } else { "s" },
                            d.entries
                                .first()
                                .map(|e| format!(" · {}", e.summary()))
                                .unwrap_or_default()
                        )
                    }));
                }
            } else if let Some(p) = &self.page {
                labels.extend(
                    p.rows
                        .iter()
                        .map(|r| format!("{} · {} · {}", r.label, r.status, r.id)),
                );
            }
            if labels.is_empty() {
                vec![if self.waiting {
                    "Reading…".into()
                } else if self.section < 4 {
                    if self
                        .snapshot
                        .as_ref()
                        .is_some_and(|s| !s.source(Section::ALL[self.section]).available)
                    {
                        "Source unavailable · Enter for complete details".into()
                    } else {
                        "No rows in this captured section.".into()
                    }
                } else {
                    "No calendar entries.".into()
                }]
            } else {
                let page_height = body.height.max(1) as usize;
                let start = self.selected / page_height * page_height;
                labels
                    .into_iter()
                    .enumerate()
                    .skip(start)
                    .take(page_height)
                    .map(|(i, label)| {
                        let escaped = terminal_text(&label);
                        let chunks =
                            crate::ui::wrap(&escaped, body.width.saturating_sub(4) as usize);
                        format!(
                            "{} {}{}",
                            if i == self.selected { "▶" } else { " " },
                            chunks.first().cloned().unwrap_or_default(),
                            if chunks.len() > 1 { "…" } else { "" }
                        )
                    })
                    .collect()
            }
        };
        frame.render_widget(Paragraph::new(lines.join("\n")), body);
        let footer = if self.review.is_some() {
            self.notice.clone()
        } else {
            format!(
                "Tab categories · F5 refresh · P pause · T output · K Stop task\n{}",
                self.notice
            )
        };
        let footer = crate::ui::wrap(&terminal_text(&footer), inner.width as usize)
            .into_iter()
            .take(3)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(
            Paragraph::new(footer),
            Rect::new(inner.x, body.bottom(), inner.width, 3),
        );
        self.visible = true;
    }
}

// The source may shrink pages to fit its byte budget; offsets are not fixed strides.
fn update_back(back: &mut Vec<usize>, old: usize, new: usize) {
    if new > old {
        back.push(old);
    } else if new < old {
        if let Some(index) = back.iter().position(|offset| *offset == new) {
            back.truncate(index);
        } else {
            back.clear();
        }
    }
}
