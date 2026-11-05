//! Deterministic, in-memory fixtures. Nothing in this module performs I/O.
use crate::completion::{Completion, CompletionKind, CompletionPicker};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ToolState {
    Success,
    Running,
    Awaiting,
    Denied,
    Error,
}

pub struct Tool {
    pub name: &'static str,
    pub summary: &'static str,
    pub arguments: &'static str,
    pub result: String,
    pub state: ToolState,
    pub expanded: bool,
}

pub struct Demo {
    pub live: Option<crate::live_chat::Chat>,
    pub tools: Vec<Tool>,
    pub selected_tool: usize,
    pub composer: crate::composer::Composer,
    pub completion: CompletionPicker,
    pub footer: crate::footer::FooterFocus,
    pub footer_fields: Vec<crate::footer::FooterField>,
    pub completion_candidates: Vec<Completion>,
    pub turns: Vec<String>,
    pub scroll: usize,
    pub max_scroll: usize,
    pub follow_selection: bool,
    pub transcript_selection: crate::transcript_selection::TranscriptSelection,
    pub compact_display: crate::compact_display::Profile,
    pub transcript_viewport: Option<(u16, u16)>,
    pub viewport_too_small: bool,
    pub command_quit: bool,
    pub approval_open: bool,
    pub approval_allow: bool,
    pub ticks: u64,
    pub notice: String,
    pub themes: Vec<crate::theme::Theme>,
    pub theme_index: usize,
    pub themes_open: bool,
    pub theme_query: String,
    pub theme_cursor: usize,
}

impl Default for Demo {
    fn default() -> Self {
        let themes = crate::theme::builtin_themes();
        let theme_index = themes
            .iter()
            .position(|theme| theme.name == "ash")
            .unwrap_or(0);
        Self {
            live: None,
            themes,
            theme_index,
            themes_open: false,
            theme_query: String::new(),
            theme_cursor: 0,
            tools: vec![
                Tool {
                    name: "grep",
                    summary: "Find the terminal entry point",
                    arguments: r#"{"pattern":"class LiteTUI", "path":"src/litetui"}"#,
                    result: "src/litetui/app.py:144: class LiteTUI(App)\n1 match in 1 file (illustrative fixture)".into(),
                    state: ToolState::Success,
                    expanded: false,
                },
                Tool {
                    name: "read",
                    summary: "Inspect the tool-call card",
                    arguments: r#"{"path":"src/litetui/widgets.py", "offset":1070, "limit":24}"#,
                    result: "class ToolMessage(FoldBlock):\n    # Expanded while live; folded when complete.\n    # Arguments, progress, result and elapsed time.".into(),
                    state: ToolState::Success,
                    expanded: true,
                },
                Tool {
                    name: "powershell",
                    summary: "Run the focused checks",
                    arguments: r#"{"command":"python -m pytest tests/test_tool_cards.py -q"}"#,
                    result: "Collecting fixture checks ...\nChecking collapsed and expanded tool cards ...".into(),
                    state: ToolState::Running,
                    expanded: false,
                },
                Tool {
                    name: "write",
                    summary: "Save the preview notes",
                    arguments: r#"{"path":"preview-notes.md", "content":"Tool-card layout notes"}"#,
                    result: "Waiting for your mock decision. F6 opens the approval dialog.".into(),
                    state: ToolState::Awaiting,
                    expanded: false,
                },
                Tool {
                    name: "web_fetch",
                    summary: "An example of a failed call",
                    arguments: r#"{"url":"https://example.invalid/preview"}"#,
                    result: "Fixture error: request timed out. No request was actually sent.\nThe error stays attached to this tool call; no silent retry.".into(),
                    state: ToolState::Error,
                    expanded: false,
                },
            ],
            selected_tool: 1,
            composer: crate::composer::Composer::new(500, 100),
            completion: CompletionPicker::default(),
            footer: crate::footer::FooterFocus::default(),
            footer_fields: {
                use crate::footer::{FieldKey as K, FooterField as F};
                vec![F::new(K::Percent, "12%"), F::new(K::Context, "24k/200k"),
                     F::new(K::Seat, "no seat"), F::new(K::Conversation, "preview"),
                     F::new(K::Model, "mock-model"), F::new(K::Thinking, "think:high"),
                     F::new(K::Plan, "plan:off")]
            },
            completion_candidates: [
                ("preview-help", "Fixture command; no dispatch", CompletionKind::Command),
                ("preview-replay", "Fixture command; no dispatch", CompletionKind::Command),
                ("preview-skill", "Fixture skill; never executed", CompletionKind::Skill),
            ].into_iter().map(|(name, description, kind)| Completion {
                name: name.into(), description: description.into(), kind,
            }).collect(),
            turns: Vec::new(),
            scroll: 0,
            max_scroll: 0,
            follow_selection: false,
            transcript_selection: Default::default(),
            compact_display: Default::default(),
            transcript_viewport: None,
            viewport_too_small: false,
            command_quit: false,
            approval_open: false,
            approval_allow: false,
            ticks: 0,
            notice: "Tab selects a tool. F2 expands it. F6 shows a mock approval.".into(),
        }
    }
}

