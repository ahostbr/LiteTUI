use crossterm::{
    cursor::Show,
    event::{
        self, DisableBracketedPaste, DisableFocusChange, DisableMouseCapture, EnableBracketedPaste,
        EnableFocusChange, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind,
        KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use litetui_terminal::{
    composer::{Delete, Motion},
    demo::Demo,
    ui,
};
use ratatui::{Terminal, backend::CrosstermBackend};
use std::{
    io::{self, IsTerminal, stdout},
    time::{Duration, Instant},
};

fn restore() {
    let _ = disable_raw_mode();
    let _ = execute!(
        stdout(),
        DisableMouseCapture,
        DisableFocusChange,
        DisableBracketedPaste,
        LeaveAlternateScreen,
        Show
    );
}

struct TerminalGuard;
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        restore();
    }
}

/// Retain ownership of a modal-closing physical key until its release or next
/// press. Terminals without release events still recover on the next press.
/// Modal confirmation keys and completion Tab/Escape cannot leak repeats.
#[derive(Default)]
struct KeyRouter {
    closing_keys: std::collections::HashSet<KeyCode>,
    live_repeat_guard: bool,
}
impl KeyRouter {
    // A terminal's character code is not a physical key identity: Shift can
    // change '1' to '!'. After live-modal ownership, reject all reported repeats
    // until fresh non-modifier intent, including before the clipboard fast path.
    fn suppress_live_repeat(&mut self, event: KeyEvent) -> bool {
        if event.kind == KeyEventKind::Press && !matches!(event.code, KeyCode::Modifier(_)) {
            self.live_repeat_guard = false;
        }
        event.kind == KeyEventKind::Repeat && self.live_repeat_guard
    }
    fn handle(&mut self, app: &mut Demo, event: KeyEvent) -> bool {
        if self.suppress_live_repeat(event) {
            return false;
        }
        let physical = match event.code {
            KeyCode::Char(ch) => KeyCode::Char(ch.to_ascii_lowercase()),
            other => other,
        };
        match event.kind {
            KeyEventKind::Release => {
                self.closing_keys.remove(&physical);
                return false;
            }
            KeyEventKind::Press => {
                self.closing_keys.remove(&physical);
            }
            KeyEventKind::Repeat if self.closing_keys.contains(&physical) => return false,
            KeyEventKind::Repeat => {}
        }
        // Live modals can disappear on a source event between key calls. Keep
        // their held keys owned even when this key does not close the modal.
        let live_owned = app.has_live_input();
        let modal_was_open = app.approval_open || live_owned || app.themes_open;
        let completion_was_open = app.completion.is_open();
        let before = app.composer.text().to_owned();
        let quit = key(app, event);
        if before != app.composer.text() {
            app.sync_completion();
        }
        if live_owned {
            self.live_repeat_guard = true;
        }
        if (modal_was_open && !(app.approval_open || app.has_live_input() || app.themes_open))
            || (completion_was_open
                && !app.completion.is_open()
                && matches!(event.code, KeyCode::Tab | KeyCode::Esc))
        {
            self.closing_keys.insert(physical);
        }
        quit
    }
}

fn key(app: &mut Demo, mut key: KeyEvent) -> bool {
    if key.kind == KeyEventKind::Release {
        return false;
    }
    if key.modifiers.contains(KeyModifiers::CONTROL)
        && let KeyCode::Char(ch) = key.code
    {
        key.code = KeyCode::Char(ch.to_ascii_lowercase());
    }
    if key.modifiers.contains(KeyModifiers::CONTROL) && matches!(key.code, KeyCode::Char('q')) {
        return true;
    }
    if key.code == KeyCode::Esc
        && key.modifiers == KeyModifiers::CONTROL
        && key.kind == KeyEventKind::Press
    {
        app.open_stop();
        return false;
    }
    if app.viewport_too_small {
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            if !app.stop_open()
                && !app.has_live_approval()
                && !app.has_live_question()
                && let Some(chat) = app.live.as_mut()
            {
                if chat.work.context_open() || chat.work.image.open {
                    chat.work.key(key);
                    return false;
                }
                if chat.settings.runtime_status_open() {
                    chat.settings.key(key);
                    return false;
                }
            }
            if app.has_live_compact() {
                let chat = app.live.as_mut().expect("compact");
                chat.compact.close();
                chat.saved_history.hide();
                chat.browser.hide();
                return false;
            }
            if app.has_live_rewrite() {
                let chat = app.live.as_mut().expect("rewrite");
                chat.rewrite.close();
                chat.saved_history.hide();
                chat.browser.hide();
                return false;
            }
            if app.has_live_history() {
                let chat = app.live.as_mut().expect("saved history");
                chat.saved_history.close();
                chat.browser.hide();
                return false;
            }
            if let Some(chat) = app.live.as_mut()
                && chat.settings.compaction.owns_input()
            {
                chat.settings.key(key);
                return false;
            }
            if let Some(chat) = app.live.as_mut() {
                chat.stop_confirm = false;
            }
            app.approval_open = false;
            if let Some(chat) = app.live.as_mut() {
                chat.runtime_controls.close();
                chat.controls.close();
                chat.queue.close();
                chat.engine.close();
                chat.commands.close();
                chat.themes.close();
                chat.saved_history.close();
                chat.work.close();
                chat.settings.close();
                chat.backend_controls.close();
                chat.browser.close();
            }
            app.themes_open = false;
            app.completion.dismiss();
        }
        return false;
    }
    if app.stop_open() {
        if key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            let chat = app.live.as_mut().expect("stop dialog");
            match key.code {
                KeyCode::Esc => chat.stop_confirm = false,
                KeyCode::Tab | KeyCode::Left | KeyCode::Right => {
                    chat.stop_selected = !chat.stop_selected
                }
                KeyCode::Enter => {
                    chat.stop_confirm = false;
                    if chat.stop_selected {
                        if let Err(error) = chat.request_stop() {
                            chat.finish(Some(error.to_string()));
                        }
                        app.notice = chat.notice.clone();
                    }
                }
                _ => {}
            }
        }
        return false;
    }
    if app.live.as_ref().is_some_and(|chat| chat.cancelling()) {
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            app.open_stop();
        }
        return false;
    }
    if app.has_live_approval() {
        let chat = app.live.as_mut().expect("live approval");
        if chat.quitting
            || chat.cancelling()
            || key.kind != KeyEventKind::Press
            || !key.modifiers.is_empty()
        {
            return false;
        }
        let current = chat.approvals.current_mut().expect("pending approval");
        match key.code {
            KeyCode::Up => current.scroll = current.scroll.saturating_sub(1),
            KeyCode::Down => current.scroll = current.scroll.saturating_add(1),
            KeyCode::PageUp => current.scroll = current.scroll.saturating_sub(10),
            KeyCode::PageDown => current.scroll = current.scroll.saturating_add(10),
            KeyCode::Tab | KeyCode::Left | KeyCode::Right if !current.waiting => {
                current.allow = !current.allow
            }
            KeyCode::Enter if !current.waiting => {
                let allow = current.allow;
                chat.approvals.choose(allow);
            }
            KeyCode::Esc if !current.waiting => chat.approvals.choose(false),
            _ => {}
        }
        return false;
    }
    if app.has_live_question() {
        let chat = app.live.as_mut().expect("live question");
        if !chat.quitting && !chat.cancelling() {
            chat.questions.key(key);
        }
        return false;
    }
    if app
        .live
        .as_ref()
        .is_some_and(|chat| chat.execution_pending())
        && key.code == KeyCode::Esc
        && key.kind == KeyEventKind::Press
        && key.modifiers.is_empty()
    {
        app.open_stop();
        return false;
    }
    if app.has_live_runtime_controls() {
        app.live
            .as_mut()
            .expect("runtime controls")
            .runtime_controls
            .key(key);
        return false;
    }
    if app.has_live_work() {
        app.live.as_mut().expect("Work").work.key(key);
        return false;
    }
    if app.has_live_engine() {
        app.live.as_mut().expect("engine").engine.key(key);
        return false;
    }
    if app.has_live_commands() {
        app.live.as_mut().expect("commands").commands.key(key);
        return false;
    }
    if app.has_live_compact() {
        app.live.as_mut().expect("compact").compact.key(key);
        return false;
    }
    if app.has_live_rewrite() {
        app.live.as_mut().expect("rewrite").rewrite.key(key);
        return false;
    }
    if app.has_live_history() {
        app.live
            .as_mut()
            .expect("saved history")
            .saved_history
            .key(key);
        return false;
    }
    if app.has_live_themes() {
        app.live.as_mut().unwrap().themes.key(key);
        return false;
    }
    if app.has_live_settings() {
        app.live.as_mut().expect("settings view").settings.key(key);
        return false;
    }
    if app.has_live_backend() {
        app.live
            .as_mut()
            .expect("backend selector")
            .backend_controls
            .key(key);
        return false;
    }
    if app.has_live_browser() {
        app.live
            .as_mut()
            .expect("conversation browser")
            .browser
            .key(key);
        return false;
    }
    if app.has_live_queue() {
        app.live.as_mut().expect("held input").queue.key(key);
        return false;
    }
    if app.has_live_controls() {
        app.live.as_mut().expect("live controls").controls.key(key);
        return false;
    }
    if app.themes_open {
        match key.code {
            KeyCode::Esc => app.themes_open = false,
            KeyCode::Up => app.move_theme(false),
            KeyCode::Down => app.move_theme(true),
            KeyCode::Enter if key.kind == KeyEventKind::Press => app.choose_theme(),
            KeyCode::Backspace => {
                app.theme_query.pop();
                app.theme_cursor = 0;
            }
            KeyCode::Char(ch)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                app.search_themes(&ch.to_string())
            }
            _ => {}
        }
        return false;
    }
    if app.approval_open {
        match key.code {
            KeyCode::Esc => app.approval_open = false,
            KeyCode::Tab | KeyCode::BackTab | KeyCode::Left | KeyCode::Right => {
                app.approval_allow = !app.approval_allow
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                app.resolve_approval(app.approval_allow)
            }
            KeyCode::Char('a' | 'A')
                if key.kind == KeyEventKind::Press
                    && !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                app.resolve_approval(true)
            }
            KeyCode::Char('d' | 'D')
                if key.kind == KeyEventKind::Press
                    && !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                app.resolve_approval(false)
            }
            _ => {}
        }
        return false;
    }
    if app.completion.is_open() && key.modifiers.is_empty() {
        match key.code {
            KeyCode::Tab => {
                if key.kind == KeyEventKind::Press {
                    app.accept_completion();
                }
            }
            KeyCode::Up => app.completion.move_by(-1),
            KeyCode::Down => app.completion.move_by(1),
            KeyCode::Esc => app.completion.dismiss(),
            _ => return composer_or_preview_key(app, key),
        }
        return false;
    }
    // A held navigation key is not fresh intent to abandon a visible or
    // disappeared chip, even if the terminal reports changed modifiers.
    if key.code == KeyCode::Down && key.kind == KeyEventKind::Repeat && app.footer.owns_enter() {
        return false;
    }
    if key.code == KeyCode::Enter && app.footer.owns_enter() {
        if key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            let (_, intent) = app.footer.activate();
            if intent == Some(litetui_terminal::footer::FooterAction::Thinking)
                && app.live.is_some()
            {
                app.open_models(litetui_terminal::model_controls::Kind::Thinking);
                return false;
            }
            if app.live.is_some() {
                use litetui_terminal::{footer::FooterAction, runtime_controls_view::Field};
                match intent {
                    Some(FooterAction::Authority) => {
                        app.open_runtime_controls(Field::Profile);
                        return false;
                    }
                    Some(FooterAction::Plan) => {
                        app.open_runtime_controls(Field::Plan);
                        return false;
                    }
                    _ => {}
                }
            }
            app.notice = match intent {
                Some(intent) => {
                    format!("Not applied: {intent:?} (runtime controls not wired).")
                }
                None => "Chip disappeared; draft not submitted.".into(),
            };
        }
        return false;
    }
    if app.footer.selected().is_some() && key.modifiers.is_empty() {
        match key.code {
            KeyCode::Left => {
                app.footer.move_by(-1);
                return false;
            }
            KeyCode::Right => {
                app.footer.move_by(1);
                return false;
            }
            KeyCode::Up | KeyCode::Esc | KeyCode::Down => {
                app.footer.leave();
                return false;
            }
            _ => {}
        }
    }
    app.footer.leave();
    if key.code == KeyCode::Down
        && key.kind == KeyEventKind::Press
        && key.modifiers.is_empty()
        && !app.composer.is_history_browsing()
        && app.footer.enter()
    {
        return false;
    }
    composer_or_preview_key(app, key)
}

fn composer_or_preview_key(app: &mut Demo, key: KeyEvent) -> bool {
    match key.code {
        KeyCode::Char('o')
            if app.live.is_some()
                && key.kind == KeyEventKind::Press
                && key.modifiers == KeyModifiers::CONTROL =>
        {
            app.open_image();
        }
        KeyCode::Char('t' | 'p')
            if app.live.is_some()
                && key.kind == KeyEventKind::Press
                && key.modifiers == KeyModifiers::CONTROL =>
        {
            let field = if key.code == KeyCode::Char('t') {
                litetui_terminal::runtime_controls_view::Field::Tools
            } else {
                litetui_terminal::runtime_controls_view::Field::Plan
            };
            app.open_runtime_controls(field);
        }
        KeyCode::BackTab
            if app.live.is_some()
                && key.kind == KeyEventKind::Press
                && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT) =>
        {
            app.open_runtime_controls(litetui_terminal::runtime_controls_view::Field::Profile);
        }
        KeyCode::F(1) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_work();
        }
        KeyCode::F(12) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_engine();
        }
        KeyCode::F(11) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_commands();
        }
        KeyCode::F(10) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_settings();
        }
        KeyCode::F(9) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_backend();
        }
        KeyCode::F(8) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_browser();
        }
        KeyCode::F(7) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_queue()
        }
        KeyCode::F(3) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_models(litetui_terminal::model_controls::Kind::Model)
        }
        KeyCode::F(4) => app.open_themes(),
        KeyCode::F(5) => app.reset(),
        KeyCode::F(6)
            if app.live.is_some()
                && key.kind == KeyEventKind::Press
                && key.modifiers.is_empty() =>
        {
            app.open_history()
        }
        KeyCode::F(6) => app.open_approval(),
        KeyCode::F(2) => app.toggle_tool(),
        KeyCode::Tab => app.next_tool(),
        KeyCode::BackTab => app.previous_tool(),
        KeyCode::Char('l')
            if app.live.is_some()
                && key.kind == KeyEventKind::Press
                && key.modifiers == KeyModifiers::CONTROL =>
        {
            if let Err(error) = app
                .open_conversation_action(litetui_terminal::conversation_actions::Action::New, "")
            {
                app.notice = error.to_string();
            }
        }
        KeyCode::Char('b' | 'e')
            if app.live.is_some() && key.modifiers == KeyModifiers::CONTROL =>
        {
            if key.kind != KeyEventKind::Press {
                return false;
            }
            let chat = app.live.as_mut().expect("live shortcut");
            let selected = if key.code == KeyCode::Char('b') {
                chat.select_previous_tool()
            } else {
                chat.toggle_recent_tool()
            };
            if selected {
                app.follow_selection = true;
            } else if key.code == KeyCode::Char('e') {
                app.composer.move_cursor(Motion::End, false);
            }
        }
        KeyCode::Char('e') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.move_cursor(Motion::End, false)
        }
        KeyCode::Char('b') if key.modifiers.contains(KeyModifiers::CONTROL) => app.previous_tool(),
        KeyCode::Char('a')
            if key
                .modifiers
                .contains(KeyModifiers::CONTROL | KeyModifiers::SHIFT) =>
        {
            app.composer.select_all()
        }
        KeyCode::Char('a') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.move_cursor(Motion::Home, false)
        }
        KeyCode::Char('w') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.delete(Delete::WordLeft)
        }
        KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.delete(Delete::WordRight)
        }
        KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.delete(Delete::ToStart)
        }
        KeyCode::Char('k') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.delete(Delete::ToEnd)
        }
        KeyCode::Char('d') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.delete(Delete::Right)
        }
        KeyCode::Left if key.modifiers.contains(KeyModifiers::CONTROL) => app.composer.move_cursor(
            Motion::WordLeft,
            key.modifiers.contains(KeyModifiers::SHIFT),
        ),
        KeyCode::Right if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.composer.move_cursor(
                Motion::WordRight,
                key.modifiers.contains(KeyModifiers::SHIFT),
            )
        }
        KeyCode::Left => app
            .composer
            .move_cursor(Motion::Left, key.modifiers.contains(KeyModifiers::SHIFT)),
        KeyCode::Right => app
            .composer
            .move_cursor(Motion::Right, key.modifiers.contains(KeyModifiers::SHIFT)),
        KeyCode::Up => {
            app.composer.history_previous();
        }
        KeyCode::Down => {
            app.composer.history_next();
        }
        KeyCode::PageUp => app.scroll_up(12),
        KeyCode::PageDown => app.scroll_down(12),
        KeyCode::Home if key.modifiers.contains(KeyModifiers::CONTROL) => app.scroll = 0,
        KeyCode::End if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.scroll = app.max_scroll
        }
        KeyCode::Home => app
            .composer
            .move_cursor(Motion::Home, key.modifiers.contains(KeyModifiers::SHIFT)),
        KeyCode::End => app
            .composer
            .move_cursor(Motion::End, key.modifiers.contains(KeyModifiers::SHIFT)),
        KeyCode::Enter if key.kind == KeyEventKind::Press => app.submit(),
        KeyCode::Backspace => app.composer.delete(Delete::Left),
        KeyCode::Delete => app.composer.delete(Delete::Right),
        KeyCode::Esc => {
            if app.live.as_ref().is_some_and(|chat| chat.busy) && key.kind == KeyEventKind::Press {
                app.open_stop();
                return false;
            }
            app.notice = if app.live.is_some() {
                "Live runtime; Ctrl+Q exits cleanly.".into()
            } else {
                "Offline preview only. F5 replays the mock conversation.".into()
            }
        }
        KeyCode::Char(ch)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            app.insert_text(&ch.to_string())
        }
        _ => {}
    }
    false
}

