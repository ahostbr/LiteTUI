//! A private, source-decoded image selected for one ordinary composer submission.
use crate::{live::terminal_text, theme::Theme, work_lifecycle, work_protocol::Identity};
#[path = "image_thumbnail.rs"]
mod thumbnail;
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{io, path::Path};
pub use thumbnail::Thumbnail;

pub const OPERATION: &str = "gui.images.native_prepare";

#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    File(String),
    Preview(Review),
    Clear(String),
    Clipboard(Option<String>),
    ReplaceFile {
        path: String,
        previous: String,
    },
    Commit {
        review: Review,
        previous: Option<String>,
    },
}

impl Request {
    pub fn operation(&self) -> &'static str {
        match self {
            Self::File(_) | Self::Clear(_) => OPERATION,
            Self::Preview(_) => "gui.images.native_preview",
            _ => "gui.images.native_clipboard",
        }
    }
    pub fn command(&self, identity: &Identity, instance: &str) -> Value {
        let mut cmd = identity.request();
        cmd["type"] = json!(self.operation());
        cmd["work_instance"] = json!(instance);
        let (path, clear) = match self {
            Self::Preview(review) => {
                cmd["token"] = json!(review.token());
                cmd["png_sha256"] = review.wire()["png_sha256"].clone();
                return cmd;
            }
            Self::File(path) => (Some(path), None),
            Self::Clear(token) => (None, Some(token)),
            other => {
                let (path, previous, select) = match other {
                    Self::Clipboard(prior) => (None, prior.as_deref(), None),
                    Self::ReplaceFile { path, previous } => {
                        (Some(path), Some(previous.as_str()), None)
                    }
                    Self::Commit { review, previous } => {
                        (None, previous.as_deref(), Some(review.token()))
                    }
                    _ => unreachable!(),
                };
                cmd["path"] = json!(path);
                cmd["previous"] = json!(previous);
                cmd["select"] = json!(select);
                return cmd;
            }
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
    if let Request::Commit { review, .. } = request {
        return if value == review.wire()
            && value["session_id"] == identity.session
            && value["agent_id"] == identity.agent
            && value["backend"] == identity.backend
            && value["model"] == identity.model
            && value["work_instance"] == instance
        {
            Ok(Some(review.clone()))
        } else {
            Err(bad())
        };
    }
    if matches!(request, Request::Clipboard(_)) {
        fields.extend(["clipboard_sequence", "clipboard_format"]);
    }
    if !matches!(request, Request::Clear(_)) {
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
        Request::File(_) | Request::Clipboard(_) | Request::ReplaceFile { .. } => {
            let valid_source = match request {
                Request::File(path) | Request::ReplaceFile { path, .. } => {
                    value["name"].as_str() == Path::new(path).file_name().and_then(|v| v.to_str())
                        && matches!(value["format"].as_str(), Some("JPEG" | "PNG" | "WEBP"))
                }
                Request::Clipboard(_) => {
                    let name = value["name"].as_str().unwrap_or("");
                    value["clipboard_sequence"]
                        .as_u64()
                        .is_some_and(|n| (1..=u64::from(u32::MAX)).contains(&n))
                        && match value["clipboard_format"].as_str() {
                            Some("PNG") => name == "Clipboard image" && value["format"] == "PNG",
                            Some("DIB" | "DIBV5") => {
                                name == "Clipboard image" && value["format"] == "DIB"
                            }
                            Some("FILES") => {
                                !name.is_empty()
                                    && name.len() <= 1024
                                    && !name
                                        .chars()
                                        .any(|c| c == '/' || c == '\\' || c.is_control())
                                    && matches!(
                                        value["format"].as_str(),
                                        Some("JPEG" | "PNG" | "WEBP" | "GIF" | "BMP")
                                    )
                            }
                            _ => false,
                        }
                }
                _ => false,
            };
            let pixels = value["width"]
                .as_u64()
                .zip(value["height"].as_u64())
                .and_then(|(w, h)| w.checked_mul(h));
            if value["kind"] != "image_review"
                || !valid_source
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
    pub thumbnail: Option<Thumbnail>,
    file_mode: bool,
    path: String,
    pub notice: String,
    visible: bool,
}

impl Picker {
    pub fn open(&mut self) {
        self.open = true;
        self.thumbnail = None;
        self.path.clear();
        self.file_mode = self.attached.is_none();
        self.candidate = None;
        self.notice.clear();
        self.visible = false;
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.thumbnail = None;
        self.candidate = None;
        self.path.clear();
        self.visible = false;
    }
    pub fn clipboard_request(&self) -> Request {
        Request::Clipboard(self.attached.as_ref().map(|r| r.token().into()))
    }
    pub fn result(&mut self, request: &Request, result: Option<Review>) {
        match request {
            Request::Clear(token) => {
                let cleared_attached = self.attached.as_ref().is_some_and(|r| r.token() == token);
                if cleared_attached {
                    self.attached = None;
                }
                self.candidate = None;
                if !self.file_mode || cleared_attached {
                    self.close();
                }
            }
            Request::Commit { .. } => {
                self.attached = result;
                self.close();
            }
            _ if self.open => {
                self.candidate = result;
                self.file_mode = false;
            }
            _ => {}
        }
        self.notice.clear();
    }
    pub fn editing(&self) -> bool {
        self.open && self.visible && self.candidate.is_none() && self.file_mode
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
        if self.thumbnail.is_some() {
            if matches!(event.code, KeyCode::Esc | KeyCode::Char('v')) {
                self.thumbnail = None;
                self.hide();
            }
            return None;
        }
        if event.code == KeyCode::Char('v') && self.visible && !self.editing() {
            return self
                .candidate
                .as_ref()
                .or(self.attached.as_ref())
                .cloned()
                .map(Request::Preview);
        }
        if event.code == KeyCode::Esc {
            self.file_mode = false;
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
        if event.code == KeyCode::F(2) {
            self.file_mode = true;
            self.path.clear();
            self.notice.clear();
            return self
                .candidate
                .as_ref()
                .map(|r| Request::Clear(r.token().into()));
        }
        if let Some(candidate) = &self.candidate {
            if event.code == KeyCode::Enter {
                let previous = self.attached.as_ref().map(|r| r.token().into());
                if candidate.wire().get("clipboard_sequence").is_some() || previous.is_some() {
                    return Some(Request::Commit {
                        review: candidate.clone(),
                        previous,
                    });
                }
                self.attached = self.candidate.take();
                self.close();
            }
        } else if !self.file_mode && self.attached.is_some() {
            match event.code {
                KeyCode::Delete => {
                    return self
                        .attached
                        .as_ref()
                        .map(|r| Request::Clear(r.token().into()));
                }
                KeyCode::Enter => self.close(),
                _ => {}
            }
        } else {
            match event.code {
                KeyCode::Enter => {
                    let path = self.path.trim().trim_matches('"');
                    if !path.is_empty() {
                        return Some(if let Some(old) = &self.attached {
                            Request::ReplaceFile {
                                path: path.into(),
                                previous: old.token().into(),
                            }
                        } else {
                            Request::File(path.into())
                        });
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
        if let Some(thumbnail) = &self.thumbnail {
            thumbnail.draw(
                frame,
                area,
                theme,
                " Image preview · Esc returns to review ",
            );
            self.visible = false;
            return;
        }
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
        let mut body = if let Some(review) = self
            .candidate
            .as_ref()
            .or_else(|| self.attached.as_ref().filter(|_| !self.file_mode))
        {
            let v = review.wire();
            let effect = if v["delivery"] == "file" {
                "Claude receives a conversation-owned file path and needs its read tool."
            } else {
                "The provider receives PNG image content. Vision support depends on the model."
            };
            let clip = v["clipboard_sequence"]
                .as_u64()
                .map(|seq| {
                    format!(
                        " · Clipboard {} #{}",
                        v["clipboard_format"].as_str().unwrap_or(""),
                        seq
                    )
                })
                .unwrap_or_default();
            format!(
                "{}{}\n{} · {} × {} source pixels\nSending {} × {} · {} PNG bytes\nSHA-256 {}\n{}\n\nV previews pixels · {}",
                terminal_text(review.name()),
                clip,
                v["format"].as_str().unwrap_or(""),
                v["width"],
                v["height"],
                v["sent_width"],
                v["sent_height"],
                v["png_bytes"],
                v["png_sha256"].as_str().expect("validated image digest"),
                effect,
                if self.candidate.is_some() && self.attached.is_some() {
                    "Enter replaces the previous image · Esc keeps it · F2 chooses a file."
                } else if self.attached.is_some() {
                    "Delete clears the attachment · F2 chooses a file · Esc returns to the draft."
                } else {
                    "Enter attaches to this draft · Esc discards · F2 chooses a file. Empty Send asks: Describe this image."
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
