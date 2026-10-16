use litetui_terminal::composer::{Composer, Delete, Motion};

// Oracle generated with Python re, independently checked against mounted
// Textual Input actions by tests/test_rust_word_boundaries.py.
struct Case {
    text: String,
    cursor: usize,
    left: usize,
    right: usize,
}
fn cases() -> Vec<Case> {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/composer_words.json")).unwrap();
    fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| Case {
            text: case["text"].as_str().unwrap().to_owned(),
            cursor: case["cursor"].as_u64().unwrap() as usize,
            left: case["left"].as_u64().unwrap() as usize,
            right: case["right"].as_u64().unwrap() as usize,
        })
        .collect()
}
fn selected(text: &str, anchor: usize, cursor: usize) -> Composer {
    let mut composer = Composer::new(1024, 0);
    composer.insert(text);
    composer.move_cursor(Motion::Home, false);
    for _ in 0..anchor {
        composer.move_cursor(Motion::Right, false);
    }
    for _ in 0..anchor.abs_diff(cursor) {
        composer.move_cursor(
            if cursor < anchor {
                Motion::Left
            } else {
                Motion::Right
            },
            true,
        );
    }
    composer
}
fn part(text: &str, start: usize, end: usize) -> String {
    text.chars().skip(start).take(end - start).collect()
}

#[test]
fn word_motion_matches_python_oracle_from_every_scalar_position() {
    for case in cases() {
        for anchor in [0, case.cursor, case.text.chars().count()] {
            for (motion, target) in [
                (Motion::WordLeft, case.left),
                (Motion::WordRight, case.right),
            ] {
                for select in [false, true] {
                    let mut composer = selected(&case.text, anchor, case.cursor);
                    composer.move_cursor(motion, select);
                    assert_eq!(
                        composer.before_cursor().chars().count(),
                        target,
                        "{:?} cursor {} anchor {anchor} {motion:?} select {select}",
                        case.text,
                        case.cursor
                    );
                    let expected_anchor = if select { anchor } else { target };
                    assert_eq!(
                        composer.selected_text(),
                        part(
                            &case.text,
                            expected_anchor.min(target),
                            expected_anchor.max(target)
                        )
                    );
                    assert_eq!(composer.text(), case.text);
                }
            }
        }
    }
}

#[test]
fn word_deletion_matches_python_oracle_and_selection_takes_precedence() {
    for case in cases() {
        let len = case.text.chars().count();
        for anchor in [0, case.cursor, len] {
            for (direction, target) in [
                (Delete::WordLeft, case.left),
                (Delete::WordRight, case.right),
            ] {
                let mut composer = selected(&case.text, anchor, case.cursor);
                composer.delete(direction);
                let other = if anchor == case.cursor {
                    target
                } else {
                    anchor
                };
                let start = other.min(case.cursor);
                let end = other.max(case.cursor);
                assert_eq!(
                    composer.text(),
                    format!(
                        "{}{}",
                        part(&case.text, 0, start),
                        part(&case.text, end, len)
                    ),
                    "{:?} cursor {} anchor {anchor} {direction:?}",
                    case.text,
                    case.cursor
                );
                assert_eq!(composer.before_cursor().chars().count(), start);
                assert!(composer.selected_text().is_empty());
            }
        }
    }
}
