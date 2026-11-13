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
        let mut line = String::new();
        for word in paragraph.split(' ') {
            let candidate = if line.is_empty() {
                word.to_owned()
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
        .set_style(area, Style::default().fg(t.foreground).bg(t.background));
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
    let surface = Style::default().fg(t.foreground).bg(t.surface);
    let edge = Style::default().fg(t.primary).bg(t.surface);
    let muted = Style::default().fg(t.thinking_text).bg(t.surface);
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
        Style::default()
            .fg(t.title)
            .bg(t.surface)
            .add_modifier(Modifier::BOLD),
    );
    let list_top = rect.y + 4;
    let mut hits = Vec::new();
    for (dy, (index, line)) in lines.iter().skip(*scroll).take(list_h).enumerate() {
        let y = list_top + dy as u16;
        let style = if *index == highlighted {
            Style::default()
                .fg(t.background)
                .bg(t.primary)
                .add_modifier(Modifier::BOLD)
        } else {
            surface
        };
        put(buf, cx, y, &" ".repeat(usize::from(cw)), style);
        put(buf, cx + 1, y, line, style);
        hits.push((Rect::new(cx, y, cw, 1), *index));
    }
    let mut y = list_top + list_h as u16;
    for (text, style) in &extra {
        put(buf, cx, y, text, style.bg(t.surface));
        y += 1;
    }
    put(buf, cx, y + 1, &terminal_text(hint), muted);
    // SwapButton: width 100%, margin-bottom 1.
    let dock = y + 2;
    let face = Style::default()
        .fg(t.foreground)
        .bg(t.panel)
        .add_modifier(Modifier::BOLD);
    let rim = Style::default().fg(t.thinking_border).bg(t.panel);
    let w = usize::from(cw);
    put(buf, cx, dock, &"▔".repeat(w), rim);
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
    put(buf, cx, dock + 2, &"▁".repeat(w), rim);
    Drawn { hits, rect }
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
