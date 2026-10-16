use litetui_terminal::composer::{Composer, Delete, Motion};

fn composer(text: &str) -> Composer {
    let mut editor = Composer::new(500, 100);
    editor.insert(text);
    editor
}

#[test]
fn edits_at_the_cursor_without_splitting_utf8() {
    let mut editor = composer("a雪🙂z");
    editor.move_cursor(Motion::Left, false);
    editor.move_cursor(Motion::Left, false);
    editor.insert("!");
    assert_eq!(editor.text(), "a雪!🙂z");
    editor.delete(Delete::Left);
    editor.delete(Delete::Right);
    assert_eq!(editor.text(), "a雪z");
    assert_eq!(editor.before_cursor(), "a雪");
    editor.move_cursor(Motion::Home, false);
    editor.delete(Delete::Left);
    editor.move_cursor(Motion::End, false);
    editor.delete(Delete::Right);
    assert_eq!(editor.text(), "a雪z");
}

#[test]
fn selections_reverse_collapse_and_replace_like_the_focused_input() {
    let mut editor = composer("abc雪");
    editor.move_cursor(Motion::Left, true);
    editor.move_cursor(Motion::Left, true);
    assert_eq!(editor.selected_text(), "c雪");
    editor.move_cursor(Motion::Left, false);
    assert_eq!(editor.before_cursor(), "ab");
    assert!(editor.selected_text().is_empty());
    editor.move_cursor(Motion::Left, true);
    editor.insert("Z");
    assert_eq!(editor.text(), "aZc雪");
    editor.select_all();
    editor.delete(Delete::Right);
    assert_eq!(editor.text(), "");
}

#[test]
fn shifted_home_and_end_reset_anchor_to_the_active_endpoint() {
    let mut editor = composer("abcd");
    editor.move_cursor(Motion::Left, true); // anchor4, active3
    editor.move_cursor(Motion::Home, true); // source Input anchors at3
    assert_eq!(editor.selected_text(), "abc");
    editor.move_cursor(Motion::End, true); // source Input anchors at0
    assert_eq!(editor.selected_text(), "abcd");
}

#[test]
fn delete_to_line_edges_respects_selection_before_direction() {
    let mut editor = composer("abcd");
    editor.move_cursor(Motion::Left, false);
    editor.delete(Delete::ToStart);
    assert_eq!(editor.text(), "d");
    editor.insert("xy");
    editor.delete(Delete::ToEnd);
    assert_eq!(editor.text(), "xy");
    editor.move_cursor(Motion::Left, true);
    editor.delete(Delete::ToStart);
    assert_eq!(editor.text(), "x");
}

#[test]
fn history_restores_original_draft_not_edits_to_recalled_entries() {
    let mut editor = composer("");
    for entry in ["first", "first", "second"] {
        editor.push_history(entry);
    }
    editor.insert("draft");
    editor.move_cursor(Motion::Home, false);
    assert!(editor.history_previous());
    assert_eq!(editor.text(), "second");
    editor.insert(" changed");
    assert!(editor.history_previous());
    assert_eq!(editor.text(), "first");
    assert!(!editor.history_previous()); // consecutive duplicate not retained
    assert!(editor.history_next());
    assert_eq!(editor.text(), "second");
    assert!(editor.history_next());
    assert_eq!(editor.text(), "draft");
    assert_eq!(editor.before_cursor(), "draft");
    assert!(!editor.history_next());
}

#[test]
fn limits_are_caller_chosen_and_replacement_credits_selected_text() {
    let mut editor = Composer::new(3, 2);
    editor.insert("雪🙂ab");
    assert_eq!(editor.text(), "雪🙂a");
    editor.move_cursor(Motion::Left, true);
    editor.insert("XY");
    assert_eq!(editor.text(), "雪🙂X");
    for text in ["one", "two", "tri"] {
        editor.push_history(text);
    }
    assert!(editor.history_previous());
    assert_eq!(editor.text(), "tri");
    assert!(editor.history_previous());
    assert_eq!(editor.text(), "two");
    assert!(!editor.history_previous());
    let mut disabled = Composer::new(0, 0);
    disabled.insert("nothing");
    disabled.push_history("nothing");
    assert!(disabled.text().is_empty());
    assert!(!disabled.history_previous());
}

#[test]
fn control_bytes_never_become_terminal_instructions_or_submissions() {
    let mut editor = composer("safe");
    editor.insert("\u{1b}[31m\u{7}\tmore\r\ntext");
    assert!(!editor.text().chars().any(char::is_control));
    assert_eq!(editor.text(), "safe[31m more  text");
}
