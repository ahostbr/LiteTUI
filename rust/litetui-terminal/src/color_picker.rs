//! Terminal colour picker for the F4 custom-theme editor: port of
//! `src/litetui/colorpicker.py` ("a terminal color picker for the theme creator
//! because hex isn't enough").
//!
//! The picker only produces a candidate `#rrggbb` for the field being edited. It
//! never saves, never talks to the runtime, and has no knowledge of the review /
//! confirm / readback path: the caller writes the accepted hex into its own draft.
//!
//! Rendering uses the half-block trick: every "▀" carries two vertical cells,
//! foreground for the top and background for the bottom, so the saturation/value
//! field is `GRID_H` logical rows drawn in `GRID_H / 2` text rows. Mouse clicks map
//! back through the same geometry as the paint, and arrow keys walk the grid.
//! Conversions are pure functions so the mapping is testable without a terminal.
use crate::prepared_skill::BoundedDraft;
use crossterm::event::{KeyCode, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
};

pub const GRID_W: usize = 48;
pub const GRID_H: usize = 24;
pub const HUE_W: usize = 48;
/// Narrower than this the field is a stripe, not a picker.
pub const MIN_W: usize = 8;
pub const HALF: char = '▀';
/// Rows the picker needs: title, field, hue bar, presets, hex row, hint.
pub const ROWS: u16 = (GRID_H / 2) as u16 + 5;
const HEX_BYTES: usize = 16;
const SWATCH: u16 = 6;
const HEX_BOX: u16 = 10;

fn rgb_of(h: f64, s: f64, v: f64) -> (f64, f64, f64) {
    // colorsys.hsv_to_rgb, step for step.
    let (s, v) = (s.clamp(0.0, 1.0), v.clamp(0.0, 1.0));
    let h = h.rem_euclid(1.0);
    if s == 0.0 {
        return (v, v, v);
    }
    let i = (h * 6.0) as i64;
    let f = h * 6.0 - i as f64;
    let p = v * (1.0 - s);
    let q = v * (1.0 - s * f);
    let t = v * (1.0 - s * (1.0 - f));
    match i.rem_euclid(6) {
        0 => (v, t, p),
        1 => (q, v, p),
        2 => (p, v, t),
        3 => (p, q, v),
        4 => (t, p, v),
        _ => (v, p, q),
    }
}

fn byte(x: f64) -> u8 {
    // Python's round() is half-to-even.
    (x * 255.0).round_ties_even() as u8
}

/// h in [0,1), s/v in [0,1] -> (r, g, b).
pub fn hsv_to_rgb(h: f64, s: f64, v: f64) -> [u8; 3] {
    let (r, g, b) = rgb_of(h, s, v);
    [byte(r), byte(g), byte(b)]
}

/// h in [0,1), s/v in [0,1] -> `#rrggbb`.
pub fn hsv_to_hex(h: f64, s: f64, v: f64) -> String {
    let [r, g, b] = hsv_to_rgb(h, s, v);
    format!("#{r:02x}{g:02x}{b:02x}")
}

/// `#rrggbb` (leading `#`s and outer whitespace tolerated) -> (h, s, v).
pub fn hex_to_hsv(hex: &str) -> Result<(f64, f64, f64), String> {
    let t = hex.trim().trim_start_matches('#');
    if t.len() != 6 || !t.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!("not a #RRGGBB hex: {hex:?}"));
    }
    let channel = |i: usize| u8::from_str_radix(&t[i..i + 2], 16).unwrap() as f64 / 255.0;
    let (r, g, b) = (channel(0), channel(2), channel(4));
    // colorsys.rgb_to_hsv, step for step.
    let maxc = r.max(g).max(b);
    let minc = r.min(g).min(b);
    if minc == maxc {
        return Ok((0.0, 0.0, maxc));
    }
    let span = maxc - minc;
    let (rc, gc, bc) = ((maxc - r) / span, (maxc - g) / span, (maxc - b) / span);
    let h = if r == maxc {
        bc - gc
    } else if g == maxc {
        2.0 + rc - bc
    } else {
        4.0 + gc - rc
    };
    Ok(((h / 6.0).rem_euclid(1.0), span / maxc, maxc))
}

