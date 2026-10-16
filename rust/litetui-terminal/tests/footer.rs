use litetui_terminal::{
    demo::Demo,
    footer::{FieldKey as K, FooterAction as A, FooterField as F, FooterFocus, FooterLayout},
    ui,
};
use ratatui::{Terminal, backend::TestBackend, style::Modifier};
use unicode_width::UnicodeWidthStr;

fn fields() -> Vec<F> {
    vec![
        F::new(K::Percent, "12%"),
        F::new(K::Context, "24k/200k"),
        F::new(K::Seat, "no seat"),
        F::new(K::Conversation, "preview"),
        F::new(K::Model, "mock-model"),
        F::new(K::Thinking, "think:high"),
        F::new(K::Cache, "cache:1k"),
        F::new(K::Throughput, "20t/s"),
        F::new(K::Background, "bg:2"),
        F::new(K::Agents, "agents:3"),
        F::new(K::Authority, "interactive"),
        F::new(K::Plan, "plan:off"),
    ]
}
#[test]
fn separate_rows_preserve_global_configured_navigation_not_invented_row_order() {
    let mut fields = fields();
    fields.rotate_right(2);
    let layout = FooterLayout::new(&fields, 200);
    assert!(layout.status.text().starts_with("12%  ·  24k/200k"));
    assert!(!layout.status.text().contains("plan"));
    assert_eq!(layout.permission.text(), "interactive  ·  plan:off");
    assert_eq!(
        layout.actions,
        [A::Authority, A::Plan, A::Thinking, A::Background, A::Agents]
    );
}
#[test]
fn compact_separators_precede_drops_and_drop_priority_is_independent_of_order() {
    let fields = fields();
    let full = FooterLayout::new(&fields, 200);
    let compact_width = full.status.text().width() - 2 * (full.status.fields.len() - 1);
    let compact = FooterLayout::new(&fields, compact_width);
    assert_eq!(compact.status.fields.len(), full.status.fields.len());
    assert_eq!(compact.status.separator, " · ");
    let mut expected = fields;
    let mut width = compact_width;
    for key in [
        K::Throughput,
        K::Background,
        K::Agents,
        K::Cache,
        K::Model,
        K::Conversation,
        K::Context,
    ] {
        let before = FooterLayout::new(&expected, width);
        let next = FooterLayout::new(&expected, before.status.text().width() - 1);
        assert!(
            !next.status.fields.iter().any(|field| field.key == key),
            "{key:?}"
        );
        assert_eq!(next.status.fields.len(), before.status.fields.len() - 1);
        expected.retain(|field| field.key != key);
        width = next.status.text().width();
    }
}
#[test]
fn narrow_seat_is_grapheme_safe_and_overflow_is_not_actionable() {
    let fields = [
        F::new(K::Percent, "12%"),
        F::new(K::Seat, "雪雪雪雪雪雪雪雪"),
        F::new(K::Thinking, "think:high"),
        F::new(K::Plan, "plan:off"),
    ];
    let layout = FooterLayout::new(&fields, 25);
    assert_eq!(layout.status.fields[1].text, "雪雪雪…");
    assert!(!layout.actions.contains(&A::Thinking));
    assert!(layout.status.text().width() <= 25);
    for width in 0..45 {
        let layout = FooterLayout::new(&fields, width);
        assert!(layout.status.text().width() <= width);
        assert!(layout.permission.text().width() <= width);
        if layout.status.overflow {
            assert!(!layout.actions.contains(&A::Thinking));
        }
        if layout.permission.overflow {
            assert!(!layout.actions.contains(&A::Plan));
        }
    }
    let cluster = format!("{}x", "\u{1100}".repeat(20));
    let layout = FooterLayout::new(&[F::new(K::Seat, cluster)], 8);
    assert_eq!(layout.status.text(), "…");
    assert!(layout.status.overflow);
    assert!(layout.status.fields.is_empty());
}
#[test]
fn permission_budget_does_not_drop_status_and_unknown_authority_has_no_target() {
    let fields = [
        F::new(K::Thinking, "think:high"),
        F::new(K::Plan, "plan:off"),
    ];
    let layout = FooterLayout::new(&fields, 10);
    assert_eq!(layout.status.text(), "think:high");
    assert_eq!(layout.permission.text(), "plan:off");
    assert_eq!(layout.actions, [A::Thinking, A::Plan]);
    let layout = FooterLayout::new(
        &[
            F::new(K::Authority, " "),
            F::new(K::Plan, "\n\u{1b}"),
            F::new(K::Thinking, "\u{200b}"),
        ],
        100,
    );
    assert!(layout.actions.is_empty());
}
#[test]
fn disappeared_selection_never_retargets_and_consumes_one_stale_activation() {
    let mut focus = FooterFocus::default();
    focus.update_visible(&[A::Thinking, A::Plan]);
    assert!(focus.enter());
    focus.move_by(-1);
    assert_eq!(focus.activate(), (true, Some(A::Plan)));
    focus.update_visible(&[A::Thinking]);
    assert_eq!(focus.selected(), None);
    focus.update_visible(&[A::Thinking, A::Plan]);
    assert!(focus.owns_enter());
    assert_eq!(focus.activate(), (true, None));
    assert_eq!(focus.activate(), (false, None));
    assert!(focus.enter());
    focus.update_visible(&[]);
    focus.leave();
    assert_eq!(focus.activate(), (false, None));
}
#[test]
fn reordering_keeps_selected_identity_and_navigation_wraps() {
    let mut focus = FooterFocus::default();
    focus.update_visible(&[A::Thinking, A::Plan]);
    focus.enter();
    focus.update_visible(&[A::Plan, A::Thinking]);
    assert_eq!(focus.selected(), Some(A::Thinking));
    focus.move_by(1);
    assert_eq!(focus.selected(), Some(A::Plan));
    focus.move_by(-1);
    assert_eq!(focus.selected(), Some(A::Thinking));
    focus.move_by(isize::MAX);
    focus.move_by(isize::MIN);
    assert!(focus.selected().is_some());
}
#[test]
fn renderer_highlights_only_the_selected_visible_chip_in_every_theme() {
    let mut app = Demo::default();
    for index in 0..app.themes.len() {
        app.theme_index = index;
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        app.footer.enter();
        let frame = terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let highlighted: String = frame
            .buffer
            .content
            .iter()
            .filter(|cell| cell.modifier.contains(Modifier::REVERSED | Modifier::BOLD))
            .map(|cell| cell.symbol())
            .collect();
        assert_eq!(highlighted, "think:high");
        let lower: String = (0..100).map(|x| frame.buffer[(x, 23)].symbol()).collect();
        assert!(lower.contains("plan:off"));
        assert!(!lower.contains("think:high"));
        app.footer.leave();
    }
}
#[test]
fn renderer_hides_focus_on_tiny_viewport_without_retargeting_on_return() {
    let mut app = Demo::default();
    let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    app.footer.enter();
    terminal.backend_mut().resize(20, 6);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert_eq!(app.footer.selected(), None);
    terminal.backend_mut().resize(100, 24);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert_eq!(app.footer.activate(), (true, None));
}

#[test]
fn arabic_ligature_width_cannot_hide_an_actionable_chip() {
    let mut app = Demo {
        footer_fields: vec![
            F::new(K::Seat, "لا".repeat(20)),
            F::new(K::Thinking, "think:high"),
            F::new(K::Plan, "plan:off"),
        ],
        ..Demo::default()
    };
    let mut terminal = Terminal::new(TestBackend::new(40, 14)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(app.footer.enter());
    let frame = terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let highlighted: String = frame
        .buffer
        .content
        .iter()
        .filter(|cell| cell.modifier.contains(Modifier::REVERSED | Modifier::BOLD))
        .map(|cell| cell.symbol())
        .collect();
    assert_eq!(highlighted, "think:high");
}

#[test]
fn unshortenable_seat_survives_by_conceding_thinking_if_the_original_fits() {
    let seat = format!("{}x", "\u{1100}".repeat(20));
    let layout = FooterLayout::new(
        &[
            F::new(K::Seat, seat.clone()),
            F::new(K::Thinking, "think:high"),
        ],
        45,
    );
    assert!(!layout.status.overflow);
    assert_eq!(layout.status.text(), seat);
    assert!(!layout.actions.contains(&A::Thinking));
}
