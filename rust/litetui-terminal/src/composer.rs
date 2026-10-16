//! Single-line editing state. No submission, clipboard, runtime or filesystem I/O.
//! Cursor steps are Unicode scalar values, matching Python string indexing in
//! Textual Input, not bytes or grapheme clusters. Limits are caller policy.
use std::{collections::VecDeque, ops::Range};

#[derive(Clone, Copy, Debug)]
pub enum Motion {
    Left,
    Right,
    Home,
    End,
}
#[derive(Clone, Copy, Debug)]
pub enum Delete {
    Left,
    Right,
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
        let range = self.selection();
        let retained =
            self.text[..range.start].chars().count() + self.text[range.end..].chars().count();
        let inserted = Self::sanitized(text, self.max_chars.saturating_sub(retained));
        if inserted.is_empty() {
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
    pub fn move_cursor(&mut self, motion: Motion, select: bool) {
        let range = self.selection();
        let target = match motion {
            Motion::Left if !select && !range.is_empty() => range.start,
            Motion::Right if !select && !range.is_empty() => range.end,
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
