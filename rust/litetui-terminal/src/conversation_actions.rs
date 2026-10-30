//! Source-reviewed current-session rename/new. No storage or provider execution.
use crate::{live::terminal_text, prepared_skill::Draft, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;
pub const SNAPSHOT: &str = "gui.conversations.native_snapshot";
pub const VALIDATE: &str = "gui.conversations.native_validate";
pub const APPLY: &str = "gui.conversations.native_apply";
pub const OPERATIONS: [&str; 3] = [SNAPSHOT, VALIDATE, APPLY];
pub const TITLE_BYTES: usize = 4096;
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text(v: &Value, key: &str, max: usize) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid conversation action {key}")))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid conversation action {key}")))
}
fn token(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid conversation action token"));
    }
    Ok(s)
}
fn shape(v: &Value, kind: &str) -> io::Result<()> {
    if serde_json::to_vec(v)?.len() > 65536 || v["version"] != 1 || v["kind"] != kind {
        return Err(fail(
            "Unsupported or oversized conversation action response",
        ));
    }
    Ok(())
}
fn effects(v: &Value) -> io::Result<()> {
    for key in ["materialize", "lifecycle_hooks", "provider_cleanup"] {
        boolean(v, key)?;
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq)]
pub struct Identity {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
}
impl Identity {
    pub fn command(&self, operation: &str) -> Value {
        json!({"type":operation,"version":1,"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"model":self.model})
    }
    pub fn verify(&self, v: &Value) -> io::Result<()> {
        if v["session_id"] != self.session
            || v["agent_id"] != self.agent
            || v["backend"] != self.backend
            || v["model"] != self.model
        {
            return Err(fail(
                "Conversation action changed owned identity or provider",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    New,
    Rename,
}
impl Action {
    pub fn as_str(self) -> &'static str {
        if self == Self::New { "new" } else { "rename" }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub identity: Identity,
    pub token: String,
    pub title: String,
    pub pending: bool,
    pub effects: Value,
    pub new: bool,
    pub rename: bool,
    pub new_reason: String,
    pub rename_reason: String,
}
impl Snapshot {
    pub fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        shape(v, "conversation_transition_snapshot")?;
        identity.verify(v)?;
        effects(&v["effects"])?;
        let pending = boolean(&v["current"], "pending")?;
        if boolean(&v["current"], "materialized")? == pending {
            return Err(fail("Contradictory current conversation persistence"));
        }
        let reason = |key: &str| -> io::Result<(bool, String)> {
            let row = &v["actions"][key];
            let eligible = boolean(row, "eligible")?;
            let reason = if eligible && row["reason"].is_null() {
                String::new()
            } else {
                row["reason"]
                    .as_str()
                    .filter(|s| s.len() <= 4096 && (eligible || !s.is_empty()))
                    .ok_or_else(|| fail("Invalid conversation action eligibility reason"))?
                    .into()
            };
            Ok((eligible, reason))
        };
        let (new, new_reason) = reason("new")?;
        let (rename, rename_reason) = reason("rename")?;
        Ok(Self {
            identity: identity.clone(),
            token: token(v, "snapshot")?,
            title: text(&v["current"], "title", 4096)?,
            pending,
            effects: v["effects"].clone(),
            new,
            rename,
            new_reason,
            rename_reason,
        })
    }
    pub fn eligible(&self, a: Action) -> bool {
        if a == Action::New {
            self.new
        } else {
            self.rename
        }
    }
    pub fn reason(&self, a: Action) -> &str {
        if a == Action::New {
            &self.new_reason
        } else {
            &self.rename_reason
        }
    }
    pub fn request(&self, a: Action, title: &str) -> io::Result<Value> {
        if !self.eligible(a) {
            return Err(fail(self.reason(a)));
        }
        if title.len() > TITLE_BYTES || a == Action::Rename && title.is_empty() {
            return Err(fail("Enter a title of at most 4096 UTF-8 bytes"));
        }
        let mut v = self.identity.command(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["action"] = json!(a.as_str());
        if a == Action::Rename {
            v["title"] = json!(title);
        }
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub before: Snapshot,
    pub action: Action,
    pub target: String,
    pub title: Option<String>,
    pub token: String,
    pub action_id: String,
    pub effects: Value,
}
impl Review {
    pub fn parse(v: &Value, before: Snapshot, action: Action) -> io::Result<Self> {
        shape(v, "conversation_transition_review")?;
        before.identity.verify(v)?;
        effects(&v["effects"])?;
        if !before.eligible(action)
            || v["snapshot"] != before.token
            || v["action"] != action.as_str()
        {
            return Err(fail("Conversation review changed selection"));
        }
        let target = text(v, "target_session_id", 36)?;
        crate::resume::conversation_id(&target)?;
        let action_id = text(v, "action_id", 36)?;
        crate::resume::conversation_id(&action_id)?;
        let title = if action == Action::Rename {
            Some(text(v, "title", 244)?)
        } else {
            if !v["title"].is_null() {
                return Err(fail("New conversation unexpectedly proposes a title"));
            }
            None
        };
        if (action == Action::Rename) != (target == before.identity.session)
            || (action == Action::Rename && title.as_ref().is_some_and(|s| s.chars().count() > 61))
            || v["effects"]["materialize"] != (action == Action::New || before.pending)
        {
            return Err(fail(
                "Conversation review changed target or materialization",
            ));
        }
        Ok(Self {
            before,
            action,
            target,
            title,
            token: token(v, "review")?,
            action_id,
            effects: v["effects"].clone(),
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.before.identity.command(APPLY);
        v["snapshot"] = json!(self.before.token);
        v["review"] = json!(self.token);
        v["action_id"] = json!(self.action_id);
        v["action"] = json!(self.action.as_str());
        v["target_session_id"] = json!(self.target);
        v["title"] = json!(self.title);
        v
    }
    pub fn after_identity(&self) -> Identity {
        let mut i = self.before.identity.clone();
        i.session = self.target.clone();
        i
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt {
    pub review: Review,
    pub refused: bool,
    pub error: Option<String>,
}
impl Receipt {
    fn identity(v: &Value, review: &Review) -> io::Result<()> {
        shape(v, "conversation_transition_apply")?;
        for key in [
            "agent_id",
            "backend",
            "model",
            "snapshot",
            "review",
            "action_id",
            "action",
            "target_session_id",
            "title",
        ] {
            if v[key] != review.request()[key] {
                return Err(fail("Conversation receipt changed reviewed operation"));
            }
        }
        if v["from_session_id"] != review.before.identity.session
            || v["session_id"] != review.before.identity.session && v["session_id"] != review.target
        {
            return Err(fail("Conversation receipt changed session identity"));
        }
        Ok(())
    }
    /// A closing connection consumes the correlated result but proves no effects.
    /// Independent shutdown acknowledgement must still prove worker retirement.
    pub fn drain(v: &Value, review: &Review) -> io::Result<()> {
        if v["status"] != "failed" {
            return Self::parse(v, review.clone()).map(|_| ());
        }
        Self::identity(v, review)?;
        if v["close_required"] != true || v["error"].as_str().is_none_or(|s| s.is_empty()) {
            return Err(fail(
                "Failed closing conversation receipt lacks uncertainty proof",
            ));
        }
        if !matches!(
            v["status"].as_str(),
            Some("refused" | "failed" | "renamed" | "created")
        ) {
            return Err(fail("Unknown closing conversation receipt"));
        }
        for key in ["mutated", "close_required"] {
            boolean(v, key)?;
        }
        for key in ["transcript", "settings"] {
            boolean(&v["persistence"], key)?;
        }
        for key in ["settled", "hooks_completed", "provider_cleanup_completed"] {
            boolean(&v["lifecycle"], key)?;
        }
        match &v["error"] {
            Value::Null => {}
            Value::String(s) if !s.is_empty() && s.len() <= 4096 => {}
            _ => return Err(fail("Invalid closing conversation error")),
        }
        Ok(())
    }
    pub fn parse(v: &Value, review: Review) -> io::Result<Self> {
        Self::identity(v, &review)?;
        let status = text(v, "status", 32)?;
        let refused = status == "refused";
        if v["from_session_id"] != review.before.identity.session
            || v["session_id"]
                != if refused {
                    review.before.identity.session.as_str()
                } else {
                    review.target.as_str()
                }
        {
            return Err(fail("Conversation receipt changed session identity"));
        }
        let mutated = boolean(v, "mutated")?;
        if boolean(v, "close_required")? || status == "failed" {
            return Err(fail(
                "Conversation mutation or cleanup is uncertain; closing connection",
            ));
        }
        let error = match &v["error"] {
            Value::Null => None,
            Value::String(s) if !s.is_empty() && s.len() <= 4096 => Some(s.clone()),
            _ => return Err(fail("Invalid conversation mutation error")),
        };
        for k in ["transcript", "settings"] {
            boolean(&v["persistence"], k)?;
        }
        for k in ["settled", "hooks_completed", "provider_cleanup_completed"] {
            boolean(&v["lifecycle"], k)?;
        }
        if refused {
            if mutated
                || error.is_none()
                || v["persistence"]["transcript"] != false
                || v["persistence"]["settings"] != false
            {
                return Err(fail("Refused conversation action claims mutation"));
            }
        } else if status
            != if review.action == Action::New {
                "created"
            } else {
                "renamed"
            }
            || !mutated
            || error.is_some()
            || v["persistence"]["transcript"] != true
            || v["persistence"]["settings"] != true
            || v["lifecycle"]["settled"] != true
            || v["lifecycle"]["hooks_completed"] != true
            || v["lifecycle"]["provider_cleanup_completed"] != true
        {
            return Err(fail("Conversation action lacks settled durable proof"));
        }
        Ok(Self {
            review,
            refused,
            error,
        })
    }
}
#[derive(Debug, PartialEq)]
pub struct Changed {
    pub action: Action,
    pub snapshot: crate::management::Snapshot,
    pub history: crate::resume::History,
    pub queue: crate::restored_queue::Snapshot,
    pub policy: Value,
    pub current: Snapshot,
    pub page: crate::conversation_browser::Page,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Read {
        action: Action,
    },
    Validate {
        snapshot: String,
        action: Action,
        title: String,
    },
    Apply {
        review: String,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Snapshot(Box<Snapshot>),
    Review(Box<Review>),
    Rejected(String),
}
#[derive(Default)]
pub struct View {
    open: bool,
    waiting: bool,
    executing: bool,
    action: Option<Action>,
    snapshot: Option<Snapshot>,
    review: Option<Review>,
    draft: Draft,
    initialized: bool,
    intent: Option<Intent>,
    visible: bool,
    reviewed: bool,
    area: Rect,
    page: usize,
    pages: usize,
    pub notice: String,
}
impl View {
    pub fn open(&mut self, action: Action) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.action = Some(action);
        self.initialized = false;
        self.draft = Draft::default();
        self.refresh();
    }
    fn refresh(&mut self) {
        self.waiting = true;
        self.executing = false;
        self.review = None;
        self.snapshot = None;
        self.intent = Some(Intent::Read {
            action: self.action.expect("open"),
        });
        self.notice = "Reading current conversation authority...".into();
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn draft(&self) -> &str {
        self.draft.text()
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.reviewed = false;
        self.page = 0;
    }
    pub fn close(&mut self) {
        self.open = false;
        if self.intent.take().is_some() {
            self.waiting = false;
            self.executing = false;
        }
        if !self.executing {
            self.review = None;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.executing = false;
        self.review = None;
        self.snapshot = None;
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.executing = false;
        self.intent = None;
        self.hide();
        if !self.open {
            return;
        }
        match update {
            Update::Snapshot(s) => {
                if !self.initialized {
                    self.draft.set(&s.title).expect("bounded title");
                    self.initialized = true;
                }
                self.notice = if s.eligible(self.action.unwrap()) {
                    if self.action == Some(Action::Rename) {
                        "Edit title, then Enter for source review."
                    } else {
                        "Enter to review a new conversation."
                    }
                    .into()
                } else {
                    s.reason(self.action.unwrap()).into()
                };
                self.snapshot = Some(*s);
                self.review = None;
            }
            Update::Review(r) => {
                self.review = Some(*r);
                self.notice = "Read every page, then confirm once.".into();
            }
            Update::Rejected(s) => {
                self.snapshot = None;
                self.review = None;
                self.notice = format!("{s} | F5 refresh; edit kept");
            }
        }
    }
    pub fn complete(&mut self, action: Action, target: &str) -> io::Result<()> {
        if !self.waiting
            || !self.executing
            || !self
                .review
                .as_ref()
                .is_some_and(|r| r.action == action && r.target == target)
        {
            return Err(fail("Unexpected conversation action completion"));
        }
        self.finish();
        Ok(())
    }
    pub fn editing(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self.action == Some(Action::Rename)
            && self.snapshot.as_ref().is_some_and(|s| s.rename)
    }
    pub fn paste(&mut self, s: &str) {
        if !self.editing() {
            return;
        }
        if self.draft.text().len().saturating_add(s.len()) > TITLE_BYTES {
            self.notice = "Title exceeds 4096 UTF-8 bytes; edit unchanged.".into();
            return;
        }
        self.draft.insert(s).expect("bounded title");
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !(key.modifiers.is_empty()
            || key.modifiers == KeyModifiers::SHIFT && matches!(key.code, KeyCode::Char(_)))
        {
            self.hide();
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.review.is_some() && !self.waiting {
                self.review = None;
                self.hide();
                self.notice = "Review cancelled; edit kept.".into();
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if key.code == KeyCode::F(5) && key.kind == KeyEventKind::Press {
            self.refresh();
            return;
        }
        if key.code == KeyCode::PageDown && self.reviewed {
            self.page = (self.page + 1).min(self.pages.saturating_sub(1));
            self.visible = false;
            self.reviewed = false;
            return;
        }
        if key.code == KeyCode::PageUp {
            self.page = self.page.saturating_sub(1);
            self.visible = false;
            self.reviewed = false;
            return;
        }
        if let Some(r) = &self.review {
            if key.code == KeyCode::Enter
                && key.kind == KeyEventKind::Press
                && self.reviewed
                && self.page + 1 == self.pages
            {
                self.intent = Some(Intent::Apply {
                    review: r.token.clone(),
                });
                self.waiting = true;
                self.executing = true;
                self.notice =
                    "Applying; verifying source lifecycle and independent state...".into();
                self.hide();
            } else {
                self.reviewed = false;
            }
            return;
        }
        if key.code == KeyCode::Enter && key.kind == KeyEventKind::Press {
            if let Some(s) = &self.snapshot {
                match s.request(self.action.unwrap(), self.draft.text()) {
                    Ok(_) => {
                        self.intent = Some(Intent::Validate {
                            snapshot: s.token.clone(),
                            action: self.action.unwrap(),
                            title: self.draft.text().into(),
                        });
                        self.waiting = true;
                        self.notice = "Validating exact action...".into();
                        self.hide();
                    }
                    Err(e) => self.notice = e.to_string(),
                }
            }
            return;
        }
        if self.editing() {
            match key.code {
                KeyCode::Char(c) if !c.is_control() => self.paste(&c.to_string()),
                other => self.draft.key(other),
            }
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        if event.modifiers.is_empty() && self.area.contains((event.column, event.row).into()) {
            match event.kind {
                MouseEventKind::ScrollDown => {
                    self.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
                    return;
                }
                MouseEventKind::ScrollUp => {
                    self.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
                    return;
                }
                _ => {}
            }
        }
        self.hide();
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            return;
        }
        if self.area != area {
            self.hide();
            self.area = area;
        }
        let style = Style::default().fg(theme.foreground).bg(theme.surface);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::default()
                .title(" Conversation action ")
                .borders(Borders::ALL)
                .style(style),
            area,
        );
        if area.width < 42 || area.height < 18 {
            frame.render_widget(
                Paragraph::new("Resize to 42x18\nEsc: cancel or hide pending action").style(style),
                area.inner(ratatui::layout::Margin::new(1, 1)),
            );
            return;
        }
        let body = Rect::new(area.x + 2, area.y + 2, area.width - 4, area.height - 7);
        let details = if let Some(r) = &self.review {
            format!(
                "Confirm {} once\nFrom: {}\nTarget: {}\nBackend: {}\nModel: {}\nOld title: {}\nNew title: {}\nMaterialize conversation files: {}\nRun conversation lifecycle hooks: {}\nSettle provider cleanup: {}\nThe composer is kept. No prompt is sent.",
                r.action.as_str(),
                r.before.identity.session,
                r.target,
                display(&r.before.identity.backend),
                display(&r.before.identity.model),
                display(&r.before.title),
                r.title
                    .as_deref()
                    .map(display)
                    .unwrap_or_else(|| "New conversation (source-derived title)".into()),
                r.effects["materialize"],
                r.effects["lifecycle_hooks"],
                r.effects["provider_cleanup"]
            )
        } else if let Some(s) = &self.snapshot {
            format!(
                "{} current conversation\nSession: {}\nCurrent title: {}\nSaved: {}\n{}",
                self.action.unwrap().as_str(),
                s.identity.session,
                display(&s.title),
                !s.pending,
                display(&self.notice)
            )
        } else {
            display(&self.notice)
        };
        let lines = crate::ui::wrap(&details, body.width as usize);
        let height = usize::from(body.height).max(1);
        self.pages = lines.len().div_ceil(height).max(1);
        self.page = self.page.min(self.pages - 1);
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(self.page * height)
                    .take(height)
                    .collect::<Vec<_>>()
                    .join("\n"),
            )
            .style(style),
            body,
        );
        if self.review.is_none() && self.action == Some(Action::Rename) {
            let before = display(&self.draft.text()[..self.draft.cursor()]);
            let after = display(&self.draft.text()[self.draft.cursor()..]);
            let width = usize::from(area.width - 4);
            use unicode_segmentation::UnicodeSegmentation;
            let mut start = before.len();
            let mut cells = 0;
            for (index, grapheme) in before.grapheme_indices(true).rev() {
                let next = unicode_width::UnicodeWidthStr::width(grapheme);
                if cells + next >= width {
                    break;
                }
                cells += next;
                start = index;
            }
            let prefix = &before[start..];
            let y = area.bottom() - 4;
            frame.render_widget(
                Paragraph::new(format!("{prefix}{after}")).style(style),
                Rect::new(area.x + 2, y, area.width - 4, 1),
            );
            if self.open && !self.waiting {
                frame.set_cursor_position((area.x + 2 + crate::rendered_width(prefix) as u16, y));
            }
        }
        let footer = if self.waiting {
            if self.executing {
                "Applying | Esc hides only"
            } else {
                "Waiting | Esc cancels"
            }
        } else if self.review.is_some() {
            "PgUp/PgDn | Enter confirm | Esc cancel"
        } else {
            "Enter review | F5 refresh | Esc close"
        };
        frame.render_widget(
            Paragraph::new(footer).style(style),
            Rect::new(area.x + 1, area.bottom() - 2, area.width - 2, 1),
        );
        self.visible = self.open && !self.waiting;
        self.reviewed = self.visible;
    }
}
fn display(s: &str) -> String {
    terminal_text(s)
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

/// Shutdown proves retirement only; it never certifies the action's effects.
pub fn verify_shutdown(result: &Value, dispatched: Option<&Value>) -> io::Result<()> {
    let Some(proof) = result.get("conversation_shutdown") else {
        return if dispatched.is_none() {
            Ok(())
        } else {
            Err(fail("Missing conversation shutdown settlement"))
        };
    };
    if serde_json::to_vec(proof)?.len() > 65536
        || proof["version"] != 1
        || proof["worker_settled"] != true
        || proof["lifecycle_settled"] != true
        || proof["native_cleanup_settled"] != true
        || proof["effects_verified"] != false
    {
        return Err(fail(
            "Conversation shutdown did not prove worker retirement",
        ));
    }
    let pending = boolean(proof, "pending_at_close")?;
    let current = text(proof, "current_session_id", 36)?;
    crate::resume::conversation_id(&current)?;
    let keys = [
        "action_id",
        "action",
        "from_session_id",
        "target_session_id",
        "agent_id",
        "backend",
        "model",
    ];
    if keys
        .iter()
        .all(|key| proof.get(*key).is_some_and(Value::is_null))
    {
        return if pending {
            Err(fail("Pending conversation shutdown lacks action identity"))
        } else {
            Ok(())
        };
    }
    let command = dispatched.ok_or_else(|| fail("Unrecognized conversation shutdown action"))?;
    for key in keys {
        let expected = if key == "from_session_id" {
            "session_id"
        } else {
            key
        };
        if proof[key].is_null() || proof[key] != command[expected] {
            return Err(fail(
                "Conversation shutdown changed the dispatched action identity",
            ));
        }
    }
    if pending
        && proof["current_session_id"] != command["session_id"]
        && proof["current_session_id"] != command["target_session_id"]
    {
        return Err(fail("Conversation shutdown changed the current session"));
    }
    Ok(())
}
