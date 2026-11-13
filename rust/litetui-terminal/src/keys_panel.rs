//! `/keys`: Python's key panel (Textual's KeyPanel), docked on the right.
//!
//! Python's /keys toggles Textual's KeyPanel: `split: right`, 33% of the screen width
//! (min 30, max 60), full height, a `vkey` left border in $foreground 30%, padding 0 1
//! and a 2-column scrollbar when the table overflows. The app keeps working beside it in
//! the narrower width. Its BindingsTable is a Rich table (box SIMPLE, no header, padding
//! 0): a blank top and bottom edge, a blank line after each section, the key column
//! right-justified, the description word-wrapped and the table centred in the panel.
//! The rows are Python's own (native_keys.rows, sent with the command catalog).
use crate::{picker_box::wrap, theme::Theme};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
};
use serde_json::Value;
use std::io;
use unicode_width::UnicodeWidthStr;

#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub key: String,
    pub text: String,
    pub tooltip: String,
    pub title: bool,
    pub end_section: bool,
}

/// The catalog's `key_table`: None when the source sends none.
pub fn parse(v: &Value) -> io::Result<Option<Vec<Row>>> {
    let fail = || io::Error::new(io::ErrorKind::InvalidData, "Invalid key table");
    let rows = match v.get("key_table") {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Array(rows)) if rows.len() <= 512 => rows,
        Some(_) => return Err(fail()),
    };
    let text = |row: &Value, name: &str| -> io::Result<String> {
        match row.get(name) {
            Some(Value::String(s)) if s.len() <= 1024 => Ok(s.clone()),
            _ => Err(fail()),
        }
    };
    let flag = |row: &Value, name: &str| row.get(name).and_then(Value::as_bool).ok_or_else(fail);
    rows.iter()
        .map(|row| {
            Ok(Row {
                key: text(row, "key")?,
                text: text(row, "text")?,
                tooltip: text(row, "tooltip")?,
                title: flag(row, "title")?,
                end_section: flag(row, "end_section")?,
            })
        })
        .collect::<io::Result<Vec<_>>>()
        .map(Some)
}

/// What a row's description cell holds on one line: (text, style kind).
#[derive(Clone, Copy, Debug, PartialEq)]
enum Kind {
    Text,
    Tooltip,
    Title,
}

#[derive(Debug, Default, PartialEq)]
struct Line {
    key: String,
    cells: Vec<(String, Kind)>,
}

/// Greedy word wrap over styled words, folding a word longer than the width (Rich's
/// Text.wrap with overflow fold).
fn wrap_cells(words: &[(String, Kind)], width: usize) -> Vec<Vec<(String, Kind)>> {
    let width = width.max(1);
    let mut lines: Vec<Vec<(String, Kind)>> = Vec::new();
    let mut line: Vec<(String, Kind)> = Vec::new();
    let mut used = 0usize;
    for (word, kind) in words {
        let w = word.width();
        let gap = usize::from(!line.is_empty());
        if used + gap + w <= width {
            if gap == 1 {
                line.push((" ".into(), *kind));
            }
            line.push((word.clone(), *kind));
            used += gap + w;
            continue;
        }
        if !line.is_empty() {
            lines.push(std::mem::take(&mut line));
        }
        let mut pieces = wrap(word, width);
        let last = pieces.pop().unwrap_or_default();
        for piece in pieces {
            lines.push(vec![(piece, *kind)]);
        }
        used = last.width();
        line.push((last, *kind));
    }
    if !line.is_empty() || lines.is_empty() {
        lines.push(line);
    }
    lines
}

fn words(row: &Row) -> Vec<(String, Kind)> {
    let mut out = Vec::new();
    let main = if row.title { Kind::Title } else { Kind::Text };
    out.extend(
        row.text
            .split(' ')
            .filter(|w| !w.is_empty())
            .map(|w| (w.to_owned(), main)),
    );
    out.extend(
        row.tooltip
            .split(' ')
            .filter(|w| !w.is_empty())
            .map(|w| (w.to_owned(), Kind::Tooltip)),
    );
    out
}

