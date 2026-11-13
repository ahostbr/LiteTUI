//! Terminal preview, wire primitives and an explicit one-prompt live child path.
pub mod approvals;
mod cancellation;
pub mod clipboard;
pub mod color_picker;
pub mod compact_display;
pub mod completion;
pub mod composer;
pub mod demo;
pub mod dictation;
pub mod footer;
pub mod live;
pub mod live_chat;
mod live_process;
pub mod management;
pub mod mark;
pub mod midturn;
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

pub mod backend_controls;
pub mod compaction_settings;
pub mod connection_preferences;
pub mod picker_box;
pub mod runtime_launch;
pub mod settings_editor;
pub mod settings_form;
pub mod settings_picker;
pub mod settings_view;

pub mod calendar_dialog;
pub mod chat_pause;
pub mod claude_reset;
pub mod command_palette;
mod command_routes;
pub mod help_view;
pub mod hooks_dialog;
pub mod keys_panel;
pub mod markdown;
pub mod prepared_skill;
pub mod tool_list_view;

pub mod engine_config;
mod engine_config_view;
pub mod engine_controls;

pub mod conversation_actions;
pub mod conversation_compact;
pub mod conversation_rewrite;

pub mod theme_controls;

pub mod agent_preferences;
pub mod context;
pub mod context_edit;
mod context_edit_view;
pub mod context_view;
pub mod glassbox_controls;
pub mod history;
pub mod history_view;
pub mod hook_edit;
pub mod hook_edit_form;
pub mod hook_edit_view;
pub mod hook_repair_editor;
pub mod hook_toggle;
pub mod hook_toggle_view;
pub mod image;
pub mod inspection;
pub mod mcp_config;
pub mod mcp_lifecycle;
pub mod model_lifecycle;
pub mod model_preferences;
pub mod monitor_controls;
pub mod playback;
pub mod plugin_reload;
pub mod presentation;
pub mod presentation_preferences;
pub mod sidecar_controls;
pub mod transcript_selection;
pub mod voice_actions;
pub mod voice_preferences;

pub mod work_lifecycle;

pub mod work_protocol;

pub mod work_view;

pub mod deferred_control;
pub mod scheduled_transition;

pub mod work_controls;
pub mod work_execution_controls;

pub mod runtime_controls;
pub mod runtime_controls_view;
