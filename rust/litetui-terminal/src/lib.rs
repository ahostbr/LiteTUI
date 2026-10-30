//! Terminal preview, wire primitives and an explicit one-prompt live child path.
pub mod approvals;
mod cancellation;
pub mod clipboard;
pub mod completion;
pub mod composer;
pub mod demo;
pub mod footer;
pub mod live;
pub mod live_chat;
mod live_process;
pub mod management;
pub mod model_controls;
pub mod native_activity;
pub mod profile_picker;
pub mod protocol;
pub mod questions;
pub mod runtime_state;
pub mod session;
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

// Shared by display escaping and its pre-layout work budget.
pub(crate) fn display_control(ch: char) -> bool {
    ch.is_control()
        || matches!(ch, '\u{061c}' | '\u{200e}' | '\u{200f}' | '\u{2028}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
}

pub mod tool_mode;

pub mod resume;

pub mod restored_queue;

pub mod conversation_browser;

pub mod runtime_launch;
