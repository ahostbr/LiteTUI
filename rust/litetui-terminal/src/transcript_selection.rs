//! Select the cells the user actually saw. Only a left-button press captures screen text.
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::Modifier,
};
use std::ops::Range;
use unicode_width::UnicodeWidthStr;

const MAX_CELLS: usize = 32 * 1024;
const MAX_BYTES: usize = 128 * 1024;

struct Glyph {
    x: u16,
    width: u16,
    bytes: Range<usize>,
}

struct Selection {
    text: String,
    rows: Vec<Vec<Glyph>>,
    start: (u16, u16),
    end: (u16, u16),
    dragging: bool,
}

impl Selection {
    fn capture(buffer: &Buffer, area: Rect, point: (u16, u16)) -> Option<Self> {
        if usize::from(area.width) * usize::from(area.height) > MAX_CELLS
            || buffer.area.intersection(area) != area
        {
            return None;
        }
        let mut text = String::new();
        let mut rows = Vec::new();
        for y in area.top()..area.bottom() {
            let mut row = Vec::new();
            let mut x = area.left();
            while x < area.right() {
                let symbol = buffer[(x, y)].symbol();
                if text.len().saturating_add(symbol.len()) > MAX_BYTES {
                    return None;
                }
                let width = symbol.width().max(1).min(usize::from(area.right() - x)) as u16;
                let from = text.len();
                text.push_str(symbol);
                row.push(Glyph {
                    x,
                    width,
                    bytes: from..text.len(),
                });
                x += width;
            }
            rows.push(row);
        }
        Some(Self {
            text,
            rows,
            start: point,
            end: point,
            dragging: true,
        })
    }

    fn range(&self, area: Rect, y: u16) -> Range<u16> {
        let (first, last) = (self.start.min(self.end), self.start.max(self.end));
        let left = if y == first.0 { first.1 } else { area.left() };
        let right = if y == last.0 {
            last.1 + 1
        } else {
            area.right()
        };
        left..right
    }

    fn selected_rows(&self) -> std::ops::RangeInclusive<u16> {
        self.start.0.min(self.end.0)..=self.start.0.max(self.end.0)
    }

    fn glyphs(&self, area: Rect, y: u16) -> impl Iterator<Item = &Glyph> {
        let range = self.range(area, y);
        self.rows[usize::from(y - area.y)]
            .iter()
            .filter(move |g| g.x < range.end && g.x + g.width > range.start)
    }
}

#[derive(Default)]
pub struct TranscriptSelection {
    viewport: Option<(Rect, usize)>,
    selected: Option<Selection>,
}

impl TranscriptSelection {
    pub fn active(&self) -> bool {
        self.selected.is_some()
    }

    pub fn clear(&mut self) -> bool {
        self.selected.take().is_some()
    }

    pub fn hide(&mut self) {
        self.viewport = None;
        self.clear();
    }

    pub fn draw(&mut self, buffer: &mut Buffer, area: Rect, scroll: usize) {
        if self.viewport.replace((area, scroll)) != Some((area, scroll)) {
            self.clear();
        }
        let Some(selection) = &self.selected else {
            return;
        };
        // Streaming may repaint the transcript between pointer events. Copy only an
        // unchanged visible range, including when the drag is later extended.
        if !selection.selected_rows().all(|y| {
            selection
                .glyphs(area, y)
                .all(|g| buffer[(g.x, y)].symbol() == &selection.text[g.bytes.clone()])
        }) {
            self.clear();
            return;
        }
        if selection.start == selection.end {
            return;
        }
        for y in selection.selected_rows() {
            for g in selection.glyphs(area, y) {
                for x in g.x..g.x + g.width {
                    buffer[(x, y)].modifier.insert(Modifier::REVERSED);
                }
            }
        }
    }

    pub fn mouse(&mut self, event: MouseEvent, shown: &Buffer) -> bool {
        let Some((area, _)) = self.viewport else {
            return false;
        };
        if !event.modifiers.is_empty() {
            self.clear();
            return false;
        }
        let point = (event.row, event.column);
        let inside = area.contains(Position::new(event.column, event.row));
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                self.selected = if inside {
                    Selection::capture(shown, area, point)
                } else {
                    None
                };
                inside
            }
            MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left) => {
                let Some(s) = self.selected.as_mut().filter(|s| s.dragging) else {
                    return false;
                };
                if !inside {
                    self.clear();
                    return true;
                }
                s.end = point;
                if matches!(event.kind, MouseEventKind::Up(_)) {
                    s.dragging = false;
                    if s.start == s.end {
                        self.clear();
                    }
                }
                true
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.clear();
                false
            }
            _ => false,
        }
    }

    pub fn text(&self) -> Option<String> {
        let (area, _) = self.viewport?;
        let selection = self
            .selected
            .as_ref()
            .filter(|s| !s.dragging && s.start != s.end)?;
        let mut output = String::new();
        for (index, y) in selection.selected_rows().enumerate() {
            if index > 0 {
                output.push('\n');
            }
            let start = output.len();
            for g in selection.glyphs(area, y) {
                output.push_str(&selection.text[g.bytes.clone()]);
            }
            let end = start + output[start..].trim_end_matches(' ').len();
            output.truncate(end);
        }
        (!output.is_empty()).then_some(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::style::Style;

    #[test]
    fn copied_rows_preserve_empty_lines_and_cancel_on_reflow_or_scroll() {
        let area = Rect::new(4, 3, 12, 3);
        let mut shown = Buffer::empty(area);
        shown.set_string(4, 3, "first", Style::default());
        shown.set_string(4, 5, "last", Style::default());
        let mut view = TranscriptSelection::default();
        let drag = |view: &mut TranscriptSelection, buffer: &Buffer| {
            for (kind, column, row) in [
                (MouseEventKind::Down(MouseButton::Left), 5, 3),
                (MouseEventKind::Up(MouseButton::Left), 5, 5),
            ] {
                assert!(view.mouse(
                    MouseEvent {
                        kind,
                        column,
                        row,
                        modifiers: KeyModifiers::NONE
                    },
                    buffer
                ));
            }
        };
        view.draw(&mut shown, area, 0);
        drag(&mut view, &shown);
        assert_eq!(view.text().as_deref(), Some("irst\n\nla"));
        shown[(11, 5)].set_symbol("x");
        view.draw(&mut shown, area, 0);
        assert!(view.active());
        view.draw(&mut shown, area, 1);
        assert!(!view.active());
        drag(&mut view, &shown);
        view.draw(&mut shown, Rect::new(4, 3, 11, 3), 1);
        assert!(!view.active());
        let oversized = Rect::new(0, 0, 256, 129);
        assert!(Selection::capture(&Buffer::empty(oversized), oversized, (0, 0)).is_none());
    }
}
