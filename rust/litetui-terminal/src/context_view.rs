//! Read-only Context panel. Work correlates RPC replies and owns global input priority.
use crate::{
    context::{Document, Page, Request, Status},
    theme::Theme,
    ui,
};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use sha2::{Digest, Sha256};

#[derive(Default)]
pub struct View {
    editor: Option<crate::context_edit_view::Editor>,
    editor_visible: bool,
    topic_index: String,
    topics: Option<Vec<Document>>,
    topic_selected: usize,
    topic_name: Option<crate::prepared_skill::BoundedDraft<512>>,
    reloading_draft: bool,
    open: bool,
    document: Document,
    pending: Option<Request>,
    page: Option<Page>,
    notice: String,
    // Offset and hash of the bytes preceding each visited boundary. Going
    // back still requests that page from the source, which checks its revision.
    boundaries: Vec<(usize, Sha256)>,
    scroll: usize,
    wrapped: Vec<String>,
    width: Option<u16>,
    body: Option<Rect>,
    tabs: Vec<(Document, Rect)>,
}

impl View {
    pub fn editing(&self) -> bool {
        self.editor.is_some()
    }
    pub fn accepts_paste(&self) -> bool {
        self.topic_name.is_some() && self.body.is_some()
            || self.editor_visible
                && self
                    .editor
                    .as_ref()
                    .is_some_and(|editor| editor.accepts_paste())
    }
    pub fn paste(&mut self, text: &str) {
        if let Some(name) = self.topic_name.as_mut() {
            if self.body.is_some()
                && !text.chars().any(char::is_control)
                && let Err(e) = name.insert(text)
            {
                self.notice = e.to_string();
            }
            return;
        }
        if let Some(editor) = self.editor.as_mut() {
            editor.paste(text);
        }
    }
    pub fn edit_key(&mut self, key: KeyEvent) -> Option<crate::context_edit::Request> {
        use crate::context_edit_view::Action;
        if let Some(editor) = self.editor.as_mut() {
            if !self.editor_visible {
                return None;
            }
            match editor.key(key) {
                Action::Request(r) => return Some(*r),
                Action::Back => self.editor = None,
                Action::Stay => {}
            }
        } else if self.body.is_some()
            && self.pending.is_none()
            && key.kind == KeyEventKind::Press
            && key.code == KeyCode::Char('e')
            && key.modifiers.is_empty()
            && crate::context_edit::editable(&self.document)
            && let Some(page) = self
                .page
                .as_ref()
                .filter(|p| matches!(p.status, Status::Page | Status::Empty | Status::Missing))
        {
            return Some(crate::context_edit::Request::Open(
                self.document.clone(),
                page.revision.clone(),
            ));
        }
        None
    }
    pub fn reload_draft(&mut self, key: KeyEvent) -> Option<Request> {
        if self.editor.is_some()
            && self.accepts_paste()
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && key.code == KeyCode::F(5)
        {
            self.reloading_draft = true;
            Some(self.select(self.document.clone()))
        } else {
            None
        }
    }
    pub fn continue_draft_reload(&mut self) -> Option<crate::context_edit::Request> {
        if !self.reloading_draft {
            return None;
        }
        if let Some(page) = self
            .page
            .as_ref()
            .filter(|p| matches!(p.status, Status::Page | Status::Empty | Status::Missing))
        {
            return Some(crate::context_edit::Request::Open(
                self.document.clone(),
                page.revision.clone(),
            ));
        }
        self.edit_rejected("The current file could not be reloaded.");
        None
    }
    pub fn reload(&mut self) -> Request {
        self.select(self.document.clone())
    }
    pub fn edit_reply(&mut self, reply: crate::context_edit::Reply) {
        use crate::context_edit::Reply;
        if !self.open {
            return;
        }
        match reply {
            Reply::Opened(base) if base.document == self.document => {
                if self.reloading_draft
                    && let Some(editor) = self.editor.as_mut()
                {
                    editor.rebase(base);
                } else {
                    self.editor = Some(crate::context_edit_view::Editor::new(base));
                }
                self.reloading_draft = false;
            }
            Reply::Reviewed(review) => {
                if let Some(editor) = self.editor.as_mut() {
                    editor.reviewed(review);
                }
            }
            Reply::Saved(base) => {
                if let Some(editor) = self.editor.as_mut() {
                    editor.saved(base);
                }
            }
            _ => {}
        }
    }
    pub fn edit_rejected(&mut self, reason: &str) {
        self.reloading_draft = false;
        if let Some(editor) = self.editor.as_mut() {
            editor.reject(reason);
        } else {
            self.notice = reason.into();
        }
    }

