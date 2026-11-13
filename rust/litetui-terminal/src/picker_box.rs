//! Python's shared list picker (src/litetui/picker.py PickerBody in PickerScreen), drawn natively.
//!
//! /model, /backend, /think and /resume all open the same Textual dialog: `#picker-box`
//! (width 78, thick $primary border, padding 1 2, $surface) centred inside a body 80% of the
//! screen tall, holding the bold title, an OptionList (at most 19 rows, labels wrapped at the
//! words, padding 0 1), any extra widgets, the muted hint and the Dock to side button. The
//! views keep their own state and keys; this only lays them out the way Textual does.
use crate::{live::terminal_text, theme::Theme};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    widgets::Clear,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// picker.DEFAULT_HINT.
pub const HINT: &str = "↑↓ move · Enter or click to select · Esc to cancel";
const BOX_WIDTH: u16 = 78;
const MAX_LIST: usize = 19;
const DOCK: &str = "Dock to side";

/// Where the picker drew: each visible row line with its row index, and the box.
#[derive(Default)]
pub struct Drawn {
    pub hits: Vec<(Rect, usize)>,
    pub rect: Rect,
}

fn clip(text: &str, cells: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|c| {
            used += c.width().unwrap_or(0);
            used <= cells
        })
        .collect()
}

/// Textual's word wrap for an option label: break at spaces, fold a word longer than the line.
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut out = Vec::new();
    for paragraph in text.split('\n') {
        // A line's leading spaces stay on its first row; wrapped rows start at column 0.
        let body = paragraph.trim_start_matches(' ');
        let indent = paragraph.len() - body.len();
        let paragraph = body;
        let mut line = " ".repeat(indent);
        let mut first = true;
        for word in paragraph.split(' ') {
            let candidate = if std::mem::take(&mut first) || line.is_empty() {
                format!("{line}{word}")
            } else {
                format!("{line} {word}")
            };
            if candidate.width() <= width {
                line = candidate;
                continue;
            }
            if !line.is_empty() {
                out.push(std::mem::take(&mut line));
            }
            let mut rest = word.to_owned();
            while rest.width() > width {
                let head = clip(&rest, width);
                rest = rest[head.len()..].to_owned();
                out.push(head);
            }
            line = rest;
        }
        out.push(line);
    }
    out
}

