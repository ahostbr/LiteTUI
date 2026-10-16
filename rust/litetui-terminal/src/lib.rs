//! Offline preview and wire primitives. No runtime process or tool execution.
pub mod clipboard;
pub mod completion;
pub mod composer;
pub mod demo;
pub mod footer;
pub mod protocol;
pub mod stream;
pub mod theme;
pub mod transport;
pub mod ui;

mod word_chars;

// Ratatui draws individual graphemes: whole-string width can be smaller
// (Arabic lam-alef). All layout accounting must match the rendered cells.
pub(crate) fn rendered_width(text: &str) -> usize {
    use unicode_segmentation::UnicodeSegmentation;
    text.graphemes(true)
        .map(unicode_width::UnicodeWidthStr::width)
        .sum()
}
