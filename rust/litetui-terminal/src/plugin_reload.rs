//! Source-owned static tool-description review and verified registry publication.
use crate::{theme::Theme, ui, work_lifecycle as life, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const OPERATIONS: [&str; 4] = [
    "gui.plugins.native_list",
    "gui.plugins.native_review",
    "gui.plugins.native_apply",
    "gui.plugins.native_readback",
];
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub name: String,
    token: String,
    before: String,
    after: String,
    before_spec: String,
    after_spec: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(String),
    Apply(Box<Review>),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reply {
    List(Vec<String>),
    Reviewed(Review),
    Verified(String),
}
fn name(v: &Value) -> io::Result<String> {
    v.as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 64
                && s.as_bytes()[0].is_ascii_alphabetic()
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_'))
        })
        .map(str::to_owned)
        .ok_or_else(|| life::bad("Invalid static tool name"))
}
fn digest(v: &Value) -> io::Result<String> {
    v.as_str()
        .filter(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|c| c.is_ascii_digit() || matches!(c, b'a'..=b'f'))
        })
        .map(str::to_owned)
        .ok_or_else(|| life::bad("Invalid plugin review digest"))
}
fn description(v: &Value) -> io::Result<String> {
    v.as_str()
        .filter(|s| s.len() <= 8192)
        .map(str::to_owned)
        .ok_or_else(|| life::bad("Invalid plugin description"))
}
impl Request {
    pub fn operation(&self) -> &'static str {
        match self {
            Self::List => OPERATIONS[0],
            Self::Review(_) => OPERATIONS[1],
            Self::Apply(_) => OPERATIONS[2],
        }
    }
    pub fn command(&self, id: &Identity, instance: &str) -> Value {
        let mut v = id.request();
        v["work_instance"] = json!(instance);
        v["type"] = json!(self.operation());
        match self {
            Self::List => {}
            Self::Review(n) => v["name"] = json!(n),
            Self::Apply(r) => {
                v["name"] = json!(r.name);
                v["token"] = json!(r.token);
            }
        }
        v
    }
    fn bind(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        kind: &str,
        fields: &[&str],
    ) -> io::Result<()> {
        let common = [
            "kind",
            "version",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
        ];
        life::keys(v, &[common.as_slice(), fields].concat())?;
        if v["kind"] != kind
            || v["version"].as_u64() != Some(1)
            || v["session_id"] != id.session
            || v["agent_id"] != id.agent
            || v["work_instance"] != instance
            || v["backend"] != id.backend
            || v["model"] != id.model
        {
            return Err(life::bad(
                "Plugin reply belongs to another owner or operation",
            ));
        }
        Ok(())
    }
    fn publication(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: &str,
        readback: bool,
    ) -> io::Result<()> {
        let Self::Apply(r) = self else {
            return Err(life::bad("Plugin publication requires a review"));
        };
        let fields: &[&str] = if readback {
            &["name", "token", "apply_id", "after_spec", "description"]
        } else {
            &["name", "token", "apply_id", "after_spec"]
        };
        self.bind(
            v,
            id,
            instance,
            if readback {
                "plugin_verified"
            } else {
                "plugin_applied"
            },
            fields,
        )?;
        if v["name"] != r.name
            || v["token"] != r.token
            || v["apply_id"] != apply_id
            || v["after_spec"] != r.after_spec
            || (readback && v["description"] != r.after)
        {
            return Err(life::bad(
                "Plugin publication differs from the reviewed description",
            ));
        }
        Ok(())
    }
    pub fn readback(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: &str,
    ) -> io::Result<Value> {
        self.publication(v, id, instance, apply_id, false)?;
        let mut command = self.command(id, instance);
        command["type"] = json!(OPERATIONS[3]);
        command["apply_id"] = json!(apply_id);
        Ok(command)
    }
    pub fn reply(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: Option<&str>,
    ) -> io::Result<Reply> {
        match self {
            Self::List => {
                self.bind(v, id, instance, "plugin_list", &["names"])?;
                let names = v["names"]
                    .as_array()
                    .filter(|n| n.len() <= 512)
                    .ok_or_else(|| life::bad("Invalid static tool catalog"))?
                    .iter()
                    .map(name)
                    .collect::<io::Result<Vec<_>>>()?;
                if !names.windows(2).all(|w| w[0] < w[1]) {
                    return Err(life::bad("Static tool names are ambiguous or out of order"));
                }
                Ok(Reply::List(names))
            }
            Self::Review(target) => {
                self.bind(
                    v,
                    id,
                    instance,
                    "plugin_review",
                    &[
                        "name",
                        "token",
                        "before",
                        "after",
                        "before_spec",
                        "after_spec",
                    ],
                )?;
                let r = Review {
                    name: name(&v["name"])?,
                    token: digest(&v["token"])?,
                    before: description(&v["before"])?,
                    after: description(&v["after"])?,
                    before_spec: digest(&v["before_spec"])?,
                    after_spec: digest(&v["after_spec"])?,
                };
                if r.name != *target || r.before == r.after || r.before_spec == r.after_spec {
                    return Err(life::bad(
                        "Source did not stage a changed description for this tool",
                    ));
                }
                Ok(Reply::Reviewed(r))
            }
            Self::Apply(r) => {
                self.publication(
                    v,
                    id,
                    instance,
                    apply_id.ok_or_else(|| life::bad("Missing plugin application id"))?,
                    true,
                )?;
                Ok(Reply::Verified(r.name.clone()))
            }
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    names: Vec<String>,
    selected: usize,
    review: Option<Review>,
    offset: usize,
    seen: Vec<bool>,
    bounds: Option<Rect>,
    visible: bool,
    notice: String,
}
impl View {
    pub fn open(&mut self) -> Request {
        *self = Self {
            open: true,
            ..Self::default()
        };
        Request::List
    }
    pub fn close(&mut self) {
        *self = Self::default();
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.bounds = None;
        self.seen.clear();
    }
    pub fn reject(&mut self, reason: String) {
        self.review = None;
        self.notice = reason;
        self.seen.clear();
    }
    pub fn reply(&mut self, r: Reply) {
        if !self.open {
            return;
        }
        match r {
            Reply::List(names) => {
                self.names = names;
                self.selected = self.selected.min(self.names.len().saturating_sub(1));
                self.notice.clear();
            }
            Reply::Reviewed(review) => {
                self.review = Some(review);
                self.seen.clear();
                self.offset = 0;
                self.notice.clear();
                self.visible = false;
            }
            Reply::Verified(name) => {
                self.review = None;
                self.notice = format!("Reloaded and verified {name} in the current tool registry.");
            }
        }
    }
    pub fn key(&mut self, k: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting || k.kind == KeyEventKind::Release || !k.modifiers.is_empty() {
            return None;
        }
        if k.kind == KeyEventKind::Press && k.code == KeyCode::Esc {
            if self.review.take().is_some() {
                self.offset = 0;
                self.seen.clear();
            } else {
                self.close();
            }
            return None;
        }
        if !self.visible {
            return None;
        }
        if let Some(r) = &self.review {
            let height = usize::from(self.bounds?.height).max(1);
            let end = self.seen.len().saturating_sub(height);
            match k.code {
                KeyCode::PageDown => self.offset = self.offset.saturating_add(height).min(end),
                KeyCode::Down => self.offset = (self.offset + 1).min(end),
                KeyCode::Up => self.offset = self.offset.saturating_sub(1),
                KeyCode::PageUp => self.offset = self.offset.saturating_sub(height),
                KeyCode::F(6)
                    if k.kind == KeyEventKind::Press
                        && !self.seen.is_empty()
                        && self.seen.iter().all(|v| *v) =>
                {
                    return Some(Request::Apply(Box::new(r.clone())));
                }
                _ => {}
            }
        } else {
            match k.code {
                KeyCode::Up => self.selected = self.selected.saturating_sub(1),
                KeyCode::Down => {
                    self.selected = (self.selected + 1).min(self.names.len().saturating_sub(1))
                }
                KeyCode::F(5) if k.kind == KeyEventKind::Press => return Some(Request::List),
                KeyCode::Enter if k.kind == KeyEventKind::Press => {
                    return self.names.get(self.selected).cloned().map(Request::Review);
                }
                _ => {}
            }
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        let width = area.width.saturating_sub(2).min(110);
        let height = area.height.saturating_sub(2).min(38);
        if width < 27 || height < 10 {
            self.hide();
            frame.render_widget(Clear, area);
            frame.render_widget(
                Paragraph::new("Resize to review tool descriptions. Esc returns."),
                area,
            );
            return;
        }
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let block = Block::bordered()
            .title(" Reload static tool description ")
            .style(Style::default().fg(theme.foreground).bg(theme.panel));
        let inner = block.inner(rect);
        let body = Rect::new(inner.x, inner.y, inner.width, inner.height - 3);
        if self.bounds != Some(body) {
            self.seen.clear();
            self.offset = 0;
        }
        self.visible = true;
        self.bounds = Some(body);
        let (rows, help) = if let Some(r) = &self.review {
            let text = format!(
                "{}\n\nCurrent description\n{}\n\nDescription from disk\n{}",
                r.name,
                ui::display_safe(&r.before),
                ui::display_safe(&r.after)
            );
            let lines = ui::wrap(&text, usize::from(body.width));
            self.seen.resize(lines.len(), false);
            let end = (self.offset + usize::from(body.height)).min(lines.len());
            self.seen[self.offset..end].fill(true);
            (
                lines[self.offset..end].join("\n"),
                if self.seen.iter().all(|p| *p) {
                    "F6 reload this description · Esc cancel"
                } else {
                    "PgDn / ↓ read all lines · Esc cancel"
                },
            )
        } else {
            let first = self
                .selected
                .saturating_sub(usize::from(body.height).saturating_sub(1));
            (
                if self.names.is_empty() {
                    "No eligible registered static descriptions.".into()
                } else {
                    self.names
                        .iter()
                        .enumerate()
                        .skip(first)
                        .take(usize::from(body.height))
                        .map(|(i, n)| format!("{} {n}", if i == self.selected { "›" } else { " " }))
                        .collect::<Vec<_>>()
                        .join("\n")
                },
                "↑ ↓ select · Enter review · F5 refresh · Esc back",
            )
        };
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);
        frame.render_widget(Paragraph::new(rows), body);
        frame.render_widget(
            Paragraph::new(ui::display_safe(&self.notice)),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 2),
        );
        frame.render_widget(
            Paragraph::new(if waiting {
                "Waiting for source…"
            } else {
                help
            })
            .style(Style::default().fg(theme.title)),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    fn reviewed() -> Review {
        Review {
            name: "read_file".into(),
            token: "c".repeat(64),
            before: "old\n".repeat(80),
            after: "replacement".into(),
            before_spec: "a".repeat(64),
            after_spec: "b".repeat(64),
        }
    }
    fn identity() -> Identity {
        Identity {
            agent: "agent".into(),
            session: "session".into(),
            backend: "custom".into(),
            model: "m".into(),
        }
    }
    fn response(kind: &str) -> Value {
        let mut v = identity().request();
        v["work_instance"] = json!("current");
        v["kind"] = json!(kind);
        v
    }
    fn render(view: &mut View, width: u16, height: u16) {
        let theme = crate::theme::builtin_themes().remove(0);
        Terminal::new(TestBackend::new(width, height))
            .unwrap()
            .draw(|f| view.draw(f, f.area(), &theme, false))
            .unwrap();
    }
    #[test]
    fn readback_requires_the_actual_reviewed_description_and_exact_owner_token_and_application() {
        let r = reviewed();
        let req = Request::Apply(Box::new(r.clone()));
        let mut v = response("plugin_verified");
        v["name"] = json!(r.name);
        v["token"] = json!(r.token);
        v["apply_id"] = json!("apply-1");
        v["after_spec"] = json!(r.after_spec);
        v["description"] = json!(r.after);
        assert!(matches!(
            req.reply(&v, &identity(), "current", Some("apply-1")),
            Ok(Reply::Verified(_))
        ));
        for key in [
            "token",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "name",
            "apply_id",
            "after_spec",
            "description",
        ] {
            let mut bad = v.clone();
            bad[key] = json!("foreign");
            assert!(
                req.reply(&bad, &identity(), "current", Some("apply-1"))
                    .is_err(),
                "{key}"
            );
        }
        let mut wrong = v.clone();
        wrong["kind"] = json!("plugin_applied");
        assert!(
            req.readback(&wrong, &identity(), "current", "apply-1")
                .is_err()
        );
    }
    #[test]
    fn full_review_fresh_confirmation_and_resize_visibility_precede_publication() {
        let mut view = View::default();
        view.open();
        let r = reviewed();
        view.reply(Reply::Reviewed(r.clone()));
        let press = |code| KeyEvent::new(code, KeyModifiers::NONE);
        render(&mut view, 74, 14);
        assert_eq!(view.key(press(KeyCode::F(6)), false), None);
        for _ in 0..15 {
            view.key(press(KeyCode::PageDown), false);
            render(&mut view, 74, 14);
        }
        render(&mut view, 56, 15);
        assert_eq!(view.key(press(KeyCode::F(6)), false), None);
        for _ in 0..15 {
            view.key(press(KeyCode::PageDown), false);
            render(&mut view, 56, 15);
        }
        assert_eq!(
            view.key(
                KeyEvent::new_with_kind(KeyCode::F(6), KeyModifiers::NONE, KeyEventKind::Repeat),
                false
            ),
            None
        );
        assert_eq!(view.key(press(KeyCode::F(6)), true), None);
        assert!(matches!(
            view.key(press(KeyCode::F(6)), false),
            Some(Request::Apply(_))
        ));
        view.hide();
        assert_eq!(view.key(press(KeyCode::F(6)), false), None);
    }
}
