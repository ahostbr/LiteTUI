//! Source-derived model/thinking choices and a native selector. No runtime I/O.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{Frame, layout::Rect, style::Style};
use serde_json::Value;
use std::{collections::HashSet, io};
use unicode_segmentation::UnicodeSegmentation;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Model,
    Thinking,
}
impl Kind {
    pub fn read(self) -> &'static str {
        match self {
            Self::Model => "gui.models.list",
            Self::Thinking => "gui.models.capabilities",
        }
    }
    pub fn apply(self) -> &'static str {
        match self {
            Self::Model => "gui.models.select",
            Self::Thinking => "gui.thinking.set",
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Open(Kind),
    /// `context` is the context-length answer for a local backend's model (None: Keep current).
    Select {
        revision: u64,
        value: String,
        context: Option<u64>,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub struct Choice {
    pub value: String,
    pub label: String,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Choices {
    pub revision: u64,
    pub kind: Kind,
    pub session: String,
    pub backend: String,
    pub model: String,
    pub current: String,
    pub rows: Vec<Choice>,
    pub note: String,
    pub message: Option<String>,
    /// What litetui/context_choice.ask needs for this backend; None for hosted providers.
    pub context: Option<Box<crate::backend_controls::ContextFacts>>,
    /// The readback of a selection the runtime applied and kept: Python's picker closes on a
    /// pick (picker.py `_selected` -> close_dialog), so the view closes on this one.
    pub applied: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Choices(Choices),
    Rejected(String),
}
fn fail(text: &str) -> io::Error {
    io::Error::other(text.to_owned())
}
fn text<'a>(v: &'a Value, k: &str, limit: usize) -> io::Result<&'a str> {
    v.get(k)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty() && v.len() <= limit)
        .ok_or_else(|| fail(&format!("Invalid model control {k}")))
}
impl Choices {
    pub fn parse(kind: Kind, value: &Value, revision: u64, session: &str) -> io::Result<Self> {
        let backend = text(value, "backend", 64)?.to_owned();
        let model = text(value, "model", 1024)?.to_owned();
        let (current, rows, note) = match kind {
            Kind::Model => {
                let values = value
                    .get("models")
                    .and_then(Value::as_array)
                    .filter(|v| v.len() <= 512)
                    .ok_or_else(|| fail("Invalid model catalogue"))?;
                let mut rows = Vec::new();
                for row in values {
                    let slug = text(row, "slug", 1024)?;
                    let name = text(row, "name", 1024)?;
                    if row.get("current").and_then(Value::as_bool) != Some(slug == model) {
                        return Err(fail(
                            "Model catalogue current flag disagrees with selection",
                        ));
                    }
                    if !row
                        .get("loaded")
                        .is_some_and(|v| v.is_null() || v.is_boolean())
                    {
                        return Err(fail("Model loaded state must be nullable boolean"));
                    }
                    // Python's /model row (plugins/model_switch._row_label) when the source sends it.
                    let python = value
                        .get("labels")
                        .and_then(|labels| labels.get(slug))
                        .and_then(Value::as_str)
                        .filter(|l| l.len() <= 4096 && !l.chars().any(char::is_control));
                    rows.push(Choice {
                        value: slug.to_owned(),
                        label: match python {
                            Some(label) => label.to_owned(),
                            None if name == slug => slug.to_owned(),
                            None => format!("{name} · {slug}"),
                        },
                    });
                }
                (
                    model.clone(),
                    rows,
                    format!("Connected catalogue: {backend}. Enter selects; Escape closes."),
                )
            }
            Kind::Thinking => {
                let thinking = &value["thinking"];
                let values = thinking
                    .get("levels")
                    .and_then(Value::as_array)
                    .filter(|v| !v.is_empty() && v.len() <= 32)
                    .ok_or_else(|| fail("Invalid thinking choices"))?;
                let current = match thinking.get("level") {
                    Some(Value::Null) => "default".to_owned(),
                    Some(Value::String(s)) if !s.is_empty() && s.len() <= 64 => s.clone(),
                    _ => {
                        return Err(fail(
                            "Runtime lacks current thinking selection; update Python source",
                        ));
                    }
                };
                let rows = values
                    .iter()
                    .map(|v| {
                        let s = v
                            .as_str()
                            .filter(|s| !s.is_empty() && s.len() <= 64)
                            .ok_or_else(|| fail("Invalid thinking level"))?;
                        Ok(Choice {
                            value: s.to_owned(),
                            label: s.to_owned(),
                        })
                    })
                    .collect::<io::Result<Vec<_>>>()?;
                // Python's /think picker rows (plugins/misc._thinking_rows) when the source sends
                // them; its "unset" row is the "default" level.
                let (current, rows) = match thinking.get("picker").and_then(Value::as_array) {
                    Some(picker) if !picker.is_empty() && picker.len() <= 32 => {
                        let level = |id: &str| {
                            if id == "unset" {
                                "default".to_owned()
                            } else {
                                id.to_owned()
                            }
                        };
                        let rows = picker
                            .iter()
                            .map(|row| match row.as_array().map(Vec::as_slice) {
                                Some([Value::String(id), Value::String(label)])
                                    if !id.is_empty()
                                        && id.len() <= 64
                                        && label.len() <= 4096
                                        && !label.chars().any(char::is_control) =>
                                {
                                    Ok(Choice {
                                        value: level(id),
                                        label: label.clone(),
                                    })
                                }
                                _ => Err(fail("Invalid thinking picker row")),
                            })
                            .collect::<io::Result<Vec<_>>>()?;
                        let current = thinking["picker_current"]
                            .as_str()
                            .map(level)
                            .unwrap_or(current);
                        (current, rows)
                    }
                    _ => (current, rows),
                };
                let source = text(thinking, "source", 1024)?;
                let note = thinking
                    .get("note")
                    .and_then(Value::as_str)
                    .filter(|s| s.len() <= 4096)
                    .ok_or_else(|| fail("Invalid thinking note"))?;
                (current, rows, format!("{source}: {note}"))
            }
        };
        let mut seen = HashSet::new();
        let mut budget = note.len() + current.len() + session.len() + backend.len() + model.len();
        for row in &rows {
            if !seen.insert(&row.value) {
                return Err(fail("Duplicate model/thinking choice"));
            }
            budget = budget.saturating_add(row.label.len() + row.value.len());
        }
        if budget > 65536 {
            return Err(fail("Model controls text budget exceeded"));
        }
        let context = match (kind, value.get("context")) {
            (Kind::Model, Some(facts)) if !facts.is_null() => {
                let mut suggestions = std::collections::HashMap::new();
                let mut remembered = std::collections::HashMap::new();
                for row in &rows {
                    for (field, into) in [
                        ("suggestions", &mut suggestions),
                        ("remembered", &mut remembered),
                    ] {
                        match &facts[field][&row.value] {
                            Value::Null => {}
                            v => {
                                let n = v
                                    .as_u64()
                                    .filter(|n| *n > 0)
                                    .ok_or_else(|| fail("Invalid context suggestion"))?;
                                into.insert(row.value.clone(), n);
                            }
                        }
                    }
                }
                Some(Box::new(crate::backend_controls::ContextFacts {
                    applies_on_engine_start: facts["applies_on_engine_start"]
                        .as_bool()
                        .ok_or_else(|| fail("Invalid context facts"))?,
                    suggestions,
                    remembered,
                }))
            }
            _ => None,
        };
        Ok(Self {
            revision,
            kind,
            session: session.to_owned(),
            backend,
            model,
            current,
            rows,
            note,
            message: None,
            context,
            applied: false,
        })
    }
}