/// The painting width, clamped. Zero means "the full-size field" (not laid out yet).
pub fn clamp_width(width: i32) -> usize {
    if width == 0 {
        GRID_W
    } else {
        width.clamp(MIN_W as i32, GRID_W as i32) as usize
    }
}

/// SV-field cell -> (saturation, value). Row 0 is the top (value 1).
pub fn cell_to_sv(col: i32, row: i32, width: i32) -> (f64, f64) {
    let w = clamp_width(width);
    let col = col.clamp(0, w as i32 - 1);
    let row = row.clamp(0, GRID_H as i32 - 1);
    (
        col as f64 / (w - 1) as f64,
        1.0 - row as f64 / (GRID_H - 1) as f64,
    )
}

pub fn sv_to_cell(s: f64, v: f64, width: i32) -> (usize, usize) {
    let w = clamp_width(width);
    (
        (s * (w - 1) as f64).round_ties_even() as usize,
        ((1.0 - v) * (GRID_H - 1) as f64).round_ties_even() as usize,
    )
}

pub fn hue_to_cell(h: f64, width: i32) -> usize {
    // The epsilon is the round trip: 15/44 * 44 lands a hair below 15 and
    // truncation would lose a whole cell (the source measured the same trap).
    let w = clamp_width(width);
    (((h.rem_euclid(1.0)) * w as f64 + 1e-9) as usize).min(w - 1)
}

pub fn cell_to_hue(col: i32, width: i32) -> f64 {
    let w = clamp_width(width);
    col.clamp(0, w as i32 - 1) as f64 / w as f64
}

fn color([r, g, b]: [u8; 3]) -> Color {
    Color::Rgb(r, g, b)
}

/// The saturation/value field at hue `h`, half-block doubled, with a `+` marker at
/// the selected cell. Always `GRID_H / 2` lines of exactly `clamp_width(width)`
/// characters: the click map depends on that invariant.
pub fn render_sv_field(h: f64, sel_s: f64, sel_v: f64, width: i32) -> Vec<Line<'static>> {
    let w = clamp_width(width);
    let (sel_col, sel_row) = sv_to_cell(sel_s, sel_v, w as i32);
    (0..GRID_H / 2)
        .map(|tr| {
            let (top_row, bot_row) = (tr * 2, tr * 2 + 1);
            Line::from(
                (0..w)
                    .map(|col| {
                        let (s_top, v_top) = cell_to_sv(col as i32, top_row as i32, w as i32);
                        let (s_bot, v_bot) = cell_to_sv(col as i32, bot_row as i32, w as i32);
                        let ch = if col == sel_col && (top_row == sel_row || bot_row == sel_row) {
                            '+'
                        } else {
                            HALF
                        };
                        Span::styled(
                            ch.to_string(),
                            Style::default()
                                .fg(color(hsv_to_rgb(h, s_top, v_top)))
                                .bg(color(hsv_to_rgb(h, s_bot, v_bot))),
                        )
                    })
                    .collect::<Vec<_>>(),
            )
        })
        .collect()
}

pub fn render_hue_bar(sel_h: f64, width: i32) -> Line<'static> {
    let w = clamp_width(width);
    let sel = hue_to_cell(sel_h, w as i32);
    Line::from(
        (0..w)
            .map(|col| {
                let hue = cell_to_hue(col as i32, w as i32);
                Span::styled(
                    if col == sel { "▼" } else { "▀" },
                    Style::default()
                        .fg(color(hsv_to_rgb(hue, 1.0, 1.0)))
                        .bg(color(hsv_to_rgb(hue, 1.0, 0.55))),
                )
            })
            .collect::<Vec<_>>(),
    )
}