fn handle_key(
    app: &mut Demo,
    input: &mut KeyRouter,
    event: KeyEvent,
    clipboard: &mut impl litetui_terminal::clipboard::Clipboard,
) -> bool {
    // Include clipboard chords in live modal ownership, before any host I/O.
    if input.suppress_live_repeat(event) {
        return false;
    }
    if app.has_live_work() {
        if !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
            && !app.viewport_too_small
            && event.kind == KeyEventKind::Press
            && event.modifiers == KeyModifiers::CONTROL
            && matches!(event.code, KeyCode::Char('v' | 'V'))
        {
            let chat = app.live.as_mut().expect("Work");
            if !chat.quitting && !chat.cancelling() && chat.work.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.work.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.work.clipboard_error(error),
                }
            }
        }
        return input.handle(app, event);
    }
    if app.has_live_compact() {
        if !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
            && !app.viewport_too_small
            && event.kind == KeyEventKind::Press
            && event.modifiers == KeyModifiers::CONTROL
            && matches!(event.code, KeyCode::Char('v' | 'V'))
        {
            let chat = app.live.as_mut().expect("compact");
            if !chat.quitting && !chat.cancelling() && chat.compact.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.compact.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.compact.notice = error,
                }
            }
        }
        return input.handle(app, event);
    }
    if app.has_live_rewrite() {
        if !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
            && !app.viewport_too_small
            && event.kind == KeyEventKind::Press
            && event.modifiers == KeyModifiers::CONTROL
            && matches!(event.code, KeyCode::Char('v' | 'V'))
        {
            let chat = app.live.as_mut().expect("rewrite");
            if !chat.quitting && !chat.cancelling() && chat.rewrite.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.rewrite.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.rewrite.notice = error,
                }
            }
        }
        return input.handle(app, event);
    }
    if app.has_live_input() {
        if !app.stop_open()
            && !app.has_live_approval()
            && !app.viewport_too_small
            && event.kind == KeyEventKind::Press
            && event.modifiers == KeyModifiers::CONTROL
            && matches!(event.code, KeyCode::Char('v' | 'V'))
        {
            let chat = app.live.as_mut().expect("live question");
            if !chat.quitting && !chat.cancelling() && chat.browser.actions.editing() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.browser.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.browser.actions.notice = error,
                }
            }
            if !chat.quitting && !chat.cancelling() && chat.commands.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.commands.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.commands.notice = error,
                }
            }
            if !chat.quitting && !chat.cancelling() && chat.themes.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.themes.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.themes.notice = error,
                }
            }
            if !chat.quitting && !chat.cancelling() && chat.settings.accepts_paste() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.settings.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.settings.clipboard_error(error),
                }
            }
            if !chat.quitting && !chat.cancelling() && chat.backend_controls.editing() {
                match clipboard.read_text() {
                    Ok(Some(text)) => chat.backend_controls.paste(&text),
                    Ok(None) => {}
                    Err(error) => chat.backend_controls.notice = error,
                }
            }

            if !chat.quitting
                && !chat.cancelling()
                && let Some(pending) = chat.questions.current_mut()
                && !pending.waiting
                && pending.note_focused()
            {
                match clipboard.read_text() {
                    Ok(Some(text)) => pending.paste(&text, true),
                    Ok(None) => {}
                    Err(error) => pending.notice = error,
                }
            }
        }
        return input.handle(app, event);
    }
    if event.kind != KeyEventKind::Release {
        let code = match event.code {
            KeyCode::Char(ch) => Some(ch.to_ascii_lowercase()),
            _ => None,
        };
        let physical = code.map(KeyCode::Char).unwrap_or(event.code);
        if event.kind == KeyEventKind::Repeat && input.closing_keys.contains(&physical) {
            return false;
        }
        if event.kind == KeyEventKind::Press {
            input.closing_keys.remove(&physical);
        }
        let control = event.modifiers == KeyModifiers::CONTROL;
        let copy = code == Some('c') && (control || event.modifiers == KeyModifiers::SUPER);
        let blocked =
            app.approval_open || app.has_live_input() || app.themes_open || app.viewport_too_small;
        if copy {
            if !blocked {
                app.footer.leave();
            }
            if blocked || app.composer.selected_text().is_empty() {
                app.notice = "Press Ctrl+Q to quit; Ctrl+C copies selected input.".into();
            } else {
                app.notice = match clipboard.copy_text(app.composer.selected_text()) {
                    Ok(()) => "Copy sequence sent; terminal clipboard support is required.".into(),
                    Err(error) => error,
                };
            }
            return false;
        }
        if control && code == Some('v') {
            if blocked {
                return false;
            }
            app.footer.leave();
            match clipboard.read_text() {
                Ok(Some(text)) if !text.is_empty() => {
                    let inserted = app.composer.paste_clipboard(&text);
                    if inserted > 0 {
                        app.sync_completion();
                    }
                    app.notice = format!(
                        "Pasted {inserted} characters from clipboard (preview limits apply)."
                    );
                }
                Ok(_) => {
                    app.notice =
                        "No text in clipboard; image paste is not implemented in this preview."
                            .into()
                }
                Err(error) => app.notice = error,
            }
            return false;
        }
    }
    input.handle(app, event)
}

fn paste(app: &mut Demo, text: &str) {
    if app.has_live_runtime_controls() && !app.has_live_question() {
        return;
    }
    if app.has_live_work() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("Work").work.paste(text);
        }
        return;
    }

    if app.has_live_compact() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("compact").compact.paste(text);
        }
        return;
    }
    if app.has_live_rewrite() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("rewrite").rewrite.paste(text);
        }
        return;
    }
    if app.has_live_history() {
        return;
    }
    if app.has_live_browser() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().browser.paste(text);
        }
        return;
    }

    if app.has_live_engine() {
        return;
    }
    if app.has_live_commands() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().commands.paste(text);
        }
        return;
    }
    if app.has_live_themes() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().themes.paste(text);
        }
        return;
    }
    if app.has_live_settings() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("settings").settings.paste(text);
        }
        return;
    }
    if app.has_live_backend() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live
                .as_mut()
                .expect("backend selector")
                .backend_controls
                .paste(text);
        }
        return;
    }

    if !app.stop_open()
        && !app.viewport_too_small
        && !app.has_live_approval()
        && app.has_live_question()
    {
        let chat = app.live.as_mut().expect("live question");
        if !chat.quitting
            && !chat.cancelling()
            && let Some(pending) = chat.questions.current_mut()
        {
            pending.paste(text, false);
        }
        return;
    }
    if app.approval_open || app.has_live_input() || app.viewport_too_small {
        return;
    }
    if app.themes_open {
        app.search_themes(text);
    } else {
        app.paste_text(text);
    }
}

fn mouse(app: &mut Demo, event: MouseEvent) {
    if app.has_live_runtime_controls() && !app.has_live_question() {
        if !app.viewport_too_small && !app.stop_open() && !app.has_live_approval() {
            app.live
                .as_mut()
                .expect("runtime controls")
                .runtime_controls
                .mouse(event);
        }
        return;
    }
    if app.has_live_work() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
            && !app.live.as_ref().is_some_and(|c| c.cancelling())
        {
            app.live.as_mut().expect("Work").work.mouse(event);
        }
        return;
    }
    if app.has_live_compact() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("compact").compact.mouse(event);
        }
        return;
    }
    if app.has_live_rewrite() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().expect("rewrite").rewrite.mouse(event);
        }
        return;
    }
    if app.has_live_history() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().saved_history.mouse(event);
        }
        return;
    }
    if app.has_live_themes() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().themes.mouse(event);
        }
        return;
    }
    if app.has_live_engine() {
        if !app.viewport_too_small {
            app.live.as_mut().unwrap().engine.mouse(event);
        }
        return;
    }
    if app.has_live_commands() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live.as_mut().unwrap().commands.mouse(event);
        }
        return;
    }
    if app.has_live_settings() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live
                .as_mut()
                .expect("settings view")
                .settings
                .mouse(event);
        }
        return;
    }

    if app.has_live_backend() {
        if !app.viewport_too_small
            && !app.stop_open()
            && !app.has_live_approval()
            && !app.has_live_question()
        {
            app.live
                .as_mut()
                .expect("backend selector")
                .backend_controls
                .mouse(event);
        }
        return;
    }

    if app.has_live_browser() {
        if !app.viewport_too_small {
            app.live
                .as_mut()
                .expect("conversation browser")
                .browser
                .mouse(event);
        }
        return;
    }
    if app.has_live_queue()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
    {
        app.live.as_mut().expect("held input").queue.mouse(event);
        return;
    }
    if app.has_live_controls() {
        if !app.viewport_too_small {
            app.live
                .as_mut()
                .expect("live controls")
                .controls
                .mouse(event);
        }
        return;
    }
    if !app.stop_open()
        && !app.viewport_too_small
        && !app.has_live_approval()
        && app.has_live_question()
    {
        let chat = app.live.as_mut().expect("live question");
        if !chat.quitting && !chat.cancelling() {
            chat.questions.mouse(event);
        }
        return;
    }
    if app.approval_open || app.has_live_input() || app.themes_open || app.viewport_too_small {
        app.completion.hide();
        return;
    }
    match event.kind {
        MouseEventKind::Down(MouseButton::Left) => {
            app.completion.pointer_down(event.column, event.row)
        }
        MouseEventKind::Up(MouseButton::Left) => {
            if app.completion.pointer_up(event.column, event.row) {
                app.accept_completion();
            }
        }
        MouseEventKind::ScrollUp => app.scroll_up(3),
        MouseEventKind::ScrollDown => app.scroll_down(3),
        _ => {}
    }
}

fn run() -> io::Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::other(
            "Open this preview in Windows Terminal or a LiteSuite terminal pane; redirected pipes are not interactive.",
        ));
    }
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        previous_hook(info);
    }));
    let _guard = TerminalGuard;
    enable_raw_mode()?;
    execute!(
        stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture,
        EnableFocusChange
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout()))?;
    terminal.clear()?;
    let mut app = Demo::default();
    let mut tick_at = Instant::now();
    let mut input = KeyRouter::default();
    let mut clipboard = litetui_terminal::clipboard::SystemClipboard::new(1024 * 1024);
    loop {
        terminal.draw(|frame| ui::draw(frame, &mut app))?;
        if event::poll(Duration::from_millis(100))? {
            match event::read()? {
                Event::Key(event) if handle_key(&mut app, &mut input, event, &mut clipboard) => {
                    break;
                }
                Event::Paste(text) => paste(&mut app, &text),
                Event::Mouse(event) => mouse(&mut app, event),

                _ => {}
            }
        }
        if tick_at.elapsed() >= Duration::from_millis(100) {
            app.tick(1);
            tick_at = Instant::now();
        }
    }
    Ok(())
}

fn choose_profile(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    tools_enabled: bool,
) -> io::Result<Option<litetui_terminal::tool_mode::Profile>> {
    use litetui_terminal::profile_picker::{Picker, Selection};
    let mut picker = Picker::default();
    loop {
        terminal.draw(|frame| picker.draw(frame, tools_enabled))?;
        if event::poll(Duration::from_millis(100))? {
            match picker.handle(event::read()?) {
                Selection::Pending => {}
                Selection::Selected(profile) => return Ok(Some(profile)),
                Selection::Cancelled => return Ok(None),
            }
        }
    }
}

/// One-shot terminal launches share the native picker; redirected launches must
/// name the profile. Cancellation returns before the runtime is constructed.
fn one_shot_profile(options: &mut litetui_terminal::live::Options) -> io::Result<bool> {
    if options.launch.engine_management {
        return Err(io::Error::other("Engine management requires --interactive"));
    }
    if options.prompt.trim().is_empty() {
        return Err(io::Error::other("--live requires --prompt"));
    }
    if options.tool_profile.is_some() {
        return Ok(true);
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::other(
            "Redirected launch requires --tool-profile strict|interactive|autonomous; Python was not started",
        ));
    }
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        previous_hook(info);
    }));
    let _guard = TerminalGuard;
    enable_raw_mode()?;
    execute!(
        stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture,
        EnableFocusChange
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout()))?;
    terminal.clear()?;
    options.tool_profile = choose_profile(&mut terminal, options.tools_enabled)?;
    Ok(options.tool_profile.is_some())
}

