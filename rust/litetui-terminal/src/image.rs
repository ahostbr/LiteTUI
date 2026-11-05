//! A private, source-decoded image selected for one ordinary composer submission.
use crate::{live::terminal_text, theme::Theme, work_lifecycle, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{io, path::Path};

pub const OPERATION: &str = "gui.images.native_prepare";

#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    File(String),
    Clear(String),
}

impl Request {
    pub fn command(&self, identity: &Identity, instance: &str) -> Value {
        let mut cmd = identity.request();
        cmd["type"] = json!(OPERATION);
        cmd["work_instance"] = json!(instance);
        let (path, clear) = match self {
            Self::File(path) => (Some(path), None),
            Self::Clear(token) => (None, Some(token)),
        };
        cmd["path"] = json!(path);
        cmd["clear"] = json!(clear);
        cmd
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Review(Value);

impl Review {
    pub fn wire(&self) -> &Value {
        &self.0
    }
    pub fn name(&self) -> &str {
        self.0["name"].as_str().expect("parsed name")
    }
    fn token(&self) -> &str {
        self.0["token"].as_str().expect("parsed token")
    }
}

pub fn parse(
    value: &Value,
    identity: &Identity,
    instance: &str,
    request: &Request,
) -> io::Result<Option<Review>> {
    let bad =
        || io::Error::other("Image reply did not match the selected file and active conversation");
    let mut fields = vec![
        "version",
        "kind",
        "session_id",
        "agent_id",
        "backend",
        "model",
        "work_instance",
        "token",
    ];
    if matches!(request, Request::File(_)) {
        fields.extend([
            "delivery",
            "name",
            "format",
            "width",
            "height",
            "sent_width",
            "sent_height",
            "png_bytes",
            "png_sha256",
        ]);
    }
    work_lifecycle::keys(value, &fields)?;
    if value["version"].as_u64() != Some(1)
        || value["session_id"] != identity.session
        || value["agent_id"] != identity.agent
        || value["backend"] != identity.backend
        || value["model"] != identity.model
        || value["work_instance"] != instance
    {
        return Err(bad());
    }
    let hash = |v: &Value| {
        v.as_str().is_some_and(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    };
    if !hash(&value["token"]) {
        return Err(bad());
    }
    match request {
        Request::Clear(token) if value["kind"] == "image_cleared" && value["token"] == *token => {
            Ok(None)
        }
        Request::File(path) => {
            let pixels = value["width"]
                .as_u64()
                .zip(value["height"].as_u64())
                .and_then(|(w, h)| w.checked_mul(h));
            if value["kind"] != "image_review"
                || value["name"].as_str() != Path::new(path).file_name().and_then(|v| v.to_str())
                || !matches!(value["format"].as_str(), Some("JPEG" | "PNG" | "WEBP"))
                || !pixels.is_some_and(|n| (1..=16_000_000).contains(&n))
                || !["sent_width", "sent_height"]
                    .iter()
                    .all(|k| value[*k].as_u64().is_some_and(|d| (1..=1536).contains(&d)))
                || !value["png_bytes"]
                    .as_u64()
                    .is_some_and(|n| (1..=16 * 1024 * 1024).contains(&n))
                || !hash(&value["png_sha256"])
                || value["delivery"]
                    != if identity.backend == "claude" {
                        "file"
                    } else {
                        "inline"
                    }
            {
                return Err(bad());
            }
            Ok(Some(Review(value.clone())))
        }
        _ => Err(bad()),
    }
}

#[derive(Default)]
pub struct Picker {
    pub open: bool,
    pub attached: Option<Review>,
    candidate: Option<Review>,
    path: String,
    pub notice: String,
    visible: bool,
}

impl Picker {
    pub fn open(&mut self) {
        self.open = true;
        self.path.clear();
        self.candidate = None;
        self.notice.clear();
        self.visible = false;
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.candidate = None;
        self.path.clear();
        self.visible = false;
    }
    pub fn result(&mut self, result: Option<Review>) {
        if result.is_none() {
            self.attached = None;
            self.close();
        } else if self.open {
            self.candidate = result;
        }
        self.notice.clear();
    }
    pub fn editing(&self) -> bool {
        self.open && self.visible && self.attached.is_none() && self.candidate.is_none()
    }
    pub fn paste(&mut self, text: &str) {
        if !self.editing() {
            return;
        }
        if self.path.len().saturating_add(text.len()) > 2048 || text.chars().any(char::is_control) {
            self.notice = "Enter a single path of up to 2048 bytes.".into();
        } else {
            self.path.push_str(text);
            self.notice.clear();
        }
    }
    pub fn key(&mut self, event: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting
            || event.kind != KeyEventKind::Press
            || !event.modifiers.difference(KeyModifiers::SHIFT).is_empty()
        {
            return None;
        }
        if event.code == KeyCode::Esc {
            let cancel = self
                .candidate
                .as_ref()
                .map(|r| Request::Clear(r.token().into()));
            if cancel.is_none() {
                self.close();
            }
            return cancel;
        }
        if !self.visible {
            return None;
        }
        if let Some(r) = self.attached.as_ref() {
            match event.code {
                KeyCode::Delete => return Some(Request::Clear(r.token().into())),
                KeyCode::Enter => self.close(),
                _ => {}
            }
        } else if self.candidate.is_some() {
            if event.code == KeyCode::Enter {
                self.attached = self.candidate.take();
                self.close();
            }
        } else {
            match event.code {
                KeyCode::Enter => {
                    let p = self.path.trim().trim_matches('"');
                    if !p.is_empty() {
                        return Some(Request::File(p.into()));
                    }
                    self.notice = "Enter an absolute image path first.".into();
                }
                KeyCode::Backspace => {
                    self.path.pop();
                }
                KeyCode::Char(c) => self.paste(&c.to_string()),
                _ => {}
            }
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        let width = area.width.min(88);
        let height = area.height.min(16);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let style = Style::default().bg(theme.background).fg(theme.title);
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(" Composer image · Ctrl+O ")
            .style(style);
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let mut body = if let Some(review) = self.candidate.as_ref().or(self.attached.as_ref()) {
            let v = review.wire();
            let effect = if v["delivery"] == "file" {
                "Claude receives a conversation-owned file path and needs its read tool."
            } else {
                "The provider receives PNG image content. Vision support depends on the model."
            };
            format!(
                "{}\n{} · {} × {} source pixels\nSending {} × {} · {} PNG bytes\n{}\n\n{}",
                terminal_text(review.name()),
                v["format"].as_str().unwrap_or(""),
                v["width"],
                v["height"],
                v["sent_width"],
                v["sent_height"],
                v["png_bytes"],
                effect,
                if self.attached.is_some() {
                    "Delete clears the attachment · Enter or Esc returns to the draft."
                } else {
                    "Enter attaches to this draft · Esc discards. With an empty draft, Send asks: Describe this image."
                }
            )
        } else {
            format!(
                "Local image file (PNG, JPEG or WebP)\n{}\n\nEnter reviews the file · Esc returns to the draft.",
                terminal_text(&self.path)
            )
        };
        if waiting {
            body.push_str("\nWaiting for the source…");
        }
        if !self.notice.is_empty() {
            body.push('\n');
            body.push_str(&terminal_text(&self.notice));
        }
        let lines = crate::ui::wrap(&body, usize::from(inner.width.max(1)));
        let height = u16::try_from(lines.len()).unwrap_or(u16::MAX);
        let paragraph = Paragraph::new(lines.join("\n")).style(style);
        // Long editor paths may scroll. A reviewed effect must actually fit before accepting Enter.
        self.visible = inner.width >= 22 && (self.candidate.is_none() || height <= inner.height);
        frame.render_widget(
            paragraph.scroll((height.saturating_sub(inner.height), 0)),
            inner,
        );
    }
}