/// Rich's column widths for a two-column table that may be no wider than `avail`:
/// each column at its widest cell; over the limit, the widest column gives way first.
fn widths(rows: &[Row], avail: usize) -> (usize, usize) {
    let kw = rows.iter().map(|r| r.key.width()).max().unwrap_or(0);
    let dw = rows
        .iter()
        .map(|r| {
            let words = words(r);
            words.iter().map(|(w, _)| w.width()).sum::<usize>() + words.len().saturating_sub(1)
        })
        .max()
        .unwrap_or(0);
    let room = avail.saturating_sub(3);
    if kw + dw <= room {
        return (kw, dw);
    }
    if kw <= room.saturating_sub(kw) {
        return (kw, room - kw);
    }
    let half = room / 2;
    (half, room - half)
}

/// The table's lines, from the blank top edge to the blank bottom edge.
fn lines(rows: &[Row], kw: usize, dw: usize) -> Vec<Line> {
    let mut out = vec![Line::default()];
    for (index, row) in rows.iter().enumerate() {
        let keys = wrap(&row.key, kw.max(1));
        let cells = wrap_cells(&words(row), dw);
        let height = keys.len().max(cells.len());
        for i in 0..height {
            out.push(Line {
                key: keys.get(i).cloned().unwrap_or_default(),
                cells: cells.get(i).cloned().unwrap_or_default(),
            });
        }
        if row.end_section && index + 1 < rows.len() {
            out.push(Line::default());
        }
    }
    out.push(Line::default());
    out
}

/// Textual's percentage width: 33% of the screen, clamped to 30..=60 and the screen.
pub fn width(screen: u16) -> u16 {
    (u32::from(screen) * 33 / 100)
        .clamp(30, 60)
        .min(u32::from(screen)) as u16
}

fn blend(over: Color, under: Color, alpha: f32) -> Color {
    match (over, under) {
        (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) => {
            let mix =
                |a: u8, b: u8| (f32::from(b) + (f32::from(a) - f32::from(b)) * alpha).round() as u8;
            Color::Rgb(mix(r1, r2), mix(g1, g2), mix(b1, b2))
        }
        _ => over,
    }
}

#[derive(Default)]
pub struct Panel {
    pub open: bool,
    scroll: usize,
    max_scroll: usize,
    hit: Option<Rect>,
}