    pub fn open(&mut self) -> Request {
        self.open_document(Document::System)
    }

    pub fn open_document(&mut self, document: Document) -> Request {
        self.open = true;
        self.select(document)
    }

    pub fn is_open(&self) -> bool {
        self.open
    }
    pub fn document(&self) -> Document {
        self.document.clone()
    }
    pub fn page(&self) -> Option<&Page> {
        self.page.as_ref()
    }
    pub fn notice(&self) -> &str {
        &self.notice
    }

    pub fn close(&mut self) {
        *self = Self::default();
    }

    pub fn hide(&mut self) {
        self.body = None;
        self.editor_visible = false;
        self.tabs.clear();
    }

    fn select(&mut self, document: Document) -> Request {
        self.topic_name = None;
        self.topics = None;
        self.topic_index.clear();
        self.document = document.clone();
        self.page = None;
        self.width = None;
        self.wrapped.clear();
        self.scroll = 0;
        self.boundaries.clear();
        self.boundaries.push((0, Sha256::new()));
        self.read(Request::first(document))
    }

    fn read(&mut self, request: Request) -> Request {
        self.notice = "Reading…".into();
        self.pending = Some(request.clone());
        request
    }

    pub fn reject(&mut self, reason: &str) {
        if self.open {
            self.page = None;
            self.topic_index.clear();
            self.topics = None;
            self.pending = None;
            self.wrapped.clear();
            self.width = None;
            self.scroll = 0;
            self.boundaries.clear();
            self.notice = format!("Read failed. {reason}");
        }
    }

    pub fn apply(&mut self, page: Page) {
        let Some(request) = self.pending.take() else {
            return;
        };
        if request.document != page.document || request.offset != page.offset {
            self.reject("Reply belongs to another document or page.");
            return;
        }
        if matches!(page.status, Status::Page | Status::Empty) {
            let Some(index) = self
                .boundaries
                .iter()
                .position(|(offset, _)| *offset == page.offset)
            else {
                self.reject("This page does not follow a verified document boundary.");
                return;
            };
            let digest = self.boundaries[index]
                .1
                .clone()
                .chain_update(page.text.as_bytes());
            if self
                .page
                .as_ref()
                .is_some_and(|prior| prior.total_bytes != page.total_bytes)
                || request
                    .revision
                    .as_ref()
                    .is_some_and(|revision| page.revision.as_ref() != Some(revision))
                || page.next_offset.is_none()
                    && page.revision.as_deref() != Some(&format!("{:x}", digest.clone().finalize()))
            {
                self.reject("Received pages disagree with the document size or revision. Press F5 to read again.");
                return;
            }
            self.boundaries.truncate(index + 1);
            if let Some(next) = page.next_offset {
                self.boundaries.push((next, digest));
            }
        } else {
            self.boundaries.clear();
        }
        self.notice = match page.status {
            Status::Page => format!(
                "Bytes {}–{} of {}",
                page.offset + 1,
                page.offset + page.text.len(),
                page.total_bytes.unwrap_or(0)
            ),
            Status::Empty => "This document is empty.".into(),
            Status::Missing => "This document is absent.".into(),
            Status::Oversize if self.document == Document::Topics => {
                "Topic list exceeds its directory, depth or file-count limit.".into()
            }
            Status::Oversize => "Document exceeds the 256 KiB reader limit.".into(),
            Status::Changed => "Document changed. Press F5 to read its current contents.".into(),
        };
        self.width = None;
        self.wrapped.clear();
        self.scroll = 0;
        if self.document == Document::Topics && matches!(page.status, Status::Page | Status::Empty)
        {
            if page.total_bytes.is_some_and(|total| total > 256 * 513) {
                self.reject("Topic list exceeds the source limits");
                return;
            }
            if self.topic_index.len() != page.offset {
                self.reject("Topic index did not progress from a verified boundary");
                return;
            }
            self.topic_index.push_str(&page.text);
            if page.next_offset.is_none() {
                let result = if self.topic_index.is_empty() {
                    Ok(Vec::new())
                } else {
                    self.topic_index
                        .split('\n')
                        .take(257)
                        .map(Document::topic)
                        .collect::<std::io::Result<Vec<_>>>()
                };
                match result {
                    Ok(rows)
                        if rows.len() <= 256
                            && rows.windows(2).all(|w| w[0].key() < w[1].key()) =>
                    {
                        self.topic_selected = self.topic_selected.min(rows.len().saturating_sub(1));
                        self.topics = Some(rows);
                        self.notice.clear();
                    }
                    _ => {
                        self.reject("Invalid or duplicate names in the topic index");
                        return;
                    }
                }
            }
        }
        self.page = Some(page);
    }