fn run_interactive(mut options: litetui_terminal::live::Options) -> io::Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::other(
            "--interactive requires a real terminal; --live supports redirected pipes",
        ));
    }
    if !options.prompt.is_empty() {
        return Err(io::Error::other(
            "--interactive takes prompts from the composer, not --prompt",
        ));
    }
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        previous_hook(info);
    }));
    let _guard = TerminalGuard;
    enable_raw_mode()?;
    execute!(
        stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture,
        EnableFocusChange
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout()))?;
    terminal.clear()?;
    if options.tool_profile.is_none() {
        options.tool_profile = choose_profile(&mut terminal, options.tools_enabled)?;
        if options.tool_profile.is_none() {
            return Ok(());
        }
        terminal.clear()?;
    }
    let mut app = Demo::live(options.backend.clone(), options.model.clone());
    let retained_root = options.data_root.clone();
    let runtime = litetui_terminal::live::Interactive::start(options);
    let outcome = runtime.with_cleanup(|runtime| {
        let mut input = KeyRouter::default();
        let mut clipboard = litetui_terminal::clipboard::SystemClipboard::new(1024 * 1024);
        let mut result: Option<io::Result<()>> = None;
        let mut quitting = false;
        let mut view_failure: Option<String> = None;
        loop {
            for observation in runtime.events.try_iter() {
                if let Err(error) = app.observe_live(observation) {
                    view_failure.get_or_insert_with(|| error.to_string());
                    app.finish_live(view_failure.clone());
                    quitting = true;
                    let _ = runtime.quit();
                    break;
                }
            }
            if result.is_none() {
                result = runtime.poll_done();
                if let Some(result) = &result {
                    // done is sent after the last observation. Drain that accepted
                    // prefix before finalizing the view, even when channels race.
                    for observation in runtime.events.try_iter() {
                        if let Err(error) = app.observe_live(observation) {
                            view_failure.get_or_insert_with(|| error.to_string());
                        }
                    }
                    let failure = view_failure
                        .clone()
                        .or_else(|| result.as_ref().err().map(ToString::to_string));
                    app.finish_live(failure);
                }
            }
            if quitting && let Some(result) = result.take() {
                return if let Some(error) = view_failure {
                    Err(io::Error::other(error))
                } else {
                    result
                };
            }
            terminal.draw(|frame| ui::draw(frame, &mut app))?;
            if event::poll(Duration::from_millis(50))? {
                let quit = match event::read()? {
                    Event::Key(event) => handle_key(&mut app, &mut input, event, &mut clipboard),
                    Event::Paste(text) => {
                        paste(&mut app, &text);
                        false
                    }
                    Event::Mouse(event) => {
                        mouse(&mut app, event);
                        false
                    }
                    Event::FocusLost | Event::Resize(..) => {
                        app.live.as_mut().expect("live").runtime_controls.hide();
                        app.live.as_mut().expect("live").engine.invalidate();
                        app.live.as_mut().expect("live").commands.hide();
                        app.live.as_mut().expect("live").themes.hide();
                        app.live.as_mut().expect("live").saved_history.hide();
                        app.live.as_mut().expect("live").rewrite.hide();
                        app.live.as_mut().expect("live").compact.hide();
                        app.live.as_mut().expect("live").work.hide();
                        app.live.as_mut().expect("live").settings.hide();
                        app.live.as_mut().expect("live").backend_controls.hide();
                        app.live.as_mut().expect("live").browser.hide();
                        false
                    }
                    _ => false,
                };
                if quit && !quitting {
                    quitting = true;
                    if result.is_none() {
                        app.live.as_mut().expect("live").begin_quit();
                        app.notice = app.live.as_ref().expect("live").notice.clone();
                        // A disconnected worker will report through done above.
                        if let Err(error) = runtime.quit() {
                            app.notice = error.to_string();
                        }
                    }
                }
            }
            if !quitting
                && let Some(action_id) = app.live.as_mut().expect("live").take_execution_stop()
                && let Err(error) = runtime.execution_stop(action_id)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(prompt_id) = app.live.as_mut().expect("live").take_abort()
                && let Err(error) = runtime.abort(prompt_id)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(decision) = app.live.as_mut().expect("live").approvals.take_decision()
                && let Err(error) = runtime.approve(decision)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(answer) = app.live.as_mut().expect("live").questions.take_decision()
                && let Err(error) = runtime.answer(answer)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting && app.command_quit {
                app.command_quit = false;
                quitting = true;
                app.live.as_mut().expect("live").begin_quit();
                if let Err(error) = runtime.quit() {
                    app.notice = error.to_string();
                }
            }
            if !quitting
                && let Some(intent) = app
                    .live
                    .as_mut()
                    .expect("live")
                    .runtime_controls
                    .take_intent()
                && let Err(error) = runtime.runtime_controls(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").engine.take_intent()
                && let Err(error) = runtime.engine(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").commands.take_intent()
                && let Err(error) = runtime.commands(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting {
                app.take_history_rewrite()?;
                if let Some(intent) = app.live.as_mut().expect("live").rewrite.take_intent() {
                    let key = intent.key().clone();
                    let preparation = if matches!(
                        intent,
                        litetui_terminal::conversation_rewrite::Intent::Apply { .. }
                    ) {
                        app.live.as_mut().expect("live").prepare_rewrite(&intent)
                    } else {
                        Ok(())
                    };
                    if let Err(error) = preparation.and_then(|()| runtime.rewrite(intent)) {
                        app.observe_live(litetui_terminal::live::Observation::Rewrite(
                            litetui_terminal::conversation_rewrite::Update::Rejected {
                                key,
                                reason: error.to_string(),
                            },
                        ))?;
                    }
                }
            }
            if !quitting {
                app.take_history_compact()?;
                if let Some(intent) = app.live.as_mut().expect("live").compact.take_intent() {
                    let key = intent.key().clone();
                    let preparation = if matches!(
                        intent,
                        litetui_terminal::conversation_compact::Intent::Apply { .. }
                    ) {
                        app.live.as_mut().expect("live").prepare_compact(&intent)
                    } else {
                        Ok(())
                    };
                    if let Err(error) = preparation.and_then(|()| runtime.compact(intent)) {
                        app.observe_live(litetui_terminal::live::Observation::Compact(
                            litetui_terminal::conversation_compact::Update::Rejected {
                                key,
                                reason: error.to_string(),
                            },
                        ))?;
                    }
                }
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").saved_history.take_intent()
            {
                let key = intent.key().clone();
                if let Err(error) = runtime.history(intent) {
                    app.live.as_mut().unwrap().saved_history.observe(
                        litetui_terminal::history_view::Update::Rejected {
                            key,
                            reason: error.to_string(),
                        },
                    )?;
                }
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").themes.take_intent()
                && let Err(error) = runtime.theme(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some((proposal_id, accept)) = app.take_schedule_decision()
                && let Err(error) = runtime.scheduled_decision(proposal_id, accept)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting && let Some(intent) = app.live.as_mut().expect("live").work.take_intent() {
                if let Err(error) = app.live.as_mut().expect("live").prepare_work(&intent) {
                    app.live.as_mut().expect("live").work.observe(
                        litetui_terminal::work_view::Update {
                            generation: intent.generation,
                            data: litetui_terminal::work_view::Data::Rejected(error.to_string()),
                        },
                    );
                    app.notice = error.to_string();
                } else if let Err(error) = runtime.work(intent) {
                    view_failure = Some(error.to_string());
                    app.finish_live(view_failure.clone());
                    let _ = runtime.quit();
                }
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").settings.take_intent()
                && let Err(error) = runtime.settings(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(intent) = app
                    .live
                    .as_mut()
                    .expect("live")
                    .backend_controls
                    .take_intent()
                && let Err(error) = runtime.backend(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Err(error) = app.take_browser_intent().and_then(|intent| match intent {
                    Some(intent) => runtime.browser(intent),
                    None => Ok(()),
                })
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").controls.take_intent()
                && let Err(error) = runtime.control(intent)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if !quitting && let Some(intent) = app.live.as_mut().expect("live").queue.take_intent()
            {
                let prepared = if matches!(
                    intent,
                    litetui_terminal::restored_queue::Intent::Continue { .. }
                ) {
                    app.live
                        .as_mut()
                        .expect("live")
                        .prepare_continuation(&intent)
                } else if matches!(
                    intent,
                    litetui_terminal::restored_queue::Intent::Reconcile { .. }
                ) {
                    app.live
                        .as_ref()
                        .expect("live")
                        .prepare_reconciliation(&intent)
                } else {
                    Ok(())
                };
                if let Err(error) = prepared {
                    app.live.as_mut().expect("live").queue.observe(
                        litetui_terminal::restored_queue::Update::Rejected(error.to_string()),
                    );
                    app.notice = error.to_string();
                } else if let Err(error) = runtime.queue(intent) {
                    view_failure = Some(error.to_string());
                    app.finish_live(view_failure.clone());
                    let _ = runtime.quit();
                }
            }
            if let Some((prompt, image)) = app.live.as_mut().expect("live").take_image_submission()
                && let Err(error) = runtime.submit_image(prompt, image)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
            if let Some(prompt) = app.live.as_mut().expect("live").take_submission()
                && let Err(error) = runtime.submit(prompt)
            {
                view_failure = Some(error.to_string());
                app.finish_live(view_failure.clone());
                let _ = runtime.quit();
            }
        }
    });
    drop(terminal);
    drop(_guard);
    println!(
        "Retained data root: {}",
        litetui_terminal::live::terminal_text(&retained_root.to_string_lossy())
    );
    if let Some(chat) = &app.live
        && let Some(state) = chat.runtime_state.startup()
    {
        println!(
            "Conversation: {}",
            litetui_terminal::live::terminal_text(&state.session)
        );
        println!(
            "Backend/model: {} / {}",
            litetui_terminal::live::terminal_text(&chat.backend),
            litetui_terminal::live::terminal_text(&chat.model)
        );
        println!(
            "Resume saved messages with --resume <conversation> and this --data-root, --backend and --model."
        );
    }
    if let Some(message) = app
        .live
        .as_ref()
        .and_then(|chat| chat.conversation_exit_notice.as_ref())
    {
        println!("{}", litetui_terminal::live::terminal_text(message));
    }
    outcome
}

fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "--interactive") {
        return run_interactive(litetui_terminal::live::Options::parse(&args[1..])?);
    }
    if args.first().is_some_and(|arg| arg == "--live") {
        let mut options = litetui_terminal::live::Options::parse(&args[1..])?;
        if !one_shot_profile(&mut options)? {
            return Ok(());
        }
        return litetui_terminal::live::run(options);
    }
    match args.as_slice() {
        [] => run(),
        [flag] if flag == "--preview" => run(),
        [flag] if flag == "--help" || flag == "-h" => {
            println!(
                "LiteTUI Rust — live vertical path / offline visual preview\n\nUsage: litetui-rs [--preview]\n       litetui-rs --interactive --python <exe> --source <repo> --data-root <absolute directory> --backend <custom|lmstudio|llamacpp|ninfer|strata|codex|claude|cline|free> --model <slug> [--timeout <seconds>] [--tools on|off] [--tool-profile strict|interactive|autonomous] [--workspace <directory>] [--resume <conversation-uuid>]\n       litetui-rs --live --python <exe> --source <repo> --data-root <absolute directory> --backend <custom|lmstudio|llamacpp|ninfer|strata|codex|claude|cline|free> --model <slug> --prompt <text> [--timeout <1..600 seconds>] [--tools on|off] [--tool-profile strict|interactive|autonomous] [--workspace <directory>] [--resume <conversation-uuid>]\n\n--interactive: real composer/transcript, serialized multi-turn, Ctrl+Q waits cleanup; optional source-authorized tools.\nLive keys: F1 Work | F3 models | F4 themes | F6 saved history | selector Tab thinking | F7 held input | F8 conversations | F9 backend/model | F10 settings | F11 commands/skills | F12 owned engine | Enter applies the visible choice | Esc closes.\n--live: Windows-only real owned Python child, one prompt, streamed reply; cannot answer questions or approvals.\nTools default off. Missing profiles prompt before Python starts; redirected launches require --tool-profile. Tools on requires an absolute existing --workspace. External MCP remains off.\nLocal backends connect by default. Server/model start requires explicit --start-server/--load-model; no fallback.\n--engine-management: interactive NInfer/Strata management when unavailable; no prompt or implicit start/load.\nInvocation options: --base-url <url> | --server-mode connect|start | --start-server | --load-model | --context-length <tokens> | --max-tokens <tokens> | --server-executable <path> | --model-path <path> | --server-command <JSON argv> | --api-key-env <variable name> | --server-timeout <seconds> | --codex-engine native|http | --thinking-level <source-supported level>.\nThe data root must be new unless --resume is supplied.\n--resume: reopen an existing Rust-owned root without rewriting settings. Match saved backend/model/tools; Python keeps saved thinking and owns all locks. No prompt is replayed.\n--preview (default): no runtime/model/registration/persistence.\nPreview keys: Tab select tool | F2 expand | F4 themes | F6 mock approval | F5 replay\nType /pre: mock completions (Tab accepts) | Up/Down: history or completion\nLeft/Right/Home/End: edit | PgUp/PgDn/mouse wheel: scroll | Enter: local mock turn | Ctrl+Q: quit"
            );
            Ok(())
        }
        _ => Err(io::Error::other(
            "Unknown option. Use --help for live and preview modes.",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_history_owns_f6_clipboard_paste_and_cancelled_late_key_repeats() {
        use litetui_terminal::history_view::{Intent, Update};
        let mut app = Demo::live("custom".into(), "fixture".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("UNSENT HISTORY DRAFT");
        let mut router = KeyRouter::default();
        let mut clipboard = TestClipboard::default();
        router.handle(&mut app, KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        assert!(app.has_live_history());
        let intent = app
            .live
            .as_mut()
            .unwrap()
            .saved_history
            .take_intent()
            .unwrap();
        let key = intent.key().clone();
        assert!(matches!(intent, Intent::Refresh { .. }));
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "MUST NOT INSERT");
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.has_live_history(), "sent read still owns input");
        app.live
            .as_mut()
            .unwrap()
            .saved_history
            .observe(Update::Rejected {
                key,
                reason: "late reply".into(),
            })
            .unwrap();
        assert!(!app.has_live_history());
        let mut repeat = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!(app.composer.text(), "UNSENT HISTORY DRAFT");
        assert_eq!(clipboard.reads, 0);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
    }

    #[test]
    fn modal_confirmation_repeat_does_not_leak_into_the_preserved_draft() {
        for code in [KeyCode::Enter, KeyCode::Char('a'), KeyCode::Char('d')] {
            let mut app = Demo::default();
            let mut input = KeyRouter::default();
            app.insert_text("saved draft");
            app.open_approval();
            input.handle(&mut app, KeyEvent::new(code, KeyModifiers::NONE));
            assert!(!app.approval_open);
            let mut repeat = KeyEvent::new(code, KeyModifiers::NONE);
            repeat.kind = KeyEventKind::Repeat;
            input.handle(&mut app, repeat);
            assert!(app.turns.is_empty());
            assert_eq!(app.composer.text(), "saved draft");
        }
    }

    #[test]
    fn slash_tab_replaces_the_whole_input_without_submitting_or_leaking_repeat() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("/pre");
        let selected_tool = app.selected_tool;
        let tab = KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE);
        input.handle(&mut app, tab);
        assert_eq!(app.composer.text(), "/preview-help ");
        assert_eq!(app.composer.before_cursor(), "/preview-help ");
        let mut repeat = tab;
        repeat.kind = KeyEventKind::Repeat;
        input.handle(&mut app, repeat);
        assert_eq!(app.selected_tool, selected_tool);
        assert!(app.turns.is_empty());
    }

    #[test]
    fn completion_owns_arrows_not_history_and_preserves_highlight_on_caret_motion() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.composer.push_history("old prompt");
        app.insert_text("/");
        input.handle(&mut app, KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.completion.selected(), 1);
        input.handle(&mut app, KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        assert_eq!(app.completion.selected(), 1);
        assert_eq!(app.composer.text(), "/");
        input.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "/preview-replay ");
        assert!(!app.completion.is_open());
        input.handle(&mut app, KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "old prompt");
        input.handle(&mut app, KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "/preview-replay ");
    }

    #[test]
    fn escape_stays_dismissed_until_text_changes_and_enter_submits_not_completes() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("/pre");
        input.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        input.handle(&mut app, KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert!(!app.completion.is_open());
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE),
        );
        assert!(app.completion.is_open());
        input.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.turns, ["/prev"]);
        assert!(!app.completion.is_open());
        app.insert_text("/pre");
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE),
        );
        assert!(!app.completion.is_open());
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE),
        );
        assert!(app.completion.is_open());
    }

    #[test]
    fn hidden_completion_cannot_be_accepted_and_modal_keys_have_priority() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("/");
        app.viewport_too_small = true;
        input.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "/");
        input.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.completion.is_open());
        app.viewport_too_small = false;
        app.insert_text("pre");
        app.open_approval();
        input.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "/pre");
        assert!(app.approval_open);
        input.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "/pre");
        assert!(app.completion.is_open());
        assert!(app.turns.is_empty());
    }

    #[test]
    fn clicking_a_scrolled_completion_uses_the_rendered_offset_and_never_submits() {
        use litetui_terminal::completion::{Completion, CompletionKind};
        use ratatui::backend::TestBackend;
        for (width, height) in [(80, 24), (36, 14)] {
            let mut app = Demo {
                completion_candidates: (0..120)
                    .map(|n| Completion {
                        name: format!("pick-{n:03}"),
                        description: String::new(),
                        kind: CompletionKind::Skill,
                    })
                    .collect(),
                ..Demo::default()
            };
            app.insert_text("/");
            app.completion.move_by(99);
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
            let row = (0..height)
                .find(|&y| {
                    (0..width)
                        .map(|x| terminal.backend().buffer()[(x, y)].symbol())
                        .collect::<String>()
                        .contains("pick-099")
                })
                .unwrap();
            let event = |kind, column| MouseEvent {
                kind,
                column,
                row,
                modifiers: KeyModifiers::NONE,
            };
            mouse(&mut app, event(MouseEventKind::Up(MouseButton::Left), 4));
            assert_eq!(app.composer.text(), "/");
            mouse(&mut app, event(MouseEventKind::Down(MouseButton::Left), 2));
            mouse(&mut app, event(MouseEventKind::Up(MouseButton::Left), 2));
            assert_eq!(app.composer.text(), "/");
            mouse(&mut app, event(MouseEventKind::Down(MouseButton::Left), 4));
            terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
            mouse(&mut app, event(MouseEventKind::Up(MouseButton::Left), 4));
            assert_eq!(app.composer.text(), "/pick-099 ");
            assert_eq!(app.composer.before_cursor(), "/pick-099 ");
            assert!(app.turns.is_empty());
            mouse(&mut app, event(MouseEventKind::Down(MouseButton::Left), 4));
            mouse(&mut app, event(MouseEventKind::Up(MouseButton::Left), 4));
            assert_eq!(app.composer.text(), "/pick-099 ");
        }
    }

    #[test]
    fn bracketed_paste_replaces_selection_with_only_first_line_and_never_submits() {
        for (text, expected) in [
            ("one\r\ntwo", "aboneef"),
            ("\nrest", "abef"),
            ("", "abcdef"),
        ] {
            let mut app = Demo::default();
            app.insert_text("abcdef");
            app.composer.move_cursor(Motion::Home, false);
            for _ in 0..2 {
                app.composer.move_cursor(Motion::Right, false);
            }
            for _ in 0..2 {
                app.composer.move_cursor(Motion::Right, true);
            }
            paste(&mut app, text);
            assert_eq!(app.composer.text(), expected);
            assert!(app.turns.is_empty());
        }
    }

    #[test]
    fn paste_respects_modal_ownership_and_refreshes_slash_completion() {
        let mut app = Demo::default();
        paste(&mut app, "/pre\nignored");
        assert_eq!(app.composer.text(), "/pre");
        assert!(app.completion.is_open());
        app.open_approval();
        paste(&mut app, "should not appear");
        assert_eq!(app.composer.text(), "/pre");
        app.approval_open = false;
        app.viewport_too_small = true;
        paste(&mut app, "hidden input");
        assert_eq!(app.composer.text(), "/pre");
        app.viewport_too_small = false;
        app.open_themes();
        paste(&mut app, "matrix");
        assert_eq!(app.theme_query, "matrix");
        assert_eq!(app.composer.text(), "/pre");
        assert!(app.turns.is_empty());
    }

    #[derive(Default)]
    struct TestClipboard {
        copied: Vec<String>,
        read: Option<String>,
        reads: usize,
        fail: bool,
    }
    impl litetui_terminal::clipboard::Clipboard for TestClipboard {
        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.reads += 1;
            if self.fail {
                Err("fixture clipboard error".into())
            } else {
                Ok(self.read.clone())
            }
        }
        fn copy_text(&mut self, text: &str) -> Result<(), String> {
            if self.fail {
                Err("fixture copy error".into())
            } else {
                self.copied.push(text.into());
                Ok(())
            }
        }
    }
    #[test]
    fn ctrl_c_copies_selection_or_explains_quit_but_never_exits() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        let mut clipboard = TestClipboard::default();
        app.insert_text("selected 雪");
        app.composer.select_all();
        let copy = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        assert!(!handle_key(&mut app, &mut input, copy, &mut clipboard));
        assert_eq!(clipboard.copied, ["selected 雪"]);
        assert_eq!(app.composer.text(), "selected 雪");
        app.composer.move_cursor(Motion::End, false);
        assert!(!handle_key(&mut app, &mut input, copy, &mut clipboard));
        assert_eq!(clipboard.copied.len(), 1);
        assert!(app.notice.contains("Ctrl+Q"));
        assert!(handle_key(
            &mut app,
            &mut input,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL),
            &mut clipboard
        ));
    }

    #[test]
    fn ctrl_v_uses_clipboard_insertion_not_bracketed_paste_or_submission() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        let mut clipboard = TestClipboard {
            read: Some("one\r\n \r\n two".into()),
            ..TestClipboard::default()
        };
        app.insert_text("abcdef");
        app.composer.move_cursor(Motion::Home, false);
        for _ in 0..2 {
            app.composer.move_cursor(Motion::Right, false);
        }
        for _ in 0..2 {
            app.composer.move_cursor(Motion::Right, true);
        }
        handle_key(
            &mut app,
            &mut input,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        assert_eq!(app.composer.text(), "abcdone  twoef");
        assert!(app.turns.is_empty());
        assert!(app.notice.contains("8 characters"));
    }
    #[test]
    fn clipboard_errors_releases_and_modal_guards_preserve_draft_without_io() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        let mut clipboard = TestClipboard {
            fail: true,
            ..TestClipboard::default()
        };
        app.insert_text("draft");
        app.composer.select_all();
        let paste = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        let copy = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        for (approval, themes, small) in [
            (true, false, false),
            (false, true, false),
            (false, false, true),
        ] {
            app.approval_open = approval;
            app.themes_open = themes;
            app.viewport_too_small = small;
            handle_key(&mut app, &mut input, paste, &mut clipboard);
            handle_key(&mut app, &mut input, copy, &mut clipboard);
        }
        assert_eq!(clipboard.reads, 0);
        app.approval_open = false;
        app.themes_open = false;
        app.viewport_too_small = false;
        let mut release = paste;
        release.kind = KeyEventKind::Release;
        handle_key(&mut app, &mut input, release, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        handle_key(&mut app, &mut input, paste, &mut clipboard);
        assert!(app.notice.contains("fixture clipboard error"));
        handle_key(&mut app, &mut input, copy, &mut clipboard);
        assert!(app.notice.contains("fixture copy error"));
        assert_eq!(app.composer.text(), "draft");
        assert_eq!(app.composer.selected_text(), "draft");
        assert!(clipboard.copied.is_empty());
        assert!(app.turns.is_empty());
    }

    #[test]
    fn down_then_enter_targets_visible_footer_without_submitting_draft() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
        app.insert_text("saved draft");
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        input.handle(&mut app, KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        input.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.turns.is_empty());
        assert_eq!(app.composer.text(), "saved draft");
    }

    #[test]
    fn held_down_does_not_drop_or_reenter_footer_ownership() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("draft");
        render_footer(&mut app);
        plain(&mut app, &mut input, KeyCode::Down);
        input.handle(
            &mut app,
            KeyEvent::new_with_kind(KeyCode::Down, KeyModifiers::NONE, KeyEventKind::Repeat),
        );
        plain(&mut app, &mut input, KeyCode::Enter);
        assert!(app.turns.is_empty());
        plain(&mut app, &mut input, KeyCode::Down);
        assert_eq!(app.footer.selected(), None);
        input.handle(
            &mut app,
            KeyEvent::new_with_kind(KeyCode::Down, KeyModifiers::NONE, KeyEventKind::Repeat),
        );
        assert_eq!(app.footer.selected(), None);
    }
    #[test]
    fn down_repeats_preserve_selected_and_stale_ownership_with_any_modifiers() {
        for stale in [false, true] {
            for modifiers in [
                KeyModifiers::NONE,
                KeyModifiers::SHIFT,
                KeyModifiers::CONTROL,
                KeyModifiers::ALT,
            ] {
                let mut app = Demo::default();
                let mut input = KeyRouter::default();
                app.insert_text("draft");
                render_footer(&mut app);
                plain(&mut app, &mut input, KeyCode::Down);
                if stale {
                    app.footer_fields.clear();
                    render_footer(&mut app);
                }
                input.handle(
                    &mut app,
                    KeyEvent::new_with_kind(KeyCode::Down, modifiers, KeyEventKind::Repeat),
                );
                assert!(app.footer.owns_enter(), "stale={stale}, {modifiers:?}");
                plain(&mut app, &mut input, KeyCode::Enter);
                assert!(app.turns.is_empty());
            }
        }
    }

    #[test]
    fn modified_enter_cannot_submit_from_selected_or_stale_footer() {
        for stale in [false, true] {
            for modifiers in [
                KeyModifiers::SHIFT,
                KeyModifiers::CONTROL,
                KeyModifiers::ALT,
            ] {
                let mut app = Demo::default();
                let mut input = KeyRouter::default();
                app.insert_text("draft");
                render_footer(&mut app);
                plain(&mut app, &mut input, KeyCode::Down);
                if stale {
                    app.footer_fields.clear();
                    render_footer(&mut app);
                }
                let notice = app.notice.clone();
                input.handle(&mut app, KeyEvent::new(KeyCode::Enter, modifiers));
                assert_eq!(app.notice, notice);
                assert!(app.turns.is_empty(), "stale={stale}, {modifiers:?}");
                assert!(app.footer.owns_enter());
            }
        }
    }

    fn render_footer(app: &mut Demo) {
        Terminal::new(ratatui::backend::TestBackend::new(100, 24))
            .unwrap()
            .draw(|frame| ui::draw(frame, app))
            .unwrap();
    }
    fn plain(app: &mut Demo, input: &mut KeyRouter, code: KeyCode) {
        input.handle(app, KeyEvent::new(code, KeyModifiers::NONE));
    }
    #[test]
    fn footer_typing_exits_preserving_caret_then_enter_submits() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("draft");
        plain(&mut app, &mut input, KeyCode::Left);
        render_footer(&mut app);
        plain(&mut app, &mut input, KeyCode::Down);
        plain(&mut app, &mut input, KeyCode::Right);
        assert_eq!(app.composer.before_cursor(), "draf");
        assert_eq!(
            app.footer.selected(),
            Some(litetui_terminal::footer::FooterAction::Plan)
        );
        plain(&mut app, &mut input, KeyCode::Enter);
        assert!(app.notice.contains("Not applied:"));
        assert_eq!(app.footer_fields.last().unwrap().text, "plan:off");
        plain(&mut app, &mut input, KeyCode::Char('X'));
        assert_eq!(app.footer.selected(), None);
        plain(&mut app, &mut input, KeyCode::Enter);
        assert_eq!(app.turns, ["drafXt"]);
    }
    #[test]
    fn history_restore_requires_another_down_to_enter_footer() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.composer.push_history("old");
        app.insert_text("draft");
        render_footer(&mut app);
        plain(&mut app, &mut input, KeyCode::Up);
        assert_eq!(app.composer.text(), "old");
        plain(&mut app, &mut input, KeyCode::Down);
        assert_eq!(app.composer.text(), "draft");
        assert_eq!(app.footer.selected(), None);
        plain(&mut app, &mut input, KeyCode::Down);
        assert!(app.footer.selected().is_some());
        plain(&mut app, &mut input, KeyCode::Up);
        assert_eq!(app.composer.text(), "draft");
        assert_eq!(app.footer.selected(), None);
    }
    #[test]
    fn completion_owns_down_and_opening_it_cancels_footer_ownership() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        render_footer(&mut app);
        plain(&mut app, &mut input, KeyCode::Down);
        assert!(app.footer.selected().is_some());
        app.insert_text("/pre");
        assert_eq!(app.footer.selected(), None);
        plain(&mut app, &mut input, KeyCode::Down);
        assert_eq!(app.footer.selected(), None);
        plain(&mut app, &mut input, KeyCode::Enter);
        assert_eq!(app.turns, ["/pre"]);
    }
    #[test]
    fn removed_footer_chip_consumes_enter_repeats_and_one_fresh_press() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("draft");
        render_footer(&mut app);
        plain(&mut app, &mut input, KeyCode::Down);
        app.footer_fields
            .retain(|field| field.key != litetui_terminal::footer::FieldKey::Thinking);
        render_footer(&mut app);
        input.handle(
            &mut app,
            KeyEvent::new_with_kind(KeyCode::Enter, KeyModifiers::NONE, KeyEventKind::Repeat),
        );
        assert!(app.turns.is_empty());
        plain(&mut app, &mut input, KeyCode::Enter);
        assert!(app.turns.is_empty());
        assert!(app.notice.contains("disappeared"));
        plain(&mut app, &mut input, KeyCode::Enter);
        assert_eq!(app.turns, ["draft"]);
    }
    #[test]
    fn clipboard_and_bracketed_paste_return_footer_ownership_to_draft() {
        for bracketed in [false, true] {
            let mut app = Demo::default();
            let mut input = KeyRouter::default();
            let mut clipboard = TestClipboard {
                read: Some("new".into()),
                ..TestClipboard::default()
            };
            render_footer(&mut app);
            plain(&mut app, &mut input, KeyCode::Down);
            if bracketed {
                paste(&mut app, "new");
            } else {
                handle_key(
                    &mut app,
                    &mut input,
                    KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
                    &mut clipboard,
                );
            }
            assert_eq!(app.footer.selected(), None);
            plain(&mut app, &mut input, KeyCode::Enter);
            assert_eq!(app.turns, ["new"]);
        }
    }

    #[test]
    fn word_chords_move_select_and_delete_at_source_word_starts() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("alpha 雪_beta...gamma");
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.before_cursor(), "alpha 雪_beta...");
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL | KeyModifiers::SHIFT),
        );
        assert_eq!(app.composer.selected_text(), "雪_beta...");
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.text(), "alpha gamma");
        input.handle(&mut app, KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.text(), "gamma");
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Right, KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.before_cursor(), "gamma");
        app.composer.clear();
        app.insert_text("one two 雪");
        input.handle(&mut app, KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        for expected in ["one ", "one two "] {
            input.handle(
                &mut app,
                KeyEvent::new(KeyCode::Right, KeyModifiers::CONTROL | KeyModifiers::SHIFT),
            );
            assert_eq!(app.composer.selected_text(), expected);
        }
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.text(), "雪");
        assert!(app.turns.is_empty());
    }

    #[test]
    fn theme_confirmation_repeat_preserves_draft_but_new_press_submits() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("saved draft");
        app.open_themes();
        app.search_themes("matrix");
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        input.handle(&mut app, enter);
        let mut repeat = enter;
        repeat.kind = KeyEventKind::Repeat;
        input.handle(&mut app, repeat);
        assert_eq!(app.palette().name, "matrix");
        assert!(!app.themes_open);
        assert_eq!(app.composer.text(), "saved draft");
        assert!(app.turns.is_empty());
        input.handle(&mut app, enter);
        assert_eq!(app.turns, ["saved draft"]);
    }

    #[test]
    fn ordinary_repeats_work_while_modal_closing_letter_remains_suppressed() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.open_approval();
        input.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT),
        );
        let x = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        input.handle(&mut app, x);
        let mut repeat = x;
        repeat.kind = KeyEventKind::Repeat;
        input.handle(&mut app, repeat);
        repeat.code = KeyCode::Char('a');
        input.handle(&mut app, repeat);
        assert_eq!(app.composer.text(), "xx");
        repeat.code = KeyCode::Left;
        input.handle(&mut app, repeat);
        assert_eq!(app.composer.before_cursor(), "x");
        let a = KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE);
        input.handle(&mut app, a);
        repeat.code = KeyCode::Char('a');
        input.handle(&mut app, repeat);
        assert_eq!(app.composer.text(), "xaax");
    }

    #[test]
    fn held_key_cannot_confirm_a_newly_opened_modal_and_release_never_types() {
        let mut app = Demo::default();
        let mut input = KeyRouter::default();
        app.insert_text("draft");
        app.open_approval();
        for code in [KeyCode::Enter, KeyCode::Char('a'), KeyCode::Char('d')] {
            let mut repeat = KeyEvent::new(code, KeyModifiers::NONE);
            repeat.kind = KeyEventKind::Repeat;
            input.handle(&mut app, repeat);
            assert!(app.approval_open);
        }
        let a = KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE);
        input.handle(&mut app, a);
        let mut release = a;
        release.kind = KeyEventKind::Release;
        input.handle(&mut app, release);
        assert_eq!(app.composer.text(), "draft");
        input.handle(&mut app, a);
        assert_eq!(app.composer.text(), "drafta");
        app.open_themes();
        let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        input.handle(&mut app, repeat);
        assert!(app.themes_open);
        assert!(app.turns.is_empty());
    }

    #[test]
    fn shifted_control_letters_select_all_without_inserting_or_submitting() {
        let mut app = Demo::default();
        app.insert_text("draft 雪");
        key(
            &mut app,
            KeyEvent::new(
                KeyCode::Char('A'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            ),
        );
        assert_eq!(app.composer.selected_text(), "draft 雪");
        key(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        assert_eq!(app.composer.text(), "x");
        assert!(app.turns.is_empty());
    }

    #[test]
    fn composer_chords_cannot_resolve_a_mock_approval() {
        let mut app = Demo::default();
        app.insert_text("saved draft");
        app.open_approval();
        for (code, modifiers) in [
            (KeyCode::Char('a'), KeyModifiers::CONTROL),
            (
                KeyCode::Char('A'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            ),
            (KeyCode::Char('d'), KeyModifiers::CONTROL),
        ] {
            key(&mut app, KeyEvent::new(code, modifiers));
            assert!(app.approval_open);
            assert_eq!(
                app.tools[3].state,
                litetui_terminal::demo::ToolState::Awaiting
            );
        }
        assert_eq!(app.composer.text(), "saved draft");
    }

    #[test]
    fn composer_keys_edit_and_recall_instead_of_scrolling_the_transcript() {
        let mut app = Demo::default();
        app.insert_text("first");
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.scroll = 4;
        app.insert_text("draft");
        key(&mut app, KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.composer.text(), "first");
        assert_eq!(app.scroll, 4);
        key(&mut app, KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        key(&mut app, KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        key(
            &mut app,
            KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.text(), "t");
        key(
            &mut app,
            KeyEvent::new(KeyCode::Char('e'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.composer.before_cursor(), "t");
        assert_eq!(app.turns, ["first"]);
    }

    #[test]
    fn picker_owns_keys_and_escape_does_not_apply_selection() {
        let mut app = Demo::default();
        app.insert_text("draft");
        for code in [
            KeyCode::F(4),
            KeyCode::Char('m'),
            KeyCode::Down,
            KeyCode::F(6),
            KeyCode::F(5),
        ] {
            key(&mut app, KeyEvent::new(code, KeyModifiers::NONE));
        }
        assert!(app.themes_open);
        assert!(!app.approval_open);
        assert_eq!(app.composer.text(), "draft");
        assert_eq!(app.theme_query, "m");
        assert_eq!(app.palette().name, "ash");
        key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.themes_open);
        assert_eq!(app.palette().name, "ash");
        app.open_themes();
        app.search_themes("matrix");
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.palette().name, "matrix");
        assert!(app.turns.is_empty());
    }

    #[test]
    fn hidden_picker_cannot_apply_and_quit_remains_available() {
        let mut app = Demo::default();
        app.open_themes();
        app.search_themes("matrix");
        app.viewport_too_small = true;
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.palette().name, "ash");
        assert!(key(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
        key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.themes_open);
    }

    #[test]
    fn release_events_do_not_duplicate_actions() {
        let mut app = Demo::default();
        let mut event = KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE);
        event.kind = KeyEventKind::Release;
        key(&mut app, event);
        assert_eq!(app.selected_tool, 1);
    }

    #[test]
    fn approval_traps_composer_keys_and_enter_denies_by_default() {
        let mut app = Demo::default();
        key(&mut app, KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        key(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        assert!(app.composer.text().is_empty());
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(
            app.tools[3].state,
            litetui_terminal::demo::ToolState::Denied
        );
    }

    #[test]
    fn hidden_approval_cannot_be_resolved_by_invisible_shortcuts() {
        let mut app = Demo {
            viewport_too_small: true,
            ..Demo::default()
        };
        app.open_approval();
        for code in [KeyCode::Enter, KeyCode::Char('a'), KeyCode::Char('d')] {
            key(&mut app, KeyEvent::new(code, KeyModifiers::NONE));
            assert_eq!(
                app.tools[3].state,
                litetui_terminal::demo::ToolState::Awaiting
            );
        }
        key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.approval_open);
    }

    #[test]
    fn quit_works_with_a_modal_open() {
        let mut app = Demo::default();
        app.open_approval();
        assert!(key(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
    }
    #[test]
    fn live_quit_remains_available_after_dense_layout_and_with_modal_open() {
        let mut app = Demo::live("claude".into(), "model".into());
        app.observe_live(litetui_terminal::live::Observation::Ready)
            .unwrap();
        app.insert_text("question");
        app.submit();
        app.observe_live(litetui_terminal::live::Observation::Text("\n".repeat(1000)))
            .unwrap();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(36, 14)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        app.open_themes();
        assert!(KeyRouter::default().handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
    }

    fn live_approval_app() -> Demo {
        let mut app = Demo::live("claude".into(), "fixture".into());
        app.observe_live(litetui_terminal::live::Observation::Ready)
            .unwrap();
        app.insert_text("question");
        app.submit();
        app.insert_text("preserved draft");
        app
    }
    fn request_approval(app: &mut Demo, id: &str) {
        let event = litetui_terminal::approvals::Ledger::default().request(&serde_json::json!({
            "type":"tool_approval_requested", "id":id, "tool":"write", "input":{"path":"fixture"},
            "profile":"strict", "why":"writes", "timeout_s":300.0
        })).unwrap();
        app.observe_live(litetui_terminal::live::Observation::Approval(event))
            .unwrap();
    }
    fn close_approval(app: &mut Demo, id: &str) {
        app.observe_live(litetui_terminal::live::Observation::Approval(
            litetui_terminal::approvals::Event::Closed {
                id: id.into(),
                outcome: Some("denied".into()),
                reason: "answered".into(),
            },
        ))
        .unwrap();
    }
    #[test]
    fn live_modal_traps_typing_paste_chords_and_enter_denies_only_once() {
        let mut app = live_approval_app();
        app.open_themes();
        request_approval(&mut app, "a");
        assert!(!app.themes_open);
        let draft = app.composer.text().to_owned();
        paste(&mut app, "not a decision");
        for event in [
            KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE),
            KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL),
            KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE),
        ] {
            key(&mut app, event);
        }
        assert_eq!(app.composer.text(), draft);
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .is_none()
        );
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(
            app.live.as_mut().unwrap().approvals.take_decision(),
            Some(litetui_terminal::approvals::Decision {
                id: "a".into(),
                allow: false
            })
        );
        key(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .is_none()
        );
        assert!(app.has_live_input());
    }
    #[test]
    fn live_modal_repeats_cannot_allow_next_request_and_escape_denies_selected_allow() {
        let mut app = live_approval_app();
        request_approval(&mut app, "a");
        request_approval(&mut app, "b");
        key(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .unwrap()
                .allow
        );
        close_approval(&mut app, "a");
        for code in [KeyCode::Tab, KeyCode::Enter, KeyCode::Esc] {
            let mut event = KeyEvent::new(code, KeyModifiers::NONE);
            event.kind = KeyEventKind::Repeat;
            key(&mut app, event);
        }
        assert!(
            !app.live
                .as_ref()
                .unwrap()
                .approvals
                .current()
                .unwrap()
                .allow
        );
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .is_none()
        );
        key(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(
            app.live.as_mut().unwrap().approvals.take_decision(),
            Some(litetui_terminal::approvals::Decision {
                id: "b".into(),
                allow: false
            })
        );
    }
    #[test]
    fn live_modal_cannot_approve_hidden_or_while_quitting_and_finish_clears_intents() {
        let mut app = live_approval_app();
        request_approval(&mut app, "a");
        app.viewport_too_small = true;
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .is_none()
        );
        app.viewport_too_small = false;
        app.live.as_mut().unwrap().begin_quit();
        key(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .take_decision()
                .is_none()
        );
        assert!(key(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
        app.finish_live(Some("disconnected".into()));
        assert!(!app.has_live_input());
    }
    #[test]
    fn live_modal_source_closure_cannot_leak_held_keys_into_the_draft() {
        let mut app = live_approval_app();
        request_approval(&mut app, "a");
        let mut router = KeyRouter::default();
        let draft = app.composer.text().to_owned();
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        close_approval(&mut app, "a");
        let mut repeat = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        router.handle(&mut app, repeat);
        assert_eq!(
            app.composer.text(),
            draft,
            "a repeat owned by the closed approval must not edit the draft"
        );
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        assert_eq!(app.composer.text(), format!("{draft}x"));
    }
    #[test]
    fn live_modal_source_closure_cannot_leak_a_clipboard_repeat() {
        let mut app = live_approval_app();
        request_approval(&mut app, "a");
        let mut router = KeyRouter::default();
        let mut clipboard = TestClipboard {
            read: Some("secret".into()),
            ..TestClipboard::default()
        };
        let press = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        handle_key(&mut app, &mut router, press, &mut clipboard);
        close_approval(&mut app, "a");
        let mut repeat = press;
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!(
            clipboard.reads, 0,
            "modal-owned repeat cannot initiate clipboard I/O"
        );
        handle_key(&mut app, &mut router, press, &mut clipboard);
        assert_eq!(clipboard.reads, 1);
    }
    #[test]
    fn live_modal_repeat_guard_does_not_guess_physical_keys_from_shifted_punctuation() {
        let mut app = live_approval_app();
        request_approval(&mut app, "a");
        let mut router = KeyRouter::default();
        let draft = app.composer.text().to_owned();
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('1'), KeyModifiers::NONE),
        );
        close_approval(&mut app, "a");
        let mut repeat = KeyEvent::new(KeyCode::Char('!'), KeyModifiers::SHIFT);
        repeat.kind = KeyEventKind::Repeat;
        router.handle(&mut app, repeat);
        assert_eq!(app.composer.text(), draft);
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('!'), KeyModifiers::SHIFT),
        );
        assert_eq!(app.composer.text(), format!("{draft}!"));
    }
    fn request_question(app: &mut Demo) {
        let raw =
            serde_json::from_str(include_str!("../tests/fixtures/question_request.json")).unwrap();
        let event = litetui_terminal::questions::Ledger::default()
            .request(&raw)
            .unwrap();
        app.observe_live(litetui_terminal::live::Observation::Question {
            event,
            settled: None,
        })
        .unwrap();
    }

    #[test]
    fn runtime_shortcuts_preserve_the_draft_and_yield_to_a_late_question() {
        use litetui_terminal::runtime_controls_view::{Field, Intent};
        for (shortcut, expected) in [
            (
                KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL),
                Field::Tools,
            ),
            (
                KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT),
                Field::Profile,
            ),
            (
                KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL),
                Field::Plan,
            ),
        ] {
            let mut app = live_approval_app();
            app.composer.select_all();
            let selected = app.composer.selected_text().to_owned();
            let mut router = KeyRouter::default();
            router.handle(&mut app, shortcut);
            assert_eq!(
                app.live.as_mut().unwrap().runtime_controls.take_intent(),
                Some(Intent::Read(expected))
            );
            paste(&mut app, "unowned paste");
            request_question(&mut app);
            let pending = app.live.as_mut().unwrap().questions.current_mut().unwrap();
            pending.jump(1);
            pending.focus = 1;
            paste(&mut app, "answer");
            assert_eq!(
                app.live
                    .as_ref()
                    .unwrap()
                    .questions
                    .current()
                    .unwrap()
                    .notes[1]
                    .text(),
                "answer"
            );
            assert_eq!(app.composer.selected_text(), selected);
            assert!(app.live.as_ref().unwrap().rows.len() == 1);
        }
    }

    #[test]
    fn live_question_owns_notes_clipboard_and_hidden_input_without_touching_draft() {
        let mut app = live_approval_app();
        app.composer.select_all();
        app.open_themes();
        request_question(&mut app);
        assert!(!app.themes_open);
        let mut router = KeyRouter::default();
        let mut clipboard = TestClipboard {
            read: Some("fixture-secret".into()),
            ..Default::default()
        };
        let copy = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        let paste_key = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        handle_key(&mut app, &mut router, paste_key, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        {
            let pending = app.live.as_mut().unwrap().questions.current_mut().unwrap();
            pending.jump(1);
            pending.focus = 1;
        }
        handle_key(&mut app, &mut router, paste_key, &mut clipboard);
        handle_key(&mut app, &mut router, copy, &mut clipboard);
        assert_eq!(clipboard.reads, 1);
        assert!(clipboard.copied.is_empty());
        assert_eq!(app.composer.text(), "preserved draft");
        assert_eq!(
            app.live
                .as_ref()
                .unwrap()
                .questions
                .current()
                .unwrap()
                .notes[1]
                .text(),
            "fixture-secret"
        );
        app.viewport_too_small = true;
        paste(&mut app, "hidden");
        handle_key(&mut app, &mut router, paste_key, &mut clipboard);
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(clipboard.reads, 1);
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .questions
                .take_decision()
                .is_none()
        );
        app.viewport_too_small = false;
        paste(&mut app, "-typed");
        assert_eq!(
            app.live
                .as_ref()
                .unwrap()
                .questions
                .current()
                .unwrap()
                .notes[1]
                .text(),
            "fixture-secret-typed"
        );
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let decision = app
            .live
            .as_mut()
            .unwrap()
            .questions
            .take_decision()
            .unwrap();
        assert_eq!(decision.action, litetui_terminal::questions::Action::Cancel);
        assert!(decision.answers.is_empty());
    }

    #[test]
    fn asynchronously_closed_question_keeps_held_keys_out_of_draft_and_clipboard() {
        let mut app = live_approval_app();
        request_question(&mut app);
        let mut router = KeyRouter::default();
        let mut clipboard = TestClipboard::default();
        let press = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        handle_key(&mut app, &mut router, press, &mut clipboard);
        app.observe_live(litetui_terminal::live::Observation::Question {
            event: litetui_terminal::questions::Event::Resolved {
                id: "ask-000000000000".into(),
                cancelled: true,
            },
            settled: None,
        })
        .unwrap();
        let mut repeat = press;
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        repeat.code = KeyCode::Char('x');
        repeat.modifiers = KeyModifiers::NONE;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        assert_eq!(app.composer.text(), "preserved draft");
    }
    #[test]
    fn stop_confirmation_defaults_to_keep_going_and_repeats_cannot_force() {
        use litetui_terminal::live::Observation;
        let mut app = live_approval_app();
        app.live.as_mut().unwrap().take_submission();
        app.observe_live(Observation::Submission {
            id: "original".into(),
        })
        .unwrap();
        let mut router = KeyRouter::default();
        let esc = KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE);
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        router.handle(&mut app, esc);
        assert!(app.stop_open());
        router.handle(&mut app, enter);
        assert!(!app.stop_open() && !app.live.as_ref().unwrap().cancelling());
        router.handle(&mut app, esc);
        router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        let mut repeat = enter;
        repeat.kind = KeyEventKind::Repeat;
        router.handle(&mut app, repeat);
        assert!(app.stop_open());
        router.handle(&mut app, enter);
        assert_eq!(
            app.live.as_mut().unwrap().take_abort().as_deref(),
            Some("original")
        );
        router.handle(&mut app, repeat);
        assert!(app.live.as_mut().unwrap().take_abort().is_none());
        assert_eq!(app.composer.text(), "preserved draft");
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::CONTROL));
        router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        router.handle(&mut app, enter);
        assert_eq!(
            app.live.as_mut().unwrap().take_abort().as_deref(),
            Some("original")
        );
        router.handle(&mut app, esc);
        assert!(!app.stop_open(), "two requests maximum");
    }
    #[test]
    fn stop_dialog_owns_clipboard_hidden_keys_and_does_not_answer_pending_input() {
        let mut app = live_approval_app();
        app.live.as_mut().unwrap().take_submission();
        request_question(&mut app);
        let mut router = KeyRouter::default();
        let mut clipboard = TestClipboard {
            read: Some("secret".into()),
            ..Default::default()
        };
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::CONTROL));
        assert!(app.stop_open());
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "bracketed");
        assert_eq!(clipboard.reads, 0);
        app.viewport_too_small = true;
        router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        router.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.stop_open() && !app.live.as_ref().unwrap().cancelling());
        app.viewport_too_small = false;
        router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        router.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .questions
                .take_decision()
                .is_none()
        );
        assert_eq!(app.composer.text(), "preserved draft");
        assert!(router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
    }
    fn control_menu() -> litetui_terminal::model_controls::Choices {
        use litetui_terminal::model_controls::{Choices, Kind};
        Choices::parse(
            Kind::Model,
            &serde_json::json!({
            "backend":"claude","model":"model-A","models":[
                {"slug":"model-A","name":"Model A","current":true,"loaded":null},
                {"slug":"model-B","name":"Model B","current":false,"loaded":null}
            ]}),
            1,
            "session",
        )
        .unwrap()
    }
    #[test]
    fn native_model_controls_own_confirmation_repeats_paste_and_clipboard() {
        use litetui_terminal::{
            live::Observation,
            model_controls::{Intent, Kind, Update},
        };
        let mut app = Demo::live("claude".into(), "model-A".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("preserved draft");
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(3));
        assert_eq!(
            app.live.as_mut().unwrap().controls.take_intent(),
            Some(Intent::Open(Kind::Model))
        );
        app.observe_live(Observation::Controls(Update::Choices(control_menu())))
            .unwrap();
        render_footer(&mut app);
        let mut clipboard = TestClipboard {
            read: Some("not pasted".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "also not pasted");
        assert_eq!(clipboard.reads, 0);
        assert_eq!(app.composer.text(), "preserved draft");
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(matches!(
            app.live.as_mut().unwrap().controls.take_intent(),
            Some(Intent::Select { .. })
        ));
        app.observe_live(Observation::Controls(Update::Choices(control_menu())))
            .unwrap();
        render_footer(&mut app);
        router.handle(
            &mut app,
            KeyEvent::new_with_kind(KeyCode::Enter, KeyModifiers::NONE, KeyEventKind::Repeat),
        );
        assert!(app.live.as_mut().unwrap().controls.take_intent().is_none());
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        plain(&mut app, &mut router, KeyCode::Esc);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new_with_kind(
                KeyCode::Char('v'),
                KeyModifiers::CONTROL,
                KeyEventKind::Repeat,
            ),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        plain(&mut app, &mut router, KeyCode::Enter);
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some("preserved draft")
        );
    }
    #[test]
    fn native_model_controls_cannot_open_during_turn_or_confirm_when_hidden() {
        use litetui_terminal::{live::Observation, model_controls::Update};
        let mut app = Demo::live("claude".into(), "model-A".into());
        app.observe_live(Observation::Ready).unwrap();
        app.live.as_mut().unwrap().busy = true;
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(3));
        assert!(!app.has_live_controls());
        app.live.as_mut().unwrap().busy = false;
        plain(&mut app, &mut router, KeyCode::F(3));
        app.live.as_mut().unwrap().controls.take_intent();
        app.observe_live(Observation::Controls(Update::Choices(control_menu())))
            .unwrap();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(app.live.as_mut().unwrap().controls.take_intent().is_none());
        assert!(router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)
        ));
    }

    #[test]
    fn held_queue_owns_native_keys_and_preserves_composer_and_clipboard() {
        use litetui_terminal::{
            live::Observation,
            restored_queue::{Intent, Snapshot, Update},
        };
        let mut app = Demo::live("codex".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("preserved draft");
        let mut router = KeyRouter::default();
        app.live.as_mut().unwrap().busy = true;
        plain(&mut app, &mut router, KeyCode::F(7));
        assert!(!app.has_live_queue());
        app.live.as_mut().unwrap().busy = false;
        plain(&mut app, &mut router, KeyCode::F(7));
        assert_eq!(
            app.live.as_mut().unwrap().queue.take_intent(),
            Some(Intent::Open)
        );
        let snapshot=Snapshot::parse(&serde_json::json!({
          "session_id":"session","backend":"codex","model":"model","policy":"hold","held":1,"snapshot":"a".repeat(64),
          "entries":[{"id":"one","revision":1,"state":"queued","content":"SAVED",
            "source":"queued","tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":true}]
        }),1).unwrap();
        app.observe_live(Observation::Queue(Update::Snapshot(snapshot)))
            .unwrap();
        render_footer(&mut app);
        let mut clipboard = TestClipboard {
            read: Some("not pasted".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "not pasted");
        assert_eq!(clipboard.reads, 0);
        plain(&mut app, &mut router, KeyCode::Delete);
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Enter);
        assert_eq!(
            app.live.as_mut().unwrap().queue.take_intent(),
            Some(Intent::Cancel {
                revision: 1,
                entry: "one".into()
            })
        );
        assert_eq!(app.composer.text(), "preserved draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(app.has_live_queue());
        app.observe_live(Observation::Queue(Update::Rejected("changed".into())))
            .unwrap();
        assert!(!app.has_live_queue());
    }
    #[test]
    fn conversation_browser_owns_f8_keys_draft_clipboard_and_visible_confirmation() {
        use litetui_terminal::{
            conversation_browser::{Intent, Page, Update},
            live::Observation,
        };
        let mut app = Demo::live("codex".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("preserved draft");
        let mut router = KeyRouter::default();
        app.live.as_mut().unwrap().busy = true;
        plain(&mut app, &mut router, KeyCode::F(8));
        assert!(!app.has_live_browser());
        app.live.as_mut().unwrap().busy = false;
        plain(&mut app, &mut router, KeyCode::F(8));
        assert_eq!(
            app.live.as_mut().unwrap().browser.take_intent(),
            Some(Intent::Refresh)
        );
        let current = "00000000-0000-4000-8000-000000000001";
        let target = "00000000-0000-4000-8000-000000000002";
        let page=Page::parse(&serde_json::json!({"session_id":current,"agent_id":current,"snapshot":"a".repeat(64),"offset":0,"total":1,
            "entries":[{"session_id":target,"agent_id":current,"agent_name":"Agent","title":"Saved target","message_count":2,"updated_at":1,
            "current":false,"can_open":true,"reason":""}],"previous_cursor":null,"next_cursor":null}),1,current).unwrap();
        app.observe_live(Observation::Browser(Update::Page(page)))
            .unwrap();
        render_footer(&mut app);
        let mut clipboard = TestClipboard {
            read: Some("not pasted".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "not pasted");
        assert_eq!(clipboard.reads, 0);
        plain(&mut app, &mut router, KeyCode::Char('o'));
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        render_footer(&mut app);
        app.live.as_mut().unwrap().browser.hide(); // focus lost invalidates drawn confirmation
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Char('o'));
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Enter);
        assert_eq!(
            app.live.as_mut().unwrap().browser.take_intent(),
            Some(Intent::Open {
                revision: 1,
                session: target.into()
            })
        );
        assert_eq!(app.composer.text(), "preserved draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(app.has_live_browser());
        app.observe_live(Observation::Browser(Update::Rejected(
            "selection changed".into(),
        )))
        .unwrap();
        assert!(!app.has_live_browser());
        assert_eq!(app.composer.text(), "preserved draft");
        plain(&mut app, &mut router, KeyCode::F(8));
        assert!(app.has_live_browser());
        let mut small = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        small.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.has_live_browser());
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        assert_eq!(app.composer.text(), "preserved draft");
    }
    #[test]
    fn saved_inspection_retains_browser_selection_through_tiny_viewport_cancel() {
        use litetui_terminal::{
            conversation_browser::{Intent as BrowserIntent, Page, Update as BrowserUpdate},
            history_view::{Intent, Target, Update},
            live::Observation,
        };
        let current = "00000000-0000-4000-8000-000000000001";
        let target = "00000000-0000-4000-8000-000000000002";
        let mut app = Demo::live("custom".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("preserved draft");
        let mut router = KeyRouter::default();
        router.handle(&mut app, KeyEvent::new(KeyCode::Left, KeyModifiers::SHIFT));
        let before_cursor = app.composer.before_cursor().to_owned();
        let selected = app.composer.selected_text().to_owned();
        plain(&mut app, &mut router, KeyCode::F(8));
        assert_eq!(
            app.take_browser_intent().unwrap(),
            Some(BrowserIntent::Refresh)
        );
        let page = Page::parse(&serde_json::json!({
            "session_id":current,"agent_id":current,"snapshot":"a".repeat(64),"offset":0,"total":2,
            "entries":[
                {"session_id":current,"agent_id":current,"agent_name":"Agent","title":"Current", "message_count":1,"updated_at":2,"current":true,"can_open":false,"reason":"Current"},
                {"session_id":target,"agent_id":current,"agent_name":"Agent","title":"Saved target", "message_count":2,"updated_at":1,"current":false,"can_open":false,"reason":"Read only"}
            ],"previous_cursor":null,"next_cursor":null}),7,current).unwrap();
        app.observe_live(Observation::Browser(BrowserUpdate::Page(page)))
            .unwrap();
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Down);
        plain(&mut app, &mut router, KeyCode::Char('i'));
        assert!(app.take_browser_intent().unwrap().is_none());
        let first = app
            .live
            .as_mut()
            .unwrap()
            .saved_history
            .take_intent()
            .unwrap();
        assert!(
            matches!(&first, Intent::Inspect { revision: 7, session, key }
            if session == target && key.target == (Target::Saved { session: target.into() }))
        );
        render_footer(&mut app);
        // A suspended browser must not gain fresh keyboard/mouse authority from drawing.
        app.live
            .as_mut()
            .unwrap()
            .browser
            .key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE));
        assert!(app.take_browser_intent().unwrap().is_none());
        let mut clipboard = TestClipboard {
            read: Some("not pasted".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "not pasted");
        plain(&mut app, &mut router, KeyCode::Enter);
        assert_eq!(clipboard.reads, 0);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        let mut small = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        small.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(
            app.has_live_history(),
            "cancelled sent read must settle before input is released"
        );
        assert!(
            app.has_live_browser(),
            "underlying browser must survive inspection cancellation"
        );
        app.observe_live(Observation::SavedHistory(Update::Rejected {
            key: first.key().clone(),
            reason: "late reply".into(),
        }))
        .unwrap();
        assert!(!app.has_live_history());
        let mut repeat = KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        router.handle(&mut app, repeat);
        assert!(
            app.has_live_browser(),
            "late Escape repeat must not close the preserved browser"
        );
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Char('i'));
        assert!(app.take_browser_intent().unwrap().is_none());
        let second = app
            .live
            .as_mut()
            .unwrap()
            .saved_history
            .take_intent()
            .unwrap();
        assert!(matches!(&second,Intent::Inspect { revision:7,session,.. } if session == target));
        assert!(second.key().generation > first.key().generation);
        assert_eq!(second.key().target, first.key().target);
        assert_eq!(app.composer.text(), "preserved draft");
        assert_eq!(app.composer.before_cursor(), before_cursor);
        assert_eq!(app.composer.selected_text(), selected);
        assert!(app.live.as_ref().unwrap().rows.is_empty());
    }

    #[test]
    fn stale_history_response_cannot_replace_active_chat_notice_or_pending_target() {
        use litetui_terminal::{history_view::Update, live::Observation};
        let mut app = Demo::live("custom".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        let chat = app.live.as_mut().unwrap();
        chat.saved_history.open();
        let old = chat.saved_history.take_intent().unwrap().key().clone();
        chat.saved_history.finish();
        chat.saved_history.open();
        let new = chat.saved_history.take_intent().unwrap().key().clone();
        chat.notice = "Current active notice".into();
        app.observe_live(Observation::SavedHistory(Update::Rejected {
            key: old,
            reason: "Stale read".into(),
        }))
        .unwrap();
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.notice, "Current active notice");
        assert_eq!(chat.saved_history.pending_key(), Some(&new));
        assert!(chat.saved_history.waiting());
    }

    #[test]
    fn conversation_title_editor_routes_clipboard_shift_and_cancel_without_changing_composer() {
        use litetui_terminal::{
            conversation_actions as a, conversation_browser as b, live::Observation,
        };
        use serde_json::json;
        let session = "00000000-0000-4000-8000-000000000001";
        let agent = "11111111-1111-4111-8111-111111111111";
        let mut app = Demo::live("custom".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("preserved draft");
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(8));
        app.live.as_mut().unwrap().browser.take_intent();
        let p=b::Page::parse(&json!({"session_id":session,"agent_id":agent,"snapshot":"a".repeat(64),"offset":0,"total":0,"entries":[],"previous_cursor":null,"next_cursor":null}),1,session).unwrap();
        app.observe_live(Observation::Browser(b::Update::Page(p)))
            .unwrap();
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Char('e'));
        assert_eq!(
            app.live.as_mut().unwrap().browser.take_intent(),
            Some(b::Intent::Action(a::Intent::Read {
                action: a::Action::Rename
            }))
        );
        let identity = a::Identity {
            session: session.into(),
            agent: agent.into(),
            backend: "custom".into(),
            model: "model".into(),
        };
        let s=a::Snapshot::parse(&json!({"version":1,"kind":"conversation_transition_snapshot","session_id":session,"agent_id":agent,"backend":"custom","model":"model","snapshot":"a".repeat(64),"current":{"title":"Old","pending":true,"materialized":false},"effects":{"materialize":true,"lifecycle_hooks":true,"provider_cleanup":false},"actions":{"rename":{"eligible":true,"reason":null},"new":{"eligible":true,"reason":null}}}),&identity).unwrap();
        app.observe_live(Observation::Browser(b::Update::Action(
            a::Update::Snapshot(Box::new(s)),
        )))
        .unwrap();
        render_footer(&mut app);
        let mut clipboard = TestClipboard {
            read: Some("  from\r\nclipboard  ".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        render_footer(&mut app);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('Δ'), KeyModifiers::SHIFT),
            &mut clipboard,
        );
        paste(&mut app, "\nend  ");
        assert_eq!(
            app.live.as_ref().unwrap().browser.actions.draft(),
            "Old  from\r\nclipboard  Δ\nend  "
        );
        assert_eq!(app.composer.text(), "preserved draft");
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.live.as_ref().unwrap().browser.actions.owns_input());
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
    }
    #[test]
    fn backend_selector_owns_model_editor_clipboard_confirmation_and_main_draft() {
        use litetui_terminal::{
            backend_controls::{Catalog, Intent, Update},
            live::Observation,
        };
        let mut app = Demo::live("custom".into(), "old".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("unsent main draft");
        let mut router = KeyRouter::default();
        app.live.as_mut().unwrap().busy = true;
        plain(&mut app, &mut router, KeyCode::F(9));
        assert!(!app.has_live_backend());
        app.live.as_mut().unwrap().busy = false;
        plain(&mut app, &mut router, KeyCode::F(9));
        assert_eq!(
            app.live.as_mut().unwrap().backend_controls.take_intent(),
            Some(Intent::Read)
        );
        let id = "00000000-0000-4000-8000-000000000001";
        let data = serde_json::json!({"version":1,"kind":"support_metadata","readiness_checked":false,"session_id":id,"agent_id":id,"agent_name":"Agent","snapshot":"a".repeat(64),"selected_backend":"custom","configured_backend":"custom","selected_model":"old","codex_engine":{"requested_native":false,"effective":null},"entries":[{"name":"custom","label":"Custom","selected":true,"owned_engine_controls":false},{"name":"lmstudio","label":"LM Studio","selected":false,"owned_engine_controls":true}]});
        app.observe_live(Observation::Backend(Update::Catalog(
            Catalog::parse(&data, 1, id, id).unwrap(),
        )))
        .unwrap();
        render_footer(&mut app);
        let mut clipboard = TestClipboard {
            read: Some("new-model".into()),
            ..Default::default()
        };
        let ctrl_v = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        handle_key(&mut app, &mut router, ctrl_v, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        plain(&mut app, &mut router, KeyCode::Down);
        plain(&mut app, &mut router, KeyCode::Enter);
        handle_key(&mut app, &mut router, ctrl_v, &mut clipboard);
        assert_eq!(clipboard.reads, 1);
        assert_eq!(
            app.live.as_ref().unwrap().backend_controls.model_text(),
            "new-model"
        );
        paste(&mut app, "\nnot-a-model");
        assert_eq!(
            app.live.as_ref().unwrap().backend_controls.model_text(),
            "new-model"
        );
        plain(&mut app, &mut router, KeyCode::Enter);
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .backend_controls
                .take_intent()
                .is_none()
        );
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(matches!(
            app.live.as_mut().unwrap().backend_controls.take_intent(),
            Some(Intent::Select { .. })
        ));
        assert_eq!(app.composer.text(), "unsent main draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        app.observe_live(Observation::Backend(Update::Rejected(
            "stale selection".into(),
        )))
        .unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        plain(&mut app, &mut router, KeyCode::F(9));
        let mut small = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        small.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.has_live_backend());
        assert_eq!(app.composer.text(), "unsent main draft");
    }
    #[test]
    fn settings_key_owns_input_without_edit_or_clipboard_and_closes_when_small() {
        use litetui_terminal::{
            live::Observation,
            settings_view::{Intent, Update},
        };
        let mut app = Demo::live("custom".into(), "old".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("unsent settings draft");
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(10));
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(Intent::Read)
        );
        let mut clipboard = TestClipboard {
            read: Some("must not paste".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "must not paste");
        plain(&mut app, &mut router, KeyCode::Char('x'));
        assert_eq!(clipboard.reads, 0);
        assert_eq!(app.composer.text(), "unsent settings draft");
        app.observe_live(Observation::Settings(Update::Rejected(
            "source unavailable".into(),
        )))
        .unwrap();
        let mut small = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        small.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.has_live_settings());
        assert_eq!(app.composer.text(), "unsent settings draft");
    }
    #[test]
    fn settings_editor_owns_numeric_clipboard_and_preserves_main_draft() {
        use litetui_terminal::{live::Observation, settings_editor, settings_view};
        let mut app = Demo::live("custom".into(), "old".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("MAIN DRAFT");
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(10));
        app.live.as_mut().unwrap().settings.take_intent();
        app.observe_live(Observation::Settings(settings_view::Update::Snapshot(
            settings_view::Snapshot {
                session: "owned".into(),
                rows: vec![settings_view::Row {
                    name: "max_tokens_chat".into(),
                    label: "Chat output".into(),
                    value_type: "int".into(),
                    source: "settings".into(),
                    read_only: false,
                    secret: false,
                    apply: "next_turn".into(),
                    requested: "101".into(),
                    effective: "101".into(),
                    deferred: false,
                }],
            },
        )))
        .unwrap();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 28)).unwrap();
        terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Char('e'));
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::Editor(settings_editor::Intent::Read))
        );
        let snapshot = settings_editor::Snapshot {
            session: "owned".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "old".into(),
            token: "a".repeat(64),
            revisions: serde_json::json!({"global":"absent","conversation":"absent","agent":"absent"}),
            budgets: [
                settings_editor::Budget {
                    saved: 101,
                    effective: 101,
                    editable: true,
                    reason: None,
                },
                settings_editor::Budget {
                    saved: 202,
                    effective: 202,
                    editable: true,
                    reason: None,
                },
            ],
        };
        app.observe_live(Observation::Settings(settings_view::Update::Editor(
            settings_editor::Update::Snapshot(snapshot),
        )))
        .unwrap();
        terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Home);
        for _ in 0..3 {
            plain(&mut app, &mut router, KeyCode::Delete);
        }
        let mut clipboard = TestClipboard {
            read: Some("257".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        assert_eq!(app.live.as_ref().unwrap().settings.editor.draft(), "257");
        paste(&mut app, "\n10");
        assert_eq!(app.live.as_ref().unwrap().settings.editor.draft(), "257");
        assert_eq!(app.composer.text(), "MAIN DRAFT");
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(matches!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::Editor(
                settings_editor::Intent::Validate { value: 257, .. }
            ))
        ));
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(
            clipboard.reads, 1,
            "Waiting editor must not read host clipboard"
        );
        app.observe_live(Observation::Settings(settings_view::Update::Editor(
            settings_editor::Update::Rejected("stale".into()),
        )))
        .unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.live.as_ref().unwrap().settings.editor.owns_input());
        assert!(app.has_live_settings());
        assert_eq!(app.composer.text(), "MAIN DRAFT");
    }
    fn compaction_preferences_surface(
        field: &str,
        selected_edit: bool,
    ) -> (
        Demo,
        KeyRouter,
        litetui_terminal::compaction_settings::Snapshot,
        u64,
    ) {
        use litetui_terminal::{compaction_settings as edit, live::Observation, settings_view};
        let data: serde_json::Value = serde_json::from_str(include_str!(
            "../tests/fixtures/compaction_settings_snapshot.json"
        ))
        .unwrap();
        let snapshot = edit::Snapshot::parse(
            &data,
            data["session_id"].as_str().unwrap(),
            data["agent_id"].as_str().unwrap(),
            "custom",
            "test",
        )
        .unwrap();
        let mut app = Demo::live("custom".into(), "test".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("KEEP α 雪 THE COMPOSER DRAFT");
        app.composer
            .move_cursor(litetui_terminal::composer::Motion::WordLeft, true);
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(10));
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::Read)
        );
        let setting = snapshot.field(field).unwrap();
        app.observe_live(Observation::Settings(settings_view::Update::Snapshot(
            settings_view::Snapshot {
                session: snapshot.session.clone(),
                rows: vec![settings_view::Row {
                    name: field.into(),
                    label: "Compaction preference".into(),
                    value_type: "source".into(),
                    source: "settings".into(),
                    read_only: false,
                    secret: false,
                    apply: setting.apply_timing.clone(),
                    requested: setting.saved.to_string(),
                    effective: setting.effective.to_string(),
                    deferred: false,
                }],
            },
        )))
        .unwrap();
        plain(
            &mut app,
            &mut router,
            KeyCode::Char(if selected_edit { 'e' } else { 'c' }),
        );
        assert!(
            app.live.as_mut().unwrap().settings.take_intent().is_none(),
            "Hidden inspector cannot open an editor"
        );
        render_footer(&mut app);
        plain(
            &mut app,
            &mut router,
            KeyCode::Char(if selected_edit { 'e' } else { 'c' }),
        );
        let Some(settings_view::Intent::Compaction(edit::Intent::Read { generation })) =
            app.live.as_mut().unwrap().settings.take_intent()
        else {
            panic!("F10 compaction preferences did not open")
        };
        (app, router, snapshot, generation)
    }
    #[test]
    fn compaction_preferences_route_owns_numeric_clipboard_and_preserves_main_selection() {
        use litetui_terminal::{compaction_settings as edit, live::Observation, settings_view};
        let (mut app, mut router, snapshot, generation) =
            compaction_preferences_surface("compact_keep_recent", false);
        let before = (
            app.composer.text().to_owned(),
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        let mut clipboard = TestClipboard {
            read: Some("0".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        app.observe_live(Observation::Settings(settings_view::Update::Compaction(
            edit::Update::Snapshot {
                generation,
                snapshot: Box::new(snapshot.clone()),
            },
        )))
        .unwrap();
        render_footer(&mut app);
        plain(&mut app, &mut router, KeyCode::Home);
        plain(&mut app, &mut router, KeyCode::Delete);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        assert_eq!(app.live.as_ref().unwrap().settings.compaction.draft(), "0");
        paste(&mut app, "\n9");
        assert_eq!(app.live.as_ref().unwrap().settings.compaction.draft(), "0");
        plain(&mut app, &mut router, KeyCode::Enter);
        let Some(settings_view::Intent::Compaction(edit::Intent::Validate {
            generation: next,
            field,
            value,
            ..
        })) = app.live.as_mut().unwrap().settings.take_intent()
        else {
            panic!("Missing explicit validation")
        };
        assert_eq!(field, "compact_keep_recent");
        assert_eq!(value, serde_json::json!(0));
        app.observe_live(Observation::Settings(settings_view::Update::Compaction(
            edit::Update::Snapshot {
                generation,
                snapshot: Box::new(snapshot),
            },
        )))
        .unwrap();
        assert!(app.live.as_ref().unwrap().settings.compaction.waiting);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        app.observe_live(Observation::Settings(settings_view::Update::Compaction(
            edit::Update::Rejected {
                generation: next,
                reason: "stale source".into(),
            },
        )))
        .unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.live.as_ref().unwrap().settings.compaction.owns_input());
        assert!(app.has_live_settings());
        assert_eq!(
            (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            ),
            before
        );
        assert!(app.live.as_ref().unwrap().rows.is_empty());
    }
    #[test]
    fn compaction_boolean_editor_blocks_clipboard_and_tiny_escape_preserves_inspector() {
        use litetui_terminal::{compaction_settings as edit, live::Observation, settings_view};
        let (mut app, mut router, snapshot, generation) =
            compaction_preferences_surface("clear_screen_after_compact", true);
        app.observe_live(Observation::Settings(settings_view::Update::Compaction(
            edit::Update::Snapshot {
                generation,
                snapshot: Box::new(snapshot),
            },
        )))
        .unwrap();
        render_footer(&mut app);
        assert_eq!(
            app.live
                .as_ref()
                .unwrap()
                .settings
                .compaction
                .selected_field(),
            "clear_screen_after_compact"
        );
        let mut clipboard = TestClipboard {
            read: Some("false".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paste(&mut app, "false");
        assert_eq!(clipboard.reads, 0);
        plain(&mut app, &mut router, KeyCode::Left);
        plain(&mut app, &mut router, KeyCode::Enter);
        let Some(settings_view::Intent::Compaction(edit::Intent::Validate {
            generation,
            field,
            value,
            ..
        })) = app.live.as_mut().unwrap().settings.take_intent()
        else {
            panic!("Missing boolean validation")
        };
        assert_eq!(field, "clear_screen_after_compact");
        assert_eq!(value, serde_json::json!(false));
        app.observe_live(Observation::Settings(settings_view::Update::Compaction(
            edit::Update::Rejected {
                generation,
                reason: "controlled refusal".into(),
            },
        )))
        .unwrap();
        let mut small = Terminal::new(ratatui::backend::TestBackend::new(35, 13)).unwrap();
        small.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.live.as_ref().unwrap().settings.compaction.owns_input());
        assert!(
            app.has_live_settings(),
            "Tiny Escape must preserve underlying F10"
        );
        plain(&mut app, &mut router, KeyCode::Esc);
        assert!(!app.has_live_settings());
        assert_eq!(app.composer.text(), "KEEP α 雪 THE COMPOSER DRAFT");
    }
    #[test]
    fn command_palette_owns_f11_exact_argument_paste_and_cancel_repeats() {
        use litetui_terminal::{
            command_palette::{Action, Catalog, Entry, Intent, Update},
            live::Observation,
        };
        let mut app = Demo::live("custom".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("main composer draft");
        let mut router = KeyRouter::default();
        plain(&mut app, &mut router, KeyCode::F(11));
        assert_eq!(
            app.live.as_mut().unwrap().commands.take_intent(),
            Some(Intent::Read { skills: false })
        );
        let mut clipboard = TestClipboard {
            read: Some("arg\r\n  ".into()),
            ..Default::default()
        };
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        let c = Catalog {
            session: "owned".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "model".into(),
            snapshot: "a".repeat(64),
            entries: vec![Entry {
                id: "b".repeat(64),
                kind: "skill".into(),
                owner: "skills".into(),
                title: "Exact".into(),
                help: "Help".into(),
                group: "tools".into(),
                group_label: "Tools".into(),
                tokens: vec!["/Exact".into()],
                palette: false,
                action: Some(Action::SkillPrepare),
                arguments: true,
                unavailable: String::new(),
                palette_order: None,
                completion_order: Some(0),
                skill_order: Some(0),
                source: Some("test".into()),
            }],
        };
        app.observe_live(Observation::Commands(Update::Catalog {
            catalog: c,
            skills: false,
        }))
        .unwrap();
        let mut t = Terminal::new(ratatui::backend::TestBackend::new(80, 28)).unwrap();
        t.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Tab);
        t.draw(|f| ui::draw(f, &mut app)).unwrap();
        plain(&mut app, &mut router, KeyCode::Enter);
        t.draw(|f| ui::draw(f, &mut app)).unwrap();
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 1);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT),
            &mut clipboard,
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char(':'), KeyModifiers::SHIFT),
            &mut clipboard,
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('Δ'), KeyModifiers::SHIFT),
            &mut clipboard,
        );
        paste(&mut app, "unicode Δ\n");
        assert_eq!(
            app.live.as_ref().unwrap().commands.arguments(),
            "arg\r\n  A:Δunicode Δ\n"
        );
        assert_eq!(app.composer.text(), "main composer draft");
        plain(&mut app, &mut router, KeyCode::Enter);
        assert!(
            matches!(app.live.as_mut().unwrap().commands.take_intent(),Some(Intent::Prepare{arguments,..}) if arguments=="arg\r\n  A:Δunicode Δ\n")
        );
        plain(&mut app, &mut router, KeyCode::Esc);
        app.observe_live(Observation::Commands(Update::Rejected(
            "cancelled read settled".into(),
        )))
        .unwrap();
        assert!(!app.has_live_commands());
        let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        assert_eq!(app.composer.text(), "main composer draft");
    }
}

