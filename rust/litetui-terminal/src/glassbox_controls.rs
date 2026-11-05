//! Explicit registered Glassbox control with independent source readback.
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

pub const OPERATIONS: [&str; 4] = [
    "gui.glassbox.native_snapshot",
    "gui.glassbox.native_review",
    "gui.glassbox.native_apply",
    "gui.glassbox.native_readback",
];
const MAX_REVISION: u64 = (1_u64 << 53) - 1;
fn bad() -> io::Error {
    io::Error::other("Glassbox owner, reviewed effects or independent readback changed")
}
fn base(id: &Identity, instance: &str) -> Value {
    json!({"version":1,"session_id":id.session,"agent_id":id.agent,"backend":id.backend,"model":id.model,"work_instance":instance})
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sample {
    recording: bool,
    state: String,
    port: Option<u16>,
    listener: Option<String>,
    revision: u64,
    path: String,
    listen_port: u16,
}
fn parse_port(v: &Value) -> io::Result<u16> {
    v.as_u64()
        .and_then(|n| u16::try_from(n).ok())
        .filter(|n| *n > 0)
        .ok_or_else(bad)
}
impl Sample {
    fn parse(v: &Value) -> io::Result<Self> {
        let recording = v["recording"].as_bool().ok_or_else(bad)?;
        let state = v["state"]
            .as_str()
            .filter(|s| matches!(*s, "stopped" | "running" | "stopping"))
            .ok_or_else(bad)?
            .to_owned();
        let port = if v["port"].is_null() {
            None
        } else {
            Some(parse_port(&v["port"])?)
        };
        let listener = if v["listener"].is_null() {
            None
        } else {
            Some(
                crate::stream::required_uuid4(v, "listener")
                    .map_err(|_| bad())?
                    .to_owned(),
            )
        };
        let revision = v["revision"]
            .as_u64()
            .filter(|r| *r <= MAX_REVISION)
            .ok_or_else(bad)?;
        let path = v["record_path"]
            .as_str()
            .filter(|p| {
                !p.is_empty()
                    && p.len() <= 4096
                    && !p.contains('\0')
                    && std::path::Path::new(p).is_absolute()
            })
            .ok_or_else(bad)?
            .to_owned();
        let listen_port = parse_port(&v["listen_port"])?;
        if (state == "running") != port.is_some() || (state == "stopped") != listener.is_none() {
            return Err(bad());
        }
        Ok(Self {
            recording,
            state,
            port,
            listener,
            revision,
            path,
            listen_port,
        })
    }
    fn add(&self, v: &mut Value) {
        v["recording"] = json!(self.recording);
        v["state"] = json!(self.state);
        v["port"] = json!(self.port);
        v["listener"] = json!(self.listener);
        v["revision"] = json!(self.revision);
        v["record_path"] = json!(self.path);
        v["listen_port"] = json!(self.listen_port);
    }
    fn lines(&self) -> String {
        format!(
            "Event recording {}\nListener {}\n{}\nFuture events append to\n{}\n\nConfigured listener http://127.0.0.1:{}/events\nLocal HTTP clients may read the event feed.",
            if self.recording {
                "enabled"
            } else {
                "disabled"
            },
            self.state,
            self.port
                .map(|p| format!("http://127.0.0.1:{p}/events"))
                .unwrap_or_default(),
            crate::live::terminal_text(&self.path),
            self.listen_port
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    before: Sample,
    action: String,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    Snapshot,
    Review(String),
    Apply(Review),
    Readback {
        review: Review,
        apply_id: String,
        after: Sample,
        status: String,
    },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot(Sample),
    Review(Review),
    Verified { sample: Sample, status: String },
}
pub enum Reply {
    Next(Request),
    Update(Update),
}
impl Request {
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut v = base(id, instance);
        let op = match self {
            Self::Snapshot => 0,
            Self::Review(action) => {
                if !matches!(action.as_str(), "start" | "stop") {
                    return Err(bad());
                }
                v["action"] = json!(action);
                1
            }
            Self::Apply(r) => {
                v["token"] = json!(r.token);
                2
            }
            Self::Readback {
                review, apply_id, ..
            } => {
                v["token"] = json!(review.token);
                v["apply_id"] = json!(apply_id);
                3
            }
        };
        v["type"] = json!(OPERATIONS[op]);
        Ok(v)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command_id: &str,
    ) -> io::Result<Reply> {
        let sample = Sample::parse(v)?;
        let mut expected = base(id, instance);
        sample.add(&mut expected);
        let kind = match &self {
            Self::Snapshot => "glassbox_snapshot",
            Self::Review(_) => "glassbox_review",
            Self::Apply(_) => "glassbox_applied",
            Self::Readback { .. } => "glassbox_verified",
        };
        expected["kind"] = json!(kind);
        let token = if matches!(self, Self::Snapshot) {
            String::new()
        } else {
            v["token"]
                .as_str()
                .filter(|s| {
                    s.len() == 64
                        && s.bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                })
                .ok_or_else(bad)?
                .to_owned()
        };
        if !token.is_empty() {
            expected["token"] = json!(token);
        }
        match self {
            Self::Snapshot => {
                if expected != *v {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Snapshot(sample)))
            }
            Self::Review(action) => {
                expected["action"] = json!(action);
                if expected != *v
                    || sample.revision == MAX_REVISION
                    || action == "start" && sample.state == "stopping"
                {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Review(Review {
                    before: sample,
                    action,
                    token,
                })))
            }
            Self::Apply(review) => {
                let status = outcome(v, &review, &sample)?;
                expected["action"] = json!(review.action);
                expected["apply_id"] = json!(command_id);
                expected["status"] = json!(status);
                if expected != *v || token != review.token {
                    return Err(bad());
                }
                Ok(Reply::Next(Self::Readback {
                    review,
                    apply_id: command_id.into(),
                    after: sample,
                    status,
                }))
            }
            Self::Readback {
                review,
                apply_id,
                after,
                status,
            } => {
                expected["action"] = json!(review.action);
                expected["apply_id"] = json!(apply_id);
                expected["status"] = json!(status);
                if expected != *v
                    || token != review.token
                    || sample != after
                    || outcome(v, &review, &sample)? != status
                {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Verified { sample, status }))
            }
        }
    }
}
fn outcome(v: &Value, r: &Review, after: &Sample) -> io::Result<String> {
    let status = v["status"]
        .as_str()
        .filter(|s| matches!(*s, "applied" | "partial" | "retained" | "failed"))
        .ok_or_else(bad)?;
    if after.path != r.before.path
        || after.listen_port != r.before.listen_port
        || after.revision != r.before.revision + 1
    {
        return Err(bad());
    }
    let target = if r.action == "start" {
        after.recording && after.state == "running" && after.port == Some(after.listen_port)
    } else {
        !after.recording && after.state == "stopped"
    };
    if (status == "applied" && !target)
        || (status == "partial" && (r.action != "start" || !after.recording || target))
        || (status == "retained"
            && (r.action != "stop" || after.recording || after.state != "stopping"))
    {
        return Err(bad());
    }
    Ok(status.into())
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    sample: Option<Sample>,
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
            notice: "Reading registered Glassbox state…".into(),
            ..Self::default()
        };
    }
    pub fn hide(&mut self) {
        self.seen.clear();
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        if !self.open {
            return;
        }
        self.waiting = false;
        self.page = 0;
        self.seen.clear();
        self.review = None;
        match result {
            Err(reason) => {
                self.sample = None;
                self.notice = format!("Outcome requires a fresh inspection. {reason}");
            }
            Ok(Update::Snapshot(s)) => {
                self.sample = Some(s);
                self.notice = String::new();
            }
            Ok(Update::Review(r)) => {
                self.sample = Some(r.before.clone());
                self.review = Some(r);
                self.notice = String::new();
            }
            Ok(Update::Verified { sample, status }) => {
                self.sample = Some(sample);
                self.notice=match status.as_str(){
                    "applied"=>"Verified requested state.",
                    "partial"=>"Recording is enabled. The listener did not reach the configured endpoint.",
                    "retained"=>"Recording is disabled. Stop retains unfinished workers. Review Stop to retry cleanup.",
                    _=>"The source handler failed. Current effects were independently read back.",
                }.into();
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        let request = match key.code {
            KeyCode::Esc => {
                self.open = false;
                self.review = None;
                None
            }
            KeyCode::F(5) => Some(Request::Snapshot),
            KeyCode::Char('s' | 't') if self.sample.is_some() && self.review.is_none() => {
                Some(Request::Review(
                    if key.code == KeyCode::Char('s') {
                        "start"
                    } else {
                        "stop"
                    }
                    .into(),
                ))
            }
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                None
            }
            KeyCode::PageUp | KeyCode::Left => {
                self.page = self.page.saturating_sub(1);
                None
            }
            KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|x| *x) => {
                self.review.take().map(Request::Apply)
            }
            _ => None,
        };
        if request.is_some() {
            self.waiting = true;
            self.review = None;
            self.seen.clear();
        }
        request
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, theme: &Theme) {
        f.render_widget(Clear, area);
        f.render_widget(
            Block::bordered()
                .title(" Glassbox controls ")
                .style(Style::default().fg(theme.foreground).bg(theme.background)),
            area,
        );
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.hide();
            return;
        }
        let mut content = self.notice.clone();
        if let Some(sample) = &self.sample {
            if !content.is_empty() {
                content.push_str("\n\n");
            }
            content.push_str(&sample.lines());
        }
        if let Some(r) = &self.review {
            content.push_str(if r.action=="start" {
                "\n\nStart enables event recording and attempts to bind the configured loopback listener. A bind failure can leave recording enabled."
            }else{
                "\n\nStop disables recording, closes this app's listener and client sockets, and joins their workers."
            });
        }
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
        let footer = if self.waiting {
            "Waiting for source and independent readback".into()
        } else if self.review.is_some() {
            format!(
                "PgUp/PgDn {}/{}  Enter confirm  Esc cancel",
                self.page + 1,
                self.pages
            )
        } else {
            "S review Start  T review Stop  F5 refresh  Esc return".into()
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
    fn sample() -> Value {
        let mut value = base(&identity(), "instance");
        value["kind"] = json!("glassbox_review");
        value["recording"] = json!(false);
        value["state"] = json!("stopped");
        value["port"] = Value::Null;
        value["listener"] = Value::Null;
        value["revision"] = json!(0);
        value["record_path"] = json!(
            std::env::temp_dir()
                .join("glassbox.jsonl")
                .to_string_lossy()
        );
        value["listen_port"] = json!(48319);
        value["action"] = json!("start");
        value["token"] = json!("a".repeat(64));
        value
    }
    #[test]
    fn review_is_exact_and_partial_start_requires_independent_readback() {
        let value = sample();
        let request = Request::Review("start".into());
        let Reply::Update(Update::Review(review)) = request
            .clone()
            .response(&value, &identity(), "instance", "review")
            .unwrap()
        else {
            panic!()
        };
        for (k, v) in [
            ("state", json!("running")),
            ("listener", json!("wrong")),
            ("work_instance", json!("foreign")),
            ("extra", json!(0)),
            ("revision", json!(MAX_REVISION)),
        ] {
            let mut forged = value.clone();
            forged[k] = v;
            assert!(
                request
                    .clone()
                    .response(&forged, &identity(), "instance", "review")
                    .is_err(),
                "{k}"
            );
        }
        let mut applied = value.clone();
        applied["kind"] = json!("glassbox_applied");
        applied["apply_id"] = json!("apply");
        applied["revision"] = json!(1);
        applied["recording"] = json!(true);
        applied["status"] = json!("partial");
        let Reply::Next(readback) = Request::Apply(review)
            .response(&applied, &identity(), "instance", "apply")
            .unwrap()
        else {
            panic!()
        };
        assert_eq!(
            readback.command(&identity(), "instance").unwrap()["type"],
            OPERATIONS[3]
        );
        assert!(
            readback
                .clone()
                .response(&applied, &identity(), "instance", "readback")
                .is_err()
        );
        applied["kind"] = json!("glassbox_verified");
        assert!(matches!(
            readback
                .clone()
                .response(&applied, &identity(), "instance", "readback")
                .unwrap(),
            Reply::Update(Update::Verified { .. })
        ));
        let mut false_recording = applied.clone();
        false_recording["recording"] = json!(false);
        assert!(
            readback
                .clone()
                .response(&false_recording, &identity(), "instance", "readback")
                .is_err()
        );
        applied["status"] = json!("applied");
        assert!(
            readback
                .response(&applied, &identity(), "instance", "readback")
                .is_err()
        );
    }
    #[test]
    fn full_review_resize_fresh_press_and_cancel_preserve_exact_effects() {
        let Reply::Update(update) = Request::Review("start".into())
            .response(&sample(), &identity(), "instance", "review")
            .unwrap()
        else {
            panic!()
        };
        let mut view = View::default();
        view.open();
        view.update(Ok(update.clone()));
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        let paint = |v: &mut View, width| {
            Terminal::new(TestBackend::new(width, 8))
                .unwrap()
                .draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
                .unwrap();
        };
        assert!(view.key(enter).is_none());
        paint(&mut view, 36);
        assert!(view.pages > 1);
        assert!(view.key(enter).is_none());
        for _ in 1..view.pages {
            view.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            paint(&mut view, 36);
        }
        paint(&mut view, 37);
        assert!(view.key(enter).is_none());
        for _ in 0..view.pages {
            view.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
            paint(&mut view, 37);
        }
        let mut repeat = enter;
        repeat.kind = KeyEventKind::Repeat;
        assert!(view.key(repeat).is_none());
        assert!(matches!(view.key(enter), Some(Request::Apply(_))));
        assert!(view.key(enter).is_none());
        view.update(Ok(update));
        assert!(view.key(enter).is_none());
        view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!view.open);
    }
}
