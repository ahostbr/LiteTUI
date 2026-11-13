//! `/hooks`: Python's HooksScreen (src/litetui/hooks_screen.py), drawn natively.
//!
//! Python opens HooksBody in HooksScreen: a round $primary box 90% of the screen each way,
//! the HooksEditor (a VerticalScroll, padding 0 1) above a row of Close and Dock to side. The
//! editor's draft and every decision are Python's: its widgets run headless in the source
//! (native_hooks_editor.py) and this view draws what they show, in their order, sends the
//! edited Inputs, TextAreas and Switch with each press or pick (the editor's handlers read
//! them then), and draws the answer. Geometry follows hooks_screen.py's CSS: a Label has a
//! 1-row top margin, Inputs, Selects and the Switch are 3-row tall boxes, the TextArea 8
//! rows, a Horizontal of Buttons 3 rows with each Button at least 9 wide and 1 apart.
use crate::theme::Theme;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    widgets::Clear,
};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::io;
use unicode_width::UnicodeWidthStr;

pub const OPEN: &str = "gui.hooks.native_editor_open";
pub const ACT: &str = "gui.hooks.native_editor_act";
pub const READ: &str = "gui.hooks.native_editor_read";
pub const CLOSE: &str = "gui.hooks.native_editor_close";
const MAX_ENTRIES: usize = 256;
const TEXTAREA_ROWS: u16 = 8;

