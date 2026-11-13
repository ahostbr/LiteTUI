//! The command palette: Textual's own CommandPalette (and its theme search), drawn natively.
//!
//! Python's header icon opens Textual's CommandPalette over LiteTUI's commands; its Theme row
//! opens the same palette over the themes (App.search_themes), where a pick sets the theme and
//! closes. The real palette runs headless in the source (native_palette_dialog.py), fitted to
//! this terminal, so the ranking, the highlight and the scroll are Python's; this view draws
//! its parts at Python's regions in the palette's own shapes (CommandPalette CSS: a
//! `$panel-darken-1` box under an hkey rule, the search icon and input, two-line rows with
//! `padding: 0 2`, the highlighted row in the blurred block cursor, an hkey rule below) over
//! the dimmed screen (`background: $background 60%`). A picked command row comes back as
//! `routed`: the frontend opens it through its own native route for that row.
use crate::theme::Theme;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::Rect,
    style::{Color, Style},
};
use serde_json::{Value, json};
use std::io;
use unicode_width::UnicodeWidthChar;

pub const OPERATIONS: [&str; 4] = [
    "gui.palette.native_dialog_open",
    "gui.palette.native_dialog_act",
    "gui.palette.native_dialog_read",
    "gui.palette.native_dialog_close",
];

pub type Lines = crate::calendar_dialog::Lines;

#[derive(Clone, Debug, PartialEq)]
pub struct Listing {
    pub region: Rect,
    pub content: Rect,
    pub scroll_y: u16,
    pub max_scroll_y: u16,
    pub bar: Option<Rect>,
    /// The scrollbar as Textual rendered it, one row of runs per bar row.
    pub bar_rows: Lines,
    /// The `border-bottom: hkey black` glyph style.
    pub rule: String,
    pub highlighted: Option<usize>,
    /// Every option as Textual rendered it: runs with Rich style specs, padding included.
    pub options: Vec<Lines>,
}

/// Textual's own colours for the active theme (Rich style specs).
#[derive(Clone, Debug, PartialEq)]
pub struct Paint {
    /// `background: $background 60%`: blended over the screen behind.
    pub backdrop: Color,
    pub alpha: f32,
    pub boxed: String,
    pub rule: String,
    pub text: String,
    pub placeholder: String,
    pub cursor: String,
    pub icon: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub themes: bool,
    pub boxed: Rect,
    pub icon: Rect,
    pub text: Rect,
    pub value: String,
    pub cursor: usize,
    pub placeholder: String,
    pub paint: Paint,
    pub list: Option<Listing>,
}

/// What an answer says: the palette as it now is, or that it closed (with the theme Python
/// now runs, and the row a pick routed, if any).
#[derive(Clone, Debug, PartialEq)]
pub enum Answer {
    Open(Box<Snapshot>),
    Closed {
        theme: String,
        routed: Option<String>,
    },
}

