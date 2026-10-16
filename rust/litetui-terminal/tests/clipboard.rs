use litetui_terminal::{
    clipboard::copy_sequence,
    composer::{Composer, Motion},
};

#[test]
fn copy_is_an_osc52_base64_payload_not_interpreted_selection_bytes() {
    assert_eq!(copy_sequence("hello"), "\x1b]52;c;aGVsbG8=\x07");
    assert_eq!(copy_sequence("雪"), "\x1b]52;c;6Zuq\x07");
    let sequence = copy_sequence("\x1b[31m\n");
    assert_eq!(sequence.matches('\x1b').count(), 1);
    assert!(!sequence.contains("[31m"));
}
#[test]
fn os_clipboard_paste_joins_nonblank_lines_and_preserves_selected_text() {
    for reverse in [false, true] {
        let mut composer = Composer::new(100, 0);
        composer.insert("abcdef");
        composer.move_cursor(Motion::Home, false);
        for _ in 0..if reverse { 4 } else { 2 } {
            composer.move_cursor(Motion::Right, false);
        }
        for _ in 0..2 {
            composer.move_cursor(if reverse { Motion::Left } else { Motion::Right }, true);
        }
        assert_eq!(composer.selected_text(), "cd");
        assert_eq!(composer.paste_clipboard("one\r\n \r\n two"), 8);
        assert_eq!(
            composer.text(),
            if reverse {
                "abone  twocdef"
            } else {
                "abcdone  twoef"
            }
        );
        assert!(composer.selected_text().is_empty());
        assert_eq!(
            composer.before_cursor(),
            if reverse {
                "abone  two"
            } else {
                "abcdone  two"
            }
        );
    }
}
#[test]
fn empty_or_capacity_rejected_clipboard_paste_preserves_selection() {
    let mut composer = Composer::new(6, 0);
    composer.insert("abcdef");
    composer.select_all();
    for text in ["", "\r\n \n\t\r\n", "more"] {
        assert_eq!(composer.paste_clipboard(text), 0);
        assert_eq!(composer.text(), "abcdef");
        assert_eq!(composer.selected_text(), "abcdef");
    }
}