/// Draw the picker over `area` (Python's modal covers the whole screen). `rows` are the option
/// labels, `highlighted` the cursor row, `scroll` the first visible wrapped line (kept so the
/// highlighted row stays in view), `extra` lines go between the list and the hint.
#[allow(clippy::too_many_arguments)]
pub fn draw(
    frame: &mut Frame,
    area: Rect,
    t: &Theme,
    title: &str,
    rows: &[String],
    highlighted: usize,
    scroll: &mut usize,
    extra: &[(String, Style)],
    hint: &str,
) -> Drawn {
    frame.render_widget(Clear, area);
    frame
        .buffer_mut()
        .set_style(area, Style::default().fg(t.text()).bg(t.screen()));
    let bw = area.width.min(BOX_WIDTH);
    if bw < 14 || area.height < 8 {
        return Drawn::default();
    }
    let cw = bw - 6; // border 1 + padding 2, each side
    let tw = usize::from(cw.saturating_sub(2)); // OptionList padding 0 1
    let extra: Vec<(String, Style)> = extra
        .iter()
        .flat_map(|(text, style)| {
            wrap(&terminal_text(text), usize::from(cw))
                .into_iter()
                .map(move |line| (line, *style))
        })
        .collect();
    // Wrapped option lines, each with its row index.
    let mut lines: Vec<(usize, String)> = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        for line in wrap(&terminal_text(row).replace('\t', " "), tw) {
            lines.push((index, line));
        }
    }
    // border+padding 2, title 2, hint 2, button 3 + margin 1, padding+border 2.
    let fixed = 12 + extra.len() as u16;
    let body = (u32::from(area.height) * 80 / 100) as u16;
    let room = usize::from(body.saturating_sub(fixed));
    let list_h = lines.len().min(MAX_LIST).min(room);
    // Keep the highlighted row's lines in view.
    if let (Some(first), Some(last)) = (
        lines.iter().position(|(i, _)| *i == highlighted),
        lines.iter().rposition(|(i, _)| *i == highlighted),
    ) {
        // An option taller than the list is shown from its first line, as OptionList does.
        if first < *scroll || last + 1 - first > list_h {
            *scroll = first;
        } else if last >= *scroll + list_h {
            *scroll = last + 1 - list_h.max(1);
        }
    }
    *scroll = (*scroll).min(lines.len().saturating_sub(list_h));
    let box_h = (fixed + list_h as u16).min(body).min(area.height);
    let body_top = area.y + (area.height - body.min(area.height)) / 2;
    // A box taller than the 80% body (a short terminal) still stays on the screen.
    let top = (body_top + body.saturating_sub(box_h) / 2).min(area.bottom() - box_h);
    let left = area.x + (area.width - bw) / 2;
    let rect = Rect::new(left, top, bw, box_h);
    frame.render_widget(Clear, rect);
    // #picker-box: `background: $surface; border: thick $primary`; #picker-hint $text-muted.
    let surface_bg = t.surface_bg();
    let surface = Style::default().fg(t.text()).bg(surface_bg);
    let edge = Style::default().fg(t.var("primary", t.primary)).bg(surface_bg);
    let muted = Style::default().fg(t.muted_on(surface_bg)).bg(surface_bg);
    let inner = usize::from(bw - 2);
    let buf = frame.buffer_mut();
    for y in rect.top()..rect.bottom() {
        let row = if y == rect.top() {
            format!("█{}█", "▀".repeat(inner))
        } else if y + 1 == rect.bottom() {
            format!("█{}█", "▄".repeat(inner))
        } else {
            format!("█{}█", " ".repeat(inner))
        };
        buf.set_string(rect.x, y, row, edge);
        if y != rect.top() && y + 1 != rect.bottom() {
            buf.set_string(rect.x + 1, y, " ".repeat(inner), surface);
        }
    }
    let cx = rect.x + 3;
    let last_row = rect.bottom().saturating_sub(2); // the padding row above the bottom border
    let put = |buf: &mut ratatui::buffer::Buffer, x: u16, y: u16, text: &str, style: Style| {
        if y < last_row {
            buf.set_string(
                x,
                y,
                clip(text, usize::from((cx + cw).saturating_sub(x))),
                style,
            );
        }
    };
    put(
        buf,
        cx,
        rect.y + 2,
        &terminal_text(title),
        // #picker-title: bold $primary-lighten-2.
        Style::default()
            .fg(t.var("primary-lighten-2", t.title))
            .bg(surface_bg)
            .add_modifier(Modifier::BOLD),
    );
    let list_top = rect.y + 4;
    // #picker-list is the focused OptionList: `background-tint: $foreground 5%` over its
    // $surface, the highlighted option in $block-cursor-* inside its `padding: 0 1`.
    let styled = !t.css.is_empty();
    let list_bg = if styled { t.input() } else { surface_bg };
    let mut hits = Vec::new();
    for (dy, (index, line)) in lines.iter().skip(*scroll).take(list_h).enumerate() {
        let y = list_top + dy as u16;
        let style = if *index == highlighted {
            {
                // $block-cursor-foreground is `auto 87%`: the contrast text of the cursor.
                let cursor = t.var("block-cursor-background", t.primary);
                Style::default()
                    .fg(t.css_over("block-cursor-foreground", cursor, t.background))
                    .bg(cursor)
                    .add_modifier(Modifier::BOLD)
            }
        } else {
            surface.bg(list_bg)
        };
        if styled {
            put(buf, cx, y, &" ".repeat(usize::from(cw)), surface.bg(list_bg));
            put(buf, cx + 1, y, &" ".repeat(usize::from(cw.saturating_sub(2))), style);
        } else {
            put(buf, cx, y, &" ".repeat(usize::from(cw)), style);
        }
        put(buf, cx + 1, y, line, style);
        hits.push((Rect::new(cx, y, cw, 1), *index));
    }
    let mut y = list_top + list_h as u16;
    for (text, style) in &extra {
        put(buf, cx, y, text, style.bg(surface_bg));
        y += 1;
    }
    put(buf, cx, y + 1, &terminal_text(hint), muted);
    // SwapButton: width 100%, margin-bottom 1; a default Button (see `button`).
    let dock = y + 2;
    let (face, top_rim, bottom_rim) = button_styles(t, false);
    let w = usize::from(cw);
    put(buf, cx, dock, &"▔".repeat(w), top_rim);
    let label = clip(DOCK, w);
    let pad = w.saturating_sub(label.width()) / 2;
    put(
        buf,
        cx,
        dock + 1,
        &format!(
            "{}{label}{}",
            " ".repeat(pad),
            " ".repeat(w - pad - label.width())
        ),
        face,
    );
    put(buf, cx, dock + 2, &"▁".repeat(w), bottom_rim);
    Drawn { hits, rect }
}