impl Panel {
    /// /keys toggles the panel, as Python's _cmd_keys does.
    pub fn toggle(&mut self) {
        self.open = !self.open;
        self.scroll = 0;
        self.hit = None;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.hit = None;
    }
    /// The app's area beside the open panel.
    pub fn app_area(&self, full: Rect) -> Rect {
        if !self.open {
            return full;
        }
        Rect {
            width: full.width - width(full.width),
            ..full
        }
    }
    /// The wheel over the panel scrolls it (2 rows a notch, Textual's scroll_sensitivity_y).
    /// True when the event was over the panel.
    pub fn mouse(&mut self, event: crossterm::event::MouseEvent) -> bool {
        use crossterm::event::MouseEventKind;
        let Some(hit) = self.hit.filter(|_| self.open) else {
            return false;
        };
        if !hit.contains((event.column, event.row).into()) {
            return false;
        }
        match event.kind {
            MouseEventKind::ScrollDown => self.scroll = (self.scroll + 2).min(self.max_scroll),
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(2),
            _ => {}
        }
        true
    }
    pub fn draw(&mut self, buf: &mut Buffer, full: Rect, rows: &[Row], t: &Theme) {
        if !self.open || full.width == 0 || full.height == 0 {
            self.hit = None;
            return;
        }
        let pw = width(full.width);
        let rect = Rect::new(full.right() - pw, full.y, pw, full.height);
        self.hit = Some(rect);
        // KeyPanel has no background of its own: the screen's $surface-darken-1 shows.
        let screen = t.screen();
        let base = Style::default().fg(t.text()).bg(screen);
        buf.set_style(rect, base);
        for y in rect.top()..rect.bottom() {
            buf.set_string(rect.x, y, " ".repeat(usize::from(pw)), base);
        }
        // border-left: vkey $foreground 30%.
        let edge = Style::default()
            .fg(t.css_alpha("foreground", 30.0, screen, blend(t.foreground, t.background, 0.3)))
            .bg(screen);
        for y in rect.top()..rect.bottom() {
            buf.set_string(rect.x, y, "▏", edge);
        }
        // Border 1, padding 1 each side; the scrollbar (2) only when the table overflows.
        let height = usize::from(rect.height);
        let inner = usize::from(pw.saturating_sub(3));
        let mut avail = inner;
        let (mut kw, mut dw) = widths(rows, avail);
        let mut table = lines(rows, kw, dw);
        let scrolling = table.len() > height;
        if scrolling {
            avail = inner.saturating_sub(2);
            (kw, dw) = widths(rows, avail);
            table = lines(rows, kw, dw);
        }
        self.max_scroll = table.len().saturating_sub(height);
        self.scroll = self.scroll.min(self.max_scroll);
        let table_w = (kw + dw + 3).min(avail);
        let x0 = rect.x + 2 + ((avail - table_w) / 2) as u16;
        // .bindings-table--key: bold $text-accent.
        let key_style = Style::default()
            .fg(t.css_over("text-accent", screen, t.primary))
            .bg(screen)
            .add_modifier(Modifier::BOLD);
        for (dy, line) in table.iter().skip(self.scroll).take(height).enumerate() {
            let y = rect.y + dy as u16;
            let kx = x0 + 1 + (kw.saturating_sub(line.key.width())) as u16;
            buf.set_string(kx, y, &line.key, key_style);
            let mut x = x0 + 2 + kw as u16;
            for (text, kind) in &line.cells {
                let style = match kind {
                    Kind::Text => base,
                    Kind::Tooltip => crate::theme::textual_dim(base.add_modifier(Modifier::DIM)),
                    // .bindings-table--header: underline $text-primary.
                    Kind::Title => Style::default()
                        .fg(t.css_over("text-primary", screen, t.title))
                        .bg(screen)
                        .add_modifier(Modifier::UNDERLINED),
                };
                buf.set_string(x, y, text, style);
                x += text.width() as u16;
            }
        }
        if scrolling {
            scrollbar(
                buf,
                rect.x + pw - 3,
                rect.y,
                rect.height,
                table.len(),
                self.scroll,
                t.var("scrollbar", t.thinking_border),
                t.var("scrollbar-background", blend(Color::Rgb(0, 0, 0), t.background, 0.1)),
            );
        }
    }
}