#[cfg(test)]
mod engine_router_tests {
    use super::*;
    use litetui_terminal::{
        engine_controls::{Admission, Intent},
        live::Observation,
    };
    use ratatui::{Terminal, backend::TestBackend};
    #[test]
    fn management_panel_owns_input_preserves_draft_and_reopens_when_stopped() {
        let mut app = Demo::live("strata".into(), "chosen".into());
        app.insert_text("unsent engine draft");
        app.observe_live(Observation::EngineAdmission(Admission::ManagementOnly))
            .unwrap();
        assert!(app.has_live_engine());
        assert_eq!(
            app.live.as_mut().unwrap().engine.take_intent(),
            Some(Intent::Read)
        );
        paste(&mut app, "must not replace draft");
        assert_eq!(app.composer.text(), "unsent engine draft");
        app.live.as_mut().unwrap().engine.observe(
            litetui_terminal::engine_controls::Update::Rejected("no configured plan".into()),
        );
        let mut terminal = Terminal::new(TestBackend::new(41, 17)).unwrap();
        terminal
            .draw(|frame| litetui_terminal::ui::draw(frame, &mut app))
            .unwrap();
        key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.has_live_engine());
        key(&mut app, KeyEvent::new(KeyCode::F(12), KeyModifiers::NONE));
        assert!(app.has_live_engine());
        assert_eq!(app.composer.text(), "unsent engine draft");
        assert!(app.live.as_mut().unwrap().submit("forbidden").is_err());
    }
}

