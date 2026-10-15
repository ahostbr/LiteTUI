//! Deterministic, in-memory fixtures. Nothing in this module performs I/O.
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
    pub tools: Vec<Tool>,
    pub selected_tool: usize,
    pub input: String,
    pub turns: Vec<String>,
    pub scroll: usize,
    pub max_scroll: usize,
    pub follow_selection: bool,
    pub viewport_too_small: bool,
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
            input: String::new(),
            turns: Vec::new(),
            scroll: 0,
            max_scroll: 0,
            follow_selection: false,
            viewport_too_small: false,
            approval_open: false,
            approval_allow: false,
            ticks: 0,
            notice: "Tab selects a tool. Ctrl+E expands it. F6 shows a mock approval.".into(),
        }
    }
}

impl Demo {
    pub fn palette(&self) -> &crate::theme::Theme {
        &self.themes[self.theme_index]
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
            self.notice = format!("Theme: {} · preview only, not saved", self.palette().name);
        }
    }

    pub fn next_tool(&mut self) {
        self.selected_tool = (self.selected_tool + 1) % self.tools.len();
        self.follow_selection = true;
    }

    pub fn previous_tool(&mut self) {
        self.selected_tool = (self.selected_tool + self.tools.len() - 1) % self.tools.len();
        self.follow_selection = true;
    }

    pub fn toggle_tool(&mut self) {
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
        self.ticks = self.ticks.saturating_add(steps);
        if self.ticks >= 80 && self.tools[2].state == ToolState::Running {
            self.tools[2].state = ToolState::Success;
            self.tools[2].result = "........................  [100%]\n24 passed in 1.28s (fixture output, not a real test run)".into();
        }
    }

    pub fn insert_text(&mut self, text: &str) {
        let remaining = 500usize.saturating_sub(self.input.chars().count());
        self.input.extend(
            text.chars()
                .filter_map(|ch| {
                    if ch == '\n' || ch == '\r' || ch == '\t' {
                        Some(' ')
                    } else if ch.is_control() {
                        None
                    } else {
                        Some(ch)
                    }
                })
                .take(remaining),
        );
    }

    pub fn submit(&mut self) {
        let text = self.input.trim();
        if text.is_empty() {
            return;
        }
        self.turns.push(text.to_string());
        if self.turns.len() > 6 {
            self.turns.remove(0);
        }
        self.input.clear();
        self.scroll = usize::MAX;
        self.notice = "Local demo turn added. No model was contacted.".into();
    }

    pub fn reset(&mut self) {
        let themes = std::mem::take(&mut self.themes);
        let theme_index = self.theme_index;
        *self = Self {
            themes,
            theme_index,
            ..Self::default()
        };
    }
}
