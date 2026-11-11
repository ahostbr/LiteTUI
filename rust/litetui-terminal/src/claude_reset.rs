//! Reviewed reset of Claude's selected segment, preserving the LiteTUI conversation.
use crate::{theme::Theme, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const OPERATIONS: [&str; 3] = [
    "gui.claude.native_reset_review",
    "gui.claude.native_reset_apply",
    "gui.claude.native_reset_readback",
];
fn bad() -> io::Error {
    io::Error::other("Claude session reset changed its reviewed owner, segment or readback")
}
fn keys(v: &Value, expected: &[&str]) -> io::Result<()> {
    let map = v.as_object().ok_or_else(bad)?;
    if map.len() != expected.len() || expected.iter().any(|k| !map.contains_key(*k)) {
        return Err(bad());
    }
    Ok(())
}
fn hex(v: &Value, length: usize) -> io::Result<&str> {
    v.as_str()
        .filter(|s| {
            s.len() == length
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or_else(bad)
}
fn base(id: &Identity, instance: &str) -> Value {
    json!({"version":1,"session_id":id.session,"agent_id":id.agent,"backend":id.backend,"model":id.model,"work_instance":instance})
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    token: String,
    value: Value,
}
impl Review {
    fn parse(v: &Value) -> io::Result<Self> {
        let r = &v["review"];
        keys(
            r,
            &[
                "segment",
                "native_session",
                "target",
                "workspace",
                "held_inputs",
                "segments",
                "materialize",
                "history_sha256",
                "ledger_before",
            ],
        )?;
        hex(&r["target"], 32)?;
        if !r["segment"].is_null() {
            hex(&r["segment"], 32)?;
        }
        if r["target"] == r["segment"] {
            return Err(bad());
        }
        if !r["native_session"].is_null() {
            crate::resume::conversation_id(r["native_session"].as_str().ok_or_else(bad)?)?;
        }
        if r["segment"].is_null() && !r["native_session"].is_null() {
            return Err(bad());
        }
        let path = r["workspace"]
            .as_str()
            .filter(|s| s.len() <= 4096 && !s.chars().any(char::is_control))
            .ok_or_else(bad)?;
        if !std::path::Path::new(path).is_absolute() {
            return Err(bad());
        }
        for k in ["held_inputs", "segments"] {
            r[k].as_u64().filter(|n| *n <= 100000).ok_or_else(bad)?;
        }
        r["materialize"].as_bool().ok_or_else(bad)?;
        hex(&r["history_sha256"], 64)?;
        hex(&r["ledger_before"], 64)?;
        Ok(Self {
            token: hex(&v["token"], 64)?.into(),
            value: r.clone(),
        })
    }
    fn text(&self) -> String {
        let r = &self.value;
        format!(
            "Reset only the Claude session in this LiteTUI conversation.\n\nOld segment: {}\nOld native session: {}\nNew empty segment: {}\nWorkspace: {}\nPreserve {} old segments and {} held deliveries.\nConversation UUID, history, authority, draft and images stay in place.\n{}\nClose and join the exact current Claude SDK owner before selection. No history is imported; no input is replayed or submitted. The next explicit prompt opens the fresh Claude session.\n\nEnter applies this reset. Esc cancels.",
            r["segment"],
            r["native_session"],
            r["target"],
            r["workspace"].as_str().unwrap(),
            r["segments"],
            r["held_inputs"],
            if r["materialize"] == true {
                "The pending conversation will be saved under its current UUID."
            } else {
                "The saved conversation transcript is unchanged."
            }
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    Review,
    Apply(Review),
    Readback(Review, String, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Review(Review),
    Saved(Review),
}
pub enum Reply {
    Next(Request),
    Update(Update),
}
impl Request {
    pub fn mutates(&self) -> bool {
        matches!(self, Self::Apply(_))
    }
    pub fn is_readback(&self) -> bool {
        matches!(self, Self::Readback(..))
    }
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        if id.backend != "claude" {
            return Err(bad());
        }
        let mut v = base(id, instance);
        v["type"] = json!(match self {
            Self::Review => OPERATIONS[0],
            Self::Apply(_) => OPERATIONS[1],
            Self::Readback(..) => OPERATIONS[2],
        });
        match self {
            Self::Review => {}
            Self::Apply(r) => v["token"] = json!(r.token),
            Self::Readback(r, apply, _) => {
                v["token"] = json!(r.token);
                v["apply_id"] = json!(apply);
            }
        }
        Ok(v)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command: &str,
    ) -> io::Result<Reply> {
        keys(
            v,
            &[
                "version",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "kind",
                "token",
                "review",
                "apply_id",
                "ledger_after",
                "cleanup_verified",
            ],
        )?;
        if serde_json::to_vec(v)?.len() > 16384
            || id.backend != "claude"
            || base(id, instance)
                .as_object()
                .unwrap()
                .iter()
                .any(|(k, x)| v[k] != *x)
        {
            return Err(bad());
        }
        let review = Review::parse(v)?;
        match self {
            Self::Review => {
                if v["kind"] != "claude_reset_review"
                    || !v["apply_id"].is_null()
                    || !v["ledger_after"].is_null()
                    || v["cleanup_verified"] != false
                {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Review(review)))
            }
            Self::Apply(expected) => {
                if expected != review
                    || v["kind"] != "claude_reset_applied"
                    || v["apply_id"] != command
                    || v["cleanup_verified"] != true
                {
                    return Err(bad());
                }
                let after = hex(&v["ledger_after"], 64)?.into();
                Ok(Reply::Next(Self::Readback(expected, command.into(), after)))
            }
            Self::Readback(expected, apply, after) => {
                if expected != review
                    || v["kind"] != "claude_reset_saved"
                    || v["apply_id"] != apply
                    || v["ledger_after"] != after
                    || v["cleanup_verified"] != true
                {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(expected)))
            }
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    review: Option<Review>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
}
impl View {
    pub fn open(&mut self) {
        *self = Self {
            open: true,
            waiting: true,
            notice: "Reading the exact Claude session…".into(),
            ..Default::default()
        };
    }
    pub(crate) fn occlude(&mut self) {
        self.geometry = None;
        self.seen.clear();
    }
    pub fn hide(&mut self) {
        self.occlude();
        self.open = false;
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        self.open = true;
        self.waiting = false;
        self.page = 0;
        self.geometry = None;
        self.seen.clear();
        match result {
            Ok(Update::Review(r)) => {
                self.review = Some(r);
                self.notice.clear();
            }
            Ok(Update::Saved(r)) => {
                self.review = None;
                self.notice = format!(
                    "Claude session reset verified. New empty segment: {}. The same conversation and held deliveries remain saved; no prompt was sent.",
                    r.value["target"]
                );
            }
            Err(e) => {
                self.review = None;
                self.notice = crate::live::terminal_text(&e.chars().take(4096).collect::<String>());
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if !self.open || self.waiting || key.kind != KeyEventKind::Press {
            return None;
        }
        match key.code {
            KeyCode::Esc => self.hide(),
            KeyCode::PageDown => self.page = (self.page + 1).min(self.pages.saturating_sub(1)),
            KeyCode::PageUp => self.page = self.page.saturating_sub(1),
            KeyCode::Enter
                if self.geometry.is_some()
                    && !self.seen.is_empty()
                    && self.seen.iter().all(|s| *s) =>
            {
                if let Some(r) = self.review.take() {
                    self.waiting = true;
                    return Some(Request::Apply(r));
                }
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open {
            return;
        }
        f.render_widget(Clear, area);
        f.render_widget(
            Block::bordered()
                .title(" Claude session reset ")
                .style(Style::default().fg(theme.foreground).bg(theme.background)),
            area,
        );
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.geometry = None;
            self.seen.clear();
            return;
        }
        let text = self
            .review
            .as_ref()
            .map_or_else(|| self.notice.clone(), Review::text);
        let lines = crate::ui::wrap(&text, body.width as usize);
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        let geometry = (body.width, body.height);
        if self.geometry != Some(geometry) || self.seen.len() != self.pages {
            self.geometry = Some(geometry);
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        f.render_widget(
            Paragraph::new(
                lines
                    .iter()
                    .skip(self.page * height)
                    .take(height)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            body,
        );
        let footer = if self.waiting {
            "Joining source and independent readback…".into()
        } else if self.review.is_some() {
            format!(
                "PgUp/PgDn {}/{} · Enter apply · Esc cancel",
                self.page + 1,
                self.pages
            )
        } else {
            "Esc return".into()
        };
        f.render_widget(
            Paragraph::new(footer),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    fn identity() -> Identity {
        Identity {
            session: "12345678-1111-4111-8111-123456789abc".into(),
            agent: "agent".into(),
            backend: "claude".into(),
            model: "fixture".into(),
        }
    }
    fn sample() -> Value {
        let mut v = base(&identity(), "instance");
        v["kind"] = json!("claude_reset_review");
        v["token"] = json!("a".repeat(64));
        v["apply_id"] = Value::Null;
        v["ledger_after"] = Value::Null;
        v["cleanup_verified"] = json!(false);
        v["review"] = json!({"segment":"b".repeat(32),"native_session":"12345678-1111-4111-8111-123456789abc","target":"c".repeat(32),"workspace":std::env::temp_dir().to_string_lossy(),"held_inputs":2,"segments":1,"materialize":false,"history_sha256":"d".repeat(64),"ledger_before":"e".repeat(64)});
        v
    }
    #[test]
    fn exact_review_apply_and_independent_readback_bind_the_same_segment() {
        let v = sample();
        let Reply::Update(Update::Review(review)) = Request::Review
            .response(&v, &identity(), "instance", "review")
            .unwrap()
        else {
            panic!()
        };
        let mut applied = v.clone();
        applied["kind"] = json!("claude_reset_applied");
        applied["apply_id"] = json!("apply");
        applied["ledger_after"] = json!("f".repeat(64));
        applied["cleanup_verified"] = json!(true);
        let Reply::Next(readback) = Request::Apply(review.clone())
            .response(&applied, &identity(), "instance", "apply")
            .unwrap()
        else {
            panic!()
        };
        assert_eq!(
            readback.command(&identity(), "instance").unwrap()["apply_id"],
            "apply"
        );
        assert!(
            readback
                .clone()
                .response(&applied, &identity(), "instance", "read")
                .is_err()
        );
        applied["kind"] = json!("claude_reset_saved");
        assert!(matches!(
            readback
                .clone()
                .response(&applied, &identity(), "instance", "read")
                .unwrap(),
            Reply::Update(Update::Saved(_))
        ));
        for (key, value) in [
            ("work_instance", json!("foreign")),
            ("apply_id", json!("other")),
            ("ledger_after", json!("0".repeat(64))),
            ("cleanup_verified", json!(false)),
            ("extra", json!(1)),
        ] {
            let mut bad = applied.clone();
            bad[key] = value;
            assert!(
                readback
                    .clone()
                    .response(&bad, &identity(), "instance", "read")
                    .is_err(),
                "{key}"
            );
        }
        applied["review"]["target"] = json!("0".repeat(32));
        assert!(
            readback
                .response(&applied, &identity(), "instance", "read")
                .is_err()
        );
    }
    #[test]
    fn malformed_review_cannot_authorize_reset() {
        for (key, value) in [
            ("target", json!("b".repeat(32))),
            ("workspace", json!("relative")),
            ("segments", json!(-1)),
            ("history_sha256", json!("bad")),
            ("extra", json!(true)),
        ] {
            let mut v = sample();
            v["review"][key] = value;
            assert!(
                Request::Review
                    .response(&v, &identity(), "instance", "review")
                    .is_err(),
                "{key}"
            );
        }
    }
    #[test]
    fn every_review_page_and_fresh_enter_are_required_after_resize() {
        let Reply::Update(update) = Request::Review
            .response(&sample(), &identity(), "instance", "review")
            .unwrap()
        else {
            panic!()
        };
        let mut view = View::default();
        view.open();
        view.update(Ok(update.clone()));
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let paint = |view: &mut View, width| {
            Terminal::new(TestBackend::new(width, 8))
                .unwrap()
                .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
                .unwrap();
        };
        assert!(view.key(key(KeyCode::Enter)).is_none());
        paint(&mut view, 36);
        assert!(view.pages > 1);
        for _ in 1..view.pages {
            view.key(key(KeyCode::PageDown));
            paint(&mut view, 36);
        }
        paint(&mut view, 37);
        assert!(view.key(key(KeyCode::Enter)).is_none());
        for _ in 0..view.pages {
            view.key(key(KeyCode::PageUp));
            paint(&mut view, 37);
        }
        let mut repeat = key(KeyCode::Enter);
        repeat.kind = KeyEventKind::Repeat;
        assert!(view.key(repeat).is_none());
        assert!(matches!(
            view.key(key(KeyCode::Enter)),
            Some(Request::Apply(_))
        ));
        assert!(view.key(key(KeyCode::Enter)).is_none());
        view.update(Ok(update));
        view.key(key(KeyCode::Esc));
        assert!(!view.open);
    }
}
