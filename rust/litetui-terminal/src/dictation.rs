//! Explicit microphone capture and source-confirmed application to the composer.
use crate::{live::terminal_text, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io,
    time::{Duration, Instant},
};

pub const OPERATION: &str = "gui.voice.native_dictation";
const MAX_TEXT: usize = 16384;
pub fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Draft {
    pub text: String,
    pub before_cursor: String,
    pub selected: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    pub capture: String,
    pub draft_hash: String,
    pub status: String,
    pub text: String,
    pub text_hash: Option<String>,
    preferences: Value,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub action: &'static str,
    pub draft_hash: String,
    pub capture: Option<String>,
    pub text_hash: Option<String>,
}
impl Request {
    pub fn review(draft: &Draft) -> Self {
        Self {
            action: "review",
            draft_hash: digest(&draft.text),
            capture: None,
            text_hash: None,
        }
    }
    pub fn command(&self, identity: &Identity, instance: &str) -> Value {
        json!({"type":OPERATION,"version":1,"session_id":identity.session,"agent_id":identity.agent,
            "backend":identity.backend,"model":identity.model,"work_instance":instance,
            "action":self.action,"capture":self.capture,"draft_sha256":self.draft_hash,"text_sha256":self.text_hash})
    }
    pub fn parse(
        &self,
        identity: &Identity,
        instance: &str,
        v: &Value,
        previous: Option<&State>,
    ) -> io::Result<State> {
        let bad = || {
            io::Error::other("Dictation result differs from the captured draft or owned recording")
        };
        let capture = crate::stream::required_uuid4(v, "capture")
            .map_err(|_| bad())?
            .to_owned();
        if self.capture.as_ref().is_some_and(|id| id != &capture) {
            return Err(bad());
        }
        let status = v["status"].as_str().ok_or_else(bad)?;
        let allowed = match self.action {
            "review" => status == "review",
            "status" => matches!(
                status,
                "review" | "starting" | "recording" | "transcribing" | "ready" | "empty" | "failed"
            ),
            "start" | "finish" => matches!(
                status,
                "starting" | "recording" | "transcribing" | "ready" | "empty" | "failed"
            ),
            "cancel" => status == "cancelled",
            "apply" => status == "applied",
            _ => false,
        };
        if !allowed {
            return Err(bad());
        }
        let preferences = &v["preferences"];
        if preferences.as_object().is_none_or(|o| o.len() != 3)
            || !matches!(
                preferences["model"].as_str(),
                Some("tiny.en" | "base.en" | "small.en")
            )
            || ["microphone", "hotkey"].iter().any(|key| {
                preferences[key]
                    .as_str()
                    .is_none_or(|s| s.len() > 512 || s.chars().any(char::is_control))
            })
            || previous.is_some_and(|p| {
                p.capture != capture
                    || p.draft_hash != self.draft_hash
                    || p.preferences != *preferences
            })
        {
            return Err(bad());
        }
        let text = v["text"]
            .as_str()
            .filter(|s| s.len() <= MAX_TEXT && !s.chars().any(char::is_control))
            .ok_or_else(bad)?
            .to_owned();
        let text_hash = if text.is_empty() {
            None
        } else {
            Some(digest(&text))
        };
        if matches!(status, "ready" | "applied") != !text.trim().is_empty()
            || !matches!(status, "ready" | "applied") && !text.is_empty()
            || self.action == "apply"
                && (self.text_hash != text_hash || previous.is_none_or(|p| p.text != text))
        {
            return Err(bad());
        }
        let expected = json!({"version":1,"kind":"dictation","session_id":identity.session,"agent_id":identity.agent,
            "backend":identity.backend,"model":identity.model,"work_instance":instance,"capture":capture,
            "draft_sha256":self.draft_hash,"preferences":preferences,"seconds":60,"status":status,"text":text,"text_sha256":text_hash});
        if expected != *v {
            return Err(bad());
        }
        Ok(State {
            capture,
            draft_hash: self.draft_hash.clone(),
            status: status.into(),
            text,
            text_hash,
            preferences: preferences.clone(),
        })
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    draft: Option<Draft>,
    state: Option<State>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
    poll_at: Option<Instant>,
    pub applied: Option<(Draft, String)>,
}
impl View {
    pub fn begin(&mut self, draft: Draft) -> Request {
        let request = Request::review(&draft);
        *self = Self {
            open: true,
            draft: Some(draft),
            notice: "Reading the current dictation preferences".into(),
            ..Self::default()
        };
        request
    }
    fn fits(&self) -> bool {
        self.draft
            .as_ref()
            .zip(self.state.as_ref())
            .is_some_and(|(d, s)| {
                d.text.chars().count() + usize::from(!d.text.is_empty()) + s.text.chars().count()
                    <= 16384
            })
    }
    pub fn request(&self, action: &'static str) -> Option<Request> {
        let state = self.state.as_ref()?;
        Some(Request {
            action,
            draft_hash: state.draft_hash.clone(),
            capture: Some(state.capture.clone()),
            text_hash: if action == "apply" {
                state.text_hash.clone()
            } else {
                None
            },
        })
    }
    pub fn reply(&mut self, state: State) {
        self.notice.clear();
        if self
            .state
            .as_ref()
            .is_none_or(|s| s.status != state.status || s.text != state.text)
        {
            self.page = 0;
            self.seen.clear();
        }
        if state.status == "applied" {
            if let Some(draft) = self.draft.take() {
                self.applied = Some((draft, state.text.clone()));
            }
            self.open = false;
        } else if state.status == "cancelled" {
            self.open = false;
        }
        self.state = Some(state);
        self.poll_at = Some(Instant::now() + Duration::from_millis(400));
    }
    pub fn reject(&mut self, reason: String) {
        self.notice = reason;
        self.poll_at = None;
    }
    pub fn active(&self) -> bool {
        self.state
            .as_ref()
            .is_some_and(|s| matches!(s.status.as_str(), "starting" | "recording" | "transcribing"))
    }
    pub fn poll(&mut self, waiting: bool) -> Option<Request> {
        if self.open
            && !waiting
            && self.active()
            && self.poll_at.is_some_and(|t| Instant::now() >= t)
        {
            self.poll_at = None;
            return self.request("status");
        }
        None
    }
    pub fn key(&mut self, key: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting || key.kind != KeyEventKind::Press || !key.modifiers.is_empty() {
            return None;
        }
        match key.code {
            KeyCode::Esc => {
                if self.state.is_some() {
                    return self.request("cancel");
                }
                self.open = false;
            }
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1))
            }
            KeyCode::PageUp | KeyCode::Left => self.page = self.page.saturating_sub(1),
            KeyCode::F(5) => return self.request("status"),
            KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|v| *v) => {
                return match self.state.as_ref().map(|s| s.status.as_str()) {
                    Some("review") => self.request("start"),
                    Some("starting" | "recording") => self.request("finish"),
                    Some("ready") if self.fits() => self.request("apply"),
                    _ => None,
                };
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, waiting: bool) {
        f.render_widget(Clear, area);
        f.render_widget(Block::bordered().title(" Dictate to composer "), area);
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.seen.clear();
            return;
        }
        let content = if let Some(s) = &self.state {
            let device = s.preferences["microphone"].as_str().unwrap_or("");
            let device = if device.is_empty() {
                "first available microphone"
            } else {
                device
            };
            let action = match s.status.as_str() {
                "review" => {
                    "Enter starts up to 60 seconds of recording with this microphone. The selected faster-whisper model must already be installed."
                }
                "starting" | "recording" => {
                    "Recording your microphone. Enter finishes recording and starts transcription."
                }
                "transcribing" => {
                    "Transcribing the recording locally. Escape cancels and retires the helper."
                }
                "ready" if self.fits() => {
                    "Review the transcript below. Enter appends it to your editable composer draft."
                }
                "ready" => {
                    "The transcript exceeds the remaining composer space. Escape preserves the original draft."
                }
                "empty" => "No speech was recognized. Escape returns to the draft.",
                "failed" => {
                    "Dictation failed. Check the microphone, ffmpeg, faster-whisper and installed model in Voice settings."
                }
                _ => "Dictation finished.",
            };
            format!(
                "Model: {}\nMicrophone: {}\n\n{action}\n\n{}\n\n{}",
                s.preferences["model"].as_str().unwrap_or(""),
                terminal_text(device),
                terminal_text(&s.text),
                terminal_text(&self.notice)
            )
        } else {
            terminal_text(&self.notice)
        };
        let lines = crate::ui::wrap(&content, body.width as usize);
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        if self.geometry != Some((body.width, body.height)) || self.seen.len() != self.pages {
            self.geometry = Some((body.width, body.height));
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        let help = if waiting {
            "Waiting for the source".into()
        } else {
            format!(
                "PgUp/PgDn {}/{}   F5 status   Esc cancel",
                self.page + 1,
                self.pages
            )
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
            Paragraph::new(help),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};

    fn fixture() -> (Draft, Identity, Value) {
        let draft = Draft {
            text: "Original draft".into(),
            before_cursor: "Original".into(),
            selected: " draft".into(),
        };
        let identity = Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "fixture".into(),
        };
        let value = json!({"version":1,"kind":"dictation","session_id":identity.session,"agent_id":identity.agent,
            "backend":identity.backend,"model":identity.model,"work_instance":"work",
            "capture":"12345678-1234-4123-8123-123456789012","draft_sha256":digest(&draft.text),
            "preferences":{"model":"base.en","microphone":"Fixture microphone","hotkey":"ctrl+space"},
            "seconds":60,"status":"review","text":"","text_sha256":null});
        (draft, identity, value)
    }

    fn paint(view: &mut View, width: u16, height: u16) {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| view.draw(frame, frame.area(), false))
            .unwrap();
    }

    #[test]
    fn exact_identity_preferences_and_transcript_receipt_are_required() {
        let (draft, id, value) = fixture();
        let review = Request::review(&draft);
        let state = review.parse(&id, "work", &value, None).unwrap();
        for (key, replacement) in [
            ("session_id", json!("foreign")),
            ("work_instance", json!("stale")),
            ("draft_sha256", json!(digest("different"))),
            ("seconds", json!(61)),
            ("text", json!("unreviewed")),
        ] {
            let mut changed = value.clone();
            changed[key] = replacement;
            assert!(review.parse(&id, "work", &changed, None).is_err(), "{key}");
        }
        let mut view = View::default();
        view.begin(draft);
        view.reply(state);
        let status = view.request("status").unwrap();
        assert!(
            status
                .parse(&id, "work", &value, view.state.as_ref())
                .is_ok()
        );
        let mut ready = value;
        ready["status"] = json!("ready");
        ready["text"] = json!("Recognized words");
        ready["text_sha256"] = json!(digest("Recognized words"));
        let state = status
            .parse(&id, "work", &ready, view.state.as_ref())
            .unwrap();
        view.reply(state);
        let apply = view.request("apply").unwrap();
        ready["status"] = json!("applied");
        assert!(
            apply
                .parse(&id, "work", &ready, view.state.as_ref())
                .is_ok()
        );
        ready["text"] = json!("Substituted transcript");
        ready["text_sha256"] = json!(digest("Substituted transcript"));
        assert!(
            apply
                .parse(&id, "work", &ready, view.state.as_ref())
                .is_err()
        );
    }

    #[test]
    fn recording_and_application_each_require_visible_complete_review() {
        let (draft, id, value) = fixture();
        let mut view = View::default();
        let request = view.begin(draft);
        view.reply(request.parse(&id, "work", &value, None).unwrap());
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        assert!(view.key(enter, false).is_none());
        paint(&mut view, 100, 20);
        assert_eq!(view.key(enter, false).unwrap().action, "start");
        let mut ready = view.state.clone().unwrap();
        ready.status = "ready".into();
        ready.text = "Fixture sentence. ".repeat(120);
        ready.text_hash = Some(digest(&ready.text));
        view.reply(ready);
        assert!(view.key(enter, false).is_none());
        paint(&mut view, 50, 10);
        assert!(view.pages > 1);
        assert!(view.key(enter, false).is_none());
        for _ in 0..view.pages {
            view.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE), false);
            paint(&mut view, 50, 10);
        }
        assert_eq!(view.key(enter, false).unwrap().action, "apply");
        paint(&mut view, 55, 10);
        assert!(view.key(enter, false).is_none());
        assert_eq!(
            view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE), false)
                .unwrap()
                .action,
            "cancel"
        );
    }

    #[test]
    fn empty_failed_overflow_and_repeat_never_apply_or_send() {
        let (draft, id, value) = fixture();
        let mut view = View::default();
        let review = view.begin(draft);
        let state = review.parse(&id, "work", &value, None).unwrap();
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        for status in ["empty", "failed", "transcribing"] {
            let mut reply = state.clone();
            reply.status = status.into();
            view.reply(reply);
            paint(&mut view, 100, 20);
            assert!(view.key(enter, false).is_none());
        }
        view.draft.as_mut().unwrap().text = "x".repeat(16384);
        let mut reply = state;
        reply.status = "ready".into();
        reply.text = "words".into();
        reply.text_hash = Some(digest(&reply.text));
        view.reply(reply);
        paint(&mut view, 100, 20);
        assert!(view.key(enter, false).is_none());
        let mut repeat = enter;
        repeat.kind = KeyEventKind::Repeat;
        assert!(view.key(repeat, false).is_none());
        assert!(view.key(enter, true).is_none());
    }
}