/// Textual's Button (`-style-default`): bold `$button-foreground` on `$surface` between a
/// `tall $surface-lighten-1` top and a `tall $surface-darken-1` bottom; `-primary`: on
/// `$primary`, `$button-color-foreground`, rims `$primary-lighten-3` / `$primary-darken-3`.
/// Returns (face, top rim, bottom rim). The preview catalog keeps its panel-faced buttons.
pub fn button_styles(t: &Theme, primary: bool) -> (Style, Style, Style) {
    if t.css.is_empty() {
        let face_bg = if primary { t.primary } else { t.panel };
        let face = Style::default()
            .fg(if primary { t.background } else { t.foreground })
            .bg(face_bg)
            .add_modifier(Modifier::BOLD);
        let rim = Style::default().fg(t.thinking_border).bg(face_bg);
        return (face, rim, rim);
    }
    let (bg, fg, top, bottom) = if primary {
        (
            t.var("primary", t.primary),
            t.css_over("button-color-foreground", t.var("primary", t.primary), t.background),
            t.var("primary-lighten-3", t.primary),
            t.var("primary-darken-3", t.primary),
        )
    } else {
        let bg = t.surface_bg();
        (
            bg,
            t.css_over("button-foreground", bg, t.foreground),
            t.var("surface-lighten-1", t.thinking_border),
            t.var("surface-darken-1", t.thinking_border),
        )
    };
    (
        Style::default().fg(fg).bg(bg).add_modifier(Modifier::BOLD),
        Style::default().fg(top).bg(bg),
        Style::default().fg(bottom).bg(bg),
    )
}

