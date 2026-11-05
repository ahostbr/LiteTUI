//! Source-owned hold between host model rounds, independently read back.
use crate::{work_lifecycle::Origin, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const REVIEW: &str = "gui.prompt.native_pause_review";
pub const APPLY: &str = "gui.prompt.native_pause_apply";
pub const READBACK: &str = "gui.prompt.native_pause_readback";
pub const OPERATIONS: [&str; 3] = [REVIEW, APPLY, READBACK];
const MAX_REVISION: u64 = (1_u64 << 53) - 1;

#[derive(Clone, Debug, PartialEq)]
pub struct Sample {
    pub paused: bool,
    pub revision: u64,
    pub stage: String,
    pub origin: Option<Origin>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub sample: Sample,
    pub token: String,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    Review { active: bool },
    Apply(Box<Review>),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Reply {
    Review(Box<Review>),
    Verified(Sample),
}
fn bad() -> io::Error {
    io::Error::other("Chat pause owner, revision or independent readback changed; review again")
}
fn base(identity: &Identity, instance: &str) -> Value {
    json!({"version":1,"session_id":identity.session,"agent_id":identity.agent,
        "backend":identity.backend,"model":identity.model,"work_instance":instance})
}
impl Sample {
    fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        let paused = v["paused"].as_bool().ok_or_else(bad)?;
        let revision = v["revision"]
            .as_u64()
            .filter(|n| *n <= MAX_REVISION)
            .ok_or_else(bad)?;
        let stage = v["stage"].as_str().ok_or_else(bad)?;
        let origin = if v["origin"].is_null() {
            None
        } else {
            Some(Origin::parse(&v["origin"])?)
        };
        if !matches!(stage, "idle" | "active" | "held" | "terminal")
            || (stage == "idle") != origin.is_none()
            || stage == "held" && !paused
            || origin
                .as_ref()
                .is_some_and(|o| o.session != identity.session)
        {
            return Err(bad());
        }
        Ok(Self {
            paused,
            revision,
            stage: stage.into(),
            origin,
        })
    }
    fn add_to(&self, v: &mut Value) {
        v["paused"] = json!(self.paused);
        v["revision"] = json!(self.revision);
        v["stage"] = json!(self.stage);
        v["origin"] = self
            .origin
            .as_ref()
            .map(Origin::value)
            .unwrap_or(Value::Null);
    }
    fn description(&self) -> &'static str {
        match (self.paused, self.stage.as_str()) {
            (true, "held") => "Paused at the next model-round boundary.",
            (true, "active") => {
                "Pause requested. The current model round and its tools may finish."
            }
            (true, _) => "Paused. The next host model round will wait.",
            (false, _) => "Running. Host model rounds may proceed.",
        }
    }
}
impl Request {
    pub fn operation(&self) -> &'static str {
        match self {
            Self::Review { .. } => REVIEW,
            Self::Apply(_) => APPLY,
        }
    }
    pub fn command(
        &self,
        identity: &Identity,
        instance: &str,
        origin: Option<&Origin>,
    ) -> io::Result<Value> {
        let mut v = base(identity, instance);
        v["type"] = json!(self.operation());
        match self {
            Self::Review { active } => {
                if *active != origin.is_some() {
                    return Err(bad());
                }
                v["origin"] = origin.map(Origin::value).unwrap_or(Value::Null);
            }
            Self::Apply(r) => {
                v["token"] = json!(r.token);
            }
        }
        Ok(v)
    }
    fn parse(
        &self,
        v: &Value,
        identity: &Identity,
        instance: &str,
        origin: Option<&Origin>,
        apply_id: Option<&str>,
        kind: &str,
    ) -> io::Result<(Sample, String)> {
        let sample = Sample::parse(v, identity)?;
        let token = v["token"]
            .as_str()
            .filter(|t| {
                t.len() == 64
                    && t.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            .ok_or_else(bad)?
            .to_owned();
        let mut expected = base(identity, instance);
        sample.add_to(&mut expected);
        expected["kind"] = json!(kind);
        expected["token"] = json!(token);
        if let Some(id) = apply_id {
            expected["apply_id"] = json!(id);
        }
        if expected != *v {
            return Err(bad());
        }
        match self {
            Self::Review { active } => {
                if sample.origin.as_ref() != origin
                    || *active != origin.is_some()
                    || sample.stage == "terminal"
                    || sample.revision == MAX_REVISION
                {
                    return Err(bad());
                }
            }
            Self::Apply(r) => {
                if token != r.token
                    || sample.paused == r.sample.paused
                    || sample.revision != r.sample.revision + 1
                    || apply_id.is_none()
                {
                    return Err(bad());
                }
            }
        }
        Ok((sample, token))
    }
    pub fn readback(
        &self,
        v: &Value,
        identity: &Identity,
        instance: &str,
        id: &str,
    ) -> io::Result<Value> {
        let Self::Apply(_) = self else {
            return Err(bad());
        };
        let (_, token) = self.parse(v, identity, instance, None, Some(id), "chat_pause_applied")?;
        let mut command = base(identity, instance);
        command["type"] = json!(READBACK);
        command["token"] = json!(token);
        command["apply_id"] = json!(id);
        Ok(command)
    }
    pub fn reply(
        &self,
        v: &Value,
        identity: &Identity,
        instance: &str,
        origin: Option<&Origin>,
        id: Option<&str>,
    ) -> io::Result<Reply> {
        let kind = if matches!(self, Self::Review { .. }) {
            "chat_pause_review"
        } else {
            "chat_pause_verified"
        };
        let (sample, token) = self.parse(v, identity, instance, origin, id, kind)?;
        Ok(match self {
            Self::Review { .. } => Reply::Review(Box::new(Review { sample, token })),
            Self::Apply(_) => Reply::Verified(sample),
        })
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    review: Option<Review>,
    notice: String,
    active: bool,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
}
impl View {
    pub fn begin(&mut self, active: bool) {
        *self = Self {
            open: true,
            active,
            notice: "Reading host chat-loop pause state…".into(),
            ..Self::default()
        };
    }
    pub fn reply(&mut self, reply: Reply) {
        match reply {
            Reply::Review(r) => {
                self.review = Some(*r);
                self.page = 0;
                self.seen.clear();
            }
            Reply::Verified(s) => {
                self.notice = format!("Verified. {}", s.description());
                self.active = s.origin.is_some();
                self.review = None;
            }
        }
    }
    pub fn foreground(&mut self, active: bool) {
        self.active = active;
    }
    pub fn hide(&mut self) {
        self.seen.clear();
    }
    pub fn reject(&mut self, reason: String) {
        self.notice = reason;
        self.review = None;
        self.seen.clear();
    }
    pub fn key(&mut self, key: KeyEvent, waiting: bool) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || waiting {
            return None;
        }
        match key.code {
            KeyCode::Esc => {
                self.open = false;
                self.review = None;
            }
            KeyCode::F(5) => {
                self.review = None;
                self.seen.clear();
                return Some(Request::Review {
                    active: self.active,
                });
            }
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1))
            }
            KeyCode::PageUp | KeyCode::Left => self.page = self.page.saturating_sub(1),
            KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|v| *v) => {
                return self.review.take().map(|r| Request::Apply(Box::new(r)));
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, waiting: bool) {
        f.render_widget(Clear, area);
        f.render_widget(
            Block::bordered().title(" Host chat-loop Pause / Resume "),
            area,
        );
        let body = area.inner(Margin::new(1, 1));
        if body.width < 12 || body.height < 4 {
            self.seen.clear();
            return;
        }
        let content = if let Some(r) = &self.review {
            format!(
                "{}\n\n{}\n\nThis runtime holds future host model rounds. Work scheduler and goal controls are managed in F1. Stop and Quit remain available.",
                r.sample.description(),
                if r.sample.paused {
                    "Resume the host chat loop?"
                } else {
                    "Pause before the next host model round?"
                }
            )
        } else {
            crate::live::terminal_text(&self.notice)
        };
        let lines = crate::ui::wrap(&content, body.width as usize);
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        let geometry = (body.width, body.height);
        if self.geometry != Some(geometry) || self.seen.len() != self.pages {
            self.geometry = Some(geometry);
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        let footer = if waiting {
            "Waiting for independent source readback".into()
        } else if self.review.is_some() {
            format!(
                "PgUp/PgDn {}/{}   Enter confirm   F5 refresh   Esc cancel",
                self.page + 1,
                self.pages
            )
        } else {
            "F5 review current state   Esc return to draft".into()
        };
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
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "fixture".into(),
        }
    }
    fn value() -> Value {
        json!({"version":1,"kind":"chat_pause_review","session_id":"session","agent_id":"agent","backend":"custom","model":"fixture","work_instance":"instance","paused":false,"revision":0,"stage":"idle","origin":null,"token":"a".repeat(64)})
    }
    #[test]
    fn exact_review_and_independent_readback_are_required() {
        let identity = identity();
        let req = Request::Review { active: false };
        let good = value();
        let Reply::Review(review) = req.reply(&good, &identity, "instance", None, None).unwrap()
        else {
            panic!()
        };
        for (key, value) in [
            ("stage", json!("held")),
            ("origin", json!({})),
            ("paused", json!(0)),
            ("revision", json!(MAX_REVISION)),
            ("work_instance", json!("other")),
            ("extra", json!(true)),
        ] {
            let mut altered = good.clone();
            altered[key] = value;
            assert!(
                req.reply(&altered, &identity, "instance", None, None)
                    .is_err(),
                "{key}"
            );
        }
        let apply = Request::Apply(review);
        let mut result = good.clone();
        result["kind"] = json!("chat_pause_applied");
        result["paused"] = json!(true);
        result["revision"] = json!(1);
        result["apply_id"] = json!("apply");
        assert!(
            apply
                .reply(&result, &identity, "instance", None, Some("apply"))
                .is_err()
        );
        assert_eq!(
            apply
                .readback(&result, &identity, "instance", "apply")
                .unwrap()["type"],
            READBACK
        );
        result["kind"] = json!("chat_pause_verified");
        assert!(matches!(
            apply
                .reply(&result, &identity, "instance", None, Some("apply"))
                .unwrap(),
            Reply::Verified(_)
        ));
        for (key, v) in [
            ("token", json!("b".repeat(64))),
            ("revision", json!(2)),
            ("paused", json!(false)),
            ("apply_id", json!("different")),
        ] {
            let mut altered = result.clone();
            altered[key] = v;
            assert!(
                apply
                    .reply(&altered, &identity, "instance", None, Some("apply"))
                    .is_err(),
                "{key}"
            );
        }
    }
    #[test]
    fn paint_all_pages_resize_and_fresh_key_are_required_and_cancel_is_pure() {
        let mut view = View::default();
        view.begin(false);
        view.reply(
            Request::Review { active: false }
                .reply(&value(), &identity(), "instance", None, None)
                .unwrap(),
        );
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        let down = KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE);
        let paint = |v: &mut View, w| {
            Terminal::new(TestBackend::new(w, 7))
                .unwrap()
                .draw(|f| v.draw(f, f.area(), false))
                .unwrap();
        };
        assert!(view.key(enter, false).is_none());
        paint(&mut view, 35);
        assert!(view.pages > 1);
        assert!(view.key(enter, false).is_none());
        for _ in 1..view.pages {
            view.key(down, false);
            paint(&mut view, 35);
        }
        paint(&mut view, 36);
        assert!(view.key(enter, false).is_none());
        for _ in 0..view.pages {
            view.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE), false);
            paint(&mut view, 36);
        }
        assert!(view.key(enter, true).is_none());
        let mut repeat = enter;
        repeat.kind = KeyEventKind::Repeat;
        assert!(view.key(repeat, false).is_none());
        assert!(matches!(view.key(enter, false), Some(Request::Apply(_))));
        assert!(
            view.key(enter, false).is_none(),
            "A held key cannot replay a consumed review"
        );
        view.reply(
            Request::Review { active: false }
                .reply(&value(), &identity(), "instance", None, None)
                .unwrap(),
        );
        assert!(
            view.key(enter, false).is_none(),
            "A response cannot authorize unseen content"
        );
        view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE), false);
        assert!(!view.open && view.review.is_none());
    }
}
