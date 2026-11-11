//! Slash-command routes that need more than one view call. Each only opens an
//! existing reviewed control, optionally preselecting a row from the command's
//! argument. Nothing here applies a change; the composer draft is never touched.
use crate::{demo::Demo, model_controls::Kind};

impl Demo {
    pub(crate) fn open_sidecar(&mut self, arguments: &str) {
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_sidecar(arguments);
        }
    }
    pub(crate) fn open_monitor(&mut self, arguments: &str) {
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_monitor(arguments);
        }
    }
    pub(crate) fn open_models_preselected(&mut self, kind: Kind, wanted: &str) {
        self.open_models(kind);
        if let Some(chat) = self.live.as_mut() {
            chat.controls.preselect(wanted);
        }
    }
    pub(crate) fn open_backend_preselected(&mut self, wanted: &str) {
        self.open_backend();
        if let Some(chat) = self.live.as_mut() {
            chat.backend_controls.preselect(wanted);
        }
    }
    pub(crate) fn open_resume(&mut self, prefix: &str) {
        self.open_browser();
        if let Some(chat) = self.live.as_mut() {
            chat.browser.preselect(prefix);
        }
    }
    /// resolve / continue go to the reviewed held-input control, new to the
    /// reviewed Claude-session reset, status to the read-only inspector.
    pub(crate) fn open_claude(&mut self, verb: &str) -> std::io::Result<()> {
        match verb.trim().to_ascii_lowercase().as_str() {
            verb @ ("resolve" | "continue") => {
                self.open_queue();
                if self.has_live_queue() {
                    self.notice = format!(
                        "/claude {verb} opened held input. Review and {verb} entries there; the command itself changed nothing."
                    );
                }
            }
            "new" => {
                self.open_settings();
                if let Some(chat) = self.live.as_mut()
                    && chat.settings.open
                {
                    chat.settings.open_claude_reset();
                }
            }
            _ => {
                self.open_settings();
                if let Some(chat) = self.live.as_mut()
                    && chat.settings.open
                {
                    chat.settings.open_runtime_status();
                }
            }
        }
        Ok(())
    }
    /// Bare `/mcp` is the F10 `M` panel; `list` opens it without review or runtime tab.
    pub(crate) fn open_mcp(&mut self, verb: &str) {
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_mcp(!verb.trim().is_empty());
        }
    }
    /// `/modelcfg` opens the reviewed current-model preferences editor, as F10 `P` does.
    pub(crate) fn open_model_preferences(&mut self) {
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_model_preferences();
        }
    }
    /// Alt+S: the one-key route to the scroll-lock (`autoscroll`) device preference. It opens the
    /// same reviewed F10 U control on that field; nothing is changed until it is reviewed and
    /// confirmed there, and, like F10, it waits for idle work.
    pub fn open_autoscroll_preference(&mut self) {
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_presentation("autoscroll");
            self.notice =
                "Scroll lock: Left/Right changes Follow new replies, Enter reviews it before saving."
                    .into();
        }
    }
    pub(crate) fn open_help(&mut self, keys_first: bool) {
        if self.has_live_input() || self.live.is_none() {
            return;
        }
        self.help.open(
            self.live
                .as_ref()
                .and_then(|chat| chat.commands.catalog.as_ref()),
            keys_first,
        );
        self.themes_open = false;
        self.completion.dismiss();
        self.footer.leave();
    }
    /// Display only, like the source command: no history, row or provider state changes.
    pub(crate) fn clear_screen(&mut self) {
        if self.has_live_input() {
            return;
        }
        let Some(chat) = self.live.as_mut() else {
            return;
        };
        match chat.clear_screen() {
            Ok(()) => {
                self.notice = "Screen cleared. The conversation is unchanged - the model still has everything it had a moment ago.".into();
                self.scroll = usize::MAX;
                self.transcript_selection.clear();
                self.compact_display.clear_hits();
            }
            Err(reason) => self.notice = reason.into(),
        }
    }
}
