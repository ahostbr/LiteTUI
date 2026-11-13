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
    style::{Color, Modifier, Style},
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
    pub highlighted: Option<usize>,
    pub options: Vec<Lines>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub themes: bool,
    pub boxed: Rect,
    pub icon: Rect,
    pub text: Rect,
    pub value: String,
    pub placeholder: String,
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
            placeholder: text(v, "placeholder")?,
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
                self.query = None;
                self.notice.clear();
                self.snapshot = Some(*snapshot);
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
        if !self.open || self.waiting {
            return;
        }
        let mut q = self.typed();
        q.push_str(pasted.lines().next().unwrap_or(""));
        self.query = Some(q);
        self.act(Value::Null);
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !self.open || self.waiting {
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
        // `background: $background 60%` over the screen behind: dim what is there.
        for y in area.top()..area.bottom() {
            for x in area.left()..area.right() {
                let cell = &mut buf[(x, y)];
                let style = cell.style().add_modifier(Modifier::DIM);
                cell.set_style(style);
            }
        }
        let Some(s) = self.snapshot.clone() else {
            return;
        };
        let panel = blend(theme.panel, theme.background, 0.25);
        let fill = Style::default().fg(theme.foreground).bg(panel);
        let rule = Style::default().fg(theme.thinking_border).bg(panel);
        let muted = Style::default().fg(theme.thinking_text).bg(panel);
        fill_rect(buf, s.boxed, fill);
        // #--input: border hkey; the bottom rule goes when the list shows (--list-visible).
        line(buf, s.boxed.x, s.boxed.y, s.boxed.width, "▔", rule);
        if s.list.is_none() && s.boxed.height > 1 {
            line(
                buf,
                s.boxed.x,
                s.boxed.bottom() - 1,
                s.boxed.width,
                "▁",
                rule,
            );
        }
        buf.set_stringn(s.icon.x, s.icon.y, "🔎", 2, fill);
        let value = self.typed();
        let (shown, style) = if value.is_empty() {
            (s.placeholder.as_str(), muted)
        } else {
            (value.as_str(), fill)
        };
        buf.set_stringn(s.text.x, s.text.y, shown, usize::from(s.text.width), style);
        if let Some(l) = &s.list {
            fill_rect(buf, l.region, fill);
            // border-bottom: hkey
            line(
                buf,
                l.region.x,
                l.region.bottom().saturating_sub(1),
                l.region.width,
                "▁",
                rule,
            );
            let cursor = blend(theme.primary, panel, 0.3);
            let mut y = i32::from(l.content.y) - i32::from(l.scroll_y);
            for (index, option) in l.options.iter().enumerate() {
                let lit = l.highlighted == Some(index);
                let base = if lit {
                    Style::default().fg(theme.foreground).bg(cursor)
                } else {
                    fill
                };
                let top = y;
                for (row, runs) in option.iter().enumerate() {
                    let yy = y + row as i32;
                    if yy >= i32::from(l.content.y) && yy < i32::from(l.content.bottom()) {
                        let yy = yy as u16;
                        for x in l.content.left()..l.content.right() {
                            buf[(x, yy)].set_symbol(" ").set_style(base);
                        }
                        // option: padding 0 2, text-style bold (the help line is not bold).
                        let mut x = l.content.x + 2;
                        let end = l.content.right().saturating_sub(2);
                        for (text, role) in runs {
                            let mut st = base;
                            if row == 0 && !role.contains("muted") {
                                st = st.add_modifier(Modifier::BOLD);
                            }
                            for w in role.split_whitespace() {
                                st = match w {
                                    "bold" => st.add_modifier(Modifier::BOLD),
                                    "dim" => st.add_modifier(Modifier::DIM),
                                    "underline" => st.add_modifier(Modifier::UNDERLINED),
                                    "muted" => {
                                        st.fg(theme.thinking_text).remove_modifier(Modifier::BOLD)
                                    }
                                    _ => st,
                                };
                            }
                            for ch in text.chars() {
                                let w = ch.width().unwrap_or(0) as u16;
                                if w == 0 || x + w > end {
                                    continue;
                                }
                                buf[(x, yy)].set_symbol(&ch.to_string()).set_style(st);
                                x += w;
                            }
                        }
                    }
                }
                let rows = option.len().max(1) as i32;
                let visible = Rect::new(
                    l.content.x,
                    top.clamp(i32::from(l.content.y), i32::from(l.content.bottom())) as u16,
                    l.content.width,
                    0,
                );
                let bottom = (top + rows)
                    .clamp(i32::from(l.content.y), i32::from(l.content.bottom()))
                    as u16;
                if bottom > visible.y {
                    self.hits.push((
                        Rect {
                            height: bottom - visible.y,
                            ..visible
                        },
                        index,
                    ));
                }
                y += rows;
            }
            if l.max_scroll_y > 0
                && let Some(bar) = l.bar
            {
                draw_bar(buf, bar, l.scroll_y, l.max_scroll_y, theme);
            }
        }
        if !self.notice.is_empty() {
            buf.set_stringn(
                s.boxed.x + 2,
                s.boxed.bottom().saturating_sub(1),
                &self.notice,
                usize::from(s.boxed.width.saturating_sub(4)),
                Style::default().fg(theme.error).bg(panel),
            );
        }
    }
}