#[cfg(test)]
mod theme_router_tests {
    use super::*;
    use litetui_terminal::{live::Observation, theme_controls as themes};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;
    #[derive(Default)]
    struct Clipboard {
        reads: usize,
    }
    impl litetui_terminal::clipboard::Clipboard for Clipboard {
        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.reads += 1;
            Ok(Some(" 雪\nraw".into()))
        }
        fn copy_text(&mut self, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    fn snapshot() -> themes::Snapshot {
        let id = themes::Identity {
            session: "12345678-1234-4123-8123-123456789abc".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "test".into(),
        };
        let mut palette = json!({});
        for k in themes::COLORS {
            palette[k] = json!([10, 20, 30]);
        }
        let tokens=themes::TOKENS.iter().enumerate().map(|(i,k)|json!({"key":k,"required":i<10,"raw":"#123456","resolved":"#123456","source":"explicit"})).collect::<Vec<_>>();
        let v = json!({"version":1,"kind":"theme_snapshot","session_id":id.session,"agent_id":id.agent,"backend":id.backend,"model":id.model,
            "snapshot":"a".repeat(64),"revisions":{"global":"absent","conversation":"absent","agent":"absent"},"scope":"device","apply_timing":"immediate",
            "selection":{"saved_name":"Theme","saved_definition":"b".repeat(64),"requested_name":"Theme","runtime_name":"Theme","effective_name":"Theme","effective_row_id":"b".repeat(64),"effective_reason":null,"effective_definition":"b".repeat(64)},
            "eligible":true,"reason":null,"invalid":[],"registered_definitions":{"Theme":"b".repeat(64)},"rows":[{"row_id":"b".repeat(64),"name":"Theme","definition":"b".repeat(64),"source":"custom","palette":palette,"tokens":tokens,"selectable":true,"reason":null}]});
        themes::Snapshot::parse(&v, &id).unwrap()
    }
    fn paint(app: &mut Demo, w: u16, h: u16) {
        Terminal::new(TestBackend::new(w, h))
            .unwrap()
            .draw(|f| ui::draw(f, app))
            .unwrap();
    }
    #[test]
    fn live_f4_owns_shift_paste_clipboard_and_cancel_without_changing_chat_draft() {
        let mut app = Demo::live("custom".into(), "test".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("MAIN DRAFT");
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE),
            &mut clipboard,
        );
        assert!(app.has_live_themes());
        assert!(!app.themes_open);
        assert_eq!(
            app.live.as_mut().unwrap().themes.take_intent(),
            Some(themes::Intent::Read)
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(
            clipboard.reads, 0,
            "pending read owns input without clipboard access"
        );
        app.observe_live(Observation::Theme(themes::Update::Snapshot(Box::new(
            snapshot(),
        ))))
        .unwrap();
        paint(&mut app, 80, 28);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::F(2), KeyModifiers::NONE),
            &mut clipboard,
        );
        paint(&mut app, 42, 18);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT),
            &mut clipboard,
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paint(&mut app, 42, 18);
        paste(&mut app, ":Δ");
        assert_eq!(
            app.live.as_ref().unwrap().themes.draft_name(),
            "A 雪\nraw:Δ"
        );
        assert_eq!(app.composer.text(), "MAIN DRAFT");
        paint(&mut app, 35, 13);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
            &mut clipboard,
        );
        assert!(!app.has_live_themes());
        assert_eq!(app.composer.text(), "MAIN DRAFT");
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
    }
    #[test]
    fn management_only_theme_read_does_not_open_prompt_admission() {
        let mut app = Demo::live("ninfer".into(), "chosen".into());
        app.observe_live(Observation::EngineAdmission(
            litetui_terminal::engine_controls::Admission::ManagementOnly,
        ))
        .unwrap();
        app.live.as_mut().unwrap().engine.finish();
        app.open_themes();
        assert!(app.has_live_themes());
        assert!(!app.live.as_ref().unwrap().ready);
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .submit("must not execute")
                .is_err()
        );
    }
}

