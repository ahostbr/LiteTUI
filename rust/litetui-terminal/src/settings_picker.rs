//! The /settings theme creator's colour picker: Python's `ColorPickerScreen`.
//!
//! Python's Themes tab opens `ColorPickerScreen` when a palette-token field
//! (`ct-<token>`) is clicked (settings_screen.SettingsBody.on_click): a
//! saturation/value field drawn with half blocks, a hue bar, the current theme's
//! colours as presets, a swatch, the hex box and Dock to side. The arithmetic and
//! the field and hue rendering are `color_picker`'s ports of `colorpicker.py`; this
//! is the dialog's state and keys as `ColorPickerBody` has them, and
//! `settings_form` draws it the way Textual lays out `#cp-box`. Accepting writes
//! the hex into the field's draft, as Python's `_picked` sets the Input's value.
use crate::color_picker::{GRID_H, GRID_W, HUE_W, cell_to_hue, cell_to_sv, hex_to_hsv, hsv_to_hex};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Python's #cp-hint; its row clips it to one line.
pub const HINT: &str = "Enter = use · Esc = cancel · arrows on the field · Tab for hex";
pub const DOCK: &str = "Dock to side";

/// What has focus: the field first (it is first in the DOM, so the arrows work), then
/// the hex box, then Dock to side.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Part {
    #[default]
    Field,
    Hex,
    Dock,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Open,
    /// close_dialog with the colour.
    Accept(String),
    Cancel,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Picker {
    /// The settings control the colour goes into, and the token it names.
    pub control: String,
    pub token: String,
    pub h: f64,
    pub s: f64,
    pub v: f64,
    /// The current theme's colours, (token, "#rrggbb"), in the form's order.
    pub presets: Vec<(String, String)>,
    /// The hex box (#cp-hex) and its caret.
    pub hex: String,
    pub cursor: usize,
    pub focus: Part,
    /// The width the field was last painted at; clicks and nudges map through it.
    pub painted: usize,
}
impl Picker {
    pub fn new(control: &str, token: &str, initial: &str, presets: Vec<(String, String)>) -> Self {
        let (h, s, v) = hex_to_hsv(initial).unwrap_or((0.0, 0.0, 0.5));
        let mut picker = Self {
            control: control.to_owned(),
            token: token.to_owned(),
            h,
            s,
            v,
            presets,
            hex: String::new(),
            cursor: 0,
            focus: Part::Field,
            painted: GRID_W,
        };
        picker.repaint();
        picker
    }
    pub fn value(&self) -> String {
        hsv_to_hex(self.h, self.s, self.v)
    }
    pub fn title(&self) -> String {
        if self.token.is_empty() {
            "Pick a color".into()
        } else {
            format!("Pick {}", self.token)
        }
    }
    /// ColorPickerBody._repaint: the hex box follows the colour unless it already says it.
    fn repaint(&mut self) {
        let value = self.value();
        if self.hex.trim().to_lowercase() != value {
            self.hex = value;
            self.cursor = self.hex.chars().count();
        }
    }
    /// action_nudge: one painted cell per press.
    fn nudge(&mut self, key: KeyCode) {
        let step = 1.0 / (self.painted.max(2) - 1) as f64;
        let vstep = 1.0 / (GRID_H - 1) as f64;
        let hstep = 1.0 / HUE_W as f64;
        match key {
            KeyCode::Left => self.s = (self.s - step).max(0.0),
            KeyCode::Right => self.s = (self.s + step).min(1.0),
            KeyCode::Up => self.v = (self.v + vstep).min(1.0),
            KeyCode::Down => self.v = (self.v - vstep).max(0.0),
            KeyCode::PageUp => self.h = (self.h + hstep).rem_euclid(1.0),
            KeyCode::PageDown => self.h = (self.h - hstep).rem_euclid(1.0),
            _ => return,
        }
        self.repaint();
    }
    /// action_accept: a typed hex that parses wins over the grid; one that does not
    /// leaves the grid's colour, as Python's does.
    fn accept(&mut self) -> Outcome {
        if let Ok((h, s, v)) = hex_to_hsv(&self.hex) {
            (self.h, self.s, self.v) = (h, s, v);
        }
        Outcome::Accept(self.value())
    }
    /// A click on the field (cell column, logical row) also focuses it, as Textual does.
    pub fn click_field(&mut self, col: u16, row: u16) {
        (self.s, self.v) = cell_to_sv(i32::from(col), i32::from(row), self.painted as i32);
        self.focus = Part::Field;
        self.repaint();
    }
    pub fn click_hue(&mut self, col: u16) {
        self.h = cell_to_hue(i32::from(col), self.painted as i32);
        self.repaint();
    }
    /// Each preset paints two cells and a gap.
    pub fn click_preset(&mut self, x: u16) {
        if let Some((h, s, v)) = self
            .presets
            .get(usize::from(x) / 3)
            .and_then(|(_, hex)| hex_to_hsv(hex).ok())
        {
            (self.h, self.s, self.v) = (h, s, v);
            self.repaint();
        }
    }
    pub fn click_hex(&mut self) {
        self.focus = Part::Hex;
        self.cursor = self.hex.chars().count();
    }
    pub fn accepts_paste(&self) -> bool {
        self.focus == Part::Hex
    }
    pub fn paste(&mut self, text: &str) {
        if self.focus != Part::Hex {
            return;
        }
        let insert: String = text.chars().filter(|c| !c.is_control()).take(64).collect();
        let at = byte(&self.hex, self.cursor);
        self.hex.insert_str(at, &insert);
        self.cursor += insert.chars().count();
    }
    /// The modal's keys: Esc cancels, Enter (a priority binding) uses the colour, Tab
    /// moves focus; the arrows drive the field unless the hex box owns them.
    pub fn key(&mut self, key: KeyEvent) -> Outcome {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Esc => return Outcome::Cancel,
            KeyCode::Enter => return self.accept(),
            KeyCode::Tab => {
                self.focus = match self.focus {
                    Part::Field => Part::Hex,
                    Part::Hex => Part::Dock,
                    Part::Dock => Part::Field,
                };
                return Outcome::Open;
            }
            KeyCode::BackTab => {
                self.focus = match self.focus {
                    Part::Field => Part::Dock,
                    Part::Hex => Part::Field,
                    Part::Dock => Part::Hex,
                };
                return Outcome::Open;
            }
            _ => {}
        }
        if self.focus == Part::Hex {
            // Textual's Input keeps its caret keys and PageUp/PageDown; Up/Down reach the picker.
            let len = self.hex.chars().count();
            self.cursor = self.cursor.min(len);
            match key.code {
                KeyCode::Char(c) if !ctrl && self.hex.len() < 64 => {
                    let at = byte(&self.hex, self.cursor);
                    self.hex.insert(at, c);
                    self.cursor += 1;
                }
                KeyCode::Backspace if self.cursor > 0 => {
                    self.cursor -= 1;
                    let at = byte(&self.hex, self.cursor);
                    self.hex.remove(at);
                }
                KeyCode::Delete if self.cursor < len => {
                    let at = byte(&self.hex, self.cursor);
                    self.hex.remove(at);
                }
                KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
                KeyCode::Right => self.cursor = (self.cursor + 1).min(len),
                KeyCode::Home => self.cursor = 0,
                KeyCode::End => self.cursor = len,
                KeyCode::Up | KeyCode::Down => self.nudge(key.code),
                _ => {}
            }
            return Outcome::Open;
        }
        // The field and the Dock control: every arrow and PageUp/PageDown nudges.
        self.nudge(key.code);
        Outcome::Open
    }
}
fn byte(text: &str, chars: usize) -> usize {
    text.char_indices()
        .nth(chars)
        .map_or(text.len(), |(i, _)| i)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::color_picker::hue_to_cell;

    #[test]
    fn matches_pythons_picker() {
        // Python, ConPTY capture target/oxide-parity/picker3/textual: #0178D4 opens as
        // "#0178d4" with the hue marker on cell 27; Up then Right then Enter gives "#007edf".
        let mut p = Picker::new("ct-primary", "primary", "#0178D4", Vec::new());
        assert_eq!(p.hex, "#0178d4");
        assert_eq!(hue_to_cell(p.h, 48), 27);
        assert_eq!(p.key(KeyEvent::from(KeyCode::Up)), Outcome::Open);
        p.key(KeyEvent::from(KeyCode::Right));
        assert_eq!(
            p.key(KeyEvent::from(KeyCode::Enter)),
            Outcome::Accept("#007edf".into())
        );
        assert_eq!(
            Picker::new("ct-x", "x", "red", Vec::new()).value(),
            "#808080"
        );
    }

    #[test]
    fn typed_hex_wins_and_tab_reaches_it() {
        let mut p = Picker::new("ct-accent", "accent", "#000000", Vec::new());
        p.key(KeyEvent::from(KeyCode::Tab));
        assert_eq!(p.focus, Part::Hex);
        for _ in 0..7 {
            p.key(KeyEvent::from(KeyCode::Backspace));
        }
        for c in "#ff8800".chars() {
            p.key(KeyEvent::from(KeyCode::Char(c)));
        }
        // Left/Right stay in the box; they do not nudge.
        p.key(KeyEvent::from(KeyCode::Left));
        assert_eq!(p.hex, "#ff8800");
        assert_eq!(
            p.key(KeyEvent::from(KeyCode::Enter)),
            Outcome::Accept("#ff8800".into())
        );
        // A hex that does not parse leaves the grid's colour.
        let mut p = Picker::new("ct-accent", "accent", "#000000", Vec::new());
        p.click_hex();
        p.paste("zz");
        assert_eq!(
            p.key(KeyEvent::from(KeyCode::Enter)),
            Outcome::Accept("#000000".into())
        );
        assert_eq!(p.key(KeyEvent::from(KeyCode::Esc)), Outcome::Cancel);
        p.key(KeyEvent::from(KeyCode::BackTab));
        assert_eq!(p.focus, Part::Field);
    }
}
