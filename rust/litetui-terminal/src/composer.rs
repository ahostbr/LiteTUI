//! Single-line editing state. No submission, clipboard, runtime or filesystem I/O.
//! Cursor steps are Unicode scalar values, matching Python string indexing in
//! Textual Input, not bytes or grapheme clusters. Limits are caller policy.
use std::{collections::VecDeque, ops::Range};

#[derive(Clone, Copy, Debug)]
pub enum Motion {
    Left,
    Right,
    WordLeft,
    WordRight,
    Home,
    End,
}
#[derive(Clone, Copy, Debug)]
pub enum Delete {
    Left,
    Right,
    WordLeft,
    WordRight,
    ToStart,
    ToEnd,
}

#[derive(Debug)]
pub struct Composer {
    text: String,
    cursor: usize,
    anchor: usize,
    max_chars: usize,
    max_history: usize,
    history: VecDeque<String>,
    history_index: Option<usize>,
    draft: String,
}

impl Composer {
    /// Zero disables the corresponding capacity; the preview's bounds are not
    /// production defaults. Cursor and anchor are always UTF-8 boundaries.
    pub fn new(max_chars: usize, max_history: usize) -> Self {
        Self {
            text: String::new(),
            cursor: 0,
            anchor: 0,
            max_chars,
            max_history,
            history: VecDeque::new(),
            history_index: None,
            draft: String::new(),
        }
    }
    pub fn is_history_browsing(&self) -> bool {
        self.history_index.is_some()
    }
    pub fn text(&self) -> &str {
        &self.text
    }
    pub fn before_cursor(&self) -> &str {
        &self.text[..self.cursor]
    }
    fn selection(&self) -> Range<usize> {
        self.anchor.min(self.cursor)..self.anchor.max(self.cursor)
    }
    pub fn selected_text(&self) -> &str {
        &self.text[self.selection()]
    }
    pub fn selection_parts(&self) -> (&str, &str, &str) {
        let range = self.selection();
        (
            &self.text[..range.start],
            &self.text[range.clone()],
            &self.text[range.end..],
        )
    }
    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.anchor = 0;
    }
    fn sanitized(text: &str, limit: usize) -> String {
        text.chars()
            .filter_map(|ch| match ch {
                '\r' | '\n' | '\t' => Some(' '),
                ch if ch.is_control() => None,
                ch => Some(ch),
            })
            .take(limit)
            .collect()
    }
    pub fn insert(&mut self, text: &str) {
        self.replace_selection(text, false);
    }
    /// Focused Textual Input consumes Paste before it bubbles to the App:
    /// first Python splitlines line only; an empty first line deletes selection.
    /// Empty events do nothing. Existing preview control filtering still applies.
    pub fn paste_bracketed(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        let line = text
            .split([
                '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
                '\u{2029}',
            ])
            .next()
            .unwrap_or("");
        self.replace_selection(line, true);
    }
    /// The application's priority Ctrl+V path joins nonblank CR/LF lines and
    /// inserts at the ACTIVE cursor, preserving (not replacing) selected text.
    /// Returns inserted scalar count after the preview's bounds/filtering.
    pub fn paste_clipboard(&mut self, text: &str) -> usize {
        let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
        let joined = if normalized.contains('\n') {
            normalized
                .split('\n')
                .filter(|line| {
                    line.chars()
                        .any(|ch| !(ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')))
                })
                .collect::<Vec<_>>()
                .join(" ")
        } else {
            normalized
        };
        let remaining = self.max_chars.saturating_sub(self.text.chars().count());
        let inserted = Self::sanitized(&joined, remaining);
        if inserted.is_empty() {
            return 0;
        }
        let count = inserted.chars().count();
        self.text.insert_str(self.cursor, &inserted);
        self.cursor += inserted.len();
        self.anchor = self.cursor;
        count
    }
    fn replace_selection(&mut self, text: &str, replace_empty: bool) {
        let range = self.selection();
        let retained =
            self.text[..range.start].chars().count() + self.text[range.end..].chars().count();
        let inserted = Self::sanitized(text, self.max_chars.saturating_sub(retained));
        if inserted.is_empty() && !replace_empty {
            return;
        }
        self.text.replace_range(range.clone(), &inserted);
        self.cursor = range.start + inserted.len();
        self.anchor = self.cursor;
    }
    fn left(&self) -> usize {
        self.text[..self.cursor]
            .char_indices()
            .next_back()
            .map_or(0, |(index, _)| index)
    }
    fn right(&self) -> usize {
        self.cursor
            + self.text[self.cursor..]
                .chars()
                .next()
                .map_or(0, char::len_utf8)
    }
    // Textual searches (?<=\W)\w on the prefix/suffix, not the whole line.
    // A word already starting at the cursor is therefore skipped on Right.
    fn word_starts(text: &str) -> impl Iterator<Item = usize> + '_ {
        text.chars()
            .zip(text.char_indices().skip(1))
            .filter(|(previous, (_, next))| {
                !crate::word_chars::is_word(*previous) && crate::word_chars::is_word(*next)
            })
            .map(|(_, (index, _))| index)
    }
    fn word_left(&self) -> usize {
        Self::word_starts(&self.text[..self.cursor])
            .last()
            .unwrap_or(0)
    }
    fn word_right(&self) -> usize {
        Self::word_starts(&self.text[self.cursor..])
            .next()
            .map_or(self.text.len(), |index| self.cursor + index)
    }
    pub fn move_cursor(&mut self, motion: Motion, select: bool) {
        let range = self.selection();
        let target = match motion {
            Motion::Left if !select && !range.is_empty() => range.start,
            Motion::Right if !select && !range.is_empty() => range.end,
            Motion::WordLeft => self.word_left(),
            Motion::WordRight => self.word_right(),
            Motion::Left => self.left(),
            Motion::Right => self.right(),
            Motion::Home => 0,
            Motion::End => self.text.len(),
        };
        if !select {
            self.anchor = target;
        } else if matches!(motion, Motion::Home | Motion::End) {
            self.anchor = self.cursor;
        }
        self.cursor = target;
    }
    pub fn select_all(&mut self) {
        self.anchor = 0;
        self.cursor = self.text.len();
    }
    pub fn delete(&mut self, direction: Delete) {
        let mut range = self.selection();
        if range.is_empty() {
            range = match direction {
                Delete::WordLeft => self.word_left()..self.cursor,
                Delete::WordRight => self.cursor..self.word_right(),
                Delete::Left => self.left()..self.cursor,
                Delete::Right => self.cursor..self.right(),
                Delete::ToStart => 0..self.cursor,
                Delete::ToEnd => self.cursor..self.text.len(),
            };
        }
        self.text.replace_range(range.clone(), "");
        self.cursor = range.start;
        self.anchor = self.cursor;
    }
    pub fn push_history(&mut self, text: &str) {
        let text = Self::sanitized(text, self.max_chars);
        if self.max_history > 0 && !text.is_empty() && self.history.back() != Some(&text) {
            if self.history.len() == self.max_history {
                self.history.pop_front();
            }
            self.history.push_back(text);
        }
        self.history_index = None;
        self.draft.clear();
    }
    fn recall(&mut self, text: String) {
        self.text = text;
        self.cursor = self.text.len();
        self.anchor = self.cursor;
    }
    pub fn history_previous(&mut self) -> bool {
        let index = match self.history_index {
            Some(0) => return false,
            Some(index) => index - 1,
            None if self.history.is_empty() => return false,
            None => {
                self.draft = self.text.clone();
                self.history.len() - 1
            }
        };
        self.history_index = Some(index);
        self.recall(self.history[index].clone());
        true
    }
    pub fn history_next(&mut self) -> bool {
        let Some(index) = self.history_index else {
            return false;
        };
        if index + 1 < self.history.len() {
            self.history_index = Some(index + 1);
            self.recall(self.history[index + 1].clone());
        } else {
            self.history_index = None;
            let draft = std::mem::take(&mut self.draft);
            self.recall(draft);
        }
        true
    }
}