fn display_line(value: &str, offset: usize) -> String {
    terminal_text(value)
        .replace('\n', "\\n")
        .replace('\t', "\\t")
        .graphemes(true)
        .skip(offset)
        .collect()
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub menu: Option<Choices>,
    pub notice: String,
    kind: Option<Kind>,
    cursor: usize,
    horizontal: usize,
    intent: Option<Intent>,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
    /// The picker's first visible wrapped line (picker_box keeps the cursor in view).
    scroll: usize,
    preselect: Option<String>,
    /// The context-length dialog that follows a model pick on a local backend.
    context_input: Option<String>,
    context_error: String,
}
impl View {
    pub fn asking_context(&self) -> bool {
        self.open && self.visible && !self.waiting && self.context_input.is_some()
    }
    pub fn paste(&mut self, text: &str) {
        if self.asking_context()
            && !text.chars().any(char::is_control)
            && let Some(input) = &mut self.context_input
            && input.len() + text.len() <= 64
        {
            input.push_str(text);
        }
    }
    fn select(&mut self, context: Option<u64>) {
        if let Some(menu) = &self.menu
            && let Some(row) = menu.rows.get(self.cursor)
        {
            self.intent = Some(Intent::Select {
                revision: menu.revision,
                value: row.value.clone(),
                context,
            });
            self.context_input = None;
            self.waiting = true;
            self.notice = "Applying selection; waiting for runtime readback…".into();
            self.hide();
        }
    }
    /// A command argument only moves the cursor once the runtime choices arrive.
    /// Applying still needs Enter on the visible row, as for any other selection.
    pub fn preselect(&mut self, wanted: &str) {
        if self.open && self.waiting && !wanted.trim().is_empty() {
            self.preselect = Some(wanted.trim().to_owned());
        }
    }
    fn apply_preselection(&mut self, menu: &Choices) {
        let Some(wanted) = self.preselect.take() else {
            return;
        };
        let found = if menu.kind == Kind::Thinking {
            let level = wanted.to_ascii_lowercase();
            let level = if matches!(level.as_str(), "unset" | "default" | "server") {
                "default".to_owned()
            } else {
                level
            };
            menu.rows
                .iter()
                .position(|r| r.value.eq_ignore_ascii_case(&level))
        } else if wanted.bytes().all(|b| b.is_ascii_digit()) {
            wanted
                .parse::<usize>()
                .ok()
                .and_then(|n| n.checked_sub(1))
                .filter(|i| *i < menu.rows.len())
        } else {
            menu.rows.iter().position(|r| r.value == wanted)
        };
        self.notice = match found {
            Some(index) => {
                self.cursor = index;
                format!(
                    "Command argument {wanted:?} selected {}. Nothing is applied until you press Enter.",
                    menu.rows[index].label
                )
            }
            None => format!(
                "Command argument {wanted:?} is not offered by this runtime. Nothing was selected."
            ),
        };
    }
    pub fn open(&mut self, kind: Kind) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.preselect = None;
        self.menu = None;
        self.horizontal = 0;
        self.kind = Some(kind);
        self.intent = Some(Intent::Open(kind));
        self.notice = "Reading runtime choices…".into();
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn close(&mut self) {
        self.open = false;
        self.preselect = None;
        self.context_input = None;
        self.hide();
        if self.intent.take().is_some() {
            self.waiting = false;
        }
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.menu = None;
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
        self.hits.clear();
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.horizontal = 0;
        match update {
            Update::Choices(menu) if menu.applied => {
                // Enter applied and the runtime kept it: closed, as Python's picker is.
                self.kind = Some(menu.kind);
                self.notice = menu.message.clone().unwrap_or_default();
                self.menu = Some(menu);
                self.open = false;
                self.preselect = None;
                self.context_input = None;
            }
            Update::Choices(menu) => {
                self.kind = Some(menu.kind);
                self.cursor = menu
                    .rows
                    .iter()
                    .position(|r| r.value == menu.current)
                    .unwrap_or(0);
                self.notice = menu
                    .message
                    .clone()
                    .unwrap_or_else(|| "Runtime selection verified.".into());
                self.apply_preselection(&menu);
                self.menu = Some(menu);
            }
            Update::Rejected(error) => {
                self.preselect = None;
                self.menu = None;
                self.notice = error;
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if self.asking_context() {
            match key.code {
                // context_choice: Esc is "Keep current"; the pick itself still happens.
                KeyCode::Esc if key.kind == KeyEventKind::Press => self.select(None),
                KeyCode::Enter if key.kind == KeyEventKind::Press => {
                    match crate::backend_controls::parse_context(
                        self.context_input.as_deref().unwrap_or(""),
                    ) {
                        Ok(tokens) => self.select(Some(tokens)),
                        Err(error) => self.context_error = error,
                    }
                }
                KeyCode::Backspace => {
                    if let Some(input) = &mut self.context_input {
                        input.pop();
                    }
                }
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if let Some(input) = &mut self.context_input
                        && input.len() < 64
                    {
                        input.push(c);
                    }
                }
                _ => {}
            }
            return;
        }
        if !key.modifiers.is_empty() {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        match key.code {
            KeyCode::Tab if key.kind == KeyEventKind::Press => {
                self.open(if self.kind == Some(Kind::Model) {
                    Kind::Thinking
                } else {
                    Kind::Model
                });
            }
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => {
                self.open(self.kind.unwrap_or(Kind::Model))
            }
            KeyCode::Up => {
                self.cursor = self.cursor.saturating_sub(1);
                self.horizontal = 0;
                self.pressed = None;
            }
            KeyCode::Down => {
                self.cursor = (self.cursor + 1).min(
                    self.menu
                        .as_ref()
                        .map_or(0, |m| m.rows.len().saturating_sub(1)),
                );
                self.horizontal = 0;
                self.pressed = None;
            }
            KeyCode::Left => self.horizontal = self.horizontal.saturating_sub(10),
            KeyCode::Right => {
                self.horizontal = self
                    .horizontal
                    .saturating_add(10)
                    .min(self.inspection_end())
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                let ask = self.menu.as_ref().and_then(|menu| {
                    let row = menu.rows.get(self.cursor)?;
                    let facts = menu.context.as_ref()?;
                    Some(
                        facts
                            .suggestions
                            .get(&row.value)
                            .map(u64::to_string)
                            .unwrap_or_default(),
                    )
                });
                match ask {
                    Some(prefill) => {
                        self.context_input = Some(prefill);
                        self.context_error.clear();
                    }
                    None => self.select(None),
                }
            }
            _ => {}
        }
    }
    fn current_label(&self) -> String {
        self.menu
            .as_ref()
            .map(|m| format!("{} · current: {}", m.backend, m.current))
            .unwrap_or_default()
    }
    fn inspection_end(&self) -> usize {
        let current = self.current_label();
        let mut longest = [current.as_str(), self.notice.as_str()]
            .into_iter()
            .map(|s| display_line(s, 0).graphemes(true).count())
            .max()
            .unwrap_or(0);
        if let Some(menu) = &self.menu {
            longest = longest.max(display_line(&menu.note, 0).graphemes(true).count());
            if let Some(row) = menu.rows.get(self.cursor) {
                longest = longest.max(display_line(&row.label, 0).graphemes(true).count());
            }
        }
        longest.saturating_sub(1)
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || !self.visible || self.waiting || event.modifiers != KeyModifiers::NONE {
            return;
        }
        let hit = self
            .hits
            .iter()
            .find(|(r, _)| r.contains((event.column, event.row).into()))
            .map(|(_, i)| *i);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take().is_some_and(|p| Some(p) == hit) {
                    self.cursor = hit.unwrap();
                    self.horizontal = 0;
                    self.key(KeyEvent::from(KeyCode::Enter));
                }
            }
            _ => {}
        }
    }
    /// Python's picker (picker_box): /model is "Select a model", /think "Thinking level".
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, t: &Theme) {
        self.visible = false;
        // Python opens the picker once its rows exist and closes it on a pick: nothing covers
        // the chat while the choices are read or a selection applies.
        if !self.owns_input() || self.waiting {
            self.hide();
            return;
        }
        let title = if self.kind == Some(Kind::Thinking) {
            "Thinking level"
        } else {
            crate::backend_controls::MODEL_TITLE
        };
        let rows: Vec<String> = self.menu.as_ref().map_or_else(Vec::new, |m| {
            m.rows.iter().map(|r| r.label.clone()).collect()
        });
        let mut extra: Vec<(String, Style)> = Vec::new();
        if self.menu.is_none() && !self.notice.is_empty() {
            extra.push((self.notice.clone(), Style::default().fg(t.error)));
        }
        if let (Some(input), Some(menu)) = (&self.context_input, &self.menu) {
            let key = menu.rows.get(self.cursor).map_or("", |r| r.value.as_str());
            let facts = menu.context.as_ref();
            let mut hint = if facts.is_some_and(|f| f.applies_on_engine_start) {
                "Applies on the next /engine start (stop a running engine first).".to_owned()
            } else {
                "The model is loaded at this size.".to_owned()
            };
            if let Some(last) = facts.and_then(|f| f.remembered.get(key)) {
                hint.push_str(&format!(" Last used for this model: {last}."));
            }
            let plain = Style::default().fg(t.foreground);
            extra.push((String::new(), plain));
            extra.push((
                format!("Context length for {} (tokens)", display_line(key, 0)),
                plain,
            ));
            extra.push((hint, Style::default().fg(t.thinking_text)));
            extra.push((format!("{input}▏"), plain));
            if !self.context_error.is_empty() {
                extra.push((self.context_error.clone(), Style::default().fg(t.error)));
            }
            extra.push(("Enter: Set context · Esc: Keep current".into(), plain));
        }
        let drawn = crate::picker_box::draw(
            frame,
            area,
            t,
            title,
            &rows,
            self.cursor,
            &mut self.scroll,
            &extra,
            crate::picker_box::HINT,
        );
        if self.geometry != Some(drawn.rect) {
            self.pressed = None;
            self.geometry = Some(drawn.rect);
        }
        self.hits = if self.context_input.is_some() {
            Vec::new()
        } else {
            drawn.hits
        };
        self.visible = self.open && !self.waiting && drawn.rect.width > 0;
    }
}