/// Textual's ScrollBarRender.render_bar, vertical, 2 columns, in eighths of a cell.
#[allow(clippy::too_many_arguments)]
fn scrollbar(
    buf: &mut Buffer,
    column: u16,
    top: u16,
    height: u16,
    lines: usize,
    scroll: usize,
    thumb: Color,
    track: Color,
) {
    const BARS: [&str; 8] = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", " "];
    let rows = usize::from(height);
    if lines <= rows || rows == 0 {
        return;
    }
    let size = f64::from(height);
    let virtual_size = lines as f64;
    let thumb_size = (size / (virtual_size / size)).max(1.0);
    let position = (size - thumb_size) * (scroll as f64 / (virtual_size - size));
    let start = (position * 8.0) as usize;
    let end = start + (thumb_size * 8.0).ceil() as usize;
    let (start_index, start_bar) = (start / 8, start % 8);
    let (end_index, end_bar) = (end / 8, end % 8);
    let empty = Style::default().fg(thumb).bg(track);
    let solid = Style::default().fg(track).bg(thumb);
    for i in 0..rows {
        buf.set_string(column, top + i as u16, "  ", empty);
    }
    for i in start_index..end_index.min(rows) {
        buf.set_string(column, top + i as u16, "  ", solid);
    }
    if start_index < rows && BARS[7 - start_bar] != " " {
        let glyph = BARS[7 - start_bar].repeat(2);
        buf.set_string(column, top + start_index as u16, glyph, empty);
    }
    if end_index < rows && BARS[7 - end_bar] != " " {
        let glyph = BARS[7 - end_bar].repeat(2);
        buf.set_string(column, top + end_index as u16, glyph, solid);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(key: &str, text: &str) -> Row {
        Row {
            key: key.into(),
            text: text.into(),
            tooltip: String::new(),
            title: false,
            end_section: false,
        }
    }

    #[test]
    fn width_is_a_third_clamped() {
        assert_eq!(width(140), 46);
        assert_eq!(width(80), 30);
        assert_eq!(width(240), 60);
        assert_eq!(width(20), 20);
    }

    #[test]
    fn description_wraps_by_word_and_sections_leave_a_blank_line() {
        let mut first = row("→", "Move cursor right or accept the completion suggestion");
        first.end_section = true;
        let rows = vec![first, row("^v", "Paste")];
        let (kw, dw) = widths(&rows, 41);
        assert_eq!((kw, dw), (2, 36));
        let (kw, dw) = (11, 27);
        let out = lines(&rows, kw, dw);
        let text: Vec<String> = out
            .iter()
            .map(|l| l.cells.iter().map(|(s, _)| s.as_str()).collect())
            .collect();
        assert_eq!(
            text,
            [
                "",
                "Move cursor right or accept",
                "the completion suggestion",
                "",
                "Paste",
                ""
            ]
        );
        assert_eq!(out[1].key, "→");
        assert_eq!(out[2].key, "");
    }

    #[test]
    fn keys_toggles_the_panel_and_the_app_lays_out_beside_it() {
        let full = Rect::new(0, 0, 140, 45);
        let mut panel = Panel::default();
        assert_eq!(panel.app_area(full), full);
        panel.toggle();
        assert_eq!(panel.app_area(full), Rect::new(0, 0, 94, 45));
        let mut buf = Buffer::empty(full);
        let theme = crate::theme::builtin_themes()[0].clone();
        let mut rows: Vec<Row> = (0..60).map(|i| row("^v", &format!("Paste {i}"))).collect();
        rows[0] = Row {
            title: true,
            ..row("", "Input")
        };
        panel.draw(&mut buf, full, &rows, &theme);
        let line = |y: u16| -> String { (94..140).map(|x| buf[(x, y)].symbol()).collect() };
        assert!(line(0).starts_with('▏'));
        assert_eq!(line(1).trim_start_matches('▏').trim(), "Input");
        // 62 lines in 45 rows: the 2-column scrollbar sits at columns 137-138.
        assert_ne!(buf[(137, 0)].bg, theme.background);
        let wheel = |kind| crossterm::event::MouseEvent {
            kind,
            column: 120,
            row: 5,
            modifiers: crossterm::event::KeyModifiers::NONE,
        };
        assert!(panel.mouse(wheel(crossterm::event::MouseEventKind::ScrollDown)));
        assert_eq!(panel.scroll, 2);
        panel.toggle();
        assert_eq!(panel.app_area(full), full);
        assert!(!panel.mouse(wheel(crossterm::event::MouseEventKind::ScrollDown)));
    }

    #[test]
    fn parse_reads_the_rows_and_refuses_a_bad_one() {
        let v = serde_json::json!({"key_table": [
            {"key": "^v", "text": "Paste", "tooltip": "", "title": false, "end_section": true}
        ]});
        let rows = parse(&v).unwrap().unwrap();
        assert_eq!(rows[0].key, "^v");
        assert!(rows[0].end_section);
        assert_eq!(parse(&serde_json::json!({})).unwrap(), None);
        assert!(parse(&serde_json::json!({"key_table": [{"key": 1}]})).is_err());
    }
}