#[cfg(test)]
mod rewrite_router_tests {
    use super::*;
    use litetui_terminal::{
        conversation_rewrite as r, history as h, history_view as hv, live::Observation,
    };
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;
    #[derive(Default)]
    struct Clipboard {
        reads: usize,
    }
    impl litetui_terminal::clipboard::Clipboard for Clipboard {
        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.reads += 1;
            Ok(Some(" 雪\nraw".into()))
        }
        fn copy_text(&mut self, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    fn paint(app: &mut Demo, width: u16, height: u16) {
        Terminal::new(TestBackend::new(width, height))
            .unwrap()
            .draw(|f| ui::draw(f, app))
            .unwrap();
    }
    fn selection() -> r::Selection {
        let session = "12345678-1234-4123-8123-123456789abc".to_owned();
        r::Selection {
            history_key: hv::ReadKey {
                target: hv::Target::Current,
                generation: 1,
            },
            identity: h::Identity {
                from_session: session.clone(),
                session,
                agent: "22345678-1234-4123-8123-123456789abc".into(),
                backend: "custom".into(),
                model: "test".into(),
                scope: h::Scope::Current,
                catalog: None,
            },
            history_snapshot: "a".repeat(64),
            context_sha256: "b".repeat(64),
            messages: 3,
            message_index: 1,
            message_sha256: "c".repeat(64),
        }
    }
    fn open(action: r::Action) -> (Demo, r::RequestKey) {
        let mut app = Demo::live("custom".into(), "test".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("PRESERVED COMPOSER DRAFT");
        app.live
            .as_mut()
            .unwrap()
            .rewrite
            .open(action, selection())
            .unwrap();
        let intent = app.live.as_mut().unwrap().rewrite.take_intent().unwrap();
        (app, intent.key().clone())
    }
    fn load(app: &mut Demo, key: r::RequestKey) {
        let mut v = selection().command(r::SNAPSHOT);
        for(k,value)in json!({"kind":"conversation_rewrite_snapshot","snapshot":"d".repeat(64),"original_text":"original","original_bytes":8,
            "prefix_messages":1,"prefix_context_sha256":"e".repeat(64),"discarded_messages":1,
            "actions":{"edit":{"eligible":true,"reason":null},"retry":{"eligible":true,"reason":null}}}).as_object().unwrap(){v[k]=value.clone();}
        let snapshot = r::Snapshot::parse(&v, &selection()).unwrap();
        app.observe_live(Observation::Rewrite(r::Update::Snapshot {
            key,
            snapshot: Box::new(snapshot),
        }))
        .unwrap();
    }
    #[test]
    fn edit_owns_unicode_clipboard_and_bracketed_paste_while_retry_never_reads_clipboard() {
        for action in [r::Action::Edit, r::Action::Retry] {
            let (mut app, key) = open(action);
            let mut router = KeyRouter::default();
            let mut clipboard = Clipboard::default();
            handle_key(
                &mut app,
                &mut router,
                KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
                &mut clipboard,
            );
            assert_eq!(
                clipboard.reads, 0,
                "pending source read cannot read clipboard"
            );
            load(&mut app, key);
            paint(&mut app, 100, 40);
            handle_key(
                &mut app,
                &mut router,
                KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
                &mut clipboard,
            );
            paint(&mut app, 100, 40);
            paste(&mut app, ":Δ");
            assert_eq!(clipboard.reads, usize::from(action == r::Action::Edit));
            assert_eq!(
                app.live.as_ref().unwrap().rewrite.draft(),
                if action == r::Action::Edit {
                    "original 雪\nraw:Δ"
                } else {
                    "original"
                }
            );
            assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
        }
    }
    #[test]
    fn tiny_cancel_and_late_reply_do_not_release_held_keys_into_main_draft() {
        let (mut app, key) = open(r::Action::Edit);
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        paint(&mut app, 35, 13);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
            &mut clipboard,
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
            &mut clipboard,
        );
        assert!(
            app.has_live_rewrite(),
            "cancelled in-flight read still owns input"
        );
        app.observe_live(Observation::Rewrite(r::Update::Rejected {
            key,
            reason: "late read".into(),
        }))
        .unwrap();
        assert!(!app.has_live_rewrite());
        paint(&mut app, 100, 40);
        let mut repeat = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
        assert_eq!(clipboard.reads, 0);
    }
    #[test]
    fn hidden_review_editor_never_accepts_paste_or_clipboard() {
        let (mut app, key) = open(r::Action::Edit);
        load(&mut app, key);
        paint(&mut app, 100, 40);
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        paint(&mut app, 35, 13);
        paste(&mut app, "hidden");
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        assert_eq!(app.live.as_ref().unwrap().rewrite.draft(), "original");
        assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
    }
    fn open_compact() -> (Demo, litetui_terminal::conversation_compact::RequestKey) {
        let (mut app, _) = open(r::Action::Edit);
        app.live.as_mut().unwrap().rewrite.finish();
        let selected = selection();
        let selected = litetui_terminal::conversation_compact::Selection {
            history_key: selected.history_key,
            identity: selected.identity,
            history_snapshot: selected.history_snapshot,
            context_sha256: selected.context_sha256,
            messages: selected.messages,
        };
        app.live.as_mut().unwrap().compact.open(selected).unwrap();
        let intent = app.live.as_mut().unwrap().compact.take_intent().unwrap();
        (app, intent.key().clone())
    }
    fn load_compact(app: &mut Demo, key: litetui_terminal::conversation_compact::RequestKey) {
        use litetui_terminal::conversation_compact as c;
        use sha2::{Digest, Sha256};
        let config = json!({"keep_recent":2,"max_tokens":1024,"max_tool_iters":4,"thinking_level":"default","tools_enabled":false,"wake_after_compact":false,"clear_screen_after_compact":true});
        let mut value = key.selection.command(c::SNAPSHOT);
        for(k,v)in json!({"kind":"conversation_compact_snapshot","snapshot":"d".repeat(64),"eligible":true,"reason":null,"config":config,
            "plan":{"messages":3,"system_messages":1,"head_messages":1,"tail_messages":1,"head_context_sha256":"e".repeat(64),"tail_context_sha256":"f".repeat(64),"prompt_sha256":"1".repeat(64),"config_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&config).unwrap()))}}).as_object().unwrap(){value[k]=v.clone();}
        let snapshot = c::Snapshot::parse(&value, &key.selection).unwrap();
        app.observe_live(Observation::Compact(c::Update::Snapshot {
            key,
            snapshot: Box::new(snapshot),
        }))
        .unwrap();
    }
    #[test]
    fn compact_hint_owns_unicode_clipboard_and_paste_without_touching_main_draft() {
        let (mut app, key) = open_compact();
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        load_compact(&mut app, key);
        paint(&mut app, 100, 40);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        paint(&mut app, 100, 40);
        paste(&mut app, ":Δ");
        assert_eq!(app.live.as_ref().unwrap().compact.draft(), " 雪\nraw:Δ");
        assert_eq!(clipboard.reads, 1);
        assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
    }
    #[test]
    fn compact_tiny_cancel_waits_for_pending_read_and_does_not_leak_repeat_keys() {
        let (mut app, key) = open_compact();
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        paint(&mut app, 35, 13);
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
            &mut clipboard,
        );
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
            &mut clipboard,
        );
        assert!(app.has_live_compact());
        app.observe_live(Observation::Compact(
            litetui_terminal::conversation_compact::Update::Rejected {
                key,
                reason: "late read".into(),
            },
        ))
        .unwrap();
        assert!(!app.has_live_compact());
        paint(&mut app, 100, 40);
        let mut repeat = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
        assert_eq!(clipboard.reads, 0);
    }
    #[test]
    fn hidden_compact_hint_rejects_paste_and_clipboard() {
        let (mut app, key) = open_compact();
        load_compact(&mut app, key);
        paint(&mut app, 100, 40);
        paint(&mut app, 35, 13);
        paste(&mut app, "hidden");
        let mut router = KeyRouter::default();
        let mut clipboard = Clipboard::default();
        handle_key(
            &mut app,
            &mut router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            &mut clipboard,
        );
        assert_eq!(clipboard.reads, 0);
        assert!(app.live.as_ref().unwrap().compact.draft().is_empty());
        assert_eq!(app.composer.text(), "PRESERVED COMPOSER DRAFT");
    }
}

#[cfg(test)]
mod work_router_tests {
    use super::*;
    use litetui_terminal::{live::Observation, work_protocol as p, work_view as v};
    use ratatui::backend::TestBackend;
    use serde_json::{Value, json};
    #[derive(Default)]
    struct Clipboard {
        reads: usize,
        copies: usize,
    }
    impl litetui_terminal::clipboard::Clipboard for Clipboard {
        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.reads += 1;
            Ok(Some("PRIVATE".into()))
        }
        fn copy_text(&mut self, _: &str) -> Result<(), String> {
            self.copies += 1;
            Ok(())
        }
    }
    fn paint(app: &mut Demo, w: u16, h: u16) {
        Terminal::new(TestBackend::new(w, h))
            .unwrap()
            .draw(|f| ui::draw(f, app))
            .unwrap();
    }
    fn fixture() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/work_inspection.json")).unwrap()
    }
    fn setup() -> (Demo, KeyRouter) {
        let mut app = Demo::live("custom".into(), "fixture".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("active prompt");
        app.submit();
        app.live.as_mut().unwrap().take_submission();
        app.insert_text("KEEP α 雪 MAIN DRAFT");
        app.composer.move_cursor(Motion::WordLeft, true);
        let mut router = KeyRouter::default();
        router.handle(&mut app, KeyEvent::new(KeyCode::F(1), KeyModifiers::NONE));
        assert!(app.has_live_work());
        assert!(app.live.as_ref().unwrap().busy);
        (app, router)
    }
    fn read(app: &mut Demo) {
        let f = fixture();
        let raw = &f["snapshot"];
        let id = p::Identity {
            session: raw["session_id"].as_str().unwrap().into(),
            agent: raw["agent_id"].as_str().unwrap().into(),
            backend: "custom".into(),
            model: raw["model"].as_str().unwrap().into(),
        };
        let snapshot = p::Snapshot::parse(raw, &id).unwrap();
        let i = app.live.as_mut().unwrap().work.take_intent().unwrap();
        app.observe_live(Observation::Work(v::Update {
            generation: i.generation,
            data: v::Data::Snapshot(Box::new(snapshot.clone())),
        }))
        .unwrap();
        let i = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let page = p::Page::parse(&f["tasks"], &snapshot, p::Section::Tasks, 0, 32).unwrap();
        app.observe_live(Observation::Work(v::Update {
            generation: i.generation,
            data: v::Data::Page(page),
        }))
        .unwrap();
    }
    fn draft(app: &Demo) -> (String, String, String) {
        (
            app.composer.text().into(),
            app.composer.before_cursor().into(),
            app.composer.selected_text().into(),
        )
    }
    #[test]
    fn f1_work_opens_during_chat_and_traps_draft_and_clipboard_input() {
        let (mut app, mut router) = setup();
        let before = draft(&app);
        read(&mut app);
        paint(&mut app, 100, 34);
        let mut clipboard = Clipboard::default();
        for c in ['v', 'c', 'w'] {
            handle_key(
                &mut app,
                &mut router,
                KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL),
                &mut clipboard,
            );
        }
        paste(&mut app, "bracketed private paste");
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE),
        );
        assert_eq!(draft(&app), before);
        assert_eq!((clipboard.reads, clipboard.copies), (0, 0));
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.has_live_work());
        let mut repeat = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        router.handle(&mut app, repeat);
        assert_eq!(draft(&app), before);
        assert!(app.live.as_ref().unwrap().busy);
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 1);
    }
    #[test]
    fn approval_and_question_preempt_work_without_losing_capture() {
        let (mut app, mut router) = setup();
        read(&mut app);
        paint(&mut app, 100, 34);
        let before = draft(&app);
        let event=litetui_terminal::approvals::Ledger::default().request(&json!({"type":"tool_approval_requested","id":"work-approval","tool":"write","input":{"path":"fixture"},"profile":"strict","why":"writes","timeout_s":300.0})).unwrap();
        app.observe_live(Observation::Approval(event)).unwrap();
        paint(&mut app, 100, 34);
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE),
        );
        assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
        app.observe_live(Observation::Approval(
            litetui_terminal::approvals::Event::Closed {
                id: "work-approval".into(),
                outcome: Some("denied".into()),
                reason: "fixture".into(),
            },
        ))
        .unwrap();
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE),
        );
        assert!(
            app.live.as_mut().unwrap().work.take_intent().is_none(),
            "hidden Work must repaint first"
        );
        let raw =
            serde_json::from_str(include_str!("../tests/fixtures/question_request.json")).unwrap();
        let event = litetui_terminal::questions::Ledger::default()
            .request(&raw)
            .unwrap();
        app.observe_live(Observation::Question {
            event,
            settled: None,
        })
        .unwrap();
        paint(&mut app, 100, 34);
        assert!(app.has_live_question() && app.has_live_work());
        router.handle(&mut app, KeyEvent::new(KeyCode::F(1), KeyModifiers::NONE));
        assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
        assert_eq!(draft(&app), before);
    }
    #[test]
    fn tiny_context_escape_returns_to_work_while_the_read_drains() {
        let (mut app, mut router) = setup();
        read(&mut app);
        paint(&mut app, 100, 34);
        let before = draft(&app);
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE),
        );
        let pending = app.live.as_mut().unwrap().work.take_intent().unwrap();
        assert!(matches!(pending.request, v::Request::Context(_)));
        paint(&mut app, 20, 10);
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let work = &app.live.as_ref().unwrap().work;
        assert!(work.open && work.waiting && !work.context_open());
        app.observe_live(Observation::Work(v::Update {
            generation: pending.generation,
            data: v::Data::Rejected("read retired".into()),
        }))
        .unwrap();
        assert!(app.has_live_work());
        assert!(!app.live.as_ref().unwrap().work.waiting);
        assert_eq!(draft(&app), before);

        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::CONTROL));
        assert!(app.stop_open());
    }
    #[test]
    fn tiny_terminal_can_close_work_and_quit_clears_stale_updates() {
        let (mut app, mut router) = setup();
        let pending = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let before = draft(&app);
        paint(&mut app, 20, 5);
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(
            app.has_live_work(),
            "dispatched read must drain before composer admission"
        );
        app.observe_live(Observation::Work(v::Update {
            generation: pending.generation,
            data: v::Data::Rejected("late".into()),
        }))
        .unwrap();
        assert!(!app.has_live_work());
        assert_eq!(draft(&app), before);
        app.live.as_mut().unwrap().begin_quit();
        app.observe_live(Observation::Work(v::Update {
            generation: pending.generation,
            data: v::Data::Paused,
        }))
        .unwrap();
        assert!(!app.has_live_work());
        assert!(app.live.as_ref().unwrap().quitting);
    }
}

