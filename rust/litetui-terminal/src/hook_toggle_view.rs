use crate::{
    hook_toggle::{List, Reply, Request, Review, Scope, TestReview},
    live::terminal_text,
    theme::Theme,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};

#[derive(Default)]
pub struct View {
    pub open: bool,
    scope: Scope,
    list: Option<List>,
    selected: usize,
    review: Option<Review>,
    test: Option<TestReview>,
    event: usize,
    page: usize,
    seen: Vec<bool>,
    bounds: Option<Rect>,
    visible: bool,
    pub notice: String,
}
impl View {
    pub fn open(&mut self) -> Request {
        *self = Self {
            open: true,
            ..Self::default()
        };
        Request::List(self.scope)
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    /// Re-read the scope shown here, after the authoring editor saved a change.
    pub fn reload(&mut self) -> Request {
        self.list = None;
        self.review = None;
        self.test = None;
        self.seen.clear();
        Request::List(self.scope)
    }
    /// The entry point to hook authoring: plain A on the idle list, in the scope being browsed.
    pub fn authoring_entry(&self, e: &KeyEvent, waiting: bool) -> Option<Scope> {
        (!waiting
            && self.visible
            && self.review.is_none()
            && self.test.is_none()
            && e.kind == KeyEventKind::Press
            && e.modifiers == KeyModifiers::NONE
            && matches!(e.code, KeyCode::Char('a' | 'A')))
        .then_some(self.scope)
    }
    pub fn close(&mut self) {
        *self = Self::default();
    }
    pub fn reject(&mut self, s: String) {
        self.review = None;
        self.test = None;
        self.list = None;
        self.notice = s;
        self.seen.clear();
    }
    pub fn reply(&mut self, reply: Reply) {
        if !self.open {
            return;
        }
        match reply {
            Reply::List(list) => {
                self.scope = list.scope;
                self.selected = self.selected.min(list.rows.len().saturating_sub(1));
                self.list = Some(list);
                self.notice.clear();
            }
            Reply::TestReview(r) => {
                self.review = None;
                self.test = Some(r);
                self.page = 0;
                self.seen.clear();
                self.notice.clear();
            }
            Reply::Tested(result) => {
                self.test = None;
                self.notice = result;
            }
            Reply::Review(r) => {
                self.review = Some(r);
                self.page = 0;
                self.seen.clear();
                self.notice.clear();
            }
            Reply::Saved(actual) => {
                self.review = None;
                self.list = None;
                self.notice = format!(
                    "Verified {} · {}. F5 reloads this scope.",
                    actual.summary(),
                    self.scope.key()
                );
            }
        }
    }
    pub fn key(&mut self, e: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting || e.kind != KeyEventKind::Press || e.modifiers != KeyModifiers::NONE {
            return None;
        }
        if e.code == KeyCode::Esc {
            if self.test.take().is_some() {
                self.notice = "Test discarded.".into();
            } else if self.review.take().is_some() {
                self.notice = "Toggle discarded.".into();
            } else {
                self.close();
            }
            self.seen.clear();
            return None;
        }
        if !self.visible {
            return None;
        }
        if self.review.is_some() || self.test.is_some() {
            match e.code {
                KeyCode::PageDown | KeyCode::Down => {
                    self.page = (self.page + 1).min(self.seen.len().saturating_sub(1))
                }
                KeyCode::PageUp | KeyCode::Up => self.page = self.page.saturating_sub(1),
                KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|b| *b) => {
                    if let Some(test) = &self.test {
                        return Some(Request::Run(Box::new(test.clone())));
                    }
                    return self.review.clone().map(Box::new).map(Request::Apply);
                }
                _ => {}
            }
            return None;
        }
        match e.code {
            KeyCode::Tab | KeyCode::Left | KeyCode::Right => {
                self.scope = self.scope.other();
                self.selected = 0;
                self.event = 0;
                self.list = None;
                return Some(Request::List(self.scope));
            }
            KeyCode::F(5) => {
                return Some(Request::List(self.scope));
            }
            KeyCode::Up => {
                self.selected = self.selected.saturating_sub(1);
                self.event = 0;
            }
            KeyCode::Down => {
                if let Some(list) = &self.list {
                    self.selected = (self.selected + 1).min(list.rows.len().saturating_sub(1));
                    self.event = 0;
                }
            }
            KeyCode::Char(']') => {
                self.event = self.event.wrapping_add(1);
                self.notice.clear();
            }
            KeyCode::Char('t') => {
                let list = self.list.as_ref()?;
                let row = list.rows.get(self.selected)?;
                return Some(Request::TestReview {
                    scope: list.scope,
                    revision: list.revision.clone(),
                    row: row.clone(),
                    event: row.test_event(self.event).into(),
                });
            }
            KeyCode::Enter => {
                let list = self.list.as_ref()?;
                return Some(Request::Review {
                    scope: list.scope,
                    revision: list.revision.clone(),
                    row: list.rows.get(self.selected)?.clone(),
                });
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        let w = area.width.min(100);
        let h = area.height.min(36);
        let rect = Rect::new(
            area.x + (area.width - w) / 2,
            area.y + (area.height - h) / 2,
            w,
            h,
        );
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(format!(" Lifecycle hooks · {} ", self.scope.key()))
            .style(Style::default().fg(theme.foreground).bg(theme.background));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        self.visible = inner.width >= 25 && inner.height >= 6;
        if !self.visible {
            return;
        }
        let rows = usize::from(inner.height) - 3;
        let detail = if let Some(r) = &self.review {
            let mut text = vec![
                format!("Review {} hook {}", r.scope.key(), r.before.id()),
                format!("Saved: {} → {}", r.before.enabled(), r.after.enabled()),
                format!("Effective: {} → {}", r.before.effect(), r.after.effect()),
            ];
            text.push(format!("Resolved before: {}", r.before.resolved()));
            text.push(format!("Resolved after: {}", r.after.resolved()));
            text.extend(r.before.details());
            text.push("Source shell-tool authorization applies when a matching hook runs.".into());
            text.push(format!("Document before: {}", r.before_hash));
            text.push(format!("Document after: {}", r.after_hash));
            text.push(
                if r.after.effect() == "enabled" {
                    "Future matching events may run this configured executable."
                } else if r.after.effect() == "shadowed" {
                    "Project configuration overrides this global hook."
                } else if r.after.effect() == "off" {
                    "Lifecycle hooks are switched off for this runtime."
                } else {
                    "Future matching events skip this hook."
                }
                .into(),
            );
            Some((text, "applies"))
        } else {
            self.test.as_ref().map(|r| {
                let mut text=vec![format!("Test {} hook {} once",r.scope.key(),r.row.id()),
                    format!("Event: {} · source: typed",r.event),format!("Sample data: {}",r.sample),
                    format!("Saved: {} · effective: {}",r.row.enabled(),r.row.effect())];
                text.extend(r.row.details());
                text.push(format!("Profile: {}. Only existing source permission can run this executable. A policy requiring confirmation refuses this test.",r.profile));
                text.push("The selected entry runs once with this synthetic event, including when disabled or shadowed. Its configured process can perform external actions. Output remains private.".into());
                (text,"runs the test")
            })
        };
        let (lines, keys) = if let Some((text, action)) = detail {
            let lines = crate::ui::wrap(&text.join("\n"), inner.width.into());
            let count = lines.len().div_ceil(rows).max(1);
            if self.seen.len() != count || self.bounds != Some(rect) {
                self.seen = vec![false; count];
                self.page = 0;
            }
            self.page = self.page.min(count - 1);
            self.seen[self.page] = true;
            (
                lines
                    .into_iter()
                    .skip(self.page * rows)
                    .take(rows)
                    .collect::<Vec<_>>(),
                format!(
                    "Page {}/{} · ↑ ↓ read · Enter {action} after all pages · Esc discards",
                    self.page + 1,
                    count
                ),
            )
        } else {
            let mut lines = Vec::new();
            if let Some(list) = &self.list {
                if list.rows.is_empty() {
                    lines.push("No hooks in this scope.".into());
                }
                let start = self.selected.saturating_add(1).saturating_sub(rows);
                for (i, r) in list.rows.iter().enumerate().skip(start).take(rows) {
                    lines.push(format!(
                        "{} {}{}",
                        if i == self.selected { "›" } else { " " },
                        r.summary(),
                        if i == self.selected {
                            format!(" · test: {}", r.test_event(self.event))
                        } else {
                            String::new()
                        }
                    ));
                }
            }
            (
                lines,
                "Tab scope · ↑ ↓ select · Enter toggle · ] sample event · T test · A author · F5 reload · Esc back"
                    .into(),
            )
        };
        self.bounds = Some(rect);
        frame.render_widget(Paragraph::new(lines.join("\n")), inner);
        frame.render_widget(
            Paragraph::new(if waiting {
                "Waiting for source…".into()
            } else {
                terminal_text(&self.notice)
            }),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(crate::ui::wrap(&keys, inner.width.into()).join("\n")),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
        );
    }
}