impl Demo {
    pub fn live(backend: String, model: String) -> Self {
        let mut app = Self::default();
        app.tools.clear();
        app.selected_tool = 0;
        app.completion_candidates.clear();
        app.footer_fields.clear();
        app.composer = crate::composer::Composer::new(16384, 100);
        let chat = crate::live_chat::Chat::new(backend, model);
        app.notice = chat.notice.clone();
        app.live = Some(chat);
        app
    }
    pub fn observe_live(&mut self, observation: crate::live::Observation) -> std::io::Result<()> {
        // A rebased/replaced row must never inherit a press on an older rendered header.
        self.compact_display.clear_hits();
        let following = self.scroll >= self.max_scroll
            || matches!(&observation, crate::live::Observation::Opened(_));
        if matches!(
            observation,
            crate::live::Observation::Approval(crate::approvals::Event::Requested(_))
                | crate::live::Observation::Question {
                    event: crate::questions::Event::Requested(_),
                    ..
                }
        ) {
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
            if let Some(chat) = self.live.as_mut() {
                chat.runtime_controls.hide();
            }
        }
        let restore_caption = match &observation {
            crate::live::Observation::ImageRefused { prompt, .. }
                if self.composer.text().is_empty() =>
            {
                Some(prompt.clone())
            }
            _ => None,
        };
        let chat = self.live.as_mut().expect("live view");
        chat.observe(observation)?;
        chat.work.chat_pause.foreground(chat.busy);
        if let Some(draft) = chat.work.midturn.queued.take()
            && self.composer.text() == draft.text
        {
            self.composer.push_history(&draft.text);
            self.composer.clear();
        }
        if let Some((draft, text)) = chat.work.dictation.applied.take() {
            if self.composer.text() == draft.text
                && self.composer.before_cursor() == draft.before_cursor
                && self.composer.selected_text() == draft.selected
            {
                self.composer
                    .move_cursor(crate::composer::Motion::End, false);
                if !draft.text.is_empty() {
                    self.composer.insert(" ");
                }
                self.composer.insert(&text);
                chat.notice = "Dictation added to the composer. Edit it before sending.".into();
            } else {
                chat.notice =
                    "Composer changed during dictation. The current draft was kept.".into();
            }
        }
        if let Some(text) = restore_caption {
            self.composer.insert(&text);
        }

        self.notice = chat.notice.clone();
        self.footer_fields = if chat.ready {
            chat.runtime_state.footer_fields()
        } else {
            Vec::new()
        };
        if let Some(catalog) = &chat.commands.catalog {
            self.completion_candidates = catalog.completions();
        }
        let route = chat.commands.take_route();
        let route_arguments = chat.commands.take_route_arguments();
        if following {
            self.scroll = usize::MAX;
        }
        if let Some(action) = route {
            use crate::command_palette::Action;
            match action {
                Action::Work(destination) => self.show_work(Some(destination)),
                Action::Engine => {
                    self.open_engine();
                    if let Some(chat) = self.live.as_mut() {
                        chat.engine.select_command(&route_arguments);
                    }
                }
                Action::Plan => {
                    self.open_runtime_controls(crate::runtime_controls_view::Field::Plan);
                    let desired = match route_arguments.trim().to_ascii_lowercase().as_str() {
                        "on" => Some(true),
                        "off" => Some(false),
                        _ => None,
                    };
                    if let Some(chat) = self.live.as_mut() {
                        chat.runtime_controls.plan_choice(desired);
                    }
                }
                Action::ModelLoad | Action::ModelUnload | Action::ModelReconnect => {
                    self.open_model_lifecycle(
                        match action {
                            Action::ModelLoad => "load",
                            Action::ModelUnload => "unload",
                            _ => "reconnect",
                        },
                        route_arguments.trim(),
                    );
                }
                Action::ConversationCompact => {
                    self.open_history();
                    if let Some(chat) = self.live.as_mut() {
                        chat.saved_history.compact_command(&route_arguments);
                    }
                }
                Action::PluginReload => {
                    self.show_work(None);
                    if let Some(chat) = self.live.as_mut()
                        && chat.work.open
                    {
                        chat.work.open_plugin_reload(&route_arguments);
                    }
                }
                Action::ChatPause => self.open_chat_pause(),
                Action::Glassbox => self.open_glassbox(&route_arguments),
                Action::Models => self.open_models(crate::model_controls::Kind::Model),
                Action::Thinking => self.open_models(crate::model_controls::Kind::Thinking),
                Action::Backend => self.open_backend(),
                Action::Conversations => self.open_browser(),
                Action::Settings => self.open_settings(),
                Action::Themes => self.open_themes(),
                Action::ConversationNew => {
                    self.open_conversation_action(crate::conversation_actions::Action::New, "")?
                }
                Action::ConversationRename => self.open_conversation_action(
                    crate::conversation_actions::Action::Rename,
                    &route_arguments,
                )?,
                Action::Quit => self.command_quit = true,
                Action::Skills | Action::SkillPrepare => {}
            }
        }
        Ok(())
    }
    pub fn take_schedule_decision(&mut self) -> Option<(String, bool)> {
        let idle = !self.command_quit
            && !self.themes_open
            && !self.approval_open
            && !self.completion.is_open();
        self.live.as_mut()?.decide_schedule(idle)
    }
    pub fn has_live_question(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| !chat.questions.is_empty())
    }
    pub fn has_live_runtime_controls(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.runtime_controls.owns_input())
    }
    pub fn open_chat_pause(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.closed
                || chat.quitting
                || chat.cancelling()
                || chat.execution_pending()
                || chat.queue_pending()
                || chat.rewrite_pending()
                || chat.engine.pending()
                || chat.runtime_controls.pending()
            {
                self.notice = "Finish the pending action before changing chat pause.".into();
                return;
            }
            chat.work.review_chat_pause(chat.busy);
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn open_runtime_controls(&mut self, field: crate::runtime_controls_view::Field) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.closed
                || chat.quitting
                || chat.cancelling()
                || chat.execution_pending()
                || chat.queue_pending()
                || chat.rewrite_pending()
                || chat.engine.pending()
                || chat.runtime_controls.pending()
            {
                self.notice = "Finish the pending action before changing runtime controls.".into();
                return;
            }
            chat.runtime_controls.open(field);
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_controls(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.controls.owns_input())
    }
    pub fn open_models(&mut self, kind: crate::model_controls::Kind) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish the current turn before opening model controls.".into();
                return;
            }
            chat.controls.open(kind);
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_engine(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.engine.owns_input())
    }
    pub fn open_engine(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if chat.busy
                || chat.closed
                || chat.quitting
                || (chat.queue_pending() || chat.rewrite_pending())
                || (!chat.ready && chat.engine_admission.is_none())
            {
                self.notice = "Wait for the owned runtime before engine management.".into();
                return;
            }
            if !matches!(chat.backend.as_str(), "ninfer" | "strata") {
                self.notice = "Engine controls require NInfer or Strata.".into();
                return;
            }
            chat.engine.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_commands(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.commands.owns_input())
    }
    pub fn open_commands(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish current work before opening commands.".into();
                return;
            }
            chat.commands.open(false);
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_rewrite(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.rewrite.owns_input())
    }
    pub fn take_history_rewrite(&mut self) -> std::io::Result<()> {
        let Some(chat) = self.live.as_mut() else {
            return Ok(());
        };
        let Some((action, selection)) = chat.saved_history.take_rewrite() else {
            return Ok(());
        };
        if chat.busy
            || chat.closed
            || chat.quitting
            || chat.queue_pending()
            || chat.rewrite_pending()
        {
            return Err(std::io::Error::other(
                "Finish current work before edit/retry",
            ));
        }
        chat.rewrite.open(action, selection)?;
        chat.saved_history.suspend_for_rewrite();
        chat.browser.hide();
        self.themes_open = false;
        self.completion.dismiss();
        self.footer.leave();
        Ok(())
    }
    pub fn has_live_compact(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.compact.owns_input())
    }
    pub fn take_history_compact(&mut self) -> std::io::Result<()> {
        let Some(chat) = self.live.as_mut() else {
            return Ok(());
        };
        let Some(selection) = chat.saved_history.take_compact() else {
            return Ok(());
        };
        if chat.busy
            || chat.closed
            || chat.quitting
            || chat.queue_pending()
            || chat.rewrite_pending()
        {
            return Err(std::io::Error::other(
                "Finish current work before compaction",
            ));
        }
        chat.compact.open(selection)?;
        chat.compact
            .prefill(&chat.saved_history.take_compact_hint())?;
        chat.saved_history.suspend_for_rewrite();
        chat.browser.hide();
        self.themes_open = false;
        self.completion.dismiss();
        self.footer.leave();
        Ok(())
    }
    pub fn has_live_history(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.saved_history.owns_input())
    }
    pub fn open_history(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if chat.busy
                || chat.closed
                || chat.quitting
                || (chat.queue_pending() || chat.rewrite_pending())
                || chat.engine.pending()
            {
                self.notice = "Finish current work before opening Saved History.".into();
                return;
            }
            chat.saved_history.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    /// Route inspection into its read-only view without entering Open review.
    /// Keeping the browser beneath it preserves its exact page and selection.
    pub fn take_browser_intent(
        &mut self,
    ) -> std::io::Result<Option<crate::conversation_browser::Intent>> {
        use crate::conversation_browser::{Intent, Update};
        let Some(chat) = self.live.as_mut() else {
            return Ok(None);
        };
        let Some(intent) = chat.browser.take_intent() else {
            return Ok(None);
        };
        let Intent::Inspect { revision, session } = &intent else {
            return Ok(Some(intent));
        };
        let selected = chat
            .browser
            .page
            .as_ref()
            .ok_or_else(|| {
                std::io::Error::other("Refresh the conversation browser before inspecting")
            })
            .and_then(|page| page.inspection(*revision, session))
            .cloned();
        match selected {
            Ok(row) => {
                chat.browser.suspend_for_inspection();
                chat.saved_history
                    .inspect(*revision, session.clone(), row.current);
                self.themes_open = false;
                self.completion.dismiss();
                self.footer.leave();
            }
            Err(error) => chat.browser.observe(Update::Rejected(error.to_string()))?,
        }
        Ok(None)
    }
    pub fn has_live_themes(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.themes.owns_input())
    }
    pub fn has_live_work(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.work.owns_input())
    }
    pub fn open_dictation(&mut self) {
        if self.has_live_input() || self.viewport_too_small {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || chat.queue_pending()
                || chat.rewrite_pending()
                || chat.engine.pending()
            {
                self.notice = "Finish the current reply before dictating.".into();
                return;
            }
            chat.work.dictate(crate::dictation::Draft {
                text: self.composer.text().into(),
                before_cursor: self.composer.before_cursor().into(),
                selected: self.composer.selected_text().into(),
            });
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn open_image(&mut self) {
        if self.has_live_input() || self.viewport_too_small {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || chat.queue_pending()
                || chat.rewrite_pending()
                || chat.engine.pending()
            {
                self.notice = "Wait for the current turn before selecting an image.".into();
                return;
            }
            chat.work.open_image();
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn open_work(&mut self) {
        self.show_work(None);
    }
    fn show_work(&mut self, destination: Option<crate::work_view::Destination>) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.closed
                || chat.quitting
                || chat.queue_pending()
                || chat.rewrite_pending()
                || chat.engine.pending()
            {
                self.notice = "Wait for the owned runtime before opening Work.".into();
                return;
            }
            if let Some(destination) = destination {
                chat.work.open_at(destination);
            } else {
                chat.work.open();
            }
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_settings(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.settings.owns_input())
    }
    fn open_model_lifecycle(&mut self, action: &str, model: &str) {
        if self.has_live_input() {
            return;
        }
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            chat.settings.open_model_lifecycle(Some((action, model)));
        }
    }
    fn open_glassbox(&mut self, arguments: &str) {
        if self.has_live_input() {
            return;
        }
        self.open_settings();
        if let Some(chat) = self.live.as_mut()
            && chat.settings.open
        {
            let action = match arguments.trim().to_ascii_lowercase().as_str() {
                "start" | "on" => Some("start"),
                "stop" | "off" => Some("stop"),
                _ => None,
            };
            chat.settings.open_glassbox(action);
        }
    }
    pub fn open_settings(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish current work before reading settings.".into();
                return;
            }
            chat.settings.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_backend(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.backend_controls.owns_input())
    }
    pub fn open_backend(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish current work before changing backend.".into();
                return;
            }
            if chat
                .runtime_state
                .startup()
                .and_then(|state| state.held_restored_input)
                .is_some_and(|count| count > 0)
            {
                self.notice = "Resolve held input before changing backend.".into();
                return;
            }
            chat.backend_controls.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_browser(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.browser.owns_input())
    }
    pub fn open_browser(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish the current turn before browsing conversations.".into();
                return;
            }
            chat.browser.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn open_conversation_action(
        &mut self,
        action: crate::conversation_actions::Action,
        title: &str,
    ) -> std::io::Result<()> {
        if self.has_live_input() {
            return Ok(());
        }
        if let Some(chat) = self.live.as_mut() {
            if !chat.ready
                || chat.busy
                || chat.closed
                || chat.quitting
                || (chat.queue_pending() || chat.rewrite_pending())
                || chat.engine.pending()
            {
                self.notice = "Finish current work before changing conversations.".into();
                return Ok(());
            }
            chat.browser.actions.open_prefilled(action, title)?;
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
        Ok(())
    }
    pub fn has_live_queue(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| chat.queue.owns_input())
    }
    pub fn open_queue(&mut self) {
        if self.has_live_input() {
            return;
        }
        if let Some(chat) = self.live.as_mut() {
            if (!chat.ready && chat.engine_admission.is_none())
                || chat.busy && !chat.uncertainty_only_busy()
                || chat.closed
                || chat.quitting
                || ((chat.queue_pending() || chat.rewrite_pending()) || chat.engine.pending())
            {
                self.notice = "Finish the current turn before opening held input.".into();
                return;
            }
            chat.queue.open();
            self.themes_open = false;
            self.completion.dismiss();
            self.footer.leave();
        }
    }
    pub fn has_live_input(&self) -> bool {
        self.has_live_runtime_controls()
            || self.has_live_engine()
            || self.has_live_commands()
            || self.has_live_themes()
            || self.has_live_history()
            || self.has_live_rewrite()
            || self.has_live_compact()
            || self.has_live_work()
            || self.has_live_settings()
            || self.has_live_backend()
            || self.has_live_browser()
            || self.has_live_controls()
            || self.has_live_queue()
            || self.has_live_approval()
            || self.has_live_question()
            || self.stop_open()
            || self.live.as_ref().is_some_and(|chat| chat.cancelling())
    }
    pub fn stop_open(&self) -> bool {
        self.live.as_ref().is_some_and(|chat| chat.stop_confirm)
    }
    pub fn open_stop(&mut self) {
        if let Some(chat) = self.live.as_mut() {
            chat.queue.hide();
            chat.open_stop();
            if chat.stop_confirm {
                self.themes_open = false;
                self.completion.dismiss();
                self.footer.leave();
            }
        }
    }
    pub fn has_live_approval(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|chat| !chat.approvals.is_empty())
    }
    pub fn finish_live(&mut self, failure: Option<String>) {
        let chat = self.live.as_mut().expect("live view");
        chat.finish(failure);
        self.notice = chat.notice.clone();
        self.footer_fields.clear();
    }
    pub fn palette(&self) -> &crate::theme::Theme {
        self.live
            .as_ref()
            .and_then(|c| c.effective_theme.as_ref())
            .unwrap_or(&self.themes[self.theme_index])
    }

    pub fn filtered_themes(&self) -> Vec<usize> {
        let query = self.theme_query.to_lowercase();
        self.themes
            .iter()
            .enumerate()
            .filter(|(_, theme)| theme.name.to_lowercase().contains(&query))
            .map(|(index, _)| index)
            .collect()
    }

    pub fn open_themes(&mut self) {
        if self.live.is_some() {
            if self.has_live_input() {
                return;
            }
            let chat = self.live.as_mut().unwrap();
            if chat.closed
                || chat.quitting
                || chat.busy
                || (chat.queue_pending() || chat.rewrite_pending())
                || chat.engine.pending()
                || (!chat.ready && chat.engine_admission.is_none())
            {
                self.notice = "Finish current work before opening themes.".into();
                return;
            }
            chat.themes.open();
            self.completion.dismiss();
            self.footer.leave();
            return;
        }
        self.footer.leave();
        if self.approval_open {
            return;
        }
        self.themes_open = true;
        self.theme_query.clear();
        self.theme_cursor = self.theme_index;
    }

    pub fn search_themes(&mut self, text: &str) {
        let remaining = 80usize.saturating_sub(self.theme_query.chars().count());
        self.theme_query
            .extend(text.chars().filter(|ch| !ch.is_control()).take(remaining));
        self.theme_cursor = 0;
    }

    pub fn move_theme(&mut self, forward: bool) {
        let count = self.filtered_themes().len();
        if count == 0 {
            self.theme_cursor = 0;
            return;
        }
        self.theme_cursor = self.theme_cursor.min(count - 1);
        self.theme_cursor = if forward {
            (self.theme_cursor + 1) % count
        } else {
            (self.theme_cursor + count - 1) % count
        };
    }

    pub fn choose_theme(&mut self) {
        let matches = self.filtered_themes();
        if let Some(&index) = matches.get(self.theme_cursor.min(matches.len().saturating_sub(1))) {
            self.theme_index = index;
            self.themes_open = false;
            self.notice = format!("Theme: {} · local only, not saved", self.palette().name);
        }
    }

    pub fn next_tool(&mut self) {
        if self.tools.is_empty() {
            return;
        }
        self.selected_tool = (self.selected_tool + 1) % self.tools.len();
        self.follow_selection = true;
    }

    pub fn previous_tool(&mut self) {
        if self.tools.is_empty() {
            return;
        }
        self.selected_tool = (self.selected_tool + self.tools.len() - 1) % self.tools.len();
        self.follow_selection = true;
    }

    pub fn toggle_tool(&mut self) {
        if self.tools.is_empty() {
            return;
        }
        self.tools[self.selected_tool].expanded = !self.tools[self.selected_tool].expanded;
        self.follow_selection = true;
    }

    pub fn scroll_up(&mut self, amount: usize) {
        self.scroll = self.scroll.saturating_sub(amount);
    }

    pub fn scroll_down(&mut self, amount: usize) {
        self.scroll = self.scroll.saturating_add(amount).min(self.max_scroll);
    }

    pub fn open_approval(&mut self) {
        if self.live.is_some() {
            self.notice = "Tools are disabled; live approval UI is not wired yet.".into();
            return;
        }
        self.footer.leave();
        if !self.themes_open && self.tools[3].state == ToolState::Awaiting {
            self.approval_open = true;
            self.approval_allow = false;
        }
    }

    pub fn resolve_approval(&mut self, allow: bool) {
        if !self.approval_open || self.tools[3].state != ToolState::Awaiting {
            return;
        }
        self.tools[3].state = if allow {
            ToolState::Success
        } else {
            ToolState::Denied
        };
        self.tools[3].result = if allow {
            "Allowed (simulated). No file was written."
        } else {
            "Denied (simulated). No file was written."
        }
        .into();
        self.tools[3].expanded = true;
        self.selected_tool = 3;
        self.follow_selection = true;
        self.approval_open = false;
        self.notice = self.tools[3].result.clone();
    }

    pub fn tick(&mut self, steps: u64) {
        if self.live.is_some() {
            return;
        }
        self.ticks = self.ticks.saturating_add(steps);
        if self.ticks >= 80 && self.tools[2].state == ToolState::Running {
            self.tools[2].state = ToolState::Success;
            self.tools[2].result = "........................  [100%]\n24 passed in 1.28s (fixture output, not a real test run)".into();
        }
    }

    pub fn sync_completion(&mut self) {
        self.completion
            .refilter(&self.completion_candidates, self.composer.text());
        if self.completion.is_open() {
            self.footer.leave();
        }
    }
    pub fn accept_completion(&mut self) {
        let Some(name) = self
            .completion
            .current()
            .map(|candidate| candidate.name.clone())
        else {
            return;
        };
        self.composer.clear();
        self.insert_text(&format!("/{name} "));
        self.completion.dismiss();
    }
    pub fn paste_text(&mut self, text: &str) {
        self.footer.leave();
        let before = self.composer.text().to_owned();
        self.composer.paste_bracketed(text);
        if self.composer.text() != before {
            self.sync_completion();
        }
    }
    pub fn insert_text(&mut self, text: &str) {
        let before = self.composer.text().to_owned();
        self.composer.insert(text);
        if self.composer.text() != before {
            self.sync_completion();
        }
    }

    pub fn submit(&mut self) {
        self.submit_chord(false);
    }
    pub fn submit_chord(&mut self, alternate: bool) {
        if self.live.is_some() && self.composer.text().trim().eq_ignore_ascii_case("/pause") {
            self.open_chat_pause();
            return;
        }
        if self.live.as_ref().is_some_and(|chat| chat.busy) && !self.has_live_input() {
            let text = self.composer.text();
            match self
                .live
                .as_mut()
                .expect("live")
                .review_midturn(text, alternate)
            {
                Ok(()) => self.completion.dismiss(),
                Err(err) => self.notice = err.to_string(),
            }
            return;
        }
        let text = if self.live.is_some() {
            self.composer.text().to_string()
        } else {
            self.composer.text().trim().to_string()
        };
        let attached = self
            .live
            .as_ref()
            .is_some_and(|chat| chat.work.image.attached.is_some());
        let text = if attached && text.trim().is_empty() {
            "Describe this image.".into()
        } else {
            text
        };
        if text.trim().is_empty() {
            return;
        }
        if attached && text.trim_start().starts_with('/') {
            self.notice = "Clear the selected image before running a slash command.".into();
            return;
        }
        if text.trim_start().starts_with('/') && self.live.is_some() {
            if !self.has_live_input()
                && let Some(chat) = self.live.as_mut()
            {
                if chat.ready
                    && !(chat.busy
                        || chat.closed
                        || chat.quitting
                        || chat.queue_pending()
                        || chat.rewrite_pending()
                        || chat.engine.pending())
                {
                    chat.commands.raw(&text);
                    self.completion.dismiss();
                    self.footer.leave();
                } else {
                    self.notice = "Finish current work before resolving commands.".into();
                }
            }
            return;
        }
        if let Some(chat) = &mut self.live {
            if let Err(error) = chat.submit_composer(&text) {
                self.notice = error.to_string();
                return;
            }
            self.notice = chat.notice.clone();
            self.composer.push_history(&text);
            self.composer.clear();
            self.scroll = usize::MAX;
            return;
        }
        self.composer.push_history(&text);
        self.turns.push(text);
        if self.turns.len() > 6 {
            self.turns.remove(0);
        }
        self.composer.clear();
        self.sync_completion();
        self.scroll = usize::MAX;
        self.notice = "Local demo turn added. No model was contacted.".into();
    }

    pub fn reset(&mut self) {
        if self.live.is_some() {
            self.notice = "Live runtime restart is not wired; Ctrl+Q exits cleanly.".into();
            return;
        }
        let themes = std::mem::take(&mut self.themes);
        let theme_index = self.theme_index;
        *self = Self {
            themes,
            theme_index,
            ..Self::default()
        };
    }
}