#[cfg(test)]
mod work_controls_clipboard_tests {
    use super::*;
    use litetui_terminal::{
        live::Observation, work_controls as c, work_protocol as p, work_view as v,
    };
    use ratatui::backend::TestBackend;
    use serde_json::{Value, json};
    #[derive(Default)]
    struct Clipboard {
        reads: usize,
        copies: usize,
    }
    impl litetui_terminal::clipboard::Clipboard for Clipboard {
        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.reads += 1;
            Ok(Some("CLIPBOARD\nΩ".into()))
        }
        fn copy_text(&mut self, _: &str) -> Result<(), String> {
            self.copies += 1;
            Ok(())
        }
    }
    fn paint(app: &mut Demo) {
        Terminal::new(TestBackend::new(100, 36))
            .unwrap()
            .draw(|f| ui::draw(f, app))
            .unwrap();
    }
    fn flow() -> Value {
        serde_json::from_str::<Value>(include_str!("../tests/fixtures/work_controls.json")).unwrap()
            ["jobs_job_update_off"]
            .clone()
    }
    fn setup(active: bool) -> (Demo, KeyRouter, Value) {
        let f = flow();
        let raw = &f["snapshot"];
        let s = p::Snapshot::parse(
            raw,
            &p::Identity {
                session: raw["session_id"].as_str().unwrap().into(),
                agent: raw["agent_id"].as_str().unwrap().into(),
                backend: raw["backend"].as_str().unwrap().into(),
                model: raw["model"].as_str().unwrap().into(),
            },
        )
        .unwrap();
        let mut app = Demo::live("custom".into(), "old-model".into());
        app.observe_live(Observation::Ready).unwrap();
        if active {
            let chat = app.live.as_mut().unwrap();
            chat.submit("Active source operation").unwrap();
            chat.take_submission();
        }
        app.insert_text("KEEP ordinary composer Ω draft");
        app.composer.move_cursor(Motion::WordLeft, true);
        let mut router = KeyRouter::default();
        router.handle(&mut app, KeyEvent::new(KeyCode::F(1), KeyModifiers::NONE));
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data: v::Data::Snapshot(Box::new(s.clone())),
        }))
        .unwrap();
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let mut page = s.request();
        page["kind"] = json!("work_page");
        page["section"] = json!("tasks");
        page["offset"] = json!(0);
        page["total"] = json!(0);
        page["next_offset"] = Value::Null;
        page["rows"] = json!([]);
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data: v::Data::Page(p::Page::parse(&page, &s, p::Section::Tasks, 0, 32).unwrap()),
        }))
        .unwrap();
        for _ in 0..2 {
            paint(&mut app);
            router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        }
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        page["section"] = json!("jobs");
        page["total"] = json!(1);
        page["rows"] = json!([f["target"]]);
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data: v::Data::Page(p::Page::parse(&page, &s, p::Section::Jobs, 0, 32).unwrap()),
        }))
        .unwrap();
        paint(&mut app);
        router.handle(
            &mut app,
            KeyEvent::new(KeyCode::Char('e'), KeyModifiers::NONE),
        );
        paint(&mut app);
        assert!(app.live.as_ref().unwrap().work.accepts_paste());
        (app, router, f)
    }
    fn main_draft(app: &Demo) -> (String, String, String) {
        (
            app.composer.text().into(),
            app.composer.before_cursor().into(),
            app.composer.selected_text().into(),
        )
    }
    fn chord(app: &mut Demo, router: &mut KeyRouter, clipboard: &mut Clipboard) {
        handle_key(
            app,
            router,
            KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL),
            clipboard,
        );
    }
    #[test]
    fn visible_work_form_reads_clipboard_once_and_preserves_composer_selection() {
        let (mut app, mut router, f) = setup(false);
        let before = main_draft(&app);
        let mut clipboard = Clipboard::default();
        chord(&mut app, &mut router, &mut clipboard);
        let mut repeat = KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL);
        repeat.kind = KeyEventKind::Repeat;
        handle_key(&mut app, &mut router, repeat, &mut clipboard);
        assert_eq!((clipboard.reads, clipboard.copies), (1, 0));
        assert_eq!(main_draft(&app), before);
        router.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let v::Request::ControlValidate(edit) = intent.request else {
            panic!()
        };
        assert_eq!(
            edit.draft["prompt"],
            format!(
                "{}CLIPBOARD\nΩ",
                f["target"]["row"]["prompt"].as_str().unwrap()
            )
        );
    }
    #[test]
    fn hidden_form_review_and_boolean_field_never_read_clipboard() {
        let (mut app, mut router, f) = setup(false);
        let before = main_draft(&app);
        let mut clipboard = Clipboard::default();
        app.live.as_mut().unwrap().work.hide();
        chord(&mut app, &mut router, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        paint(&mut app);
        for _ in 0..3 {
            router.handle(&mut app, KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        }
        chord(&mut app, &mut router, &mut clipboard);
        assert_eq!(clipboard.reads, 0);
        // Return to Label and form the exact edit represented by the real source fixture.
        for _ in 0..2 {
            router.handle(
                &mut app,
                KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT),
            );
        }
        paste(&mut app, "Reviewed label");
        router.handle(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let v::Request::ControlValidate(edit) = intent.request else {
            panic!()
        };
        let review = c::Review::parse(&f["review"], &edit).unwrap();
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data: v::Data::ControlReview(Box::new(review)),
        }))
        .unwrap();
        paint(&mut app);
        chord(&mut app, &mut router, &mut clipboard);
        assert_eq!((clipboard.reads, clipboard.copies), (0, 0));
        assert_eq!(main_draft(&app), before);
        assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
    }
    #[test]
    fn pending_approval_preempts_form_clipboard_without_host_io() {
        let (mut app, mut router, _) = setup(true);
        let before = main_draft(&app);
        let mut clipboard = Clipboard::default();
        let event=litetui_terminal::approvals::Ledger::default().request(&json!({"type":"tool_approval_requested","id":"control-approval","tool":"write","input":{"path":"fixture"},"profile":"strict","why":"writes","timeout_s":300.0})).unwrap();
        app.observe_live(Observation::Approval(event)).unwrap();
        paint(&mut app);
        chord(&mut app, &mut router, &mut clipboard);
        assert_eq!((clipboard.reads, clipboard.copies), (0, 0));
        assert_eq!(main_draft(&app), before);
        assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
    }
}

