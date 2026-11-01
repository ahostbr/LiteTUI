//! Exclusive inert Saved History inspector. It never merges into live turn rows.
use crate::{
    history::{Anchor, Fragment, Kind, Page, PageRequest, Snapshot},
    theme::Theme,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph},
};
use std::io;
use unicode_segmentation::UnicodeSegmentation;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Target {
    #[default]
    Current,
    Saved {
        session: String,
    },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadKey {
    pub target: Target,
    pub generation: u64,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Inspect {
        key: ReadKey,
        revision: u64,
        session: String,
    },
    Refresh {
        key: ReadKey,
        anchor: Anchor,
    },
    Page {
        key: ReadKey,
        snapshot: String,
        position: PageRequest,
    },
}
impl Intent {
    pub fn key(&self) -> &ReadKey {
        match self {
            Self::Inspect { key, .. } | Self::Refresh { key, .. } | Self::Page { key, .. } => key,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Loaded {
        key: ReadKey,
        snapshot: Box<Snapshot>,
        page: Box<Page>,
    },
    Page {
        key: ReadKey,
        page: Box<Page>,
    },
    Rejected {
        key: ReadKey,
        reason: String,
    },
}
impl Update {
    pub fn key(&self) -> &ReadKey {
        match self {
            Self::Loaded { key, .. } | Self::Page { key, .. } | Self::Rejected { key, .. } => key,
        }
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum Edge {
    #[default]
    Keep,
    Start,
    End,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Button {
    Older,
    Newer,
    Refresh,
    Close,
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct TextAnchor {
    block: String,
    part: u64,
    offset: Option<u64>,
}
struct RenderedLine {
    text: String,
    kind: Kind,
    heading: bool,
    anchor: TextAnchor,
}

#[derive(Default)]
pub struct View {
    pub snapshot: Option<Snapshot>,
    pub page: Option<Page>,
    pub notice: String,
    open: bool,
    waiting: bool,
    target: Target,
    generation: u64,
    pending: Option<ReadKey>,
    stale: bool,
    visible: bool,
    intent: Option<Intent>,
    scroll: usize,
    max_scroll: usize,
    body_rows: usize,
    edge: Edge,
    anchor: Option<TextAnchor>,
    width: u16,
    geometry: Option<Rect>,
    area: Rect,
    body: Rect,
    buttons: Vec<(Button, Rect)>,
    pressed: Option<(Button, u16, u16)>,
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.select_target(Target::Current);
        self.open = true;
        self.refresh();
    }
    pub fn inspect(&mut self, revision: u64, session: String, current: bool) {
        if self.waiting {
            return;
        }
        self.select_target(if current {
            Target::Current
        } else {
            Target::Saved {
                session: session.clone(),
            }
        });
        self.open = true;
        let Some(key) = self.begin_read() else {
            return;
        };
        self.intent = Some(Intent::Inspect {
            key,
            revision,
            session,
        });
        self.edge = Edge::End;
        self.notice = "Inspecting saved history; the current conversation stays unchanged.".into();
    }
    fn select_target(&mut self, target: Target) {
        if self.target != target {
            self.snapshot = None;
            self.page = None;
            self.scroll = 0;
            self.max_scroll = 0;
            self.anchor = None;
            self.stale = false;
        }
        self.target = target;
    }
    fn begin_read(&mut self) -> Option<ReadKey> {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice =
                "History read identity exhausted; reopen the conversation runtime.".into();
            return None;
        };
        self.generation = generation;
        let key = ReadKey {
            target: self.target.clone(),
            generation,
        };
        self.pending = Some(key.clone());
        self.waiting = true;
        self.hide();
        Some(key)
    }
    pub fn pending_key(&self) -> Option<&ReadKey> {
        self.pending.as_ref()
    }
    /// Startup/open may offer a preverified tail without replaying it as live output.
    pub fn install(&mut self, snapshot: Snapshot, page: Page, show: bool) -> io::Result<()> {
        verify_loaded(&snapshot, &page)?;
        verify_target(&Target::Current, &snapshot)?;
        self.target = Target::Current;
        self.pending = None;
        self.snapshot = Some(snapshot);
        self.page = Some(page);
        self.open = show;
        self.waiting = false;
        self.stale = false;
        self.intent = None;
        self.edge = Edge::End;
        self.anchor = None;
        self.notice =
            "Saved display only; full conversation context remains in the source runtime.".into();
        self.hide();
        Ok(())
    }
    pub fn is_open(&self) -> bool {
        self.open
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn waiting(&self) -> bool {
        self.waiting
    }
    pub fn is_stale(&self) -> bool {
        self.stale
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
    }
    pub fn close(&mut self) {
        self.open = false;
        if self.intent.take().is_some() {
            self.waiting = false;
            self.pending = None;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        let generation = self.generation;
        *self = Self {
            generation,
            ..Self::default()
        };
    }
    /// An admitted turn invalidates historical navigation but does not erase local outcomes.
    pub fn invalidate(&mut self) {
        self.stale = true;
        self.notice =
            "History changed. F5 reads a fresh saved snapshot; the previous page is retained."
                .into();
        self.hide();
    }
    fn refresh(&mut self) {
        if self.waiting {
            return;
        }
        let Some(key) = self.begin_read() else {
            return;
        };
        self.intent = Some(Intent::Refresh {
            key,
            anchor: Anchor::Tail,
        });
        self.edge = Edge::End;
        self.notice = "Reading saved history; no provider refresh or execution.".into();
    }
    pub fn observe(&mut self, update: Update) -> io::Result<()> {
        // Late reads cannot clear a newer pending read, visibility, or retained page.
        if self.pending.as_ref() != Some(update.key()) {
            return Ok(());
        }
        match &update {
            Update::Loaded { snapshot, page, .. } => {
                verify_loaded(snapshot, page)?;
                verify_target(&self.target, snapshot)?;
            }
            Update::Page { page, .. } => {
                let snapshot = self
                    .snapshot
                    .as_ref()
                    .ok_or_else(|| io::Error::other("History page arrived without descriptor"))?;
                verify_loaded(snapshot, page)?;
                verify_target(&self.target, snapshot)?;
            }
            Update::Rejected { .. } => {}
        }
        self.waiting = false;
        self.pending = None;
        self.intent = None;
        self.hide();
        // A cancelled read settles only its wire, without reopening the inspector.
        if !self.open {
            return Ok(());
        }
        match update {
            Update::Loaded { snapshot, page, .. } => {
                self.snapshot = Some(*snapshot);
                self.page = Some(*page);
                self.stale = false;
                self.anchor = None;
                self.notice = "Saved display only; no historical action is active.".into();
            }
            Update::Page { page, .. } => {
                self.page = Some(*page);
                self.stale = false;
                self.anchor = None;
                self.notice = "Saved display only; no historical action is active.".into();
            }
            Update::Rejected { reason, .. } => {
                self.stale = true;
                self.edge = Edge::Keep;
                self.notice = format!("{reason} · F5 refresh; previous page kept.");
            }
        }
        Ok(())
    }
    fn request(&mut self, position: PageRequest, edge: Edge) {
        if self.waiting || self.stale {
            return;
        }
        let Some(snapshot) = self
            .snapshot
            .as_ref()
            .map(|snapshot| snapshot.token.clone())
        else {
            return;
        };
        let Some(key) = self.begin_read() else {
            return;
        };
        self.intent = Some(Intent::Page {
            key,
            snapshot,
            position,
        });
        self.edge = edge;
        self.notice = "Loading saved history page…".into();
    }
    fn older(&mut self) {
        if let Some(cursor) = self.page.as_ref().and_then(|p| p.previous.clone()) {
            self.request(PageRequest::Cursor(cursor), Edge::End);
        }
    }
    fn newer(&mut self) {
        if let Some(cursor) = self.page.as_ref().and_then(|p| p.next.clone()) {
            self.request(PageRequest::Cursor(cursor), Edge::Start);
        }
    }
    fn scroll_up(&mut self, amount: usize) {
        if self.scroll == 0 {
            self.older();
        } else {
            self.scroll = self.scroll.saturating_sub(amount);
            self.anchor = None;
        }
    }
    fn scroll_down(&mut self, amount: usize) {
        if self.scroll >= self.max_scroll {
            self.newer();
        } else {
            self.scroll = self.scroll.saturating_add(amount).min(self.max_scroll);
            self.anchor = None;
        }
    }
    pub fn key(&mut self, event: KeyEvent) {
        if event.kind == KeyEventKind::Release {
            return;
        }
        if !event.modifiers.is_empty() {
            self.hide();
            return;
        }
        if event.code == KeyCode::Esc && event.kind == KeyEventKind::Press {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        match event.code {
            KeyCode::Up => self.scroll_up(1),
            KeyCode::Down => self.scroll_down(1),
            KeyCode::PageUp => self.scroll_up(self.body_rows.max(1)),
            KeyCode::PageDown => self.scroll_down(self.body_rows.max(1)),
            KeyCode::Left if event.kind == KeyEventKind::Press => self.older(),
            KeyCode::Right if event.kind == KeyEventKind::Press => self.newer(),
            KeyCode::Home if event.kind == KeyEventKind::Press => {
                self.request(PageRequest::Anchor(Anchor::Head), Edge::Start)
            }
            KeyCode::End if event.kind == KeyEventKind::Press => {
                self.request(PageRequest::Anchor(Anchor::Tail), Edge::End)
            }
            KeyCode::F(5) if event.kind == KeyEventKind::Press => self.refresh(),
            _ => self.pressed = None,
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if !event.modifiers.is_empty() || !self.area.contains((event.column, event.row).into()) {
            self.hide();
            return;
        }
        let hit = self
            .buttons
            .iter()
            .find(|(_, r)| r.contains((event.column, event.row).into()))
            .map(|(b, _)| *b);
        match event.kind {
            MouseEventKind::ScrollUp if self.body.contains((event.column, event.row).into()) => {
                self.scroll_up(3)
            }
            MouseEventKind::ScrollDown if self.body.contains((event.column, event.row).into()) => {
                self.scroll_down(3)
            }
            MouseEventKind::Down(MouseButton::Left) => {
                self.pressed = hit.map(|b| (b, event.column, event.row))
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if let Some(button) = hit
                    && self.pressed.take() == Some((button, event.column, event.row))
                {
                    match button {
                        Button::Older => self.older(),
                        Button::Newer => self.newer(),
                        Button::Refresh => self.refresh(),
                        Button::Close => self.close(),
                    }
                }
            }
            _ => self.pressed = None,
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open {
            self.hide();
            return;
        }
        if self.geometry != Some(area) {
            self.hide();
            self.geometry = Some(area);
        }
        let width = area.width.min(110);
        let height = area.height.min(42);
        let panel = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        self.area = panel;
        frame.render_widget(Clear, panel);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(" Saved History · read only ")
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(panel);
        frame.render_widget(block, panel);
        if area.width < 42 || area.height < 18 {
            frame.render_widget(
                Paragraph::new("Resize to 42×18 for Saved History.\nEsc: close"),
                inner,
            );
            self.hide();
            return;
        }
        self.visible = true;
        self.buttons.clear();
        let summary = self
            .snapshot
            .as_ref()
            .map(|s| {
                format!(
                    "{} · {} messages ({} system hidden)",
                    safe(&s.title),
                    s.counts.messages,
                    s.counts.system
                )
            })
            .unwrap_or_else(|| "Reading source-owned saved context…".into());
        frame.render_widget(
            Paragraph::new(
                crate::ui::wrap(&summary, inner.width as usize)
                    .into_iter()
                    .take(2)
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            Rect::new(inner.x, inner.y, inner.width, 2),
        );
        let status = if self.waiting {
            "Loading · Esc cancels this view"
        } else if self.stale {
            "Snapshot changed · F5 refresh before paging"
        } else {
            "Saved display only · no actions or timers"
        };
        frame.render_widget(
            Paragraph::new(status).style(Style::default().fg(if self.stale {
                theme.error
            } else {
                theme.thinking_text
            })),
            Rect::new(inner.x, inner.y + 2, inner.width, 1),
        );
        self.body = Rect::new(
            inner.x,
            inner.y + 4,
            inner.width,
            inner.height.saturating_sub(8),
        );
        self.body_rows = self.body.height as usize;
        let mut lines = Vec::new();
        if let Some(page) = &self.page {
            for fragment in &page.fragments {
                append_fragment(&mut lines, fragment, self.body.width as usize);
            }
        }
        if lines.is_empty() {
            frame.render_widget(
                Paragraph::new(if self.waiting {
                    "Waiting for saved history…"
                } else {
                    "No visible saved messages. Full context proof retained."
                }),
                self.body,
            );
            self.scroll = 0;
            self.max_scroll = 0;
        } else {
            self.max_scroll = lines.len().saturating_sub(self.body_rows);
            if self.width != self.body.width
                && self.edge == Edge::Keep
                && let Some(anchor) = &self.anchor
                && let Some(index) = lines.iter().rposition(|line| {
                    line.anchor.block == anchor.block
                        && line.anchor.part == anchor.part
                        && match (line.anchor.offset, anchor.offset) {
                            (Some(a), Some(b)) => a <= b,
                            (None, None) => true,
                            _ => false,
                        }
                })
            {
                self.scroll = index;
            }
            self.width = self.body.width;
            match self.edge {
                Edge::Start => self.scroll = 0,
                Edge::End => self.scroll = self.max_scroll,
                Edge::Keep => {}
            }
            self.edge = Edge::Keep;
            self.scroll = self.scroll.min(self.max_scroll);
            self.anchor = lines.get(self.scroll).map(|l| l.anchor.clone());
            let shown = lines
                .into_iter()
                .skip(self.scroll)
                .take(self.body_rows)
                .map(|line| {
                    let color = match line.kind {
                        Kind::Tool | Kind::Attachment => theme.tool_text,
                        Kind::Reasoning => theme.thinking_text,
                        Kind::Plan | Kind::Summary => theme.title,
                        Kind::Notice => theme.thinking_text,
                        _ => theme.foreground,
                    };
                    let style = Style::default().fg(color).add_modifier(if line.heading {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    });
                    Line::styled(line.text, style)
                })
                .collect::<Vec<_>>();
            frame.render_widget(Paragraph::new(shown), self.body);
        }
        let location = self
            .page
            .as_ref()
            .map(|p| match (p.fragments.first(), p.fragments.last()) {
                (Some(first), Some(last)) => format!(
                    "Blocks {}–{} / {} · scroll {}/{}",
                    first.block_index + 1,
                    last.block_index + 1,
                    p.counts.blocks,
                    self.scroll,
                    self.max_scroll
                ),
                _ => "Empty saved display".into(),
            })
            .unwrap_or_default();
        frame.render_widget(
            Paragraph::new(location),
            Rect::new(inner.x, inner.bottom() - 4, inner.width, 1),
        );
        let notice = crate::ui::wrap(&safe(&self.notice), inner.width as usize)
            .into_iter()
            .take(1)
            .collect::<Vec<_>>()
            .join("");
        frame.render_widget(
            Paragraph::new(notice),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
        );
        let labels = [
            (Button::Older, "← Older", 9u16),
            (Button::Newer, "Newer →", 9),
            (Button::Refresh, "F5 Refresh", 12),
            (Button::Close, "Esc Close", 10),
        ];
        let mut x = inner.x;
        for (button, label, width) in labels {
            let rect = Rect::new(
                x,
                inner.bottom() - 2,
                width.min(inner.right().saturating_sub(x)),
                1,
            );
            frame.render_widget(
                Paragraph::new(label).style(Style::default().fg(theme.primary)),
                rect,
            );
            self.buttons.push((button, rect));
            x = x.saturating_add(width);
        }
        frame.render_widget(
            Paragraph::new("PgUp/PgDn scroll · Home/End first/last"),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}
fn verify_target(target: &Target, snapshot: &Snapshot) -> io::Result<()> {
    let matches = match target {
        Target::Current => {
            snapshot.identity.scope == crate::history::Scope::Current
                && snapshot.identity.session == snapshot.identity.from_session
                && snapshot.identity.catalog.is_none()
        }
        Target::Saved { session } => {
            snapshot.identity.scope == crate::history::Scope::Preview
                && &snapshot.identity.session == session
                && snapshot.identity.catalog.is_some()
        }
    };
    if !matches {
        return Err(io::Error::other(
            "History view received another inspection target",
        ));
    }
    Ok(())
}
fn verify_loaded(snapshot: &Snapshot, page: &Page) -> io::Result<()> {
    if snapshot.identity != page.identity
        || snapshot.token != page.snapshot
        || snapshot.context_sha256 != page.context_sha256
        || snapshot.replay_sha256 != page.replay_sha256
        || snapshot.counts != page.counts
    {
        return Err(io::Error::other(
            "History view received mismatched descriptor and page",
        ));
    }
    Ok(())
}
fn safe(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            '\n' => "\\n".into(),
            '\t' => "    ".into(),
            c if crate::display_control(c) => format!("\\u{{{:x}}}", c as u32),
            c => c.to_string(),
        })
        .collect()
}
fn append_fragment(lines: &mut Vec<RenderedLine>, fragment: &Fragment, width: usize) {
    let header = fragment.heading();
    for text in crate::ui::wrap(&safe(&header), width) {
        lines.push(RenderedLine {
            text,
            kind: fragment.kind,
            heading: true,
            anchor: TextAnchor {
                block: fragment.block_id.clone(),
                part: fragment.start,
                offset: None,
            },
        });
    }
    let mut source_offset = 0usize;
    for source in fragment.text.split('\n') {
        let mut text = String::new();
        let mut used = 0usize;
        let mut line_start = source_offset;
        for (offset, glyph) in source.grapheme_indices(true) {
            let shown = safe(glyph);
            let cells = crate::rendered_width(&shown);
            if used.saturating_add(cells) > width && !text.is_empty() {
                lines.push(RenderedLine {
                    text: std::mem::take(&mut text),
                    kind: fragment.kind,
                    heading: false,
                    anchor: TextAnchor {
                        block: fragment.block_id.clone(),
                        part: fragment.start,
                        offset: Some(fragment.start + line_start as u64),
                    },
                });
                used = 0;
                line_start = source_offset + offset;
            }
            if cells <= width {
                text.push_str(&shown);
                used += cells;
            } else {
                text.push('�');
                used += 1;
            }
        }
        lines.push(RenderedLine {
            text,
            kind: fragment.kind,
            heading: false,
            anchor: TextAnchor {
                block: fragment.block_id.clone(),
                part: fragment.start,
                offset: Some(fragment.start + line_start as u64),
            },
        });
        source_offset += source.len() + 1;
    }
    lines.push(RenderedLine {
        text: String::new(),
        kind: fragment.kind,
        heading: false,
        anchor: TextAnchor {
            block: fragment.block_id.clone(),
            part: fragment.start,
            offset: Some(fragment.end),
        },
    });
}