fn bad() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Malformed command palette")
}
fn text(v: &Value, key: &str) -> io::Result<String> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| s.len() <= 64 * 1024)
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn region(v: &Value) -> io::Result<Rect> {
    let n = v.as_array().filter(|a| a.len() == 4).ok_or_else(bad)?;
    let get = |i: usize| -> io::Result<u16> {
        n[i].as_i64()
            .filter(|x| (0..=10_000).contains(x))
            .map(|x| x as u16)
            .ok_or_else(bad)
    };
    Ok(Rect::new(get(0)?, get(1)?, get(2)?, get(3)?))
}
impl Answer {
    pub fn parse(v: &Value) -> io::Result<Self> {
        if v["version"] != 1 || v["kind"] != "palette_dialog" {
            return Err(bad());
        }
        let routed = v.get("routed").and_then(Value::as_str).map(str::to_owned);
        let screen = v["screen"].as_str().ok_or_else(bad)?;
        if screen == "closed" {
            return Ok(Answer::Closed {
                theme: text(v, "theme")?,
                routed,
            });
        }
        if screen != "commands" && screen != "themes" {
            return Err(bad());
        }
        let list = match &v["list"] {
            Value::Null => None,
            l => Some(Listing {
                region: region(&l["region"])?,
                content: region(&l["content"])?,
                scroll_y: l["scroll_y"].as_u64().ok_or_else(bad)? as u16,
                max_scroll_y: l["max_scroll_y"].as_u64().ok_or_else(bad)? as u16,
                bar: match &l["bar"] {
                    Value::Null => None,
                    b => Some(region(b)?),
                },
                bar_rows: crate::calendar_dialog::lines(&l["bar_rows"])?,
                rule: text(l, "rule")?,
                highlighted: l["highlighted"].as_u64().map(|h| h as usize),
                options: l["options"]
                    .as_array()
                    .filter(|o| o.len() <= 4096)
                    .ok_or_else(bad)?
                    .iter()
                    .map(crate::calendar_dialog::lines)
                    .collect::<io::Result<Vec<_>>>()?,
            }),
        };
        Ok(Answer::Open(Box::new(Snapshot {
            themes: screen == "themes",
            boxed: region(&v["box"])?,
            icon: region(&v["icon"])?,
            text: region(&v["text"])?,
            value: text(v, "value")?,
            cursor: v["cursor"].as_u64().ok_or_else(bad)? as usize,
            placeholder: text(v, "placeholder")?,
            paint: {
                let p = &v["paint"];
                Paint {
                    backdrop: crate::calendar_dialog::rich_style(&text(p, "backdrop")?, Style::default())
                        .fg
                        .ok_or_else(bad)?,
                    alpha: p["backdrop_alpha"]
                        .as_f64()
                        .filter(|a| (0.0..=1.0).contains(a))
                        .ok_or_else(bad)? as f32,
                    boxed: text(p, "box")?,
                    rule: text(p, "rule")?,
                    text: text(p, "text")?,
                    placeholder: text(p, "placeholder")?,
                    cursor: text(p, "cursor")?,
                    icon: text(p, "icon")?,
                }
            },
            list,
        })))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Open {
        size: [u16; 2],
        themes: bool,
    },
    Act {
        size: [u16; 2],
        query: Option<String>,
        action: Value,
    },
    Close,
}
impl Intent {
    pub fn command(&self) -> Value {
        let [open, act, _read, close] = OPERATIONS;
        match self {
            Intent::Open { size, themes } => json!({
                "type": open, "version": 1, "size": size,
                "mode": if *themes { "themes" } else { "commands" }
            }),
            Intent::Act {
                size,
                query,
                action,
            } => {
                let values = match query {
                    Some(q) => json!({"query": q}),
                    None => json!({}),
                };
                json!({"type": act, "version": 1, "size": size, "values": values, "action": action})
            }
            Intent::Close => json!({"type": close, "version": 1}),
        }
    }
    pub fn closing(&self) -> bool {
        matches!(self, Intent::Close)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Answer(Answer),
    Closed,
    Rejected(String),
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    snapshot: Option<Snapshot>,
    /// The typed search, sent with the next act.
    query: Option<String>,
    /// Keys pressed while an answer was in flight, sent in order after it (typing ahead of
    /// Python's answer is the palette's normal use: "Theme" then Enter).
    queued: Vec<Value>,
    intent: Option<Intent>,
    size: [u16; 2],
    hits: Vec<(Rect, usize)>,
    /// A pick Python routed back: the palette row's text, for the native route.
    routed: Option<String>,
    /// The theme Python runs after the palette closed, when it changed.
    theme: Option<String>,
    pub notice: String,
}

impl View {
    pub fn owns_input(&self) -> bool {
        self.open
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn take_routed(&mut self) -> Option<String> {
        self.routed.take()
    }
    pub fn take_theme(&mut self) -> Option<String> {
        self.theme.take()
    }
    pub fn set_size(&mut self, area: Rect) {
        self.size = [area.width, area.height];
    }
    fn size(&self) -> [u16; 2] {
        if self.size[0] >= 10 && self.size[1] >= 10 {
            self.size
        } else {
            crossterm::terminal::size()
                .map(|(w, h)| [w.max(10), h.max(10)])
                .unwrap_or([80, 24])
        }
    }
    /// The header icon (commands), or the Theme route (the theme search).
    pub fn open(&mut self, themes: bool) {
        if self.waiting {
            return;
        }
        let size = self.size();
        *self = Self {
            open: true,
            waiting: true,
            size,
            intent: Some(Intent::Open { size, themes }),
            ..Self::default()
        };
    }
    fn act(&mut self, action: Value) {
        if self.waiting {
            // The typed search is already kept in `query`; a key waits its turn.
            if !action.is_null() && self.queued.len() < 64 {
                self.queued.push(action);
            }
            return;
        }
        self.waiting = true;
        self.intent = Some(Intent::Act {
            size: self.size(),
            query: self.query.clone(),
            action,
        });
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        match update {
            Update::Answer(Answer::Open(snapshot)) => {
                if self.query.as_deref() == Some(snapshot.value.as_str()) {
                    self.query = None;
                }
                self.notice.clear();
                self.snapshot = Some(*snapshot);
                // What was typed or pressed meanwhile goes now, in order.
                if !self.queued.is_empty() {
                    let next = self.queued.remove(0);
                    self.act(next);
                } else if self.query.is_some() {
                    self.act(Value::Null);
                }
            }
            Update::Answer(Answer::Closed { theme, routed }) => {
                *self = Self {
                    size: self.size,
                    routed,
                    theme: Some(theme),
                    ..Self::default()
                };
            }
            Update::Closed => {
                *self = Self {
                    size: self.size,
                    ..Self::default()
                };
            }
            Update::Rejected(reason) => {
                self.queued.clear();
                self.query = None;
                if self.snapshot.is_none() {
                    *self = Self {
                        size: self.size,
                        notice: reason,
                        ..Self::default()
                    };
                } else {
                    self.notice = reason;
                }
            }
        }
    }
    fn typed(&self) -> String {
        match (&self.query, &self.snapshot) {
            (Some(q), _) => q.clone(),
            (None, Some(s)) => s.value.clone(),
            _ => String::new(),
        }
    }
    pub fn paste(&mut self, pasted: &str) {
        if !self.open || self.snapshot.is_none() {
            return;
        }
        let mut q = self.typed();
        q.push_str(pasted.lines().next().unwrap_or(""));
        self.query = Some(q);
        self.act(Value::Null);
    }
    pub fn key(&mut self, key: KeyEvent) {
        // Before the first answer there is no palette to type into yet.
        if key.kind == KeyEventKind::Release || !self.open || self.snapshot.is_none() {
            return;
        }
        let name = match key.code {
            KeyCode::Esc => Some("escape"),
            KeyCode::Down => Some("down"),
            KeyCode::Up => Some("up"),
            KeyCode::PageDown => Some("pagedown"),
            KeyCode::PageUp => Some("pageup"),
            KeyCode::Enter => Some("enter"),
            _ => None,
        };
        if let Some(name) = name {
            self.act(json!({"key": name}));
            return;
        }
        match key.code {
            KeyCode::Char(c)
                if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
            {
                let mut q = self.typed();
                q.push(c);
                self.query = Some(q);
                self.act(Value::Null);
            }
            KeyCode::Backspace => {
                let mut q = self.typed();
                if q.pop().is_some() {
                    self.query = Some(q);
                    self.act(Value::Null);
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        let at = (event.column, event.row).into();
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                if let Some(&(_, index)) = self.hits.iter().find(|(r, _)| r.contains(at)) {
                    self.act(json!({"option": index}));
                    return;
                }
                let inside = self.snapshot.as_ref().is_some_and(|s| {
                    s.boxed.contains(at) || s.list.as_ref().is_some_and(|l| l.region.contains(at))
                });
                if !inside {
                    // Textual dismisses its palette on a click outside it.
                    self.act(json!({"key": "escape"}));
                }
            }
            MouseEventKind::ScrollDown => self.act(json!({"key": "down"})),
            MouseEventKind::ScrollUp => self.act(json!({"key": "up"})),
            _ => {}
        }
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.set_size(area);
        self.hits.clear();
        if !self.open {
            return;
        }
        let buf = frame.buffer_mut();
        let Some(s) = self.snapshot.clone() else {
            // Textual pushes the palette screen at once; its box shows when ready.
            backdrop(buf, area, theme.background, 0.6, theme);
            return;
        };
        let p = &s.paint;
        backdrop(buf, area, p.backdrop, p.alpha, theme);
        let rs = |spec: &str| crate::theme::textual_style(spec, Style::default());
        let boxed = rs(&p.boxed);
        fill_rect(buf, s.boxed, boxed);
        // #--input: `border: hkey black 50%`; the bottom edge goes when the list shows.
        line(buf, s.boxed.x, s.boxed.y, s.boxed.width, "▔", rs(&p.rule));
        if s.list.is_none() && s.boxed.height > 1 {
            line(buf, s.boxed.x, s.boxed.bottom() - 1, s.boxed.width, "▁", rs(&p.rule));
        }
        put(buf, s.icon.x, s.icon.y, "🔎", s.icon.right(), rs(&p.icon));
        let value = self.typed();
        let end = s.text.right();
        if value.is_empty() {
            // Textual's Input: the cursor sits on the placeholder's first cell.
            let mut chars = s.placeholder.chars();
            let first: String = chars.next().map(String::from).unwrap_or_else(|| " ".into());
            let x = put(buf, s.text.x, s.text.y, &first, end, rs(&p.cursor));
            put(buf, x, s.text.y, chars.as_str(), end, rs(&p.placeholder));
        } else {
            let at = if self.query.is_some() { value.chars().count() } else { s.cursor };
            let before: String = value.chars().take(at).collect();
            let rest: String = value.chars().skip(at).collect();
            let x = put(buf, s.text.x, s.text.y, &before, end, rs(&p.text));
            let mut tail = rest.chars();
            let under: String = tail.next().map(String::from).unwrap_or_else(|| " ".into());
            let x = put(buf, x, s.text.y, &under, end, rs(&p.cursor));
            put(buf, x, s.text.y, tail.as_str(), end, rs(&p.text));
        }
        if let Some(l) = &s.list {
            fill_rect(buf, l.region, boxed);
            // CommandList `border-bottom: hkey black`.
            line(
                buf,
                l.region.x,
                l.region.bottom().saturating_sub(1),
                l.region.width,
                "▁",
                rs(&l.rule),
            );
            let mut y = i32::from(l.content.y) - i32::from(l.scroll_y);
            for (index, option) in l.options.iter().enumerate() {
                let top = y;
                for runs in option {
                    if y >= i32::from(l.content.y) && y < i32::from(l.content.bottom()) {
                        let mut x = l.content.x;
                        for (text, style) in runs {
                            x = put(buf, x, y as u16, text, l.content.right(), rs(style));
                        }
                    }
                    y += 1;
                }
                let low = top.clamp(i32::from(l.content.y), i32::from(l.content.bottom())) as u16;
                let high = y.clamp(i32::from(l.content.y), i32::from(l.content.bottom())) as u16;
                if high > low {
                    self.hits
                        .push((Rect::new(l.content.x, low, l.content.width, high - low), index));
                }
            }
            if let Some(bar) = l.bar {
                for (row, runs) in l.bar_rows.iter().enumerate().take(usize::from(bar.height)) {
                    let mut x = bar.x;
                    for (text, style) in runs {
                        x = put(buf, x, bar.y + row as u16, text, bar.right(), rs(style));
                    }
                }
            }
        }
        if !self.notice.is_empty() {
            put(
                buf,
                s.boxed.x + 2,
                s.boxed.bottom().saturating_sub(1),
                &self.notice,
                s.boxed.right().saturating_sub(2),
                Style::default().fg(theme.error).bg(boxed.bg.unwrap_or(theme.panel)),
            );
        }
    }
}

/// Textual's `background: <colour> <alpha>` over what is already drawn: every cell's
/// foreground and background blended toward it (the screen behind the palette).
fn backdrop(buf: &mut Buffer, area: Rect, colour: Color, alpha: f32, theme: &Theme) {
    let area = area.intersection(buf.area);
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = &mut buf[(x, y)];
            let fg = match cell.fg {
                Color::Reset => theme.foreground,
                c => c,
            };
            let bg = match cell.bg {
                Color::Reset => theme.background,
                c => c,
            };
            cell.fg = crate::theme::blend(fg, colour, alpha);
            cell.bg = crate::theme::blend(bg, colour, alpha);
        }
    }
}

/// `text` from (x, y) in `style` alone (the cell's earlier attributes cleared), never past
/// `end`. Returns the next x.
fn put(buf: &mut Buffer, x: u16, y: u16, text: &str, end: u16, style: Style) -> u16 {
    if y >= buf.area.bottom() {
        return x;
    }
    let end = end.min(buf.area.right());
    let mut x = x;
    for ch in text.chars() {
        let w = ch.width().unwrap_or(0) as u16;
        if w == 0 {
            continue;
        }
        if x + w > end {
            break;
        }
        let cell = &mut buf[(x, y)];
        cell.reset();
        cell.set_symbol(&ch.to_string()).set_style(style);
        for dx in 1..w {
            let next = &mut buf[(x + dx, y)];
            next.reset();
            next.set_symbol("").set_style(style);
        }
        x += w;
    }
    x
}

fn fill_rect(buf: &mut Buffer, r: Rect, style: Style) {
    let r = r.intersection(buf.area);
    for y in r.top()..r.bottom() {
        for x in r.left()..r.right() {
            let cell = &mut buf[(x, y)];
            cell.reset();
            cell.set_symbol(" ").set_style(style);
        }
    }
}
fn line(buf: &mut Buffer, x: u16, y: u16, w: u16, glyph: &str, style: Style) {
    put(buf, x, y, &glyph.repeat(usize::from(w)), x.saturating_add(w), style);
}
#[cfg(test)]
mod tests {
    use super::*;