#[cfg(test)]
mod execution_keys_tests {
    use super::*;
    use litetui_terminal::{
        live::Observation, work_execution_controls as execution, work_lifecycle as life,
        work_protocol as p, work_view as v,
    };
    use ratatui::backend::TestBackend;
    use serde_json::{Value, json};
    fn paint(app: &mut Demo) {
        Terminal::new(TestBackend::new(90, 36))
            .unwrap()
            .draw(|frame| ui::draw(frame, app))
            .unwrap();
    }
    fn press(app: &mut Demo, router: &mut KeyRouter, code: KeyCode) {
        router.handle(app, KeyEvent::new(code, KeyModifiers::NONE));
    }
    fn reads(app: &mut Demo, fixture: &Value) {
        let raw = &fixture["snapshot"];
        let snapshot = p::Snapshot::parse(
            raw,
            &p::Identity {
                session: raw["session_id"].as_str().unwrap().into(),
                agent: raw["agent_id"].as_str().unwrap().into(),
                backend: raw["backend"].as_str().unwrap().into(),
                model: raw["model"].as_str().unwrap().into(),
            },
        )
        .unwrap();
        while let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
            let data = match intent.request {
                v::Request::Snapshot => v::Data::Snapshot(Box::new(snapshot.clone())),
                v::Request::Page { section, offset } => {
                    let mut page = snapshot.request();
                    let rows = &fixture["selected"][section.key()];
                    page["kind"] = json!("work_page");
                    page["section"] = json!(section.key());
                    page["offset"] = json!(offset);
                    page["rows"] = rows.clone();
                    page["total"] = json!(rows.as_array().unwrap().len());
                    page["next_offset"] = Value::Null;
                    v::Data::Page(p::Page::parse(&page, &snapshot, section, offset, 32).unwrap())
                }
                other => panic!("Unexpected read {other:?}"),
            };
            app.observe_live(Observation::Work(v::Update {
                generation: intent.generation,
                data,
            }))
            .unwrap();
        }
    }
    #[test]
    fn pending_launch_escape_reaches_exact_stop_and_preserves_composer_selection() {
        let all: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/work_execution_controls.json"
        ))
        .unwrap();
        let f = all["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["name"] == "goal_start-cron")
            .unwrap();
        let mut app = Demo::live(
            f["snapshot"]["backend"].as_str().unwrap().into(),
            f["snapshot"]["model"].as_str().unwrap().into(),
        );
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("keep composer α selected Ω");
        app.composer.move_cursor(Motion::WordLeft, true);
        let before = (
            app.composer.text().to_owned(),
            app.composer.selected_text().to_owned(),
            app.composer.before_cursor().to_owned(),
        );
        let mut router = KeyRouter::default();
        press(&mut app, &mut router, KeyCode::F(1));
        reads(&mut app, f);
        for _ in 0..4 {
            paint(&mut app);
            press(&mut app, &mut router, KeyCode::Tab);
            reads(&mut app, f);
        }
        paint(&mut app);
        press(&mut app, &mut router, KeyCode::Char('n'));
        paint(&mut app);
        paste(
            &mut app,
            f["review"]["draft"]["objective"].as_str().unwrap(),
        );
        press(&mut app, &mut router, KeyCode::Enter);
        let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
        let v::Request::ExecutionValidate(edit) = intent.request else {
            panic!("Expected inert goal validation")
        };
        let review = execution::Review::parse(
            &f["review"],
            &edit,
            f["capture"]["instance"].as_str().unwrap(),
            life::Provider::Host,
        )
        .unwrap();
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data: v::Data::ExecutionReview(Box::new(review.clone())),
        }))
        .unwrap();
        let mut apply = None;
        for _ in 0..2000 {
            paint(&mut app);
            press(&mut app, &mut router, KeyCode::Enter);
            if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
                apply = Some(intent);
                break;
            }
        }
        let apply = apply.expect("Painted whole exact review");
        app.live.as_mut().unwrap().prepare_work(&apply).unwrap();
        app.observe_live(Observation::WorkLaunchPending {
            action_id: review.action_id.clone(),
            generation: apply.generation,
            reason: "Waiting for actual admission".into(),
        })
        .unwrap();
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .submit("must remain unsent")
                .is_err()
        );
        paint(&mut app);
        press(&mut app, &mut router, KeyCode::Esc);
        assert!(app.stop_open());
        press(&mut app, &mut router, KeyCode::Enter);
        assert!(app.live.as_mut().unwrap().take_execution_stop().is_none());
        press(&mut app, &mut router, KeyCode::Esc);
        paint(&mut app);
        press(&mut app, &mut router, KeyCode::Tab);
        press(&mut app, &mut router, KeyCode::Enter);
        assert_eq!(
            app.live.as_mut().unwrap().take_execution_stop(),
            Some(review.action_id)
        );
        assert!(app.live.as_mut().unwrap().take_execution_stop().is_none());
        assert!(app.live.as_mut().unwrap().take_abort().is_none());
        assert_eq!(
            (
                app.composer.text().to_owned(),
                app.composer.selected_text().to_owned(),
                app.composer.before_cursor().to_owned()
            ),
            before
        );
        assert!(app.live.as_ref().unwrap().execution_pending());
        assert!(app.live.as_ref().unwrap().rows.is_empty());
    }
}

#[cfg(test)]
mod live_keyboard_navigation_tests {
    #[test]
    fn image_shortcut_keeps_draft_and_repeats_out_of_composer() {
        let mut app = Demo::live("custom".into(), "model".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("keep these words");
        let mut router = KeyRouter::default();
        press(&mut app, &mut router, 'o');
        assert!(draw(&mut app, 100, 30).contains("Local image file"));
        paste(&mut app, "C:\\chosen.png");
        router.handle(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.has_live_input());
        router.handle(
            &mut app,
            KeyEvent::new_with_kind(KeyCode::Enter, KeyModifiers::NONE, KeyEventKind::Repeat),
        );
        assert_eq!(app.composer.text(), "keep these words");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        app.open_work();
        press(&mut app, &mut router, 'o');
        assert!(!app.live.as_ref().unwrap().work.image.open);
    }

    use super::*;
    use litetui_terminal::{
        conversation_actions::{Action, Intent},
        conversation_browser,
        live::Observation,
        native_activity::Activity,
    };
    use ratatui::backend::TestBackend;

    fn press(app: &mut Demo, router: &mut KeyRouter, character: char) {
        router.handle(
            app,
            KeyEvent::new(KeyCode::Char(character), KeyModifiers::CONTROL),
        );
    }
    fn draw(app: &mut Demo, width: u16, height: u16) -> String {
        let mut term = Terminal::new(TestBackend::new(width, height)).unwrap();
        term.draw(|frame| ui::draw(frame, app)).unwrap();
        term.backend()
            .buffer()
            .content
            .chunks(width as usize)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }
    enum Detail<'a> {
        Input(&'a str),
        Result(&'a str, Option<bool>),
    }
    fn record(app: &mut Demo, id: &str, name: &str, detail: Detail<'_>) {
        let mut frame = serde_json::json!({"type":"native_activity","provider":"claude","id":id,
            "name":name,"card_index":0,"complete":true,"input":null,"result":null,"is_error":null});
        match detail {
            Detail::Input(text) => {
                frame["kind"] = "tool_use".into();
                frame["input"] = serde_json::json!({"argument":text});
            }
            Detail::Result(text, error) => {
                frame["kind"] = "tool_result".into();
                frame["result"] = text.into();
                frame["is_error"] = serde_json::json!(error);
            }
        }
        app.observe_live(Observation::Activity(Activity::from_event(&frame).unwrap()))
            .unwrap();
    }

    #[test]
    fn live_ctrl_l_only_requests_reviewed_new_after_an_idle_fresh_press() {
        let mut app = Demo::live("custom".into(), "m".into());
        let mut router = KeyRouter::default();
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("Keep the selected draft");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (
            app.composer.text().to_owned(),
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        draw(&mut app, 80, 24);
        router.handle(
            &mut app,
            KeyEvent::new_with_kind(
                KeyCode::Char('l'),
                KeyModifiers::CONTROL,
                KeyEventKind::Repeat,
            ),
        );
        assert!(!app.has_live_input());
        app.open_settings();
        press(&mut app, &mut router, 'l');
        assert!(app.live.as_ref().unwrap().settings.owns_input());
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        app.live.as_mut().unwrap().settings.close();
        app.live.as_mut().unwrap().submit("q").unwrap();
        press(&mut app, &mut router, 'l');
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        app.observe_live(Observation::Complete).unwrap();
        press(&mut app, &mut router, 'l');
        let action = app.live.as_mut().unwrap().browser.take_intent().unwrap();
        assert!(matches!(
            action,
            conversation_browser::Intent::Action(Intent::Read {
                action: Action::New
            })
        ));
        press(&mut app, &mut router, 'l');
        assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 1);
        assert_eq!(
            draft,
            (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
    }

    #[test]
    fn live_ctrl_b_e_follow_owned_cards_across_turns_updates_and_reflow() {
        let mut app = Demo::live("claude".into(), "m".into());
        let mut router = KeyRouter::default();
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("draft stays");
        app.composer.move_cursor(Motion::Home, false);
        press(&mut app, &mut router, 'b');
        press(&mut app, &mut router, 'e'); // composer End when there are no observed tools
        assert_eq!(app.composer.before_cursor(), "draft stays");
        app.composer.move_cursor(Motion::WordLeft, true);
        let selection = app.composer.selected_text().to_owned();
        app.live.as_mut().unwrap().submit("first").unwrap();
        record(
            &mut app,
            "same",
            "OldRecord",
            Detail::Input("old arguments"),
        );
        record(
            &mut app,
            "same",
            "OldRecord",
            Detail::Result("old returned body", Some(false)),
        );
        app.observe_live(Observation::Complete).unwrap();
        app.live.as_mut().unwrap().backend = "custom".into();
        app.live.as_mut().unwrap().submit("second").unwrap();
        record(
            &mut app,
            "same",
            "NewRecord",
            Detail::Input("new arguments"),
        );
        press(&mut app, &mut router, 'b');
        assert!(draw(&mut app, 110, 60).contains("› ▾ Host tool · NewRecord"));
        press(&mut app, &mut router, 'e');
        assert!(!draw(&mut app, 110, 60).contains("new arguments"));
        record(
            &mut app,
            "same",
            "NewRecord",
            Detail::Result("new returned body", None),
        );
        let collapsed = draw(&mut app, 110, 60);
        assert!(collapsed.contains("outcome unknown") && !collapsed.contains("new returned body"));
        press(&mut app, &mut router, 'b');
        press(&mut app, &mut router, 'e');
        let small = draw(&mut app, 42, 18);
        assert!(small.contains("› ▾ Claude · OldRecord"));
        let full = draw(&mut app, 110, 60);
        assert!(full.contains("old returned body") && !full.contains("new returned body"));
        press(&mut app, &mut router, 'b');
        assert!(draw(&mut app, 110, 60).contains("› ▸ Host tool · NewRecord"));
        app.open_work();
        press(&mut app, &mut router, 'e');
        assert!(app.live.as_ref().unwrap().work.owns_input());
        assert_eq!(app.composer.selected_text(), selection);
        assert_eq!(app.composer.text(), "draft stays");
    }
}