/// One Button's three rows: the tall top rim, the face, the tall bottom rim.
#[derive(Clone, Copy, Debug, Default)]
pub struct Look {
    pub face: Style,
    pub top: Style,
    pub bottom: Style,
}
/// Textual's Button for the running theme, every variant the dialogs use, unfocused and
/// focused (`:focus`: `background-tint: $foreground 5%`, `text-style: $button-focus-text-style`,
/// "b reverse"); `get` returns None for the preview catalog, whose buttons keep their look.
#[derive(Clone, Copy, Debug, Default)]
pub struct Buttons {
    styled: bool,
    looks: [[Look; 2]; 4],
}
impl Buttons {
    pub fn of(t: &Theme) -> Self {
        if t.css.is_empty() {
            return Self::default();
        }
        let variant = |name: &str| -> [Look; 2] {
            let (bg, fg, top, bottom) = if name.is_empty() {
                let bg = t.surface_bg();
                (
                    bg,
                    t.css_value("button-foreground"),
                    t.var("surface-lighten-1", bg),
                    t.var("surface-darken-1", bg),
                )
            } else {
                let bg = t.var(name, t.primary);
                (
                    bg,
                    t.css_value("button-color-foreground"),
                    t.var(&format!("{name}-lighten-3"), bg),
                    t.var(&format!("{name}-darken-3"), bg),
                )
            };
            let look = |bg: ratatui::style::Color| {
                let fg = fg.map_or(t.text(), |c| c.over(bg));
                Look {
                    face: Style::default().fg(fg).bg(bg).add_modifier(Modifier::BOLD),
                    top: Style::default().fg(top).bg(bg),
                    bottom: Style::default().fg(bottom).bg(bg),
                }
            };
            let tinted = t.css_alpha("foreground", 5.0, bg, bg);
            let mut focused = look(tinted);
            focused.face = focused.face.add_modifier(Modifier::REVERSED);
            [look(bg), focused]
        };
        Self {
            styled: true,
            looks: [variant(""), variant("primary"), variant("warning"), variant("error")],
        }
    }
    /// The variant's look (`default`, `primary`, `warning`, `error`); `:disabled` is
    /// `text-opacity: 0.6`, the face's text blended toward its background.
    pub fn get(&self, variant: &str, focused: bool, disabled: bool) -> Option<Look> {
        if !self.styled {
            return None;
        }
        let row = match variant {
            "primary" => 1,
            "warning" => 2,
            "error" => 3,
            _ => 0,
        };
        let mut look = self.looks[row][usize::from(focused)];
        if disabled
            && let (Some(fg), Some(bg)) = (look.face.fg, look.face.bg)
        {
            look.face = look.face.fg(crate::theme::blend(bg, fg, 0.6));
        }
        Some(look)
    }
}

/// Textual's `tall` border around `rect`, as Textual draws it: the left edge "▊" is the
/// colour outside in front of the border colour, the right edge "▎" the border colour in
/// front of the outside, the top "▔" and bottom "▁" the border colour over the inside, and the
/// inside filled.
pub fn tall_edges(
    buf: &mut ratatui::buffer::Buffer,
    rect: Rect,
    border: ratatui::style::Color,
    inside: ratatui::style::Color,
    outside: ratatui::style::Color,
    text: ratatui::style::Color,
) {
    let rect = rect.intersection(buf.area);
    if rect.width < 2 || rect.height == 0 {
        return;
    }
    let inner = usize::from(rect.width - 2);
    let left = Style::default().fg(outside).bg(border);
    let right = Style::default().fg(border).bg(outside);
    for y in rect.top()..rect.bottom() {
        let (fill, style) = if y == rect.top() && rect.height > 1 {
            ("▔", Style::default().fg(border).bg(inside))
        } else if y + 1 == rect.bottom() && rect.height > 1 {
            ("▁", Style::default().fg(border).bg(inside))
        } else {
            (" ", Style::default().fg(text).bg(inside))
        };
        buf.set_string(rect.x, y, "▊", left);
        buf.set_string(rect.x + 1, y, fill.repeat(inner), style);
        buf.set_string(rect.right() - 1, y, "▎", right);
    }
}

