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
        /// The key that asked for an authority change ("shift+tab", "footer chip").
        source: Option<&'static str>,
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
    desired_plan: Option<bool>,
    review: Option<controls::Review>,
    tool_snapshot: Option<Box<controls::Snapshot>>,
    selected_tool: usize,
    intent: Option<Intent>,
    scroll: usize,
    max_scroll: usize,
    pub notice: String,
    /// /tools: Python's tool list, where a tick applies at once (the source's checks
    /// still run: Review then Apply, with no review page shown).
    list_mode: bool,
    list: crate::tool_list_view::List,
    /// A change sent from the list: a refusal re-reads the list once.
    reread_on_reject: bool,
    /// Ctrl+T and Shift+Tab: Python toggles at once with no page, so nothing is drawn and
    /// the composer keeps the keys; the runtime's Read, Review and Apply still run.
    quick: bool,
    /// What opened a quick authority change, for Python's own line.
    source: Option<&'static str>,
}
impl View {
    pub fn owns_input(&self) -> bool {
        self.open && !self.quick
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
        self.desired_plan = None;
        self.review = None;
        self.tool_snapshot = None;
        self.selected_tool = 0;
        self.scroll = 0;
        self.painted_until = 0;
        self.selected_apply = false;
        self.painted = false;
        self.notice = "Reading the current runtime choice…".into();
        self.intent = Some(Intent::Read(field));
        self.list_mode = field == Field::ToolSelection;
        self.reread_on_reject = false;
        self.quick = matches!(field, Field::Tools | Field::Profile);
        self.source = None;
        if self.list_mode || self.quick {
            self.list.reset();
            self.notice.clear();
        }
    }
    /// Name the key that opened this quick authority change.
    pub fn set_source(&mut self, source: &'static str) {
        self.source = Some(source);
    }
    /// The end of a quick toggle: nothing stays open.
    fn finish_quick(&mut self) {
        self.open = false;
        self.quick = false;
        self.review = None;
        self.painted = false;
    }
    /// The list's own re-read after a change; keeps its focus, scroll and last snapshot.
    fn reread(&mut self) {
        self.waiting = true;
        self.field = Some(Field::ToolSelection);
        self.intent = Some(Intent::Read(Field::ToolSelection));
    }
    fn list_toggle(&mut self, target: crate::tool_list_view::Target) {
        use crate::tool_list_view::Target;
        if self.pending() {
            return;
        }
        let Some(snapshot) = self.tool_snapshot.as_ref() else {
            return;
        };
        let rows = &snapshot.tools.as_ref().expect("verified list").rows;
        let change = match target {
            Target::Switch => controls::Change::Tools(!snapshot.facts.tools_enabled),
            Target::Tool(i) => match rows.get(i) {
                Some(row) => controls::Change::ToolSelection {
                    name: row.name.clone(),
                    enabled: !row.enabled,
                },
                None => return,
            },
            Target::Scroll | Target::Close | Target::Dock => return,
        };
        if let Err(error) = snapshot.validate_request(&change) {
            self.notice = error.to_string();
            return;
        }
        let snapshot = snapshot.clone();
        self.notice.clear();
        self.field = Some(change.field());
        self.waiting = true;
        self.reread_on_reject = true;
        self.intent = Some(Intent::Review {
            snapshot,
            change,
            source: None,
        });
    }
    pub(crate) fn plan_choice(&mut self, desired: Option<bool>) {
        if self.field == Some(Field::Plan) && matches!(self.intent, Some(Intent::Read(Field::Plan)))
        {
            self.desired_plan = desired;
        }
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
        if self.quick {
            match update {
                Update::Snapshot { field, snapshot } => {
                    if self.field != Some(field) {
                        return;
                    }
                    if let Some(reason) = snapshot.editable.get(&field).and_then(Option::as_deref) {
                        self.notice = reason.to_owned();
                        self.finish_quick();
                        return;
                    }
                    let (change, source) = match field {
                        Field::Tools => {
                            (controls::Change::Tools(!snapshot.facts.tools_enabled), None)
                        }
                        _ => (
                            controls::Change::Profile(snapshot.next_profile),
                            self.source,
                        ),
                    };
                    self.waiting = true;
                    self.intent = Some(Intent::Review {
                        snapshot,
                        change,
                        source,
                    });
                }
                Update::Review(review) => {
                    self.waiting = true;
                    self.intent = Some(Intent::Apply(review));
                }
                Update::Settled(readback) => {
                    // Python's own line ("Tools ON …", the authority level) arrives as a
                    // system line from the source; only a failure is said here.
                    self.notice = match (readback.release, readback.status) {
                        (controls::Release::Install, controls::Status::SessionOnly) => format!(
                            "Applied for this session; saving failed: {}",
                            readback.wire["reason"].as_str().unwrap_or("unknown reason")
                        ),
                        (controls::Release::Install, _) => String::new(),
                        (controls::Release::Unchanged, _) => format!(
                            "Not applied: {}",
                            readback.wire["reason"]
                                .as_str()
                                .unwrap_or("source refused the change")
                        ),
                        (controls::Release::Close, _) => {
                            "The change could not be verified. The runtime is closing.".into()
                        }
                    };
                    self.finish_quick();
                }
                Update::Rejected(reason) => {
                    self.notice = reason;
                    self.finish_quick();
                }
            }
            return;
        }
        if self.list_mode {
            match update {
                Update::Snapshot { field, snapshot } if field == Field::ToolSelection => {
                    self.reread_on_reject = false;
                    if !self.open {
                        return;
                    }
                    if let Some(reason) = snapshot.editable.get(&field).and_then(Option::as_deref) {
                        self.notice = reason.to_owned();
                    }
                    self.tool_snapshot = Some(snapshot);
                    return;
                }
                Update::Review(review) => {
                    // Python's tick applies at once: no review page, the checks still run.
                    self.waiting = true;
                    self.intent = Some(Intent::Apply(review));
                    return;
                }
                Update::Settled(readback) => {
                    self.reread_on_reject = false;
                    self.notice = match readback.release {
                        controls::Release::Install => match readback.status {
                            controls::Status::SessionOnly => format!(
                                "Applied for this session; saving failed: {}",
                                readback.wire["reason"].as_str().unwrap_or("unknown reason")
                            ),
                            _ => String::new(),
                        },
                        controls::Release::Unchanged => format!(
                            "Not applied: {}",
                            readback.wire["reason"]
                                .as_str()
                                .unwrap_or("source refused the change")
                        ),
                        controls::Release::Close => {
                            self.tool_snapshot = None;
                            self.list_mode = false;
                            self.notice =
                                "The change could not be verified. The runtime is closing.".into();
                            return;
                        }
                    };
                    if self.open {
                        self.reread();
                    }
                    return;
                }
                Update::Rejected(reason) => {
                    self.notice = reason;
                    if std::mem::take(&mut self.reread_on_reject) && self.open {
                        self.reread();
                    }
                    return;
                }
                Update::Snapshot { .. } => return,
            }
        }
        match update {
            Update::Snapshot { field, snapshot } => {
                if !self.open || self.field != Some(field) {
                    return;
                }
                if field == Field::ToolSelection {
                    self.notice = snapshot
                        .editable
                        .get(&field)
                        .and_then(Option::as_deref)
                        .unwrap_or(
                            "Choose one tool to review. Authority and argument gates still apply.",
                        )
                        .to_owned();
                    self.tool_snapshot = Some(snapshot);
                    return;
                }
                if let Some(reason) = snapshot.editable.get(&field).and_then(Option::as_deref) {
                    self.notice = reason.to_owned();
                    return;
                }
                let change = match field {
                    Field::Tools => controls::Change::Tools(!snapshot.facts.tools_enabled),
                    Field::Profile => controls::Change::Profile(snapshot.next_profile),
                    Field::Plan => controls::Change::Plan(
                        self.desired_plan
                            .take()
                            .unwrap_or(!snapshot.facts.plan_mode),
                    ),
                    Field::ToolSelection => unreachable!("list handled above"),
                };
                self.waiting = true;
                self.notice = "Preparing the exact change for review…".into();
                self.intent = Some(Intent::Review {
                    snapshot,
                    change,
                    source: None,
                });
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
                self.tool_snapshot = None;
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
                self.tool_snapshot = None;
                self.notice = reason;
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if !self.open || self.quick || key.kind != KeyEventKind::Press {
            return;
        }
        if self.list_mode {
            match self.list.key(key) {
                crate::tool_list_view::Act::Close => self.close(),
                crate::tool_list_view::Act::Toggle(target) => self.list_toggle(target),
                crate::tool_list_view::Act::Nothing => {}
            }
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
        if !self.waiting && self.review.is_none() && self.field == Some(Field::ToolSelection) {
            if key.code == KeyCode::F(5) {
                self.open(Field::ToolSelection);
                return;
            }
            if let Some(snapshot) = self.tool_snapshot.as_ref() {
                let rows = &snapshot.tools.as_ref().expect("verified list").rows;
                match key.code {
                    KeyCode::Up => {
                        self.selected_tool = self.selected_tool.saturating_sub(1);
                        self.hide();
                        return;
                    }
                    KeyCode::Down => {
                        self.selected_tool =
                            (self.selected_tool + 1).min(rows.len().saturating_sub(1));
                        self.hide();
                        return;
                    }
                    KeyCode::Enter | KeyCode::Char('g') if self.painted => {
                        let change = if key.code == KeyCode::Char('g') {
                            controls::Change::Tools(!snapshot.facts.tools_enabled)
                        } else if let Some(row) = rows.get(self.selected_tool) {
                            controls::Change::ToolSelection {
                                name: row.name.clone(),
                                enabled: !row.enabled,
                            }
                        } else {
                            return;
                        };
                        if let Err(error) = snapshot.validate_request(&change) {
                            self.notice = error.to_string();
                            return;
                        }
                        let snapshot = snapshot.clone();
                        self.field = Some(change.field());
                        self.waiting = true;
                        self.intent = Some(Intent::Review {
                            snapshot,
                            change,
                            source: None,
                        });
                        self.hide();
                        return;
                    }
                    _ => {}
                }
            }
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
        if !self.open || self.quick {
            return;
        }
        if self.list_mode {
            match self.list.mouse(event) {
                crate::tool_list_view::Act::Close => self.close(),
                crate::tool_list_view::Act::Toggle(target) => self.list_toggle(target),
                crate::tool_list_view::Act::Nothing => {}
            }
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(3),
            MouseEventKind::ScrollDown => self.scroll = (self.scroll + 3).min(self.max_scroll),
            _ => {}
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open || self.quick || area.width < 24 || area.height < 8 {
            self.hide();
            return;
        }
        if self.list_mode {
            frame.render_widget(Clear, area);
            let notice = self.notice.clone();
            match self.tool_snapshot.as_deref() {
                Some(snapshot) => {
                    self.list
                        .draw(frame.buffer_mut(), area, snapshot, &notice, theme)
                }
                None => frame.buffer_mut().set_style(
                    area,
                    Style::default().fg(theme.foreground).bg(theme.background),
                ),
            }
            self.painted = true;
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
            Some(Field::ToolSelection) => " Individual tools ",
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
            if let Some(snapshot) = self.tool_snapshot.as_ref() {
                let inventory = snapshot.tools.as_ref().expect("verified list");
                let rows = usize::from(inner.height.saturating_sub(9)).max(1);
                let first = self.selected_tool.saturating_sub(rows - 1);
                let mut lines = vec![
                    format!(
                        "Global Tools: {} · Authority: {} · MCP: {}",
                        snapshot.facts.tools_enabled,
                        snapshot.facts.effective_profile.as_str(),
                        snapshot.facts.mcp_enabled
                    ),
                    "Preferences apply to host tools; authority and argument gates still apply."
                        .into(),
                    format!(
                        "Dynamic sources: {} · MCP tools are controlled per server.",
                        inventory.dynamic_sources
                    ),
                ];
                if snapshot.facts.inventory == controls::Inventory::ClaudeSession {
                    lines
                        .push("Claude inventory stays fixed until an explicit new session.".into());
                }
                for (index, tool) in inventory.rows.iter().enumerate().skip(first).take(rows) {
                    lines.push(format!(
                        "{} [{}] {} · {}",
                        if index == self.selected_tool {
                            "›"
                        } else {
                            " "
                        },
                        if tool.enabled { "x" } else { " " },
                        tool.name,
                        tool.authority
                    ));
                }
                lines.push(self.notice.clone());
                lines.push(
                    "↑↓ select · Enter review tool · G review global Tools · F5 refresh · Esc"
                        .into(),
                );
                let safe = lines
                    .iter()
                    .map(|line| crate::ui::display_safe(line))
                    .collect::<Vec<_>>()
                    .join("\n");
                frame.render_widget(Paragraph::new(safe), inner);
                self.painted = inner.width >= 40 && inner.height >= 10;
                return;
            }
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