    pub fn continue_topics(&mut self) -> Option<Request> {
        if self.document == Document::Topics && self.topics.is_none() {
            self.adjacent(true)
        } else {
            None
        }
    }
    fn adjacent(&mut self, forward: bool) -> Option<Request> {
        let page = self.page.as_ref()?;
        let offset = if forward {
            page.next_offset?
        } else {
            self.boundaries
                .iter()
                .rev()
                .find(|(offset, _)| *offset < page.offset)?
                .0
        };
        let revision = page.revision.clone()?;
        Some(self.read(Request {
            document: self.document.clone(),
            offset,
            revision: Some(revision),
        }))
    }

    fn scroll_by(&mut self, delta: isize) {
        if let Some(body) = self.body {
            let limit = self.wrapped.len().saturating_sub(usize::from(body.height));
            self.scroll = self.scroll.saturating_add_signed(delta).min(limit);
        }
    }

    pub fn key(&mut self, event: KeyEvent) -> Option<Request> {
        if !self.open || event.kind == KeyEventKind::Release {
            return None;
        }
        if self.topic_name.is_some() {
            if (self.body.is_none() && event.code != KeyCode::Esc)
                || event.kind != KeyEventKind::Press
            {
                return None;
            }
            let name = self.topic_name.as_mut().expect("active topic name");
            match (event.code, event.modifiers) {
                (KeyCode::Esc, KeyModifiers::NONE) => {
                    self.topic_name = None;
                    self.notice.clear();
                }
                (KeyCode::Enter, KeyModifiers::NONE) => {
                    let full = format!("memories/{}", name.text());
                    match Document::topic(&full) {
                        Ok(doc) => return Some(self.select(doc)),
                        Err(e) => self.notice = e.to_string(),
                    }
                }
                (KeyCode::Char(c), KeyModifiers::NONE | KeyModifiers::SHIFT) => {
                    self.paste(c.encode_utf8(&mut [0; 4]))
                }
                (code, KeyModifiers::NONE) => name.key(code),
                _ => {}
            }
            return None;
        }
        if event.code == KeyCode::Esc
            && event.modifiers.is_empty()
            && event.kind == KeyEventKind::Press
        {
            if matches!(self.document, Document::Topic(_)) {
                return Some(self.select(Document::Topics));
            }
            self.close();
            return None;
        }
        let body = self.body?;
        if self.pending.is_some() {
            return None;
        }
        let previous = event.code == KeyCode::BackTab
            && matches!(event.modifiers, KeyModifiers::NONE | KeyModifiers::SHIFT);
        if !event.modifiers.is_empty() && !previous {
            return None;
        }
        if self.document == Document::Topics
            && let Some(rows) = &self.topics
        {
            match event.code {
                KeyCode::Up => {
                    self.topic_selected = self.topic_selected.saturating_sub(1);
                    return None;
                }
                KeyCode::Down => {
                    self.topic_selected =
                        (self.topic_selected + 1).min(rows.len().saturating_sub(1));
                    return None;
                }
                KeyCode::Home => {
                    self.topic_selected = 0;
                    return None;
                }
                KeyCode::End => {
                    self.topic_selected = rows.len().saturating_sub(1);
                    return None;
                }
                KeyCode::PageUp => {
                    self.topic_selected =
                        self.topic_selected.saturating_sub(usize::from(body.height));
                    return None;
                }
                KeyCode::PageDown => {
                    self.topic_selected = (self.topic_selected + usize::from(body.height))
                        .min(rows.len().saturating_sub(1));
                    return None;
                }
                KeyCode::Char('[' | ']') => return None,
                KeyCode::Enter if event.kind == KeyEventKind::Press => {
                    return rows
                        .get(self.topic_selected)
                        .cloned()
                        .map(|doc| self.select(doc));
                }
                KeyCode::Char('n') if event.kind == KeyEventKind::Press => {
                    self.topic_name = Some(Default::default());
                    self.notice.clear();
                    return None;
                }
                _ => {}
            }
        }
        match event.code {
            KeyCode::Up => self.scroll_by(-1),
            KeyCode::Down => self.scroll_by(1),
            KeyCode::PageUp => self.scroll_by(-isize::try_from(body.height.max(1)).unwrap_or(1)),
            KeyCode::PageDown => self.scroll_by(isize::try_from(body.height.max(1)).unwrap_or(1)),
            KeyCode::Home => self.scroll = 0,
            KeyCode::End => self.scroll_by(isize::MAX),
            _ if event.kind != KeyEventKind::Press => {}
            KeyCode::F(5) => return Some(self.select(self.document.clone())),
            KeyCode::Char('[') => return self.adjacent(false),
            KeyCode::Char(']') => return self.adjacent(true),
            KeyCode::Tab | KeyCode::BackTab | KeyCode::Left | KeyCode::Right => {
                let tab = if matches!(self.document, Document::Topic(_)) {
                    &Document::Topics
                } else {
                    &self.document
                };
                let index = Document::ALL.iter().position(|doc| doc == tab)?;
                let step = if previous || event.code == KeyCode::Left {
                    Document::ALL.len() - 1
                } else {
                    1
                };
                return Some(
                    self.select(Document::ALL[(index + step) % Document::ALL.len()].clone()),
                );
            }
            _ => {}
        }
        None
    }