    static PAINT: std::sync::LazyLock<Value> = std::sync::LazyLock::new(|| {
        json!({"backdrop": "#121212", "backdrop_alpha": 0.6, "box": "#e0e0e0 on #141f27",
            "rule": "#0a0f13 on #141f27", "text": "#e0e0e0 on #141f27",
            "placeholder": "#6d7479 on #141f27", "cursor": "reverse #e0e0e0 on #141f27",
            "icon": "#000000 on #141f27"})
    });

    #[test]
    fn a_closed_answer_names_the_routed_row_and_the_theme() {
        let v = json!({"version": 1, "kind": "palette_dialog", "screen": "closed",
                       "theme": "nord", "routed": "Settings"});
        assert_eq!(
            Answer::parse(&v).unwrap(),
            Answer::Closed {
                theme: "nord".into(),
                routed: Some("Settings".into())
            }
        );
        let mut view = View {
            size: [140, 45],
            ..View::default()
        };
        view.open(false);
        assert!(matches!(
            view.take_intent(),
            Some(Intent::Open { themes: false, .. })
        ));
        view.observe(Update::Answer(Answer::parse(&v).unwrap()));
        assert!(!view.owns_input());
        assert_eq!(view.take_routed().as_deref(), Some("Settings"));
        assert_eq!(view.take_theme().as_deref(), Some("nord"));
    }