/// Where the picker last drew, absolute: the mouse map is built beside the paint.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hits {
    pub field: Rect,
    pub hue: Rect,
    pub presets: Rect,
    pub hex: Rect,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Focus {
    Field,
    Hex,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Pick {
    Pending,
    Cancel,
    Accept(String),
}

pub struct Picker {
    token: String,
    h: f64,
    s: f64,
    v: f64,
    hex: BoundedDraft<HEX_BYTES>,
    focus: Focus,
    painted_w: usize,
    presets: Vec<(String, String)>,
    hits: Option<Hits>,
    pressed: Option<(u16, u16)>,
    message: Option<String>,
}

impl Picker {
    /// Starts FROM the field's current value; anything that is not a `#RRGGBB`
    /// opens on mid grey (the source's fallback).
    pub fn new(initial: &str, token: &str) -> Self {
        let (h, s, v) = hex_to_hsv(initial).unwrap_or((0.0, 0.0, 0.5));
        let mut hex = BoundedDraft::<HEX_BYTES>::default();
        hex.set(&hsv_to_hex(h, s, v)).expect("hex fits");
        Self {
            token: token.into(),
            h,
            s,
            v,
            hex,
            focus: Focus::Field,
            painted_w: GRID_W,
            presets: Vec::new(),
            hits: None,
            pressed: None,
            message: None,
        }
    }
    pub fn value(&self) -> String {
        hsv_to_hex(self.h, self.s, self.v)
    }
    pub fn hsv(&self) -> (f64, f64, f64) {
        (self.h, self.s, self.v)
    }
    pub fn focus(&self) -> Focus {
        self.focus
    }
    pub fn hex_text(&self) -> &str {
        self.hex.text()
    }
    pub fn disarm(&mut self) {
        self.pressed = None;
    }
    fn set_hsv(&mut self, (h, s, v): (f64, f64, f64)) {
        (self.h, self.s, self.v) = (h, s, v);
        self.message = None;
        self.sync_hex();
    }
    /// A grid move rewrites the box; typing never does (the box is the hatch).
    fn sync_hex(&mut self) {
        let value = self.value();
        if !self.hex.text().trim().eq_ignore_ascii_case(&value) {
            self.hex.set(&value).expect("hex fits");
        }
    }
    fn nudge(&mut self, code: KeyCode) {
        // One PAINTED cell: in a narrow panel a full-size step would skip colours.
        let step = 1.0 / (self.painted_w.saturating_sub(1)).max(1) as f64;
        let vstep = 1.0 / (GRID_H - 1) as f64;
        let hstep = 1.0 / HUE_W as f64;
        match code {
            KeyCode::Left => self.s = (self.s - step).max(0.0),
            KeyCode::Right => self.s = (self.s + step).min(1.0),
            KeyCode::Up => self.v = (self.v + vstep).min(1.0),
            KeyCode::Down => self.v = (self.v - vstep).max(0.0),
            KeyCode::PageUp => self.h = (self.h + hstep).rem_euclid(1.0),
            KeyCode::PageDown => self.h = (self.h - hstep).rem_euclid(1.0),
            _ => return,
        }
        self.message = None;
        self.sync_hex();
    }
    /// A typed value that parses drives the field and swatch immediately, without
    /// rewriting what the user is typing.
    fn follow_hex(&mut self) {
        self.message = None;
        if let Ok(hsv) = hex_to_hsv(self.hex.text()) {
            (self.h, self.s, self.v) = hsv;
        }
    }
    pub fn paste(&mut self, text: &str) {
        if self.focus != Focus::Hex {
            return;
        }
        if text.is_empty() || !text.chars().all(|c| c == '#' || c.is_ascii_hexdigit()) {
            self.message = Some("Only # and hex digits fit the hex box.".into());
            return;
        }
        if self.hex.insert(text).is_err() {
            self.message = Some("Hex box is full.".into());
            return;
        }
        self.follow_hex();
    }
    fn accept(&mut self) -> Pick {
        // An edited-but-unsubmitted hex wins if it parses; a typed value that does
        // not parse is refused rather than silently replaced by the grid colour.
        match hex_to_hsv(self.hex.text()) {
            Ok(hsv) => {
                (self.h, self.s, self.v) = hsv;
                Pick::Accept(self.value())
            }
            Err(_) => {
                self.message = Some(format!("Not a #RRGGBB hex: {}", self.hex.text()));
                Pick::Pending
            }
        }
    }
    pub fn key(&mut self, code: KeyCode) -> Pick {
        match code {
            KeyCode::Esc => return Pick::Cancel,
            KeyCode::Enter => return self.accept(),
            KeyCode::Tab | KeyCode::BackTab => {
                self.focus = match self.focus {
                    Focus::Field => Focus::Hex,
                    Focus::Hex => Focus::Field,
                };
                self.message = None;
            }
            KeyCode::Up | KeyCode::Down | KeyCode::PageUp | KeyCode::PageDown => self.nudge(code),
            KeyCode::Left | KeyCode::Right if self.focus == Focus::Field => self.nudge(code),
            KeyCode::Char(c) if self.focus == Focus::Hex && !c.is_control() => {
                self.paste(&c.to_string())
            }
            KeyCode::Left
            | KeyCode::Right
            | KeyCode::Home
            | KeyCode::End
            | KeyCode::Backspace
            | KeyCode::Delete
                if self.focus == Focus::Hex =>
            {
                self.hex.key(code);
                self.follow_hex();
            }
            _ => {}
        }
        Pick::Pending
    }
    /// A completed left press/release pair on one cell acts; anything else only
    /// disarms. Clicking never accepts: Enter is the only door out.
    pub fn mouse(&mut self, event: MouseEvent) {
        let at = (event.column, event.row);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = Some(at),
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take() == Some(at) {
                    self.click(at.0, at.1);
                }
            }
            _ => self.pressed = None,
        }
    }
    pub fn click(&mut self, x: u16, y: u16) {
        let Some(hits) = self.hits else { return };
        let inside = |r: Rect| r.contains((x, y).into());
        let w = self.painted_w as i32;
        if inside(hits.field) {
            // Each text row is two logical cells; the top one is the click.
            let (s, v) = cell_to_sv(
                i32::from(x - hits.field.x),
                i32::from(y - hits.field.y) * 2,
                w,
            );
            self.s = s;
            self.v = v;
            self.message = None;
            self.sync_hex();
            self.focus = Focus::Field;
        } else if inside(hits.hue) {
            self.h = cell_to_hue(i32::from(x - hits.hue.x), w);
            self.message = None;
            self.sync_hex();
            self.focus = Focus::Field;
        } else if inside(hits.presets) {
            // Each preset paints 2 cells + 1 gap = 3 columns.
            let index = usize::from(x - hits.presets.x) / 3;
            if let Some(hsv) = self
                .presets
                .get(index)
                .and_then(|(_, hex)| hex_to_hsv(hex).ok())
            {
                self.set_hsv(hsv);
            }
        } else if inside(hits.hex) {
            self.focus = Focus::Hex;
        }
    }
    /// Draws into `inner`. Returns false (and draws a resize note) when the
    /// picker does not fit; the caller must then treat the view as not visible.
    pub fn draw(&mut self, frame: &mut Frame, inner: Rect, theme: &crate::theme::Theme) -> bool {
        // `pressed` survives a redraw at the same geometry (the host disarms it on
        // resize), so a press and its release can straddle a frame.
        self.hits = None;
        if inner.height < ROWS || inner.width < MIN_W as u16 {
            frame.render_widget(
                Paragraph::new(format!(
                    "Resize to {}×{} for the colour picker.\nEsc: close picker",
                    MIN_W + 2,
                    ROWS + 2
                )),
                inner,
            );
            return false;
        }
        let w = clamp_width(i32::from(inner.width));
        self.painted_w = w;
        let wide = w as u16;
        let focus_style = Style::default()
            .fg(theme.title)
            .add_modifier(Modifier::BOLD);
        let plain = Style::default().fg(theme.foreground);
        let row = |dy: u16, width: u16| Rect::new(inner.x, inner.y + dy, width, 1);
        let on_field = self.focus == Focus::Field;
        frame.render_widget(
            Paragraph::new(format!(
                "Pick {} · {}",
                crate::live::terminal_text(&self.token),
                if on_field {
                    "arrows move the field"
                } else {
                    "editing hex"
                }
            ))
            .style(focus_style),
            row(0, inner.width),
        );
        let field = Rect::new(inner.x, inner.y + 1, wide, (GRID_H / 2) as u16);
        frame.render_widget(
            Paragraph::new(render_sv_field(self.h, self.s, self.v, i32::from(wide))),
            field,
        );
        let hue = row(1 + (GRID_H / 2) as u16, wide);
        frame.render_widget(Paragraph::new(render_hue_bar(self.h, i32::from(wide))), hue);
        self.presets = preset_colors(theme);
        let presets = row(hue.y - inner.y + 1, inner.width);
        let fit = ((usize::from(inner.width) + 1) / 3).min(self.presets.len());
        self.presets.truncate(fit);
        frame.render_widget(
            Paragraph::new(Line::from(
                self.presets
                    .iter()
                    .flat_map(|(_, hex)| {
                        let bg = hex_to_hsv(hex)
                            .map(|(h, s, v)| color(hsv_to_rgb(h, s, v)))
                            .unwrap_or(Color::Reset);
                        [Span::styled("  ", Style::default().bg(bg)), Span::raw(" ")]
                    })
                    .collect::<Vec<_>>(),
            )),
            presets,
        );
        let hex_row = row(presets.y - inner.y + 1, inner.width);
        frame.render_widget(
            Paragraph::new("      ")
                .style(Style::default().bg(color(hsv_to_rgb(self.h, self.s, self.v)))),
            Rect::new(hex_row.x, hex_row.y, SWATCH.min(inner.width), 1),
        );
        let hex = Rect::new(
            hex_row.x + (SWATCH + 1).min(inner.width),
            hex_row.y,
            HEX_BOX.min(inner.width.saturating_sub(SWATCH + 1)),
            1,
        );
        frame.render_widget(
            Paragraph::new(self.hex.text().to_owned()).style(if on_field {
                plain
            } else {
                focus_style
            }),
            hex,
        );
        if !on_field && hex.width > 0 {
            let cursor = crate::rendered_width(&self.hex.text()[..self.hex.cursor()]) as u16;
            frame.set_cursor_position((hex.x + cursor.min(hex.width - 1), hex.y));
        }
        let hint = row(hex_row.y - inner.y + 1, inner.width);
        frame.render_widget(
            Paragraph::new(
                self.message
                    .clone()
                    .unwrap_or_else(|| "Enter use · Esc cancel · Tab hex/field".into()),
            )
            .style(plain),
            hint,
        );
        self.hits = Some(Hits {
            field,
            hue,
            presets: Rect::new(
                presets.x,
                presets.y,
                (self.presets.len() * 3).min(usize::from(inner.width)) as u16,
                1,
            ),
            hex,
        });
        true
    }
}