    pub fn mouse(&mut self, event: MouseEvent) -> Option<Request> {
        let body = self.body?;
        if self.pending.is_some() || self.topic_name.is_some() || !event.modifiers.is_empty() {
            return None;
        }
        let at = Position::new(event.column, event.row);
        if let Some(rows) = self.topics.as_ref()
            && body.contains(at)
        {
            let end = rows.len().saturating_sub(1);
            match event.kind {
                MouseEventKind::ScrollUp => {
                    self.topic_selected = self.topic_selected.saturating_sub(3)
                }
                MouseEventKind::ScrollDown => {
                    self.topic_selected = (self.topic_selected + 3).min(end)
                }
                MouseEventKind::Down(MouseButton::Left) => {
                    let first = self
                        .topic_selected
                        .saturating_sub(usize::from(body.height).saturating_sub(1));
                    self.topic_selected = (first + usize::from(at.y - body.y)).min(end);
                }
                _ => {}
            }
            return None;
        }
        match event.kind {
            MouseEventKind::ScrollUp if body.contains(at) => self.scroll_by(-3),
            MouseEventKind::ScrollDown if body.contains(at) => self.scroll_by(3),
            MouseEventKind::Down(MouseButton::Left) => {
                let doc = self
                    .tabs
                    .iter()
                    .find(|(_, tab)| tab.contains(at))?
                    .0
                    .clone();
                if doc != self.document {
                    return Some(self.select(doc));
                }
            }
            _ => {}
        }
        None
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.hide();
        if !self.open {
            return;
        }
        let area = area.intersection(frame.area());
        let width = area.width.saturating_sub(2).min(110);
        let height = area.height.saturating_sub(2).min(36);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if width < 24 || height < 9 {
            if let Some(editor) = self.editor.as_mut() {
                editor.invalidate_layout();
            }
            frame.render_widget(Clear, area);
            frame.render_widget(
                Paragraph::new("Resize to read Context, or press Esc.")
                    .block(Block::bordered().title(" Context ")),
                area,
            );
            return;
        }
        let block = Block::bordered()
            .title(format!(
                " Context · {} ",
                ui::display_safe(self.document.label())
            ))
            .style(Style::default().fg(theme.foreground).bg(theme.panel));
        let inner = block.inner(rect);
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);

