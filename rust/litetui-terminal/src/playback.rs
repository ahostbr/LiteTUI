//! Reviewed playback bound to one complete, source-owned assistant block.
use crate::{
    history::{Fragment, Kind, SavedState, Snapshot},
    live::terminal_text,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{
    io,
    time::{Duration, Instant},
};

pub const OPERATION: &str = "gui.voice.native_playback";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub block: u64,
    pub block_id: String,
    pub message: u64,
    pub message_hash: String,
}
impl Selection {
    pub fn from_fragment(fragment: &Fragment) -> Option<Self> {
        (fragment.kind == Kind::Assistant
            && fragment.role == "assistant"
            && fragment.state == Some(SavedState::Complete))
        .then(|| Self {
            block: fragment.block_index,
            block_id: fragment.block_id.clone(),
            message: fragment.message_index,
            message_hash: fragment.message_sha256.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub action: &'static str,
    pub snapshot: String,
    pub selected: Selection,
    pub playback: Option<String>,
    pub text_hash: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    pub token: String,
    pub text: String,
    pub text_hash: String,
    pub preferences: Value,
    pub status: String,
    pub outcome: Option<String>,
}
impl Request {
    pub fn command(&self, snapshot: &Snapshot) -> Value {
        let mut value = snapshot.identity.command(OPERATION);
        for (key, item) in [
            ("snapshot", json!(self.snapshot)),
            ("block_index", json!(self.selected.block)),
            ("block_id", json!(self.selected.block_id)),
            ("message_index", json!(self.selected.message)),
            ("message_sha256", json!(self.selected.message_hash)),
            ("action", json!(self.action)),
            ("playback", json!(self.playback)),
            ("text_sha256", json!(self.text_hash)),
        ] {
            value[key] = item;
        }
        value
    }
    pub fn parse(
        &self,
        value: &Value,
        snapshot: &Snapshot,
        previous: Option<&State>,
    ) -> io::Result<State> {
        let bad = || {
            io::Error::other("Playback receipt differs from the selected answer or reviewed voice")
        };
        if self.snapshot != snapshot.token || !snapshot.materialized {
            return Err(bad());
        }
        let token = crate::stream::required_uuid4(value, "playback")
            .map_err(|_| bad())?
            .to_owned();
        if self.playback.as_ref().is_some_and(|p| p != &token) {
            return Err(bad());
        }
        let text = value["text"]
            .as_str()
            .filter(|s| {
                !s.trim().is_empty() && s.len() <= 16384 && !s.chars().any(char::is_control)
            })
            .ok_or_else(bad)?
            .to_owned();
        let hash = crate::dictation::digest(&text);
        let preferences = &value["preferences"];
        if preferences.as_object().is_none_or(|o| o.len() != 4)
            || !matches!(preferences["engine"].as_str(), Some("pyttsx3" | "edge"))
            || preferences["voice"]
                .as_str()
                .is_none_or(|s| s.len() > 512 || s.chars().any(char::is_control))
            || preferences["timeout"]
                .as_u64()
                .is_none_or(|n| !(1..=3600).contains(&n))
            || !preferences["litesuite_first"].is_boolean()
            || previous.is_some_and(|p| {
                p.token != token
                    || p.text != text
                    || p.preferences != *preferences
                    || p.status != "review" && value["status"] == "review"
            })
            || self.action != "review"
                && (self.text_hash.as_ref() != Some(&hash) || previous.is_none())
        {
            return Err(bad());
        }
        let status = value["status"].as_str().ok_or_else(bad)?;
        let valid_status = |s: &str| {
            matches!(
                s,
                "review"
                    | "starting"
                    | "local"
                    | "local_finished"
                    | "queued"
                    | "uncertain"
                    | "unavailable"
                    | "failed"
            )
        };
        if match self.action {
            "review" => status != "review",
            "start" => !valid_status(status) || status == "review",
            "status" => !valid_status(status),
            "close" => status != "closed",
            _ => true,
        } {
            return Err(bad());
        }
        let outcome = if status == "closed" {
            let outcome = value["outcome"]
                .as_str()
                .filter(|s| valid_status(s) || *s == "stopped")
                .ok_or_else(bad)?;
            Some(outcome.to_owned())
        } else {
            None
        };
        if (status == "queued" || outcome.as_deref() == Some("queued"))
            && preferences["litesuite_first"] != true
        {
            return Err(bad());
        }
        let mut expected = self.command(snapshot);
        let object = expected.as_object_mut().unwrap();
        object.remove("type");
        object.remove("action");
        object.insert("kind".into(), json!("response_playback"));
        object.insert("playback".into(), json!(token));
        object.insert("text".into(), json!(text));
        object.insert("text_sha256".into(), json!(hash));
        object.insert("preferences".into(), preferences.clone());
        object.insert("status".into(), json!(status));
        object.insert("outcome".into(), json!(outcome));
        if *value != expected {
            return Err(bad());
        }
        Ok(State {
            token,
            text,
            text_hash: hash,
            preferences: preferences.clone(),
            status: status.into(),
            outcome,
        })
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    request: Option<Request>,
    state: Option<State>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
    poll_at: Option<Instant>,
}
impl View {
    pub fn begin(&mut self, snapshot: String, selected: Selection) -> Request {
        let request = Request {
            action: "review",
            snapshot,
            selected,
            playback: None,
            text_hash: None,
        };
        *self = Self {
            open: true,
            request: Some(request.clone()),
            notice: "Reading the selected response and current voice…".into(),
            ..Self::default()
        };
        request
    }
    pub fn reply(&mut self, state: State) -> Option<String> {
        self.notice.clear();
        if self
            .state
            .as_ref()
            .is_none_or(|old| old.status != state.status)
        {
            self.page = 0;
            self.seen.clear();
        }
        let closed = state.status == "closed";
        let notice = closed.then(|| {
            match state.outcome.as_deref() {
                Some("queued") => "LiteSuite accepted this speech and owns its queue and playback.",
                Some("uncertain" | "starting") => {
                    "Speech acknowledgement is uncertain. Playback was not retried."
                }
                Some("stopped") => "The selected local playback helper has stopped.",
                Some("local_finished") => "The selected local playback helper finished.",
                _ => "Response playback closed.",
            }
            .into()
        });
        self.open = !closed;
        self.state = Some(state);
        self.poll_at = Some(Instant::now() + Duration::from_millis(400));
        notice
    }
    pub fn reject(&mut self, reason: String) {
        self.notice = reason;
        self.poll_at = None;
    }
    fn request(&self, action: &'static str) -> Option<Request> {
        let mut request = self.request.clone()?;
        if let Some(state) = &self.state {
            request.playback = Some(state.token.clone());
            request.text_hash = Some(state.text_hash.clone());
        } else {
            return None;
        }
        request.action = action;
        Some(request)
    }
    pub fn poll(&mut self, waiting: bool) -> Option<Request> {
        if self.open
            && !waiting
            && self
                .state
                .as_ref()
                .is_some_and(|s| matches!(s.status.as_str(), "starting" | "local"))
            && self.poll_at.is_some_and(|at| Instant::now() >= at)
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
                    return self.request("close");
                }
                self.open = false;
            }
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1))
            }
            KeyCode::PageUp | KeyCode::Left => self.page = self.page.saturating_sub(1),
            KeyCode::F(5) => return self.request("status"),
            KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|s| *s) => {
                return match self.state.as_ref().map(|s| s.status.as_str()) {
                    Some("review") => self.request("start"),
                    Some("local") => self.request("close"),
                    _ => None,
                };
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, waiting: bool) {
        frame.render_widget(Clear, area);
        frame.render_widget(Block::bordered().title(" Speak selected response "), area);
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.seen.clear();
            return;
        }
        let content = if let Some(state) = &self.state {
            let routing = if state.preferences["litesuite_first"] == true {
                "LiteSuite receives this text first and owns accepted queued playback. Local Stop only controls a local fallback. An uncertain remote acknowledgement is never replayed automatically."
            } else {
                "The selected local speech helper owns this playback. Enter or Escape stops that helper."
            };
            let instruction = match state.status.as_str() {
                "review" => "Read every page. Enter speaks this exact reviewed text once.",
                "starting" => {
                    "Submitting the reviewed speech. Escape retires local work. A remote acknowledgement may remain uncertain."
                }
                "local" => "Local playback is active. Enter stops this response's helper.",
                "local_finished" => "The local playback helper finished.",
                "queued" => "LiteSuite acknowledged queued speech and owns its playback.",
                "uncertain" => "Speech acknowledgement is uncertain. No retry was issued.",
                "unavailable" => "The configured speech engine is unavailable.",
                "failed" => "The local playback helper failed.",
                _ => "Playback closed.",
            };
            let voice = state.preferences["voice"].as_str().unwrap_or("");
            let voice = if voice.is_empty() {
                "engine default"
            } else {
                voice
            };
            format!(
                "Engine: {}\nVoice: {}\nTimeout: {} seconds\n\n{routing}\n\n{instruction}\n\n{}\n\n{}",
                state.preferences["engine"].as_str().unwrap_or(""),
                terminal_text(voice),
                state.preferences["timeout"],
                terminal_text(&state.text),
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
        frame.render_widget(
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
        let help = if waiting {
            "Waiting for the source…".into()
        } else {
            format!(
                "PgUp/PgDn {}/{}  F5 status  Esc return",
                self.page + 1,
                self.pages
            )
        };
        frame.render_widget(
            Paragraph::new(help),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}
