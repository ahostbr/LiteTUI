//! Explicit screen-overlay capture, result review, and an unsent composer attachment.
use crate::{
    dictation::{Draft, digest},
    image,
    live::terminal_text,
    theme::Theme,
    work_protocol::Identity,
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

pub const OPERATION: &str = "gui.screen.native_mark";
#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub action: &'static str,
    draft: Option<String>,
    draft_hash: String,
    capture: Option<String>,
    previous: Option<image::Review>,
    result_hash: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct State {
    pub status: String,
    pub applied: bool,
    capture: String,
    draft_hash: String,
    previous: Option<image::Review>,
    plan: Value,
    result: Value,
    result_hash: Option<String>,
    image: Option<image::Review>,
    preview: Option<image::Thumbnail>,
    message: String,
}
fn bad() -> io::Error {
    io::Error::other("Mark result differs from its reviewed draft, overlay or captured image")
}
fn bounded(v: &Value, cap: usize) -> io::Result<&str> {
    v.as_str()
        .filter(|s| s.len() <= cap && !s.chars().any(|c| c.is_control() && c != '\n' && c != '\t'))
        .ok_or_else(bad)
}
fn stamp(v: &Value) -> io::Result<()> {
    crate::work_lifecycle::keys(v, &["path", "sha256"])?;
    let path = bounded(&v["path"], 4096)?;
    if path.is_empty()
        || path.chars().any(char::is_control)
        || !std::path::Path::new(path).is_absolute()
    {
        return Err(bad());
    }
    crate::command_palette::token(v, "sha256")?;
    Ok(())
}
impl Request {
    pub fn review(draft: &Draft, previous: Option<image::Review>) -> Self {
        Self {
            action: "review",
            draft: Some(draft.text.clone()),
            draft_hash: digest(&draft.text),
            capture: None,
            previous,
            result_hash: None,
        }
    }
    pub fn command(&self, identity: &Identity, instance: &str) -> Value {
        json!({"type":OPERATION,"version":1,"session_id":identity.session,"agent_id":identity.agent,
            "backend":identity.backend,"model":identity.model,"work_instance":instance,
            "action":self.action,"capture":self.capture,"draft":self.draft,"draft_sha256":self.draft_hash,
            "previous":self.previous.as_ref().map(image::Review::wire),"result_sha256":self.result_hash})
    }
    pub fn parse(
        &self,
        id: &Identity,
        instance: &str,
        v: &Value,
        previous: Option<&State>,
    ) -> io::Result<State> {
        let capture = crate::stream::required_uuid4(v, "capture")
            .map_err(|_| bad())?
            .to_owned();
        if self.capture.as_ref().is_some_and(|c| c != &capture) {
            return Err(bad());
        }
        let status = v["status"].as_str().ok_or_else(bad)?;
        let allowed = match self.action {
            "review" => status == "review" && previous.is_none(),
            "start" => {
                matches!(status, "running" | "ready" | "cancelled" | "failed")
                    && previous.is_some_and(|s| s.status == "review")
            }
            "status" => matches!(
                status,
                "review" | "running" | "ready" | "cancelled" | "failed" | "applied"
            ),
            "apply" => status == "applied" && previous.is_some_and(|s| s.status == "ready"),
            "close" => status == "closed",
            _ => false,
        };
        let applied = v["applied"].as_bool().ok_or_else(bad)?;
        if !allowed
            || (self.action == "apply" && !applied)
            || (self.action != "apply" && applied != previous.is_some_and(|s| s.applied))
            || previous.is_some_and(|s| {
                s.capture != capture
                    || s.draft_hash != self.draft_hash
                    || s.previous != self.previous
            })
        {
            return Err(bad());
        }
        let plan = &v["plan"];
        crate::work_lifecycle::keys(plan, &["script", "launcher", "ring", "delivery", "seconds"])?;
        stamp(&plan["script"])?;
        stamp(&plan["launcher"])?;
        if !plan["ring"].is_null() {
            stamp(&plan["ring"])?;
        }
        if plan["seconds"] != 180
            || !matches!(plan["delivery"].as_str(), Some("inline" | "file"))
            || previous.is_some_and(|s| s.plan != *plan)
        {
            return Err(bad());
        }
        let result = &v["result"];
        let (mut image, mut preview, mut message, mut result_hash) =
            (None, None, String::new(), None);
        if !result.is_null() {
            crate::work_lifecycle::keys(
                result,
                &["coordinates", "note", "message", "image", "preview"],
            )?;
            let coordinates = &result["coordinates"];
            crate::work_lifecycle::keys(coordinates, &["x", "y", "mon", "mon_x", "mon_y"])?;
            let coordinate = |key| coordinates[key].as_i64().ok_or_else(bad);
            let (x, y, mon, mx, my) = (
                coordinate("x")?,
                coordinate("y")?,
                coordinate("mon")?,
                coordinate("mon_x")?,
                coordinate("mon_y")?,
            );
            if i32::try_from(x).is_err() || i32::try_from(y).is_err() || !(0..256).contains(&mon) {
                return Err(bad());
            }
            let note = bounded(&result["note"], 4096)?;
            let review = image::parse(
                &result["image"],
                id,
                instance,
                &image::Request::File("mark.png".into()),
            )?
            .ok_or_else(bad)?;
            let wire = review.wire();
            let (w, h, sw, sh) = (
                wire["width"].as_i64().ok_or_else(bad)?,
                wire["height"].as_i64().ok_or_else(bad)?,
                wire["sent_width"].as_i64().ok_or_else(bad)?,
                wire["sent_height"].as_i64().ok_or_else(bad)?,
            );
            if !(0..w).contains(&mx)
                || !(0..h).contains(&my)
                || wire["format"] != "PNG"
                || wire["delivery"] != plan["delivery"]
            {
                return Err(bad());
            }
            message = format!(
                "[mark] I marked the screen at ({x},{y}) — monitor {mon}, monitor-local ({mx},{my}) in the original {w}×{h} monitor image. The attached image shows that whole monitor with the ring at approximately ({},{}) in its delivered {sw}×{sh} pixels. Respond to what I am pointing at.",
                mx * sw / w,
                my * sh / h
            );
            if !note.is_empty() {
                message.push_str(" My note: ");
                message.push_str(&note.replace(['\n', '\t'], " "));
            }
            if result["message"] != message {
                return Err(bad());
            }
            preview = Some(image::Thumbnail::parse(
                &result["preview"],
                id,
                instance,
                &review,
            )?);
            image = Some(review);
            result_hash = Some(digest(&serde_json::to_string(result)?));
        }
        if matches!(status, "ready" | "applied") && result.is_null()
            || matches!(status, "review" | "running" | "failed" | "cancelled") && !result.is_null()
            || applied && result.is_null()
            || previous.is_some_and(|s| !s.result.is_null() && s.result != *result)
            || self.action == "apply" && self.result_hash != result_hash
        {
            return Err(bad());
        }
        let expected = json!({"version":1,"kind":"mark","session_id":id.session,"agent_id":id.agent,
            "backend":id.backend,"model":id.model,"work_instance":instance,"capture":capture,
            "draft_sha256":self.draft_hash,"previous":self.previous.as_ref().map(image::Review::wire),
            "plan":plan,"status":status,"result":result,"result_sha256":result_hash,"applied":applied});
        if expected != *v {
            return Err(bad());
        }
        Ok(State {
            status: status.into(),
            applied,
            capture,
            draft_hash: self.draft_hash.clone(),
            previous: self.previous.clone(),
            plan: plan.clone(),
            result: result.clone(),
            result_hash,
            image,
            preview,
            message,
        })
    }
}
pub struct Applied {
    pub draft: Draft,
    pub previous: Option<image::Review>,
    pub image: image::Review,
    pub message: String,
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    draft: Option<Draft>,
    previous: Option<image::Review>,
    state: Option<State>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
    poll_at: Option<Instant>,
    pub applied: Option<Applied>,
}
impl View {
    pub fn begin(&mut self, draft: Draft, previous: Option<image::Review>) -> Request {
        let request = Request::review(&draft, previous.clone());
        *self = Self {
            open: true,
            draft: Some(draft),
            previous,
            notice: "Reviewing the registered marker and current image preferences".into(),
            ..Self::default()
        };
        request
    }
    pub fn matches(&self, draft: &Draft, image: &Option<image::Review>) -> bool {
        self.draft.as_ref() == Some(draft) && &self.previous == image
    }
    fn fits(&self) -> bool {
        self.draft
            .as_ref()
            .zip(self.state.as_ref())
            .is_some_and(|(d, s)| {
                d.text.chars().count()
                    + if d.text.is_empty() { 0 } else { 1 }
                    + s.message.chars().count()
                    <= 16384
            })
    }
    fn request(&self, action: &'static str) -> Option<Request> {
        let state = self.state.as_ref()?;
        Some(Request {
            action,
            draft: None,
            draft_hash: state.draft_hash.clone(),
            capture: Some(state.capture.clone()),
            previous: self.previous.clone(),
            result_hash: if action == "apply" {
                state.result_hash.clone()
            } else {
                None
            },
        })
    }
    pub fn reply(&mut self, state: State) {
        if self
            .state
            .as_ref()
            .is_none_or(|s| s.status != state.status || s.result != state.result)
        {
            self.page = 0;
            self.seen.clear();
        }
        self.notice.clear();
        if state.status == "closed" {
            if state.applied
                && let (Some(draft), Some(image)) = (self.draft.take(), state.image.clone())
            {
                self.applied = Some(Applied {
                    draft,
                    previous: self.previous.clone(),
                    image,
                    message: state.message.clone(),
                });
            }
            self.open = false;
        }
        self.state = Some(state);
        self.poll_at = Some(Instant::now() + Duration::from_millis(400));
    }
    pub fn reject(&mut self, reason: String) {
        self.notice = reason;
        self.poll_at = None;
    }
    pub fn poll(&mut self, waiting: bool) -> Option<Request> {
        if !self.open || waiting {
            return None;
        }
        let status = self.state.as_ref()?.status.as_str();
        if status == "applied" {
            return self.request("close");
        }
        if status == "running" && self.poll_at.is_some_and(|t| Instant::now() >= t) {
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
            KeyCode::F(5) => return self.request("status"),
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1))
            }
            KeyCode::PageUp | KeyCode::Left => self.page = self.page.saturating_sub(1),
            KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|v| *v) => {
                return match self.state.as_ref().map(|s| s.status.as_str()) {
                    Some("review") => self.request("start"),
                    Some("ready") if self.fits() => self.request("apply"),
                    _ => None,
                };
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::bordered().title(" Mark screen → unsent composer "),
            area,
        );
        let mut body = area.inner(Margin::new(1, 1));
        if body.width < 40 || body.height < 10 {
            self.seen.clear();
            return;
        }
        let content = if let Some(s) = &self.state {
            if let Some(preview) = &s.preview {
                let image_width = (body.width / 3).min(48);
                preview.draw(
                    frame,
                    Rect::new(body.right() - image_width, body.y, image_width, body.height),
                    theme,
                    " Whole monitor ",
                );
                body.width -= image_width + 1;
            }
            let action = match s.status.as_str() {
                "review" => {
                    "Enter Start opens the desktop overlay for up to 180 seconds. Drag the ring and enter an optional note. Overlay Send returns here for a second review; it never sends a prompt."
                }
                "running" => {
                    "The overlay is open. Its Send captures the whole marked monitor and note. Esc cancels and joins the overlay."
                }
                "ready" if self.fits() => {
                    "Enter Apply appends this text and image to your editable, UNSENT composer."
                }
                "ready" => {
                    "The complete composer exceeds 16384 characters. Esc discards this capture."
                }
                "applied" => {
                    "Applied; joining and independently reading back the retained image before returning to the composer."
                }
                "cancelled" => "The marker was cancelled. Esc closes this review.",
                "failed" => {
                    "Capture failed or produced an invalid image. Esc joins and closes the capture."
                }
                _ => "Closing the marker.",
            };
            let replacement = if self.previous.is_some() {
                "Apply REPLACES your previous image. Cancel keeps it and your draft exactly."
            } else {
                "Cancel keeps your draft and attachment unchanged."
            };
            format!(
                "Status: {}\n{action}\n\n{replacement}\n\nScript: {}\nLauncher: {}\nRing: {}\nDelivery: {}\n\n{}\n\n{}",
                s.status,
                terminal_text(s.plan["script"]["path"].as_str().unwrap_or("")),
                terminal_text(s.plan["launcher"]["path"].as_str().unwrap_or("")),
                if s.plan["ring"].is_null() {
                    "drawn by the bundled script"
                } else {
                    "bundled ring image"
                },
                s.plan["delivery"].as_str().unwrap_or(""),
                terminal_text(&s.message),
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
            "Waiting for the source".into()
        } else {
            format!(
                "PgUp/PgDn {}/{}   F5 status   Esc cancel/close",
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

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    fn fixture() -> (Draft, Identity, Value) {
        let draft = Draft {
            text: "Keep draft Δ".into(),
            before_cursor: "Keep ".into(),
            selected: "draft Δ".into(),
        };
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let exe = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let value = json!({"version":1,"kind":"mark","session_id":"s","agent_id":"a","backend":"custom","model":"m","work_instance":"w",
            "capture":"12345678-1234-4123-8123-123456789012","draft_sha256":digest(&draft.text),"previous":null,
            "plan":{"script":{"path":exe,"sha256":"1".repeat(64)},"launcher":{"path":exe,"sha256":"2".repeat(64)},"ring":null,"delivery":"inline","seconds":180},
            "status":"review","result":null,"result_sha256":null,"applied":false});
        (draft, id, value)
    }
    fn ready(mut value: Value) -> Value {
        let image = json!({"version":1,"kind":"image_review","session_id":"s","agent_id":"a","backend":"custom","model":"m","work_instance":"w",
            "token":"a".repeat(64),"delivery":"inline","name":"mark.png","format":"PNG","width":1,"height":1,"sent_width":1,"sent_height":1,"png_bytes":68,"png_sha256":"b".repeat(64)});
        let preview = json!({"version":1,"kind":"image_preview","session_id":"s","agent_id":"a","backend":"custom","model":"m","work_instance":"w",
            "token":"a".repeat(64),"png_sha256":"b".repeat(64),"width":1,"height":1,"rgba":"ChQe/w=="});
        value["status"] = json!("ready");
        value["result"] = json!({"coordinates":{"x":-1920,"y":0,"mon":1,"mon_x":0,"mon_y":0},"note":"Look Δ",
            "message":"[mark] I marked the screen at (-1920,0) — monitor 1, monitor-local (0,0) in the original 1×1 monitor image. The attached image shows that whole monitor with the ring at approximately (0,0) in its delivered 1×1 pixels. Respond to what I am pointing at. My note: Look Δ",
            "image":image,"preview":preview});
        value["result_sha256"] = json!(digest(&serde_json::to_string(&value["result"]).unwrap()));
        value
    }
    fn draw(view: &mut View) {
        let mut terminal = Terminal::new(TestBackend::new(150, 45)).unwrap();
        terminal
            .draw(|frame| {
                view.draw(
                    frame,
                    frame.area(),
                    &crate::theme::builtin_themes()[0],
                    false,
                )
            })
            .unwrap();
    }
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    #[test]
    fn two_visible_reviews_and_close_precede_unsent_result() {
        let (draft, id, value) = fixture();
        let mut view = View::default();
        let request = view.begin(draft.clone(), None);
        view.reply(request.parse(&id, "w", &value, None).unwrap());
        assert!(view.key(key(KeyCode::Enter), false).is_none());
        draw(&mut view);
        let start = view.key(key(KeyCode::Enter), false).unwrap();
        assert_eq!(start.action, "start");
        let ready = ready(value);
        let state = start.parse(&id, "w", &ready, view.state.as_ref()).unwrap();
        view.reply(state);
        assert!(view.key(key(KeyCode::Enter), false).is_none());
        draw(&mut view);
        let apply = view.key(key(KeyCode::Enter), false).unwrap();
        assert_eq!(apply.action, "apply");
        let mut applied = ready.clone();
        applied["status"] = json!("applied");
        applied["applied"] = json!(true);
        view.reply(
            apply
                .parse(&id, "w", &applied, view.state.as_ref())
                .unwrap(),
        );
        assert!(view.applied.is_none() && view.open);
        let close = view.poll(false).unwrap();
        assert_eq!(close.action, "close");
        applied["status"] = json!("closed");
        view.reply(
            close
                .parse(&id, "w", &applied, view.state.as_ref())
                .unwrap(),
        );
        assert!(!view.open);
        let outcome = view.applied.take().unwrap();
        assert_eq!(outcome.draft, draft);
        assert!(outcome.message.ends_with("My note: Look Δ"));
    }
    #[test]
    fn corrupted_mark_coordinates_note_image_and_result_digest_fail_closed() {
        let (draft, id, value) = fixture();
        let request = Request::review(&draft, None);
        let review = request.parse(&id, "w", &value, None).unwrap();
        let mut view = View::default();
        view.begin(draft, None);
        view.reply(review);
        let start = view.request("start").unwrap();
        let valid = ready(value);
        for (pointer, replacement) in [
            ("/result/coordinates/mon_x", json!(1)),
            ("/result/coordinates/x", json!(true)),
            ("/result/note", json!("silently replaced")),
            ("/result/image/token", json!("c".repeat(64))),
            ("/result/preview/png_sha256", json!("c".repeat(64))),
            ("/result_sha256", json!("c".repeat(64))),
            ("/plan/launcher/sha256", json!("c".repeat(64))),
            ("/previous", json!({"changed":true})),
            ("/applied", json!(true)),
        ] {
            let mut bad = valid.clone();
            *bad.pointer_mut(pointer).unwrap() = replacement;
            assert!(
                start.parse(&id, "w", &bad, view.state.as_ref()).is_err(),
                "{pointer}"
            );
        }
        let mut extra = valid.clone();
        extra["unreviewed"] = json!(true);
        assert!(start.parse(&id, "w", &extra, view.state.as_ref()).is_err());
    }
    #[test]
    fn cancel_preserves_captured_selection_and_complete_composer_limit_blocks_apply() {
        let (mut draft, id, mut value) = fixture();
        draft.text = "x".repeat(16384);
        value["draft_sha256"] = json!(digest(&draft.text));
        let mut view = View::default();
        let request = view.begin(draft.clone(), None);
        view.reply(request.parse(&id, "w", &value, None).unwrap());
        let start = view.request("start").unwrap();
        let ready = ready(value);
        view.reply(start.parse(&id, "w", &ready, view.state.as_ref()).unwrap());
        draw(&mut view);
        assert!(!view.fits());
        assert!(view.key(key(KeyCode::Enter), false).is_none());
        assert!(view.matches(&draft, &None));
        let close = view.key(key(KeyCode::Esc), false).unwrap();
        let mut closed = ready;
        closed["status"] = json!("closed");
        view.reply(close.parse(&id, "w", &closed, view.state.as_ref()).unwrap());
        assert!(!view.open && view.applied.is_none());
        assert_eq!(view.draft, Some(draft));
    }
}