        if let Some(editor) = self.editor.as_mut() {
            editor.draw(frame, inner, theme);
            self.editor_visible = true;
            return;
        }
        let mut x = inner.x;
        for doc in Document::ALL {
            let label = format!(" {} ", doc.label());
            let width = label.len() as u16;
            if width > inner.right().saturating_sub(x) {
                break;
            }
            let tab = Rect::new(x, inner.y, width, 1);
            let style = if doc == self.document
                || doc == Document::Topics && matches!(self.document, Document::Topic(_))
            {
                Style::default().fg(theme.background).bg(theme.primary)
            } else {
                Style::default().fg(theme.title)
            };
            frame.render_widget(Paragraph::new(label).style(style), tab);
            self.tabs.push((doc, tab));
            x += width;
        }
        let body = Rect::new(inner.x, inner.y + 2, inner.width, inner.height - 4);
        // An error/absence is readable even when it is too long for the status row.
        let has_text = self.page.as_ref().is_some_and(|p| p.status == Status::Page);
        let status = if has_text { self.notice.as_str() } else { "" };
        frame.render_widget(
            Paragraph::new(status).style(Style::default().fg(theme.title)),
            Rect::new(inner.x, inner.y + 1, inner.width, 1),
        );
        if self.width != Some(body.width) {
            let source = self
                .page
                .as_ref()
                .filter(|p| p.status == Status::Page)
                .map_or(self.notice.as_str(), |p| p.text.as_str());
            self.wrapped = ui::wrap(
                &ui::display_safe(&source.replace("\r\n", "\n")),
                usize::from(body.width),
            );
            self.width = Some(body.width);
        }
        self.scroll = self
            .scroll
            .min(self.wrapped.len().saturating_sub(usize::from(body.height)));
        let lines = self
            .wrapped
            .iter()
            .skip(self.scroll)
            .take(usize::from(body.height))
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(Paragraph::new(lines), body);
        self.body = Some(body);
        if self.document == Document::Topics {
            let (content, help) = if let Some(name) = &self.topic_name {
                let (before, after) = name.text().split_at(name.cursor());
                (
                    format!(
                        "Topic path beneath memories/\n{}▏{}\n{}",
                        ui::display_safe(before),
                        ui::display_safe(after),
                        ui::display_safe(&self.notice)
                    ),
                    "Enter open path · Esc cancel",
                )
            } else if let Some(rows) = &self.topics {
                let start = self
                    .topic_selected
                    .saturating_sub(usize::from(body.height).saturating_sub(1));
                let visible = rows
                    .iter()
                    .enumerate()
                    .skip(start)
                    .take(usize::from(body.height))
                    .map(|(index, doc)| {
                        format!(
                            "{} {}",
                            if index == self.topic_selected {
                                "›"
                            } else {
                                " "
                            },
                            ui::display_safe(doc.key().strip_prefix("memories/").unwrap())
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                (
                    if rows.is_empty() {
                        "No memory topics.".into()
                    } else {
                        visible
                    },
                    "↑ ↓ select · Enter open · n new · F5 refresh",
                )
            } else {
                (
                    ui::display_safe(&self.notice),
                    "F5 refresh · Esc returns to Work",
                )
            };
            frame.render_widget(Clear, body);
            frame.render_widget(Paragraph::new(content), body);
            frame.render_widget(
                Paragraph::new(help).style(Style::default().fg(theme.title)),
                Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
            );
            return;
        }
        let keys = if matches!(self.document, Document::Topic(_)) {
            "e edit or create · [ ] pages · Esc topics"
        } else if self.document == Document::Hooks && inner.width >= 48 {
            "← → / Tab documents · E manage hook toggles\n↑ ↓ scroll · F5 refresh · Esc returns to Work"
        } else if crate::context_edit::editable(&self.document) && inner.width >= 48 {
            "← → / Tab documents · [ ] pages · e edit\n↑ ↓ / PgUp PgDn scroll · F5 refresh · Esc returns to Work"
        } else if inner.width >= 48 {
            "← → / Tab documents · [ ] pages · F5 refresh\n↑ ↓ / PgUp PgDn scroll · Esc returns to Work"
        } else {
            "← → docs · [ ] pages\n↑ ↓ scroll · F5 · Esc"
        };
        frame.render_widget(
            Paragraph::new(keys).style(Style::default().fg(theme.title)),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
        );
    }
}
