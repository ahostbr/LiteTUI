//! Explicit, source-validated edits for conversation output budgets.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const READ: &str = "gui.settings.native_snapshot";
pub const VALIDATE: &str = "gui.settings.native_validate";
pub const SAVE: &str = "gui.settings.native_save";
pub const FIELDS: [&str; 2] = ["max_tokens_chat", "max_tokens_tools"];
const MAX: u64 = 2_147_483_647;
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text<'a>(v: &'a Value, key: &str, max: usize) -> io::Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(|| fail("Invalid settings edit text"))
}
fn token(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid settings edit token"));
    }
    Ok(s.into())
}
fn number(v: &Value) -> io::Result<u64> {
    v.as_u64()
        .filter(|n| (1..=MAX).contains(n))
        .ok_or_else(|| fail("Output budget must be an integer from 1 to 2147483647"))
}
fn revisions(v: &Value) -> io::Result<Value> {
    let r = v
        .as_object()
        .filter(|r| r.len() == 3)
        .ok_or_else(|| fail("Invalid settings revisions"))?;
    for key in ["global", "conversation", "agent"] {
        if r.get(key).and_then(Value::as_str) != Some("absent") {
            token(v, key)?;
        }
    }
    Ok(v.clone())
}
fn bounded(
    v: &Value,
    kind: &str,
    session: &str,
    agent: &str,
    backend: &str,
    model: &str,
) -> io::Result<()> {
    if serde_json::to_vec(v)?.len() > 256 * 1024
        || v["version"] != 1
        || v["kind"] != kind
        || v["session_id"] != session
        || v["agent_id"] != agent
        || v["backend"] != backend
        || v["model"] != model
    {
        return Err(fail("Settings edit identity, version, or size changed"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Budget {
    pub saved: u64,
    pub effective: u64,
    pub editable: bool,
    pub reason: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub token: String,
    pub revisions: Value,
    pub budgets: [Budget; 2],
}
impl Snapshot {
    pub fn parse(
        v: &Value,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        bounded(v, "output_budget_snapshot", session, agent, backend, model)?;
        let fields = v["fields"]
            .as_object()
            .filter(|f| f.len() == 2)
            .ok_or_else(|| fail("Invalid output budget fields"))?;
        let mut budgets = Vec::new();
        for field in FIELDS {
            let f = fields
                .get(field)
                .ok_or_else(|| fail("Missing output budget"))?;
            if f["scope"] != "conversation"
                || f["apply_timing"] != "next_turn"
                || f["minimum"] != 1
                || f["maximum"] != MAX
            {
                return Err(fail("Unsupported output budget scope, timing, or bounds"));
            }
            let editable = f["editable"]
                .as_bool()
                .ok_or_else(|| fail("Missing edit authority"))?;
            let reason = if f["reason"].is_null() {
                None
            } else {
                Some(text(f, "reason", 4096)?.into())
            };
            if editable == reason.is_some() {
                return Err(fail("Contradictory output budget authority"));
            }
            budgets.push(Budget {
                saved: number(&f["saved"])?,
                effective: number(&f["effective"])?,
                editable,
                reason,
            });
        }
        Ok(Self {
            session: session.into(),
            agent: agent.into(),
            backend: backend.into(),
            model: model.into(),
            token: token(v, "snapshot")?,
            revisions: revisions(&v["revisions"])?,
            budgets: budgets.try_into().expect("two budgets"),
        })
    }
    pub fn budget(&self, field: &str) -> io::Result<&Budget> {
        FIELDS
            .iter()
            .position(|f| *f == field)
            .map(|i| &self.budgets[i])
            .ok_or_else(|| fail("Unsupported settings edit field"))
    }
    pub fn request(&self, field: &str, value: u64) -> io::Result<Value> {
        let budget = self.budget(field)?;
        if !budget.editable {
            return Err(fail(
                budget.reason.as_deref().unwrap_or("Setting is read-only"),
            ));
        }
        number(&json!(value))?;
        Ok(
            json!({"version":1,"session_id":self.session,"agent_id":self.agent,"snapshot":self.token,"revisions":self.revisions,"field":field,"value":value}),
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub snapshot: Snapshot,
    pub field: String,
    pub value: u64,
    pub token: String,
}
impl Review {
    pub fn parse(v: &Value, snapshot: &Snapshot, field: &str, value: u64) -> io::Result<Self> {
        bounded(
            v,
            "output_budget_review",
            &snapshot.session,
            &snapshot.agent,
            &snapshot.backend,
            &snapshot.model,
        )?;
        snapshot.request(field, value)?;
        let budget = snapshot.budget(field)?;
        if v["snapshot"] != snapshot.token
            || v["revisions"] != snapshot.revisions
            || v["field"] != field
            || v["value"] != value
            || v["scope"] != "conversation"
            || v["apply_timing"] != "next_turn"
            || v["saved_before"] != budget.saved
            || v["effective_before"] != budget.effective
        {
            return Err(fail("Settings review differs from the requested edit"));
        }
        Ok(Self {
            snapshot: snapshot.clone(),
            field: field.into(),
            value,
            token: token(v, "review")?,
        })
    }
    pub fn request(&self) -> io::Result<Value> {
        let mut v = self.snapshot.request(&self.field, self.value)?;
        v["review"] = json!(self.token);
        Ok(v)
    }
}
fn destination(v: &Value, session: &str) -> bool {
    text(v, "destination", 4096).is_ok_and(|path| {
        let path = path.replace('\\', "/");
        !path.chars().any(char::is_control)
            && !path.split('/').any(|part| part == ".." || part == ".")
            && path.ends_with(&format!("/conversations/{session}/settings.json"))
    })
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub status: String,
    pub saved: u64,
    pub effective: u64,
    pub runtime: String,
    pub action: String,
    pub reason: Option<String>,
    pub revisions: Value,
}
impl Outcome {
    /// A failed or ambiguous receipt is a connection error, never a local refusal.
    pub fn parse(v: &Value, review: &Review) -> io::Result<Result<Self, String>> {
        let s = &review.snapshot;
        bounded(
            v,
            "output_budget_save",
            &s.session,
            &s.agent,
            &s.backend,
            &s.model,
        )?;
        if v["snapshot"] != s.token
            || v["review"] != review.token
            || v["field"] != review.field
            || v["value"] != review.value
        {
            return Err(fail("Settings receipt does not match confirmed edit"));
        }
        let mutated = v["mutated"]
            .as_bool()
            .ok_or_else(|| fail("Missing settings mutation status"))?;
        let close = v["close_required"]
            .as_bool()
            .ok_or_else(|| fail("Missing settings closure status"))?;
        let status = text(v, "status", 16)?;
        if close || status == "failed" {
            return Err(fail(
                "Settings save could not establish a safe runtime state; reconnect required",
            ));
        }
        if status == "refused" {
            if mutated {
                return Err(fail("Settings refusal reports a mutation"));
            }
            let p = v["persistence"]
                .as_array()
                .filter(|p| p.len() <= 1)
                .ok_or_else(|| fail("Invalid refused persistence receipt"))?;
            if v["runtime"] != json!([]) {
                return Err(fail("Refused save reports runtime application"));
            }
            if let Some(p) = p.first() {
                if !destination(p, &s.session)
                    || p["scope"] != "conversation"
                    || p["saved"] != false
                    || p["fields"] != json!([review.field])
                    || text(p, "error", 4096).is_err()
                    || v["revisions"] != s.revisions
                    || v["saved"] != s.budget(&review.field)?.saved
                    || v["effective"] != s.budget(&review.field)?.effective
                {
                    return Err(fail("Contradictory refused persistence receipt"));
                }
            } else if !v["saved"].is_null()
                || !v["effective"].is_null()
                || !v["revisions"].is_null()
            {
                return Err(fail("Unexpected values on preflight refusal"));
            }
            return Ok(Err(text(v, "error", 4096)?.into()));
        }
        if !mutated || !matches!(status, "saved" | "partial") {
            return Err(fail("Invalid settings save status"));
        }
        let rows = v["persistence"]
            .as_array()
            .filter(|r| r.len() == 1)
            .ok_or_else(|| fail("Unexpected settings persistence destinations"))?;
        let p = &rows[0];
        let rev = revisions(&v["revisions"])?;
        if !destination(p, &s.session)
            || p["scope"] != "conversation"
            || p["saved"] != true
            || !p["error"].is_null()
            || p["fields"] != json!([review.field])
            || p["revision"] != rev["conversation"]
        {
            return Err(fail("Settings persistence receipt disagrees"));
        }
        let runtime = v["runtime"]
            .as_array()
            .filter(|r| r.len() == 1)
            .ok_or_else(|| fail("Unexpected settings runtime outcomes"))?;
        let r = &runtime[0];
        let runtime = text(r, "status", 16)?;
        let action = text(r, "action", 16)?;
        let saved = number(&v["saved"])?;
        let effective = number(&v["effective"])?;
        if saved != review.value
            || r["field"] != review.field
            || r["scope"] != "conversation"
            || r["requested"] != saved
            || r["effective"] != effective
            || !matches!(runtime, "applied" | "pending" | "failed")
            || !matches!(
                action,
                "none" | "retry" | "reconnect" | "reload" | "restart"
            )
        {
            return Err(fail("Settings runtime receipt disagrees"));
        }
        let reason = if r["reason"].is_null() {
            None
        } else {
            Some(text(r, "reason", 4096)?.into())
        };
        if status == "saved" {
            if runtime != "applied"
                || action != "none"
                || effective != saved
                || reason.is_some()
                || !v["error"].is_null()
            {
                return Err(fail("Contradictory successful settings receipt"));
            }
        } else if runtime == "applied" || reason.is_none() {
            return Err(fail("Contradictory partial settings receipt"));
        }
        Ok(Ok(Self {
            status: status.into(),
            saved,
            effective,
            runtime: runtime.into(),
            action: action.into(),
            reason,
            revisions: rev,
        }))
    }
    pub fn verify(&self, after: &Snapshot, review: &Review) -> io::Result<()> {
        let old = &review.snapshot;
        if after.revisions != self.revisions
            || self.revisions["global"] != old.revisions["global"]
            || self.revisions["agent"] != old.revisions["agent"]
        {
            return Err(fail("Saved settings revision changed before readback"));
        }
        for (i, field) in FIELDS.iter().enumerate() {
            if *field == review.field {
                if after.budgets[i].saved != self.saved
                    || after.budgets[i].effective != self.effective
                    || after.budgets[i].editable != old.budgets[i].editable
                    || after.budgets[i].reason != old.budgets[i].reason
                {
                    return Err(fail(
                        "Saved settings values disagree with independent readback",
                    ));
                }
            } else if after.budgets[i] != old.budgets[i] {
                return Err(fail("Unselected output budget changed"));
            }
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Read,
    Validate {
        snapshot: String,
        field: String,
        value: u64,
    },
    Save {
        review: String,
    },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot(Snapshot),
    Review(Review),
    Rejected(String),
    Saved {
        snapshot: Snapshot,
        outcome: Box<Outcome>,
    },
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub snapshot: Option<Snapshot>,
    field: String,
    draft: String,
    cursor: usize,
    initialized: bool,
    review: Option<Review>,
    intent: Option<Intent>,
    visible: bool,
    reviewed: bool,
    geometry: Option<Rect>,
    page: usize,
    pages: usize,
}
impl View {
    pub fn open(&mut self, field: &str) {
        if self.waiting || !FIELDS.contains(&field) {
            return;
        }
        self.open = true;
        self.field = field.into();
        self.draft.clear();
        self.cursor = 0;
        self.initialized = false;
        self.snapshot = None;
        self.refresh();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn draft(&self) -> &str {
        &self.draft
    }
    pub fn editing(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self
                .snapshot
                .as_ref()
                .and_then(|s| s.budget(&self.field).ok())
                .is_some_and(|b| b.editable)
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
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
        }
        self.review = None;
        self.hide();
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.snapshot = None;
    }
    fn refresh(&mut self) {
        self.waiting = true;
        self.intent = Some(Intent::Read);
        self.review = None;
        self.notice = "Reading edit authority…".into();
        self.hide();
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.intent = None;
        self.hide();
        match update {
            Update::Snapshot(s) => {
                if !self.initialized
                    && let Ok(b) = s.budget(&self.field)
                {
                    self.draft = b.saved.to_string();
                    self.cursor = self.draft.len();
                    self.initialized = true;
                }
                self.notice = s
                    .budget(&self.field)
                    .ok()
                    .and_then(|b| b.reason.clone())
                    .unwrap_or_else(|| "Edit locally, then Enter to validate.".into());
                self.snapshot = Some(s);
                self.review = None;
            }
            Update::Review(r) => {
                self.review = Some(r);
                self.notice =
                    "Review every page. Enter saves only after the final page is visible.".into();
            }
            Update::Rejected(reason) => {
                self.review = None;
                self.snapshot = None;
                self.notice = format!("{reason} · R refresh; draft kept");
            }
            Update::Saved { snapshot, outcome } => {
                self.notice = if outcome.status == "saved" {
                    format!(
                        "Saved {}. Effective {} for the next turn.",
                        outcome.saved, outcome.effective
                    )
                } else {
                    format!(
                        "Saved {}. Effective {}. Runtime {}: {}",
                        outcome.saved,
                        outcome.effective,
                        outcome.runtime,
                        outcome.reason.as_deref().unwrap_or("pending")
                    )
                };
                self.snapshot = Some(snapshot);
                self.review = None;
            }
        }
    }
    pub fn paste(&mut self, text: &str) {
        if !self.editing() {
            return;
        }
        if text.is_empty()
            || !text.bytes().all(|b| b.is_ascii_digit())
            || self.draft.len() + text.len() > 10
        {
            self.notice = "Budget accepts at most 10 decimal digits.".into();
            return;
        }
        self.draft.insert_str(self.cursor, text);
        self.cursor += text.len();
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if !key.modifiers.is_empty() {
            self.reviewed = false;
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if self.review.take().is_some() {
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
        if let Some(review) = &self.review {
            match key.code {
                KeyCode::PageDown if self.reviewed => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                    self.reviewed = false;
                }
                KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    self.reviewed = false;
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && self.reviewed
                        && self.page + 1 == self.pages =>
                {
                    self.intent = Some(Intent::Save {
                        review: review.token.clone(),
                    });
                    self.waiting = true;
                    self.notice = "Saving confirmed budget…".into();
                    self.hide();
                }
                _ => {
                    self.reviewed = false;
                }
            }
            return;
        }
        if key.code == KeyCode::Char('r') && key.kind == KeyEventKind::Press {
            self.refresh();
            return;
        }
        if !self.editing() {
            return;
        }
        match key.code {
            KeyCode::Char(c) if c.is_ascii_digit() => self.paste(&c.to_string()),
            KeyCode::Backspace if self.cursor > 0 => {
                self.cursor -= 1;
                self.draft.remove(self.cursor);
            }
            KeyCode::Delete if self.cursor < self.draft.len() => {
                self.draft.remove(self.cursor);
            }
            KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right => self.cursor = (self.cursor + 1).min(self.draft.len()),
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.draft.len(),
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                match self
                    .draft
                    .parse::<u64>()
                    .ok()
                    .filter(|n| (1..=MAX).contains(n))
                {
                    Some(value) => {
                        self.intent = Some(Intent::Validate {
                            snapshot: self.snapshot.as_ref().expect("editable").token.clone(),
                            field: self.field.clone(),
                            value,
                        });
                        self.waiting = true;
                        self.notice = "Validating edit with source…".into();
                        self.hide();
                    }
                    None => self.notice = "Enter an integer from 1 to 2147483647.".into(),
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, _event: MouseEvent) {
        self.reviewed = false;
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            self.hide();
            return;
        }
        if area.width < 42 || area.height < 18 {
            self.hide();
            if area.width >= 36 && area.height >= 14 {
                let r = Rect::new(area.x + 1, area.y + 2, area.width - 2, 6);
                frame.render_widget(Clear, r);
                frame.render_widget(
                    Paragraph::new("Budget editor needs 42×18.\nResize, or Esc to return.")
                        .block(Block::bordered().title(" Output budget ")),
                    r,
                );
            }
            return;
        }
        let width = area.width.saturating_sub(2).min(96);
        let height = area.height.saturating_sub(2).min(30);
        let r = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(r) {
            self.reviewed = false;
            self.page = 0;
            self.geometry = Some(r);
        }
        frame.render_widget(Clear, r);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(" Output budget · conversation ")
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(r);
        frame.render_widget(block, r);
        let body = Rect::new(
            inner.x,
            inner.y,
            inner.width,
            inner.height.saturating_sub(4),
        );
        let display = |s: &str| terminal_text(s).replace('\n', "\\n").replace('\t', "\\t");
        if let Some(review) = &self.review {
            let budget = review
                .snapshot
                .budget(&review.field)
                .expect("validated review");
            let fields = [
                "CONFIRM SAVE".to_owned(),
                format!("Field: {}", review.field),
                format!("Session: {}", display(&review.snapshot.session)),
                format!("Backend: {}", display(&review.snapshot.backend)),
                format!("Model: {}", display(&review.snapshot.model)),
                format!("Saved: {} → {}", budget.saved, review.value),
                format!("Effective before: {}", budget.effective),
                "Scope: conversation".into(),
                "Applies: next_turn".into(),
            ];
            let lines: Vec<String> = fields
                .iter()
                .flat_map(|s| crate::ui::wrap(s, body.width as usize))
                .collect();
            self.pages = lines.len().div_ceil(usize::from(body.height)).max(1);
            self.page = self.page.min(self.pages - 1);
            frame.render_widget(
                Paragraph::new(
                    lines
                        .into_iter()
                        .skip(self.page * usize::from(body.height))
                        .take(usize::from(body.height))
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                body,
            );
            self.reviewed = self.open && !self.waiting;
        } else {
            let budget = self
                .snapshot
                .as_ref()
                .and_then(|s| s.budget(&self.field).ok());
            let info = format!(
                "{}\nScope: conversation\nApplies: next_turn\nSaved: {}\nEffective: {}\n\nNew value: {}",
                self.field,
                budget.map_or("?".into(), |b| b.saved.to_string()),
                budget.map_or("?".into(), |b| b.effective.to_string()),
                self.draft
            );
            frame.render_widget(
                Paragraph::new(crate::ui::wrap(&info, body.width as usize).join("\n")),
                body,
            );
            if budget.is_some_and(|b| b.editable) && self.open && !self.waiting {
                frame.set_cursor_position((body.x + 11 + self.cursor as u16, body.y + 6));
            }
        }
        let footer = if self.review.is_some() {
            format!(
                "Page {}/{} · PgUp/PgDn · Enter save",
                self.page + 1,
                self.pages
            )
        } else {
            "Enter validate · R refresh · Esc back".into()
        };
        let notice = crate::ui::wrap(&display(&self.notice), inner.width as usize)
            .into_iter()
            .take(2)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(
            Paragraph::new(notice),
            Rect::new(inner.x, inner.bottom() - 4, inner.width, 2),
        );
        frame.render_widget(
            Paragraph::new(footer),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new("Esc keeps the local edit unsaved"),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
    }
}
