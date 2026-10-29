use crossterm::{
    cursor::Show,
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
        MouseEventKind,
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
    if app.viewport_too_small {
        if key.code == KeyCode::Esc {
            if let Some(chat) = app.live.as_mut() {
                chat.stop_confirm = false;
            }
            app.approval_open = false;
            if let Some(chat) = app.live.as_mut() {
                chat.controls.close();
            }
            app.themes_open = false;
            app.completion.dismiss();
        }
        return false;
    }
    if key.code == KeyCode::Esc
        && key.modifiers == KeyModifiers::CONTROL
        && key.kind == KeyEventKind::Press
    {
        app.open_stop();
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
        KeyCode::F(3) if key.kind == KeyEventKind::Press && key.modifiers.is_empty() => {
            app.open_models(litetui_terminal::model_controls::Kind::Model)
        }
        KeyCode::F(4) => app.open_themes(),
        KeyCode::F(5) => app.reset(),
        KeyCode::F(6) => app.open_approval(),
        KeyCode::F(2) => app.toggle_tool(),
        KeyCode::Tab => app.next_tool(),
        KeyCode::BackTab => app.previous_tool(),
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
    if app.has_live_input() {
        if !app.stop_open()
            && !app.has_live_approval()
            && !app.viewport_too_small
            && event.kind == KeyEventKind::Press
            && event.modifiers == KeyModifiers::CONTROL
            && matches!(event.code, KeyCode::Char('v' | 'V'))
        {
            let chat = app.live.as_mut().expect("live question");
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
        EnableMouseCapture
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
        EnableMouseCapture
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
        EnableMouseCapture
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
    let runtime = litetui_terminal::live::Interactive::start(options);
    runtime.with_cleanup(|runtime| {
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
            if !quitting
                && let Some(intent) = app.live.as_mut().expect("live").controls.take_intent()
                && let Err(error) = runtime.control(intent)
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
    })
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
                "LiteTUI Rust — live vertical path / offline visual preview\n\nUsage: litetui-rs [--preview]\n       litetui-rs --interactive --python <exe> --source <repo> --data-root <NEW absolute directory> --backend <claude|codex> --model <slug> [--timeout <seconds>] [--tools on|off] [--tool-profile strict|interactive|autonomous] [--workspace <directory>]\n       litetui-rs --live --python <exe> --source <repo> --data-root <NEW absolute directory> --backend <claude|codex> --model <slug> --prompt <text> [--timeout <1..600 seconds>] [--tools on|off] [--tool-profile strict|interactive|autonomous] [--workspace <directory>]\n\n--interactive: real composer/transcript, serialized multi-turn, Ctrl+Q waits cleanup; optional source-authorized tools.\nLive keys: F3 models | selector Tab thinking | Enter applies the visible choice | Esc closes.\n--live: Windows-only real owned Python child, one frontier prompt, streamed reply; cannot answer questions or approvals.\nTools default off. Missing profiles prompt before Python starts; redirected launches require --tool-profile. Tools on requires an absolute existing --workspace. External MCP remains off.\n--preview (default): no runtime/model/registration/persistence.\nPreview keys: Tab select tool | F2 expand | F4 themes | F6 mock approval | F5 replay\nType /pre: mock completions (Tab accepts) | Up/Down: history or completion\nLeft/Right/Home/End: edit | PgUp/PgDn/mouse wheel: scroll | Enter: local mock turn | Ctrl+Q: quit"
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
}
