//! A small plain-text editor for a hook document the source's parser refuses.
//! It edits text only; the source's own validator decides whether the corrected text is a hook document.
use crate::{hook_edit::MAX_REPAIR, live::terminal_text};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

pub struct Editor {
    text: String,
    cursor: usize,
}

impl Editor {
    pub fn new(text: &str) -> Self {
        Self {
            text: text.replace("\r\n", "\n").replace('\r', "\n"),
            cursor: 0,
        }
    }
    pub fn text(&self) -> &str {
        &self.text
    }
    pub fn insert(&mut self, text: &str) -> Result<(), String> {
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        if text.contains('\0') {
            return Err("A hook document cannot contain a null character".into());
        }
        if self.text.len() + text.len() > MAX_REPAIR {
            return Err("The corrected document is at the 64 KiB native repair limit".into());
        }
        self.text.insert_str(self.cursor, &text);
        self.cursor += text.len();
        Ok(())
    }
    fn previous(&self, at: usize) -> usize {
        self.text[..at]
            .char_indices()
            .next_back()
            .map_or(0, |(i, _)| i)
    }
    fn next(&self, at: usize) -> usize {
        at + self.text[at..].chars().next().map_or(0, char::len_utf8)
    }
    fn line_start(&self, at: usize) -> usize {
        self.text[..at].rfind('\n').map_or(0, |i| i + 1)
    }
    fn line_end(&self, at: usize) -> usize {
        self.text[at..]
            .find('\n')
            .map_or(self.text.len(), |i| at + i)
    }
    /// The byte offset `column` characters into the line that starts at `start`, clamped to the line.
    fn column_to_offset(&self, start: usize, column: usize) -> usize {
        let end = self.line_end(start);
        self.text[start..end]
            .char_indices()
            .nth(column)
            .map_or(end, |(i, _)| start + i)
    }
    fn column(&self) -> usize {
        self.text[self.line_start(self.cursor)..self.cursor]
            .chars()
            .count()
    }
    /// Returns true when F6 asks the source to review the corrected text.
    pub fn key(&mut self, key: KeyEvent) -> Result<bool, String> {
        if key.kind != KeyEventKind::Press {
            return Ok(false);
        }
        let plain = key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT;
        match key.code {
            KeyCode::F(6) => return Ok(true),
            KeyCode::Left if plain => self.cursor = self.previous(self.cursor),
            KeyCode::Right if plain => self.cursor = self.next(self.cursor),
            KeyCode::Home if plain => self.cursor = self.line_start(self.cursor),
            KeyCode::End if plain => self.cursor = self.line_end(self.cursor),
            KeyCode::Up if plain => {
                let start = self.line_start(self.cursor);
                self.cursor = if start == 0 {
                    0
                } else {
                    let column = self.column();
                    self.column_to_offset(self.line_start(start - 1), column)
                };
            }
            KeyCode::Down if plain => {
                let end = self.line_end(self.cursor);
                self.cursor = if end == self.text.len() {
                    end
                } else {
                    let column = self.column();
                    self.column_to_offset(end + 1, column)
                };
            }
            KeyCode::Backspace if plain && self.cursor > 0 => {
                let before = self.previous(self.cursor);
                self.text.drain(before..self.cursor);
                self.cursor = before;
            }
            KeyCode::Delete if plain && self.cursor < self.text.len() => {
                let after = self.next(self.cursor);
                self.text.drain(self.cursor..after);
            }
            KeyCode::Enter if plain => self.insert("\n")?,
            KeyCode::Tab if key.modifiers.is_empty() => self.insert("  ")?,
            KeyCode::Char(ch) if plain => self.insert(&ch.to_string())?,
            _ => {}
        }
        Ok(false)
    }
    pub fn lines(&self, width: usize, height: usize) -> Vec<String> {
        if height == 0 {
            return Vec::new();
        }
        let before = terminal_text(&self.text[..self.cursor]);
        let after = terminal_text(&self.text[self.cursor..]);
        let shown = crate::ui::wrap(&format!("{before}│{after}"), width.max(1));
        let cursor_row = crate::ui::wrap(&before, width.max(1))
            .len()
            .saturating_sub(1);
        let start = (cursor_row + 1).saturating_sub(height);
        shown.into_iter().skip(start).take(height).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn press(editor: &mut Editor, code: KeyCode) -> bool {
        editor.key(KeyEvent::new(code, KeyModifiers::NONE)).unwrap()
    }
    fn type_text(editor: &mut Editor, text: &str) {
        for ch in text.chars() {
            press(editor, KeyCode::Char(ch));
        }
    }

    #[test]
    fn edits_lines_with_cursor_movement_and_unicode() {
        let mut editor = Editor::new("{\r\n  \"雪\": 1\r\n}");
        assert_eq!(editor.text(), "{\n  \"雪\": 1\n}");
        press(&mut editor, KeyCode::Down);
        press(&mut editor, KeyCode::End);
        press(&mut editor, KeyCode::Backspace);
        type_text(&mut editor, "2");
        press(&mut editor, KeyCode::Down);
        type_text(&mut editor, " ");
        press(&mut editor, KeyCode::Up);
        press(&mut editor, KeyCode::Home);
        press(&mut editor, KeyCode::Delete);
        assert_eq!(editor.text(), "{\n \"雪\": 2\n} ");
        press(&mut editor, KeyCode::Enter);
        press(&mut editor, KeyCode::Tab);
        assert!(
            editor.text().contains("\n\n   \"雪\""),
            "{:?}",
            editor.text()
        );
        press(&mut editor, KeyCode::Up);
        press(&mut editor, KeyCode::Up);
        press(&mut editor, KeyCode::Up);
        press(&mut editor, KeyCode::Up);
        press(&mut editor, KeyCode::Delete);
        assert!(editor.text().starts_with("\n\n"), "{:?}", editor.text());
    }

    #[test]
    fn f6_requests_review_and_bounds_hold() {
        let mut editor = Editor::new("{}");
        assert!(press(&mut editor, KeyCode::F(6)));
        assert!(editor.insert("a\0b").is_err());
        assert!(editor.insert(&"x".repeat(MAX_REPAIR)).is_err());
        assert_eq!(editor.text(), "{}");
        assert!(editor.insert(&"x".repeat(MAX_REPAIR - 2)).is_ok());
        assert!(editor.insert("y").is_err());
    }

    #[test]
    fn display_keeps_the_cursor_row_visible_and_escapes_controls() {
        let mut editor = Editor::new(&"line\n".repeat(30));
        for _ in 0..40 {
            press(&mut editor, KeyCode::Down);
        }
        editor.insert("\u{1b}[31mred").unwrap();
        let lines = editor.lines(40, 5);
        assert_eq!(lines.len(), 5);
        assert!(lines.last().unwrap().contains('│'));
        assert!(lines.iter().all(|l| !l.contains('\u{1b}')));
        assert!(Editor::new("").lines(10, 0).is_empty());
    }
}