fn fill_rect(buf: &mut Buffer, r: Rect, style: Style) {
    let r = r.intersection(buf.area);
    for y in r.top()..r.bottom() {
        for x in r.left()..r.right() {
            buf[(x, y)].set_symbol(" ").set_style(style);
        }
    }
}
fn line(buf: &mut Buffer, x: u16, y: u16, w: u16, glyph: &str, style: Style) {
    if y >= buf.area.bottom() {
        return;
    }
    for dx in 0..w {
        if x + dx < buf.area.right() {
            buf[(x + dx, y)].set_symbol(glyph).set_style(style);
        }
    }
}
/// `a` over `b` at `t` (0..1), for RGB theme colours; anything else keeps `a`.
fn blend(a: Color, b: Color, t: f32) -> Color {
    match (a, b) {
        (Color::Rgb(ar, ag, ab), Color::Rgb(br, bg, bb)) => {
            let mix = |x: u8, y: u8| (f32::from(x) * t + f32::from(y) * (1.0 - t)).round() as u8;
            Color::Rgb(mix(ar, br), mix(ag, bg), mix(ab, bb))
        }
        _ => a,
    }
}
fn draw_bar(buf: &mut Buffer, at: Rect, scroll_y: u16, max: u16, theme: &Theme) {
    // The same eighth-block thumb as calendar_dialog's (Textual's render_bar).
    let size = f64::from(at.height);
    let virtual_size = size + f64::from(max);
    if size <= 0.0 {
        return;
    }
    let thumb = (size / (virtual_size / size)).max(1.0);
    let position = (size - thumb) * (f64::from(scroll_y) / (virtual_size - size));
    let (start, end) = (position as u16, (position + thumb).ceil() as u16);
    for row in 0..at.height {
        let on = row >= start && row < end;
        let style = Style::default().bg(if on { theme.primary } else { theme.panel });
        for dx in 0..at.width.max(1) {
            if at.x + dx < buf.area.right() && at.y + row < buf.area.bottom() {
                buf[(at.x + dx, at.y + row)]
                    .set_symbol(" ")
                    .set_style(style);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
            "field": [4, 2, 135, 3], "text": [5, 3, 133, 1], "value": "", "placeholder": "Search for commands…",
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
        view.observe(Update::Answer(Answer::parse(&open).unwrap()));
        view.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert!(
            matches!(view.take_intent(), Some(Intent::Act { action, .. }) if action == json!({"key": "down"}))
        );
    }
}