/// Textual's thick border on $surface (#picker-box, #help-box): the box filled, its edges drawn.
pub fn thick_box(buf: &mut ratatui::buffer::Buffer, rect: Rect, t: &Theme) {
    let surface = Style::default().fg(t.text()).bg(t.surface_bg());
    let edge = Style::default().fg(t.var("primary", t.primary)).bg(t.surface_bg());
    let inner = usize::from(rect.width.saturating_sub(2));
    for y in rect.top()..rect.bottom() {
        let row = if y == rect.top() {
            format!("█{}█", "▀".repeat(inner))
        } else if y + 1 == rect.bottom() {
            format!("█{}█", "▄".repeat(inner))
        } else {
            format!("█{}█", " ".repeat(inner))
        };
        buf.set_string(rect.x, y, row, edge);
        if y != rect.top() && y + 1 != rect.bottom() {
            buf.set_string(rect.x + 1, y, " ".repeat(inner), surface);
        }
    }
}
/// A Textual Button (min-width 16): ▔ / centred label / ▁ rows; `primary` is the variant.
pub fn button(
    buf: &mut ratatui::buffer::Buffer,
    x: u16,
    y: u16,
    label: &str,
    t: &Theme,
    primary: bool,
) -> u16 {
    let w = (label.width() + 2).max(16);
    let (face, top_rim, bottom_rim) = button_styles(t, primary);
    let pad = (w - label.width()) / 2;
    buf.set_string(x, y, "▔".repeat(w), top_rim);
    buf.set_string(
        x,
        y + 1,
        format!(
            "{}{label}{}",
            " ".repeat(pad),
            " ".repeat(w - pad - label.width())
        ),
        face,
    );
    buf.set_string(x, y + 2, "▁".repeat(w), bottom_rim);
    w as u16
}
/// Textual's ScrollBarRender.render_bar, vertical, in eighths of a cell.
#[allow(clippy::too_many_arguments)]
pub fn scrollbar(
    buf: &mut ratatui::buffer::Buffer,
    column: u16,
    top: u16,
    height: u16,
    lines: usize,
    scroll: usize,
    t: &Theme,
) {
    if lines <= usize::from(height) || height == 0 {
        return;
    }
    const BARS: [&str; 8] = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", " "];
    let size = f64::from(height);
    let virtual_size = lines as f64;
    let thumb = (size / (virtual_size / size)).max(1.0);
    let position = (size - thumb) * (scroll as f64 / (virtual_size - size));
    let start = (position * 8.0) as usize;
    let end = start + (thumb * 8.0).ceil() as usize;
    let (start_index, start_bar) = (start / 8, start % 8);
    let (end_index, end_bar) = (end / 8, end % 8);
    // Textual's scrollbar: `scrollbar-color: $scrollbar` on `scrollbar-background:
    // $scrollbar-background` (the preview catalog keeps its own pair).
    let bar = t.var("scrollbar", t.thinking_border);
    let back = t.var("scrollbar-background", t.surface);
    let track = Style::default().fg(bar).bg(back);
    let solid = Style::default().fg(back).bg(bar);
    let rows = usize::from(height);
    if !t.css.is_empty() {
        // Textual paints the whole track in the scrollbar background.
        for i in 0..rows {
            buf.set_string(column, top + i as u16, " ", Style::default().fg(bar).bg(back));
        }
    }
    for i in start_index..end_index.min(rows) {
        buf.set_string(column, top + i as u16, " ", solid);
    }
    if start_index < rows && BARS[7 - start_bar] != " " {
        buf.set_string(column, top + start_index as u16, BARS[7 - start_bar], track);
    }
    if end_index < rows && BARS[7 - end_bar] != " " {
        buf.set_string(column, top + end_index as u16, BARS[7 - end_bar], solid);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_like_textual_at_seventy_cells() {
        // Python's /backend capture (target/oxide-parity/screens1/textual/03-backend.txt).
        let rows = wrap(
            "Xolotl (DeepSeek-V4.1-Flash, GPU + RAM arena + NVMe)  · not installed — set xolotl_root to a built Xolotl checkout",
            70,
        );
        assert_eq!(
            rows,
            [
                "Xolotl (DeepSeek-V4.1-Flash, GPU + RAM arena + NVMe)  · not installed",
                "— set xolotl_root to a built Xolotl checkout"
            ]
        );
        assert_eq!(wrap("short", 70), ["short"]);
        assert_eq!(wrap("abcdefgh", 3), ["abc", "def", "gh"]);
    }
}
