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
}
impl KeyRouter {
    fn handle(&mut self, app: &mut Demo, event: KeyEvent) -> bool {
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
        let modal_was_open = app.approval_open || app.themes_open;
        let completion_was_open = app.completion.is_open();
        let before = app.composer.text().to_owned();
        let quit = key(app, event);
        if before != app.composer.text() {
            app.sync_completion();
        }
        if (modal_was_open && !(app.approval_open || app.themes_open))
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
    if key.modifiers.contains(KeyModifiers::CONTROL) && matches!(key.code, KeyCode::Char('q' | 'c'))
    {
        return true;
    }
    if app.viewport_too_small {
        if key.code == KeyCode::Esc {
            app.approval_open = false;
            app.themes_open = false;
            app.completion.dismiss();
        }
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
    composer_or_preview_key(app, key)
}

fn composer_or_preview_key(app: &mut Demo, key: KeyEvent) -> bool {
    match key.code {
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
            app.notice = "Offline preview only. F5 replays the mock conversation.".into()
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

fn paste(app: &mut Demo, text: &str) {
    if app.approval_open || app.viewport_too_small {
        return;
    }
    if app.themes_open {
        app.search_themes(text);
    } else {
        app.paste_text(text);
    }
}

fn mouse(app: &mut Demo, event: MouseEvent) {
    if app.approval_open || app.themes_open || app.viewport_too_small {
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
    loop {
        terminal.draw(|frame| ui::draw(frame, &mut app))?;
        if event::poll(Duration::from_millis(100))? {
            match event::read()? {
                Event::Key(event) if input.handle(&mut app, event) => break,
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

fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [] => run(),
        [flag] if flag == "--preview" => run(),
        [flag] if flag == "--help" || flag == "-h" => {
            println!(
                "LiteTUI Rust — OFFLINE VISUAL PREVIEW\n\nUsage: litetui-rs [--preview]\n\nNo Python child, model, tool execution, file persistence or fleet registration.\nTab: select tool | F2: expand | F4: themes | F6: mock approval | F5: replay\nType /pre: mock completions (Tab accepts) | Up/Down: history or completion
Left/Right/Home/End: edit | PgUp/PgDn/mouse wheel: scroll | Enter: local mock turn | Ctrl+Q: quit"
            );
            Ok(())
        }
        _ => Err(io::Error::other(
            "Unknown option. Use --help. Only the offline preview is implemented.",
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
}
