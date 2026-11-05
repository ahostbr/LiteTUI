use crate::{
    context::MAX_DOCUMENT_BYTES,
    context_edit::{Base, Request, Review},
    prepared_skill::BoundedDraft,
    theme::Theme,
    ui,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{Frame, layout::Rect, style::Style, widgets::Paragraph};

pub(super) enum Action {
    Stay,
    Back,
    Request(Box<Request>),
}

enum Mode {
    Edit,
    Review(Box<Review>),
    Discard,
}
struct Layout {
    width: u16,
    height: u16,
    lines: Vec<String>,
    cursor_row: usize,
    seen: Vec<bool>,
    remaining: usize,
}

pub(super) struct Editor {
    layout: Option<Layout>,
    base: Base,
    draft: BoundedDraft<MAX_DOCUMENT_BYTES>,
    mode: Mode,
    notice: String,
    scroll: usize,
}

impl Editor {
    pub(super) fn new(base: Base) -> Self {
        let mut draft = BoundedDraft::default();
        draft.set(&base.text).expect("bounded source document");
        let notice = match base.system.as_ref().map(|f| f.stored.as_str()) {
            Some("session_only") => {
                "Not in a conversation file yet; saved with the first message.".to_owned()
            }
            Some("diverged") => {
                "The transcript differs from the live prompt; applying rewrites it.".to_owned()
            }
            _ => String::new(),
        };
        Self {
            base,
            draft,
            mode: Mode::Edit,
            notice,
            scroll: 0,
            layout: None,
        }
    }
    /// A `/system <text>` draft: the source base stays the comparison, the text is only a prefill.
    pub(super) fn with_draft(base: Base, text: &str) -> Result<Self, String> {
        let mut editor = Self::new(base);
        editor.draft.set(text).map_err(|e| e.to_string())?;
        editor.notice =
            "Prefilled from /system. Nothing is saved until you review and apply.".into();
        Ok(editor)
    }
    pub(super) fn invalidate_layout(&mut self) {
        self.layout = None;
    }
    pub(super) fn paste(&mut self, text: &str) {
        if self.accepts_paste() {
            self.layout = None;
            self.notice = self
                .draft
                .insert(text)
                .err()
                .map_or_else(String::new, |e| e.to_string());
        }
    }
    pub(super) fn accepts_paste(&self) -> bool {
        matches!(self.mode, Mode::Edit)
    }
    pub(super) fn reviewed(&mut self, review: Review) {
        if review.base == self.base && review.draft == self.draft.text() {
            self.mode = Mode::Review(Box::new(review));
            self.layout = None;
            self.scroll = 0;
            self.notice.clear();
        }
    }
    pub(super) fn reject(&mut self, notice: &str) {
        self.mode = Mode::Edit;
        self.layout = None;
        self.notice = format!("Draft kept. {notice}");
    }
    pub(super) fn rebase(&mut self, base: Base) {
        self.base = base;
        self.mode = Mode::Edit;
        self.layout = None;
        self.notice = "Current file reloaded. Draft kept for a new review.".into();
    }
    pub(super) fn saved(&mut self, base: Base) {
        let notice = match base.system.as_ref().map(|f| f.stored.as_str()) {
            Some("session_only") => {
                "Applied to this session and read back. It is written to the conversation file with the first message."
            }
            Some(_) => {
                "Saved to the transcript and verified on disk. Takes effect as described in the review."
            }
            None => "Saved and verified on disk.",
        };
        *self = Self::new(base);
        self.notice = notice.into();
    }
    pub(super) fn key(&mut self, key: KeyEvent) -> Action {
        if key.kind == KeyEventKind::Release {
            return Action::Stay;
        }
        let plain = key.modifiers.is_empty();
        let press = key.kind == KeyEventKind::Press;
        match &self.mode {
            Mode::Discard => {
                if plain && press {
                    match key.code {
                        KeyCode::Enter => return Action::Back,
                        KeyCode::Esc => {
                            self.mode = Mode::Edit;
                            self.layout = None;
                        }
                        _ => {}
                    }
                }
            }
            Mode::Review(review) => {
                if plain {
                    match key.code {
                        KeyCode::Esc if press => {
                            self.mode = Mode::Edit;
                            self.layout = None;
                        }
                        KeyCode::F(6)
                            if press && self.layout.as_ref().is_some_and(|p| p.remaining == 0) =>
                        {
                            return Action::Request(Box::new(Request::Apply(
                                review.as_ref().clone(),
                            )));
                        }
                        KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
                        KeyCode::Down => self.scroll = self.scroll.saturating_add(1),
                        KeyCode::PageUp => {
                            self.scroll = self.scroll.saturating_sub(
                                self.layout.as_ref().map_or(1, |p| usize::from(p.height)),
                            )
                        }
                        KeyCode::PageDown => {
                            self.scroll = self.scroll.saturating_add(
                                self.layout.as_ref().map_or(1, |p| usize::from(p.height)),
                            )
                        }
                        KeyCode::Home => self.scroll = 0,
                        KeyCode::End => self.scroll = usize::MAX,
                        _ => {}
                    }
                }
            }
            Mode::Edit => {
                if plain && press && key.code == KeyCode::Esc {
                    if self.base.text == self.draft.text() {
                        return Action::Back;
                    }
                    self.mode = Mode::Discard;
                    self.layout = None;
                } else if plain && press && key.code == KeyCode::F(6) {
                    return Action::Request(Box::new(Request::Review(
                        self.base.clone(),
                        self.draft.text().into(),
                    )));
                } else if plain && key.code == KeyCode::Enter {
                    self.paste("\n");
                } else if let KeyCode::Char(ch) = key.code
                    && matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::SHIFT)
                {
                    self.paste(ch.encode_utf8(&mut [0; 4]));
                } else if plain {
                    self.draft.key(key.code);
                    self.layout = None;
                }
            }
        }
        Action::Stay
    }

    fn layout(&self, body: Rect) -> Layout {
        let width = usize::from(body.width).max(1);
        let (lines, cursor_row) = match &self.mode {
            Mode::Discard => (Vec::new(), 0),
            Mode::Review(r) => {
                let before = if r.base.revision.is_none() {
                    "(absent)"
                } else if r.base.text.is_empty() {
                    "(empty)"
                } else {
                    &r.base.text
                };
                let after = if r.draft.is_empty() {
                    "(empty)"
                } else {
                    &r.draft
                };
                let path = if let crate::context::Document::Topic(name) = &r.base.document {
                    format!("{}\n\n", ui::display_safe(name))
                } else if let Some(facts) = &r.base.system {
                    format!("Takes effect: {}\n\n", ui::display_safe(&facts.effect))
                } else {
                    String::new()
                };
                let rendered = format!(
                    "{path}Before\n{}\n\nAfter\n{}",
                    ui::display_safe(before),
                    ui::display_safe(after)
                );
                (ui::wrap(&rendered, width), 0)
            }
            Mode::Edit => {
                let (left, right) = self.draft.text().split_at(self.draft.cursor());
                let prefix = format!("{}▏", ui::display_safe(left));
                let rendered = format!("{prefix}{}", ui::display_safe(right));
                (
                    ui::wrap(&rendered, width),
                    ui::wrap(&prefix, width).len().saturating_sub(1),
                )
            }
        };
        let remaining = lines.len();
        Layout {
            width: body.width,
            height: body.height,
            cursor_row,
            seen: vec![false; remaining],
            remaining,
            lines,
        }
    }
    pub(super) fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let body = Rect::new(
            area.x,
            area.y + 1,
            area.width,
            area.height.saturating_sub(3),
        );
        if self
            .layout
            .as_ref()
            .is_none_or(|p| p.width != body.width || p.height != body.height)
        {
            self.layout = Some(self.layout(body));
            self.scroll = 0;
        }
        let layout = self.layout.as_mut().expect("current render layout");
        let height = usize::from(body.height);
        if matches!(self.mode, Mode::Edit) {
            self.scroll = layout.cursor_row.saturating_sub(height.saturating_sub(1));
        }
        self.scroll = self.scroll.min(layout.lines.len().saturating_sub(height));
        let end = (self.scroll + height).min(layout.lines.len());
        if matches!(self.mode, Mode::Review(_)) {
            for drawn in &mut layout.seen[self.scroll..end] {
                if !*drawn {
                    layout.remaining -= 1;
                    *drawn = true;
                }
            }
        }
        let (title, help) = match &self.mode {
            Mode::Discard => (
                if self.base.system.is_some() {
                    "Discard this system prompt draft?"
                } else {
                    "Discard this memory draft?"
                }
                .to_owned(),
                "Enter discard · Esc keep editing",
            ),
            Mode::Review(r) => (
                format!(
                    "Review {}  ·  {} → {} bytes  ·  lines {}–{} of {}",
                    ui::display_safe(r.base.document.label()),
                    r.base.text.len(),
                    r.draft.len(),
                    self.scroll + 1,
                    end,
                    layout.lines.len()
                ),
                if layout.remaining == 0 {
                    "F6 apply reviewed draft · Esc back to editing · ↑ ↓ / PgUp PgDn"
                } else {
                    "Read every line before applying · PgUp PgDn / ↑ ↓ scroll · Esc back to editing"
                },
            ),
            Mode::Edit => (
                format!(
                    "Edit {}  ·  {} bytes",
                    ui::display_safe(self.base.document.label()),
                    self.draft.text().len()
                ),
                "F6 review · F5 reload base, keep draft · Esc return · ← → move · Home / End",
            ),
        };
        let view = layout.lines[self.scroll..end].join("\n");
        frame.render_widget(
            Paragraph::new(title).style(Style::default().fg(theme.primary)),
            Rect::new(area.x, area.y, area.width, 1),
        );
        frame.render_widget(Paragraph::new(view), body);
        frame.render_widget(
            Paragraph::new(ui::display_safe(&self.notice)),
            Rect::new(area.x, area.bottom() - 2, area.width, 1),
        );
        frame.render_widget(
            Paragraph::new(help).style(Style::default().fg(theme.title)),
            Rect::new(area.x, area.bottom() - 1, area.width, 1),
        );
    }
}