/// The current theme's own colours, named: the palette you are most likely nudging
/// toward is the one already on screen. Dark presets only; nothing is invented.
pub fn preset_colors(theme: &crate::theme::Theme) -> Vec<(String, String)> {
    [
        ("background", theme.background),
        ("surface", theme.surface),
        ("primary", theme.primary),
        ("panel", theme.panel),
        ("tool_background", theme.tool_background),
        ("foreground", theme.foreground),
        ("title", theme.title),
        ("assistant_border", theme.assistant_border),
        ("thinking_text", theme.thinking_text),
        ("thinking_border", theme.thinking_border),
        ("tool_text", theme.tool_text),
        ("error", theme.error),
        ("footer_foreground", theme.footer_foreground),
        ("footer_background", theme.footer_background),
    ]
    .into_iter()
    .filter_map(|(name, c)| match c {
        Color::Rgb(r, g, b) => Some((name.to_owned(), format!("#{r:02x}{g:02x}{b:02x}"))),
        _ => None,
    })
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(line: &Line) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn hex_hsv_roundtrip_on_theme_colors() {
        for hex in [
            "#c9a24d", "#00ff41", "#0a0a0b", "#e8e4dc", "#ffffff", "#000000",
        ] {
            let (h, s, v) = hex_to_hsv(hex).unwrap();
            assert_eq!(hsv_to_hex(h, s, v), hex);
        }
    }

    #[test]
    fn hex_hsv_roundtrip_over_a_strided_rgb_cube() {
        for r in (0..=255).step_by(5) {
            for g in (0..=255).step_by(5) {
                for b in (0..=255).step_by(5) {
                    let hex = format!("#{r:02x}{g:02x}{b:02x}");
                    let (h, s, v) = hex_to_hsv(&hex).unwrap();
                    assert_eq!(hsv_to_hex(h, s, v), hex);
                }
            }
        }
    }

    #[test]
    fn hex_to_hsv_rejects_junk() {
        for bad in ["red", "#12345", "#12345g", "", "#1234567"] {
            assert!(hex_to_hsv(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn known_conversions_match_colorsys() {
        assert_eq!(hsv_to_hex(0.0, 1.0, 1.0), "#ff0000");
        assert_eq!(hsv_to_hex(1.0 / 3.0, 1.0, 1.0), "#00ff00");
        assert_eq!(hsv_to_hex(2.0 / 3.0, 1.0, 1.0), "#0000ff");
        assert_eq!(hsv_to_hex(1.0, 1.0, 1.0), "#ff0000", "hue wraps");
        assert_eq!(hsv_to_hex(0.3, -4.0, 9.0), "#ffffff", "s/v clamp");
        let (h, s, v) = hex_to_hsv("#808080").unwrap();
        assert_eq!((h, s), (0.0, 0.0));
        assert!((v - 128.0 / 255.0).abs() < 1e-12);
    }

    #[test]
    fn grid_corners_are_the_canonical_colors() {
        let (s, v) = cell_to_sv(0, 0, 0);
        assert_eq!(hsv_to_hex(0.0, s, v), "#ffffff");
        let (s, v) = cell_to_sv(GRID_W as i32 - 1, 0, 0);
        assert_eq!(hsv_to_hex(0.0, s, v), "#ff0000");
        let (s, v) = cell_to_sv(0, GRID_H as i32 - 1, 0);
        assert_eq!(hsv_to_hex(0.0, s, v), "#000000");
    }

    #[test]
    fn cell_sv_roundtrip_every_cell() {
        for col in 0..GRID_W {
            for row in 0..GRID_H {
                let (s, v) = cell_to_sv(col as i32, row as i32, 0);
                assert_eq!(sv_to_cell(s, v, 0), (col, row), "{col},{row}");
            }
        }
    }

    #[test]
    fn hue_cell_roundtrip() {
        for col in 0..HUE_W {
            assert_eq!(hue_to_cell(cell_to_hue(col as i32, 0), 0), col);
        }
    }

    #[test]
    fn out_of_range_clicks_clamp_instead_of_panicking() {
        assert_eq!(cell_to_sv(-5, -5, 0), cell_to_sv(0, 0, 0));
        assert_eq!(
            cell_to_sv(999, 999, 0),
            cell_to_sv(GRID_W as i32 - 1, GRID_H as i32 - 1, 0)
        );
    }

    const NARROW: [usize; 6] = [MIN_W, 12, 30, 36, 44, GRID_W];

    #[test]
    fn cell_sv_and_hue_roundtrip_at_every_painted_width() {
        for w in NARROW {
            for col in 0..w {
                for row in 0..GRID_H {
                    let (s, v) = cell_to_sv(col as i32, row as i32, w as i32);
                    assert_eq!(sv_to_cell(s, v, w as i32), (col, row), "{w}:{col},{row}");
                }
                assert_eq!(
                    hue_to_cell(cell_to_hue(col as i32, w as i32), w as i32),
                    col,
                    "hue {w}:{col}"
                );
            }
        }
    }

    #[test]
    fn the_field_is_exactly_w_columns_and_always_12_rows() {
        for w in NARROW {
            let lines = render_sv_field(0.6, 0.5, 0.5, w as i32);
            assert_eq!(lines.len(), GRID_H / 2, "{w}");
            assert!(lines.iter().all(|l| plain(l).chars().count() == w), "{w}");
            assert_eq!(plain(&render_hue_bar(0.6, w as i32)).chars().count(), w);
        }
    }

    #[test]
    fn a_bad_width_clamps_instead_of_panicking() {
        assert_eq!(clamp_width(0), GRID_W);
        assert_eq!(clamp_width(-4), MIN_W);
        assert_eq!(clamp_width(9999), GRID_W);
        assert!(
            render_sv_field(0.2, 0.3, 0.4, 0)
                .iter()
                .all(|l| plain(l).chars().count() == GRID_W)
        );
    }

    #[test]
    fn field_has_exactly_one_marker_and_hue_bar_one_selection() {
        let marks: String = render_sv_field(0.6, 0.5, 0.5, 0)
            .iter()
            .map(plain)
            .collect();
        assert_eq!(marks.matches('+').count(), 1);
        assert_eq!(plain(&render_hue_bar(0.5, 0)).matches('▼').count(), 1);
    }

    #[test]
    fn half_block_cell_carries_top_in_foreground_and_bottom_in_background() {
        let lines = render_sv_field(0.0, 0.0, 0.0, 0);
        // Column GRID_W-1, text row 0 is the pure-hue edge: top row 0 (v=1), bottom row 1.
        let span = &lines[0].spans[GRID_W - 1];
        assert_eq!(span.content.as_ref(), "▀");
        assert_eq!(span.style.fg, Some(Color::Rgb(255, 0, 0)));
        let (s, v) = cell_to_sv(GRID_W as i32 - 1, 1, 0);
        let [r, g, b] = hsv_to_rgb(0.0, s, v);
        assert_eq!(span.style.bg, Some(Color::Rgb(r, g, b)));
    }

    #[test]
    fn picker_starts_from_the_field_value_and_falls_back_to_grey() {
        let p = Picker::new("#c9a24d", "primary");
        assert_eq!(p.value(), "#c9a24d");
        assert_eq!(p.hex_text(), "#c9a24d");
        let p = Picker::new("not a colour", "primary");
        assert_eq!(p.value(), "#808080");
    }

    fn start() -> Picker {
        Picker::new("#c9a24d", "primary")
    }

    #[test]
    fn every_arrow_moves_its_own_axis() {
        // Saturated, non-grey start: each axis has room both ways and is read
        // directly, never through the hex (a hue turn on a grey changes nothing).
        for (code, axis, sign) in [
            (KeyCode::Right, 1, 1.0),
            (KeyCode::Left, 1, -1.0),
            (KeyCode::Up, 2, 1.0),
            (KeyCode::Down, 2, -1.0),
            (KeyCode::PageUp, 0, 1.0),
            (KeyCode::PageDown, 0, -1.0),
        ] {
            let mut p = start();
            let before = [p.hsv().0, p.hsv().1, p.hsv().2];
            assert_eq!(p.key(code), Pick::Pending);
            let after = [p.hsv().0, p.hsv().1, p.hsv().2];
            for i in 0..3 {
                if i == axis {
                    assert!((after[i] - before[i]) * sign > 0.0, "{code:?}");
                } else {
                    assert_eq!(after[i], before[i], "{code:?}");
                }
            }
            assert_eq!(p.hex_text(), p.value(), "arrows rewrite the hex box");
        }
    }

    #[test]
    fn nudges_clamp_and_hue_wraps() {
        let mut p = Picker::new("#ffffff", "x");
        p.key(KeyCode::Up);
        p.key(KeyCode::Left);
        assert_eq!(p.hsv(), (0.0, 0.0, 1.0));
        let mut p = Picker::new("#ff0000", "x");
        p.key(KeyCode::PageDown);
        assert!(p.hsv().0 > 0.9, "hue wraps below zero");
    }

    #[test]
    fn hex_box_is_the_escape_hatch_and_wins_on_accept() {
        let mut p = start();
        p.key(KeyCode::Tab);
        assert_eq!(p.focus(), Focus::Hex);
        for _ in 0..7 {
            p.key(KeyCode::Backspace);
        }
        for c in "#00ff41".chars() {
            p.key(KeyCode::Char(c));
        }
        assert_eq!(p.value(), "#00ff41", "a parsing hex drives the swatch live");
        assert_eq!(p.key(KeyCode::Enter), Pick::Accept("#00ff41".into()));
    }

    #[test]
    fn a_typed_hex_that_does_not_parse_is_refused_not_replaced_by_the_grid() {
        let mut p = start();
        p.key(KeyCode::Tab);
        p.key(KeyCode::Backspace);
        assert_eq!(p.key(KeyCode::Enter), Pick::Pending);
        assert_eq!(p.value(), "#c9a24d", "the grid colour was not touched");
        p.key(KeyCode::Char('z'));
        p.paste("zz");
        assert_eq!(p.hex_text(), "#c9a24", "non-hex input is bounded out");
    }

    #[test]
    fn arrows_in_the_hex_box_are_cursor_movement_but_up_down_still_nudge() {
        let mut p = start();
        p.key(KeyCode::Tab);
        let before = p.hsv();
        p.key(KeyCode::Left);
        assert_eq!(p.hsv(), before, "left moved the caret, not the colour");
        p.key(KeyCode::Up);
        assert!(p.hsv().2 > before.2);
    }

    #[test]
    fn escape_cancels_and_enter_accepts_the_grid_colour() {
        let mut p = start();
        assert_eq!(p.key(KeyCode::Esc), Pick::Cancel);
        p.key(KeyCode::Up);
        let Pick::Accept(hex) = p.key(KeyCode::Enter) else {
            panic!("accepts")
        };
        assert_eq!(hex, p.value());
        assert_ne!(hex, "#c9a24d");
    }
}