#[derive(Clone, Debug, PartialEq)]
pub enum Entry {
    Text {
        text: String,
    },
    Label {
        text: String,
    },
    Select {
        id: String,
        prompt: String,
        options: Vec<String>,
        value: Option<usize>,
        disabled: bool,
    },
    Switch {
        id: String,
        value: bool,
        disabled: bool,
    },
    Input {
        id: String,
        value: String,
        placeholder: String,
        disabled: bool,
    },
    TextArea {
        id: String,
        value: String,
        disabled: bool,
    },
    Button {
        id: String,
        label: String,
        variant: String,
        disabled: bool,
    },
    Line(Vec<Entry>),
}
impl Entry {
    fn id(&self) -> Option<&str> {
        match self {
            Entry::Select { id, .. }
            | Entry::Switch { id, .. }
            | Entry::Input { id, .. }
            | Entry::TextArea { id, .. }
            | Entry::Button { id, .. } => Some(id),
            _ => None,
        }
    }
    fn disabled(&self) -> bool {
        match self {
            Entry::Select { disabled, .. }
            | Entry::Switch { disabled, .. }
            | Entry::Input { disabled, .. }
            | Entry::TextArea { disabled, .. }
            | Entry::Button { disabled, .. } => *disabled,
            _ => true,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub entries: Vec<Entry>,
    pub testing: bool,
}

fn bad() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Malformed hooks editor")
}
fn text(v: &Value, key: &str) -> io::Result<String> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| s.len() <= 64 * 1024)
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn wid(v: &Value) -> io::Result<String> {
    v.get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 96)
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn entry(v: &Value, depth: usize, count: &mut usize) -> io::Result<Entry> {
    *count += 1;
    if *count > MAX_ENTRIES || depth > 2 {
        return Err(bad());
    }
    let disabled = v.get("disabled").and_then(Value::as_bool).unwrap_or(false);
    Ok(match v.get("kind").and_then(Value::as_str) {
        Some("text") => Entry::Text {
            text: text(v, "text")?,
        },
        Some("label") => Entry::Label {
            text: text(v, "text")?,
        },
        Some("select") => {
            let options = v
                .get("options")
                .and_then(Value::as_array)
                .filter(|o| o.len() <= 512)
                .ok_or_else(bad)?
                .iter()
                .map(|o| o.as_str().map(str::to_owned).ok_or_else(bad))
                .collect::<io::Result<Vec<_>>>()?;
            let value = match text(v, "value")?.as_str() {
                "" => None,
                n => Some(n.parse::<usize>().map_err(|_| bad())?).filter(|i| *i < options.len()),
            };
            Entry::Select {
                id: wid(v)?,
                prompt: text(v, "prompt")?,
                options,
                value,
                disabled,
            }
        }
        Some("switch") => Entry::Switch {
            id: wid(v)?,
            value: v.get("value").and_then(Value::as_bool).ok_or_else(bad)?,
            disabled,
        },
        Some("input") => Entry::Input {
            id: wid(v)?,
            value: text(v, "value")?,
            placeholder: text(v, "placeholder")?,
            disabled,
        },
        Some("textarea") => Entry::TextArea {
            id: wid(v)?,
            value: text(v, "value")?,
            disabled,
        },
        Some("button") => Entry::Button {
            id: wid(v)?,
            label: text(v, "label")?,
            variant: text(v, "variant")?,
            disabled,
        },
        Some("line") => Entry::Line(
            v.get("items")
                .and_then(Value::as_array)
                .ok_or_else(bad)?
                .iter()
                .map(|i| entry(i, depth + 1, count))
                .collect::<io::Result<Vec<_>>>()?,
        ),
        _ => return Err(bad()),
    })
}
impl Snapshot {
    pub fn parse(v: &Value) -> io::Result<Self> {
        if v["version"] != 1 || v["kind"] != "hooks_editor" {
            return Err(bad());
        }
        let mut count = 0;
        let entries = v["items"]
            .as_array()
            .ok_or_else(bad)?
            .iter()
            .map(|e| entry(e, 0, &mut count))
            .collect::<io::Result<Vec<_>>>()?;
        Ok(Self {
            entries,
            testing: v["testing"].as_bool().ok_or_else(bad)?,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Open,
    Act {
        values: Map<String, Value>,
        action: Option<Value>,
    },
    Read,
    Close,
}
impl Intent {
    pub fn command(&self) -> Value {
        match self {
            Intent::Open => json!({"type": OPEN, "version": 1}),
            Intent::Read => json!({"type": READ, "version": 1}),
            Intent::Close => json!({"type": CLOSE, "version": 1}),
            Intent::Act { values, action } => {
                json!({"type": ACT, "version": 1, "values": values, "action": action})
            }
        }
    }
    pub fn closing(&self) -> bool {
        matches!(self, Intent::Close)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Snapshot(Box<Snapshot>),
    Closed,
    Rejected(String),
}

/// What a click or the focus can land on: a control by its id, Close, or Dock to side.
#[derive(Clone, Debug, PartialEq)]
enum Stop {
    Control(String),
    Close,
    Dock,
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    snapshot: Option<Snapshot>,
    /// Unsent edits: Input and TextArea text, the Switch's value.
    edits: BTreeMap<String, Value>,
    focus: usize,
    cursor: usize,
    /// An open Select's option list: its id and the highlighted option.
    dropdown: Option<(String, usize)>,
    scroll: usize,
    max_scroll: usize,
    page: usize,
    hits: Vec<(Rect, Stop)>,
    option_hits: Vec<(Rect, usize)>,
    intent: Option<Intent>,
    poll_at: Option<std::time::Instant>,
    /// The stop last scrolled into view: Textual reveals a widget when it gains focus, not
    /// on every frame, so the wheel can move away from it.
    revealed: Option<Stop>,
    pub notice: String,
}

/// A control's place in the editor's content: its stop, first row, height, x and width.
type Placed = (Stop, u16, u16, u16, u16);
/// One drawn row of the editor's content: spans at x offsets.
#[derive(Default, Clone)]
struct Row {
    spans: Vec<(u16, String, Style)>,
}

struct Paint {
    surface: Color,
    text: Color,
    muted: Color,
    border: Color,
    accent: Color,
    panel: Color,
    error: Color,
    background: Color,
    warning: Color,
}
impl Paint {
    fn of(t: &Theme) -> Self {
        Self {
            surface: t.surface,
            text: t.foreground,
            muted: t.thinking_text,
            border: t.thinking_border,
            accent: t.primary,
            panel: t.panel,
            error: t.error,
            background: t.background,
            warning: t.title,
        }
    }
}

fn clip(text: &str, cells: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|c| {
            used += unicode_width::UnicodeWidthChar::width(*c).unwrap_or(0);
            used <= cells
        })
        .collect()
}
fn pad(text: &str, cells: usize) -> String {
    let t = clip(text, cells);
    let w = t.width();
    format!("{t}{}", " ".repeat(cells.saturating_sub(w)))
}
fn button_width(label: &str) -> u16 {
    (label.width() as u16 + 2).max(9)
}

impl View {
    pub fn owns_input(&self) -> bool {
        self.open
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        if self.intent.is_none()
            && !self.waiting
            && self.open
            && self.snapshot.as_ref().is_some_and(|s| s.testing)
            && self
                .poll_at
                .is_some_and(|at| std::time::Instant::now() >= at)
        {
            // Test script runs in Python's worker; its result lands in the editor.
            self.poll_at = None;
            self.waiting = true;
            self.intent = Some(Intent::Read);
        }
        self.intent.take()
    }
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        *self = Self {
            open: true,
            waiting: true,
            intent: Some(Intent::Open),
            ..Self::default()
        };
    }
    fn close(&mut self) {
        if self.waiting {
            return;
        }
        self.dropdown = None;
        self.waiting = true;
        self.intent = Some(Intent::Close);
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        match update {
            Update::Snapshot(snapshot) => {
                let testing = snapshot.testing;
                self.snapshot = Some(*snapshot);
                self.edits.clear();
                self.notice.clear();
                self.poll_at = testing
                    .then(|| std::time::Instant::now() + std::time::Duration::from_millis(400));
                let stops = self.stops();
                self.focus = self.focus.min(stops.len().saturating_sub(1));
                self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
            }
            Update::Closed => {
                *self = Self::default();
            }
            Update::Rejected(reason) => {
                // A refused open or act: Python's editor is unchanged; say why and stay.
                self.notice = reason;
                if self.snapshot.is_none() {
                    *self = Self::default();
                }
            }
        }
    }
    fn controls(&self) -> Vec<&Entry> {
        fn visit<'a>(entries: &'a [Entry], out: &mut Vec<&'a Entry>) {
            for e in entries {
                match e {
                    Entry::Line(items) => visit(items, out),
                    e if e.id().is_some() => out.push(e),
                    _ => {}
                }
            }
        }
        let mut out = Vec::new();
        if let Some(s) = &self.snapshot {
            visit(&s.entries, &mut out);
        }
        out
    }
    /// Textual's focus chain: the editor's controls in order, then Close and Dock to side.
    fn stops(&self) -> Vec<Stop> {
        let mut out: Vec<Stop> = self
            .controls()
            .into_iter()
            .filter(|e| !e.disabled())
            .filter_map(|e| e.id().map(|id| Stop::Control(id.to_owned())))
            .collect();
        out.push(Stop::Close);
        out.push(Stop::Dock);
        out
    }
    fn focused(&self) -> Option<Stop> {
        self.stops().get(self.focus).cloned()
    }
    fn entry(&self, id: &str) -> Option<&Entry> {
        self.controls().into_iter().find(|e| e.id() == Some(id))
    }
    fn text_value(&self, entry: &Entry) -> Option<String> {
        let id = entry.id()?;
        if let Some(Value::String(s)) = self.edits.get(id) {
            return Some(s.clone());
        }
        match entry {
            Entry::Input { value, .. } | Entry::TextArea { value, .. } => Some(value.clone()),
            _ => None,
        }
    }
    fn switch_value(&self, entry: &Entry) -> bool {
        match (entry.id().and_then(|id| self.edits.get(id)), entry) {
            (Some(Value::Bool(b)), _) => *b,
            (_, Entry::Switch { value, .. }) => *value,
            _ => false,
        }
    }
    fn text_of_focus(&self) -> Option<String> {
        match self.focused()? {
            Stop::Control(id) => self.entry(&id).and_then(|e| self.text_value(e)),
            _ => None,
        }
    }
    fn send(&mut self, action: Option<Value>) {
        if self.waiting {
            return;
        }
        self.dropdown = None;
        self.waiting = true;
        self.intent = Some(Intent::Act {
            values: self.edits.clone().into_iter().collect(),
            action,
        });
    }
    fn press(&mut self, stop: Stop) {
        match stop {
            Stop::Close => self.close(),
            // Dock to side stays inert, as everywhere so far.
            Stop::Dock => {}
            Stop::Control(id) => match self.entry(&id).cloned() {
                Some(Entry::Button {
                    disabled: false, ..
                }) => self.send(Some(json!({"press": id}))),
                Some(Entry::Switch {
                    disabled: false, ..
                }) => {
                    let on = !self.switch_value(self.entry(&id).expect("switch"));
                    self.edits.insert(id, Value::Bool(on));
                }
                Some(Entry::Select {
                    value,
                    disabled: false,
                    options,
                    ..
                }) if !options.is_empty() => {
                    self.dropdown = Some((id, value.unwrap_or(0)));
                }
                _ => {}
            },
        }
    }
    fn edit(&mut self, change: impl FnOnce(&mut String, &mut usize, bool)) {
        let Some(Stop::Control(id)) = self.focused() else {
            return;
        };
        let Some(entry) = self.entry(&id).cloned() else {
            return;
        };
        let multiline = matches!(entry, Entry::TextArea { .. });
        if !matches!(entry, Entry::Input { .. } | Entry::TextArea { .. }) || entry.disabled() {
            return;
        }
        let mut text = self.text_value(&entry).unwrap_or_default();
        let mut cursor = self.cursor.min(text.chars().count());
        change(&mut text, &mut cursor, multiline);
        self.cursor = cursor;
        self.edits.insert(id, Value::String(text));
    }
    fn byte(text: &str, chars: usize) -> usize {
        text.char_indices()
            .nth(chars)
            .map_or(text.len(), |(i, _)| i)
    }
    pub fn paste(&mut self, pasted: &str) {
        if self.waiting || self.dropdown.is_some() {
            return;
        }
        self.edit(|text, cursor, multiline| {
            let insert: String = if multiline {
                pasted.replace("\r\n", "\n")
            } else {
                pasted.lines().next().unwrap_or("").to_owned()
            };
            let at = Self::byte(text, *cursor);
            text.insert_str(at, &insert);
            *cursor += insert.chars().count();
        });
    }
    fn step_focus(&mut self, delta: isize) {
        let n = self.stops().len() as isize;
        if n == 0 {
            return;
        }
        self.focus = (self.focus as isize + delta).rem_euclid(n) as usize;
        self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !self.open {
            return;
        }
        if self.waiting {
            return;
        }
        if let Some((id, highlighted)) = self.dropdown.clone() {
            let count = match self.entry(&id) {
                Some(Entry::Select { options, .. }) => options.len(),
                _ => 0,
            };
            match key.code {
                KeyCode::Esc => self.dropdown = None,
                KeyCode::Up => self.dropdown = Some((id, highlighted.saturating_sub(1))),
                KeyCode::Down => {
                    self.dropdown = Some((id, (highlighted + 1).min(count.saturating_sub(1))))
                }
                KeyCode::Enter | KeyCode::Char(' ') => self.pick(&id, highlighted),
                _ => {}
            }
            return;
        }
        let focused = self.focused();
        let entry = match &focused {
            Some(Stop::Control(id)) => self.entry(id).cloned(),
            _ => None,
        };
        let typing = matches!(entry, Some(Entry::Input { .. } | Entry::TextArea { .. }));
        match key.code {
            KeyCode::Esc => self.close(),
            KeyCode::Tab if key.modifiers.is_empty() => self.step_focus(1),
            KeyCode::BackTab => self.step_focus(-1),
            KeyCode::Enter | KeyCode::Char(' ')
                if !typing
                    || (key.code == KeyCode::Enter
                        && matches!(entry, Some(Entry::Input { .. }))) =>
            {
                if let Some(stop) = focused
                    && !typing
                {
                    self.press(stop);
                }
            }
            KeyCode::Up | KeyCode::Down if matches!(entry, Some(Entry::Select { .. })) => {
                if let Some(stop) = focused {
                    self.press(stop);
                }
            }
            KeyCode::Enter if matches!(entry, Some(Entry::TextArea { .. })) => {
                self.edit(|text, cursor, _| {
                    let at = Self::byte(text, *cursor);
                    text.insert(at, '\n');
                    *cursor += 1;
                })
            }
            KeyCode::Char(c)
                if typing && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT) =>
            {
                self.edit(|text, cursor, _| {
                    let at = Self::byte(text, *cursor);
                    text.insert(at, c);
                    *cursor += 1;
                })
            }
            KeyCode::Backspace if typing => self.edit(|text, cursor, _| {
                if *cursor > 0 {
                    let at = Self::byte(text, *cursor - 1);
                    text.remove(at);
                    *cursor -= 1;
                }
            }),
            KeyCode::Delete if typing => self.edit(|text, cursor, _| {
                if *cursor < text.chars().count() {
                    let at = Self::byte(text, *cursor);
                    text.remove(at);
                }
            }),
            KeyCode::Left if typing => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right if typing => {
                let n = self.text_of_focus().map_or(0, |t| t.chars().count());
                self.cursor = (self.cursor + 1).min(n);
            }
            KeyCode::Home if typing => self.cursor = 0,
            KeyCode::End if typing => {
                self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count())
            }
            // The editor's scroll bindings, reached from any control that does not take them.
            KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
            KeyCode::Down => self.scroll = (self.scroll + 1).min(self.max_scroll),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(self.page.max(1)),
            KeyCode::PageDown => {
                self.scroll = (self.scroll + self.page.max(1)).min(self.max_scroll)
            }
            KeyCode::Home => self.scroll = 0,
            KeyCode::End => self.scroll = self.max_scroll,
            _ => {}
        }
    }
    fn pick(&mut self, id: &str, index: usize) {
        let current = match self.entry(id) {
            Some(Entry::Select { value, .. }) => *value,
            _ => None,
        };
        self.dropdown = None;
        if current != Some(index) {
            // Python's Select.Changed handler runs (scope reload, hook switch).
            self.send(Some(json!({"select": id, "index": index})));
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        let at = (event.column, event.row).into();
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                if let Some((id, _)) = self.dropdown.clone() {
                    if let Some(&(_, index)) = self.option_hits.iter().find(|(r, _)| r.contains(at))
                    {
                        self.pick(&id, index);
                    } else {
                        self.dropdown = None;
                    }
                    return;
                }
                let Some((_, stop)) = self.hits.iter().find(|(r, _)| r.contains(at)).cloned()
                else {
                    return;
                };
                if let Some(index) = self.stops().iter().position(|s| *s == stop) {
                    self.focus = index;
                    self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
                }
                let typing = matches!(&stop, Stop::Control(id)
                    if matches!(self.entry(id), Some(Entry::Input { .. } | Entry::TextArea { .. })));
                if !typing {
                    self.press(stop);
                }
            }
            MouseEventKind::ScrollDown => self.scroll = (self.scroll + 2).min(self.max_scroll),
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(2),
            _ => {}
        }
    }

    /// The editor's content rows at `width`, with each control's (first row, height, x, w).
    fn layout(&self, width: u16, p: &Paint) -> (Vec<Row>, Vec<Placed>) {
        let mut rows: Vec<Row> = Vec::new();
        let mut boxes = Vec::new();
        let focused = self.focused();
        let w = usize::from(width);
        let surface = Style::default().fg(p.text).bg(p.surface);
        for e in self.snapshot.iter().flat_map(|s| s.entries.iter()) {
            match e {
                Entry::Text { text } => {
                    for line in crate::picker_box::wrap(text, w) {
                        rows.push(Row {
                            spans: vec![(0, line, surface)],
                        });
                    }
                }
                Entry::Label { text } => {
                    rows.push(Row::default());
                    for line in crate::picker_box::wrap(text, w) {
                        rows.push(Row {
                            spans: vec![(0, line, surface)],
                        });
                    }
                }
                Entry::Line(items) => {
                    let top = rows.len();
                    let mut block = [Row::default(), Row::default(), Row::default()];
                    let mut x = 0u16;
                    for item in items {
                        if let Entry::Button {
                            id,
                            label,
                            variant,
                            disabled,
                        } = item
                        {
                            let bw = button_width(label);
                            let is_focus = focused == Some(Stop::Control(id.clone()));
                            for (row, spans) in block
                                .iter_mut()
                                .zip(self.button(label, variant, x, bw, is_focus, *disabled, p))
                            {
                                row.spans.extend(spans);
                            }
                            boxes.push((Stop::Control(id.clone()), top as u16, 3, x, bw));
                            x += bw + 1;
                        }
                    }
                    rows.extend(block);
                }
                Entry::Button {
                    id,
                    label,
                    variant,
                    disabled,
                } => {
                    let top = rows.len();
                    let bw = button_width(label);
                    let is_focus = focused == Some(Stop::Control(id.clone()));
                    for spans in self.button(label, variant, 0, bw, is_focus, *disabled, p) {
                        rows.push(Row { spans });
                    }
                    boxes.push((Stop::Control(id.clone()), top as u16, 3, 0, bw));
                }
                control => {
                    let id = control.id().expect("control").to_owned();
                    let is_focus = focused == Some(Stop::Control(id.clone()));
                    let top = rows.len();
                    let (cw, height) = match control {
                        Entry::Switch { .. } => (10, 3),
                        Entry::TextArea { .. } => (width, TEXTAREA_ROWS),
                        _ => (width, 3),
                    };
                    for spans in self.control(control, cw, is_focus, p) {
                        rows.push(Row { spans });
                    }
                    boxes.push((Stop::Control(id), top as u16, height, 0, cw));
                }
            }
        }
        (rows, boxes)
    }
    #[allow(clippy::too_many_arguments)]
    fn button(
        &self,
        label: &str,
        variant: &str,
        x: u16,
        width: u16,
        focused: bool,
        disabled: bool,
        p: &Paint,
    ) -> [Vec<(u16, String, Style)>; 3] {
        let base = match variant {
            "primary" => p.accent,
            "warning" => p.warning,
            _ => p.panel,
        };
        let bg = if focused { p.accent } else { base };
        let fg = if disabled {
            p.muted
        } else if variant == "default" && !focused {
            p.text
        } else {
            p.background
        };
        let face = Style::default().fg(fg).bg(bg).add_modifier(Modifier::BOLD);
        let rim = Style::default().fg(p.border).bg(bg);
        let w = usize::from(width);
        let lw = label.width();
        let left = w.saturating_sub(lw) / 2;
        [
            vec![(x, "▔".repeat(w), rim)],
            vec![(
                x,
                format!(
                    "{}{}{}",
                    " ".repeat(left),
                    clip(label, w),
                    " ".repeat(w.saturating_sub(left + lw))
                ),
                face,
            )],
            vec![(x, "▁".repeat(w), rim)],
        ]
    }
    fn control(
        &self,
        e: &Entry,
        width: u16,
        focused: bool,
        p: &Paint,
    ) -> Vec<Vec<(u16, String, Style)>> {
        let edge = Style::default()
            .fg(if focused { p.accent } else { p.border })
            .bg(p.surface);
        let text = Style::default()
            .fg(if e.disabled() { p.muted } else { p.text })
            .bg(p.surface);
        let muted = Style::default().fg(p.muted).bg(p.surface);
        let inner = usize::from(width.saturating_sub(2));
        let tall = |body: String, style: Style| {
            vec![
                vec![
                    (0, "▊".into(), edge),
                    (1, "▔".repeat(inner), edge),
                    (1 + inner as u16, "▎".into(), edge),
                ],
                vec![
                    (0, "▊".into(), edge),
                    (1, body, style),
                    (1 + inner as u16, "▎".into(), edge),
                ],
                vec![
                    (0, "▊".into(), edge),
                    (1, "▁".repeat(inner), edge),
                    (1 + inner as u16, "▎".into(), edge),
                ],
            ]
        };
        match e {
            Entry::Switch { .. } => {
                let on = self.switch_value(e);
                let mut rows = tall(" ".repeat(inner), text);
                rows[1] = vec![
                    (0, "▊".into(), edge),
                    (1, " ".repeat(inner), Style::default().bg(p.surface)),
                    (
                        if on { 5 } else { 3 },
                        "  ".into(),
                        Style::default().bg(if on { p.accent } else { p.border }),
                    ),
                    (9, "▎".into(), edge),
                ];
                rows
            }
            Entry::Select {
                prompt,
                options,
                value,
                ..
            } => {
                let shown = value.and_then(|i| options.get(i)).cloned();
                let style = if shown.is_some() { text } else { muted };
                let body = inner.saturating_sub(5);
                tall(
                    format!("  {}▼  ", pad(shown.as_deref().unwrap_or(prompt), body)),
                    style,
                )
            }
            Entry::Input { placeholder, .. } => {
                let value = self.text_value(e).unwrap_or_default();
                let (shown, style) = if value.is_empty() {
                    (placeholder.clone(), muted)
                } else {
                    (value, text)
                };
                let body = inner.saturating_sub(4);
                let start = if focused {
                    self.cursor.saturating_sub(body.saturating_sub(1))
                } else {
                    0
                };
                let visible: String = shown.chars().skip(start).collect();
                tall(format!("  {}  ", pad(&visible, body)), style)
            }
            Entry::TextArea { .. } => {
                let value = self.text_value(e).unwrap_or_default();
                let lines: Vec<&str> = value.split('\n').collect();
                let shown_rows = usize::from(TEXTAREA_ROWS - 2);
                let body = inner.saturating_sub(2);
                let mut out = vec![vec![
                    (0, "▊".into(), edge),
                    (1, "▔".repeat(inner), edge),
                    (1 + inner as u16, "▎".into(), edge),
                ]];
                for i in 0..shown_rows {
                    let line = lines.get(i).copied().unwrap_or("");
                    out.push(vec![
                        (0, "▊".into(), edge),
                        (1, format!(" {} ", pad(line, body)), text),
                        (1 + inner as u16, "▎".into(), edge),
                    ]);
                }
                out.push(vec![
                    (0, "▊".into(), edge),
                    (1, "▁".repeat(inner), edge),
                    (1 + inner as u16, "▎".into(), edge),
                ]);
                out
            }
            _ => Vec::new(),
        }
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.hits.clear();
        self.option_hits.clear();
        if !self.open {
            return;
        }
        let p = Paint::of(theme);
        frame.render_widget(Clear, area);
        let buf = frame.buffer_mut();
        buf.set_style(area, Style::default().fg(p.text).bg(p.background));
        // HooksScreen: HooksBody 90% x 90%, centred, round $primary border on $surface.
        let bw = (u32::from(area.width) * 90 / 100) as u16;
        let bh = (u32::from(area.height) * 90 / 100) as u16;
        if bw < 30 || bh < 15 {
            return;
        }
        let rect = Rect::new(
            area.x + (area.width - bw) / 2,
            area.y + (area.height - bh) / 2,
            bw,
            bh,
        );
        let edge = Style::default().fg(p.accent).bg(p.surface);
        let fill = Style::default().fg(p.text).bg(p.surface);
        for y in rect.top()..rect.bottom() {
            let inner = usize::from(bw - 2);
            let row = if y == rect.top() {
                format!("╭{}╮", "─".repeat(inner))
            } else if y + 1 == rect.bottom() {
                format!("╰{}╯", "─".repeat(inner))
            } else {
                format!("│{}│", " ".repeat(inner))
            };
            buf.set_string(rect.x, y, row, edge);
            if y != rect.top() && y + 1 != rect.bottom() {
                buf.set_string(rect.x + 1, y, " ".repeat(inner), fill);
            }
        }
        let Some(_) = self.snapshot.as_ref() else {
            return;
        };
        // HooksBody's content: the editor (height 1fr) above the 3-row Close / Dock row.
        let inner_x = rect.x + 1;
        let inner_w = bw - 2;
        let editor_top = rect.y + 1;
        let editor_h = bh.saturating_sub(2 + 3);
        // HooksEditor: padding 0 1; its 2-column scrollbar when the content overflows.
        let full = inner_w.saturating_sub(2);
        let (mut rows, mut boxes) = self.layout(full, &p);
        let scrolling = rows.len() > usize::from(editor_h);
        let width = if scrolling {
            full.saturating_sub(2)
        } else {
            full
        };
        if scrolling {
            (rows, boxes) = self.layout(width, &p);
        }
        self.page = usize::from(editor_h);
        self.max_scroll = rows.len().saturating_sub(usize::from(editor_h));
        // Focus reveals its control when it moves there, as Textual's focus does.
        let focused = self.focused();
        if focused != self.revealed
            && let Some(Stop::Control(id)) = focused.clone()
            && let Some(&(_, top, h, _, _)) =
                boxes.iter().find(|b| b.0 == Stop::Control(id.clone()))
        {
            let (top, h) = (usize::from(top), usize::from(h));
            if top < self.scroll {
                self.scroll = top;
            } else if top + h > self.scroll + usize::from(editor_h) {
                self.scroll = (top + h).saturating_sub(usize::from(editor_h));
            }
        }
        self.revealed = focused;
        self.scroll = self.scroll.min(self.max_scroll);
        let cx = inner_x + 1;
        let first = self.scroll;
        for (dy, row) in rows
            .iter()
            .skip(first)
            .take(usize::from(editor_h))
            .enumerate()
        {
            let y = editor_top + dy as u16;
            for (x, text, style) in &row.spans {
                if *x < width {
                    buf.set_string(cx + x, y, clip(text, usize::from(width - x)), *style);
                }
            }
        }
        for (stop, top, h, x, w) in &boxes {
            let top = usize::from(*top);
            let bottom = top + usize::from(*h);
            if bottom <= first || top >= first + usize::from(editor_h) {
                continue;
            }
            let shown_top = top.max(first);
            let shown_bottom = bottom.min(first + usize::from(editor_h));
            self.hits.push((
                Rect::new(
                    cx + x,
                    editor_top + (shown_top - first) as u16,
                    *w,
                    (shown_bottom - shown_top) as u16,
                ),
                stop.clone(),
            ));
        }
        if scrolling {
            let column = inner_x + inner_w - 3;
            for col in [column, column + 1] {
                crate::picker_box::scrollbar(
                    buf,
                    col,
                    editor_top,
                    editor_h,
                    rows.len(),
                    self.scroll,
                    theme,
                );
            }
        }
        // The row of Close (16 wide) and Dock to side (width 100%, so it runs off the right).
        let by = editor_top + editor_h;
        let close_focus = self.focused() == Some(Stop::Close);
        let dock_focus = self.focused() == Some(Stop::Dock);
        let close = self.button("Close", "default", 0, 16, close_focus, false, &p);
        let dock = self.button("Dock to side", "default", 0, inner_w, dock_focus, false, &p);
        for (i, (c, d)) in close.iter().zip(dock.iter()).enumerate() {
            for (_, text, style) in d {
                buf.set_string(
                    inner_x + 16,
                    by + i as u16,
                    clip(text, usize::from(inner_w - 16)),
                    *style,
                );
            }
            for (_, text, style) in c {
                buf.set_string(inner_x, by + i as u16, text, *style);
            }
        }
        self.hits.push((Rect::new(inner_x, by, 16, 3), Stop::Close));
        self.hits
            .push((Rect::new(inner_x + 16, by, inner_w - 16, 3), Stop::Dock));
        // The caret in a focused Input.
        if let Some(Stop::Control(id)) = self.focused()
            && let Some(Entry::Input { .. }) = self.entry(&id)
            && let Some(&(_, top, _, x, _)) =
                boxes.iter().find(|b| b.0 == Stop::Control(id.clone()))
        {
            let row = usize::from(top) + 1;
            if row >= first && row < first + usize::from(editor_h) {
                let body = usize::from(width.saturating_sub(6));
                let start = self.cursor.saturating_sub(body.saturating_sub(1));
                let caret = cx + x + 3 + (self.cursor - start) as u16;
                if let Some(cell) = buf.cell_mut((caret, editor_top + (row - first) as u16)) {
                    cell.set_style(Style::default().add_modifier(Modifier::REVERSED));
                }
            }
        }
        // An open Select's options, under it.
        if let Some((id, highlighted)) = self.dropdown.clone()
            && let Some(Entry::Select { options, .. }) = self.entry(&id).cloned()
            && let Some(&(_, top, _, x, w)) =
                boxes.iter().find(|b| b.0 == Stop::Control(id.clone()))
        {
            let y0 = editor_top + (usize::from(top) + 3).saturating_sub(first) as u16;
            let max = usize::from(area.bottom().saturating_sub(y0 + 1)).min(12);
            for (i, option) in options.iter().enumerate().take(max) {
                let y = y0 + i as u16;
                let style = if i == highlighted {
                    Style::default().fg(p.background).bg(p.accent)
                } else {
                    Style::default().fg(p.text).bg(p.panel)
                };
                draw_option(buf, cx + x, y, w, option, style);
                self.option_hits.push((Rect::new(cx + x, y, w, 1), i));
            }
        }
        if !self.notice.is_empty() {
            let y = rect.bottom().saturating_sub(1);
            let msg = clip(
                &format!(" {} ", self.notice),
                usize::from(bw.saturating_sub(4)),
            );
            buf.set_string(
                rect.x + 2,
                y,
                msg,
                Style::default().fg(p.error).bg(p.surface),
            );
        }
    }
}

