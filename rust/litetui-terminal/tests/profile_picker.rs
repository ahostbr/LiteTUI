use crossterm::event::{
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::{
    profile_picker::{Picker, Selection},
    tool_mode::Profile,
};
use ratatui::{Terminal, backend::TestBackend};

fn key(code: KeyCode) -> Event {
    Event::Key(KeyEvent::new(code, KeyModifiers::NONE))
}
fn draw(picker: &mut Picker, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| picker.draw(frame, true)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}
#[test]
fn rendering_is_not_selection_and_all_choices_require_fresh_confirmation() {
    for (steps, expected) in [
        (0, Profile::Strict),
        (1, Profile::Interactive),
        (2, Profile::Autonomous),
    ] {
        let mut picker = Picker::default();
        let rendered = draw(&mut picker, 36, 14);
        for text in [
            "Choose tool profile",
            "Python has not started.",
            "strict",
            "interactive",
            "autonomous",
            "Enter starts",
            "Esc cancels",
        ] {
            assert!(rendered.contains(text), "missing {text}");
        }
        assert_eq!(picker.selection(), Selection::Pending);
        for _ in 0..steps {
            assert_eq!(picker.handle(key(KeyCode::Down)), Selection::Pending);
        }
        let mut enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        enter.kind = KeyEventKind::Repeat;
        assert_eq!(picker.handle(Event::Key(enter)), Selection::Pending);
        enter.kind = KeyEventKind::Release;
        assert_eq!(picker.handle(Event::Key(enter)), Selection::Pending);
        assert_eq!(
            picker.handle(key(KeyCode::Enter)),
            Selection::Selected(expected)
        );
        assert_eq!(
            picker.handle(key(KeyCode::Down)),
            Selection::Selected(expected),
            "choice is final"
        );
    }
}
#[test]
fn hidden_pasted_modified_or_cancelled_input_never_selects() {
    let mut picker = Picker::default();
    assert_eq!(picker.handle(key(KeyCode::Enter)), Selection::Pending);
    draw(&mut picker, 35, 13);
    assert_eq!(picker.handle(key(KeyCode::Enter)), Selection::Pending);
    draw(&mut picker, 80, 24);
    for event in [
        Event::Paste("autonomous\n".into()),
        Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL)),
    ] {
        assert_eq!(picker.handle(event), Selection::Pending);
    }
    assert_eq!(picker.handle(key(KeyCode::Esc)), Selection::Cancelled);
    assert_eq!(picker.handle(key(KeyCode::Enter)), Selection::Cancelled);
    let mut picker = Picker::default();
    assert_eq!(
        picker.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('q'),
            KeyModifiers::CONTROL
        ))),
        Selection::Cancelled
    );
}
#[test]
fn resize_invalidates_hidden_confirmation_and_pointer_press() {
    fn mouse(kind: MouseEventKind) -> Event {
        Event::Mouse(MouseEvent {
            kind,
            column: 2,
            row: 8,
            modifiers: KeyModifiers::NONE,
        })
    }
    let mut picker = Picker::default();
    draw(&mut picker, 36, 14);
    picker.handle(mouse(MouseEventKind::Down(MouseButton::Left)));
    picker.handle(Event::Resize(80, 24));
    assert_eq!(picker.handle(key(KeyCode::Enter)), Selection::Pending);
    draw(&mut picker, 36, 14);
    picker.handle(mouse(MouseEventKind::Up(MouseButton::Left)));
    assert_eq!(
        picker.handle(key(KeyCode::Enter)),
        Selection::Selected(Profile::Strict)
    );
    let mut picker = Picker::default();
    draw(&mut picker, 36, 14);
    picker.handle(mouse(MouseEventKind::Down(MouseButton::Left)));
    assert_eq!(
        picker.handle(mouse(MouseEventKind::Up(MouseButton::Left))),
        Selection::Pending
    );
    assert_eq!(
        picker.handle(key(KeyCode::Enter)),
        Selection::Selected(Profile::Autonomous)
    );
}
#[test]
fn navigation_wraps_without_starting_and_shift_tab_matches_source_order() {
    let mut picker = Picker::default();
    draw(&mut picker, 80, 24);
    assert_eq!(
        picker.handle(Event::Key(KeyEvent::new(
            KeyCode::BackTab,
            KeyModifiers::SHIFT
        ))),
        Selection::Pending
    );
    assert_eq!(
        picker.handle(key(KeyCode::Enter)),
        Selection::Selected(Profile::Autonomous)
    );
}