    #[test]
    fn typing_sends_the_whole_search_and_keys_are_the_palettes() {
        let open = json!({"version": 1, "kind": "palette_dialog", "screen": "commands",
            "size": [140, 45], "theme": "x", "box": [0, 2, 140, 2], "icon": [2, 3, 2, 1],
            "field": [4, 2, 135, 3], "text": [5, 3, 133, 1], "value": "", "placeholder": "Search for commands…", "cursor": 0, "paint": PAINT.clone(),
            "list": null});
        let mut view = View {
            size: [140, 45],
            ..View::default()
        };
        view.open(false);
        view.take_intent();
        view.observe(Update::Answer(Answer::parse(&open).unwrap()));
        view.key(KeyEvent::new(KeyCode::Char('T'), KeyModifiers::SHIFT));
        assert!(matches!(view.take_intent(), Some(Intent::Act { query: Some(q), .. }) if q == "T"));
        let mut typed = open.clone();
        typed["value"] = json!("T");
        view.observe(Update::Answer(Answer::parse(&typed).unwrap()));
        view.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert!(
            matches!(view.take_intent(), Some(Intent::Act { action, .. }) if action == json!({"key": "down"}))
        );
    }

    #[test]
    fn keys_typed_ahead_of_pythons_answer_are_kept_and_sent_in_order() {
        let snap = |value: &str| {
            json!({"version": 1, "kind": "palette_dialog", "screen": "commands",
                "size": [140, 45], "theme": "x", "box": [0, 2, 140, 2], "icon": [2, 3, 2, 1],
                "field": [4, 2, 135, 3], "text": [5, 3, 133, 1], "value": value,
                "placeholder": "Search for commands…", "cursor": 0, "paint": PAINT.clone(), "list": null})
        };
        let mut view = View {
            size: [140, 45],
            ..View::default()
        };
        view.open(false);
        view.take_intent();
        view.observe(Update::Answer(Answer::parse(&snap("")).unwrap()));
        for c in "Theme".chars() {
            view.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        // Only the first key went; the rest waited for Python.
        assert!(matches!(view.take_intent(), Some(Intent::Act { query: Some(q), .. }) if q == "T"));
        assert_eq!(view.typed(), "Theme");
        view.observe(Update::Answer(Answer::parse(&snap("T")).unwrap()));
        assert!(matches!(view.take_intent(),
            Some(Intent::Act { query: Some(q), action, .. }) if q == "Theme" && action == json!({"key": "enter"})));
        view.observe(Update::Answer(Answer::parse(&snap("Theme")).unwrap()));
        assert_eq!(view.take_intent(), None);
    }
}