fn draw_option(buf: &mut Buffer, x: u16, y: u16, w: u16, text: &str, style: Style) {
    buf.set_string(x, y, pad(&format!(" {text}"), usize::from(w)), style);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Value {
        json!({"version": 1, "kind": "hooks_editor", "testing": false, "disabled": false, "items": [
            {"kind": "text", "id": "hook-status", "text": "Global: g\nProject: p\nEffective: none", "disabled": false},
            {"kind": "label", "id": null, "text": "Edit scope", "disabled": false},
            {"kind": "select", "id": "hook-scope", "prompt": "Select", "options": ["Project", "Global"], "value": "0", "disabled": false},
            {"kind": "select", "id": "hook-select", "prompt": "Choose a hook", "options": [], "value": "", "disabled": false},
            {"kind": "line", "items": [
                {"kind": "button", "id": "hook-new", "label": "New", "variant": "default", "disabled": false}
            ]},
            {"kind": "switch", "id": "hook-enabled", "value": true, "disabled": false},
            {"kind": "input", "id": "hook-field-id", "value": "check", "placeholder": "", "disabled": false}
        ]})
    }

    #[test]
    fn parse_reads_the_editor_and_refuses_a_bad_value() {
        let s = Snapshot::parse(&snapshot()).unwrap();
        assert_eq!(s.entries.len(), 7);
        let mut bad = snapshot();
        bad["items"][2]["value"] = json!("x");
        assert!(Snapshot::parse(&bad).is_err());
    }

    #[test]
    fn edits_ride_with_the_next_press_and_a_pick_is_sent_at_once() {
        let mut v = View::default();
        v.open();
        assert_eq!(v.take_intent(), Some(Intent::Open));
        v.observe(Update::Snapshot(Box::new(
            Snapshot::parse(&snapshot()).unwrap(),
        )));
        // Focus chain: scope, hook, New, the switch, the ID input, Close, Dock.
        assert_eq!(v.stops().len(), 7);
        for _ in 0..4 {
            v.key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        }
        v.key(KeyEvent::new(KeyCode::Char('2'), KeyModifiers::NONE));
        v.key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
        v.key(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
        v.key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
        v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        match v.take_intent() {
            Some(Intent::Act { values, action }) => {
                assert_eq!(values["hook-field-id"], json!("check2"));
                assert_eq!(values["hook-enabled"], json!(false));
                assert_eq!(action, Some(json!({"press": "hook-new"})));
            }
            other => panic!("{other:?}"),
        }
        v.observe(Update::Snapshot(Box::new(
            Snapshot::parse(&snapshot()).unwrap(),
        )));
        v.focus = 0;
        v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        v.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(matches!(v.take_intent(),
            Some(Intent::Act { action: Some(a), .. }) if a == json!({"select": "hook-scope", "index": 1})));
        v.observe(Update::Snapshot(Box::new(
            Snapshot::parse(&snapshot()).unwrap(),
        )));
        v.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(v.take_intent(), Some(Intent::Close));
        v.observe(Update::Closed);
        assert!(!v.owns_input());
    }
}
