use litetui_terminal::completion::{Completion, CompletionKind, CompletionPicker};

fn candidate(name: &str, kind: CompletionKind) -> Completion {
    Completion {
        name: name.into(),
        description: "fixture only".into(),
        kind,
    }
}
#[test]
fn prefix_quality_precedes_producer_order_and_commands_and_skills_are_distinct() {
    let candidates = [
        candidate("x-alpha", CompletionKind::Command),
        candidate("alpha-command", CompletionKind::Command),
        candidate("alpha-skill", CompletionKind::Skill),
    ];
    let mut picker = CompletionPicker::default();
    picker.refilter(&candidates, "/ALPHA");
    assert_eq!(
        picker
            .matches()
            .iter()
            .map(|c| c.name.as_str())
            .collect::<Vec<_>>(),
        ["alpha-command", "alpha-skill", "x-alpha"]
    );
    assert_eq!(picker.matches()[1].kind, CompletionKind::Skill);
    picker.move_by(1);
    assert_eq!(picker.current().unwrap().name, "alpha-skill");
    picker.refilter(&candidates, "/alpha-");
    assert_eq!(picker.selected(), 0);
}
#[test]
fn only_bare_slash_input_opens_and_navigation_clamps_then_dismisses() {
    let candidates = [
        candidate("one", CompletionKind::Command),
        candidate("two", CompletionKind::Skill),
    ];
    let mut picker = CompletionPicker::default();
    for input in ["", "one", " /one", "/one argument", "/unmatched", "//"] {
        picker.refilter(&candidates, input);
        assert!(!picker.is_open(), "{input:?}");
    }
    picker.refilter(&candidates, "/");
    picker.move_by(-1);
    assert_eq!(picker.selected(), 0);
    picker.move_by(100);
    assert_eq!(picker.selected(), 1);
    picker.move_by(-100);
    assert_eq!(picker.selected(), 0);
    picker.dismiss();
    picker.move_by(1);
    assert!(picker.current().is_none());
}
#[test]
fn results_are_bounded_after_ranking_and_refilter_consumes_updated_catalog() {
    let mut candidates: Vec<_> = (0..220)
        .map(|index| candidate(&format!("x-test-{index}"), CompletionKind::Skill))
        .collect();
    candidates.push(candidate("test-prefix", CompletionKind::Skill));
    let mut picker = CompletionPicker::default();
    picker.refilter(&candidates, "/test");
    assert_eq!(picker.matches().len(), 200);
    assert_eq!(picker.current().unwrap().name, "test-prefix");
    picker.refilter(&[], "/test");
    assert!(!picker.is_open());
}

#[test]
fn clicks_require_one_visible_row_and_cannot_survive_refilter_or_hide() {
    use ratatui::layout::Rect;
    let candidates = [
        candidate("one", CompletionKind::Command),
        candidate("two", CompletionKind::Skill),
        candidate("three", CompletionKind::Skill),
    ];
    let mut picker = CompletionPicker::default();
    picker.refilter(&candidates, "/");
    picker.record_view(Rect::new(3, 4, 30, 2), 1);
    picker.pointer_down(4, 4);
    assert!(!picker.pointer_up(4, 5));
    picker.pointer_down(33, 4);
    assert!(!picker.pointer_up(33, 4));
    picker.pointer_down(4, 4);
    picker.refilter(&candidates, "/");
    picker.record_view(Rect::new(3, 4, 30, 2), 1);
    assert!(!picker.pointer_up(4, 4));
    picker.pointer_down(4, 4);
    picker.hide();
    assert!(!picker.pointer_up(4, 4));
    picker.record_view(Rect::new(3, 4, 30, 2), 1);
    picker.pointer_down(4, 5);
    assert!(picker.pointer_up(4, 5));
    assert_eq!(picker.current().unwrap().name, "three");
}
