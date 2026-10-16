//! Synthetic selected-event pipeline, not a live connection or historical replay.
use litetui_terminal::{
    protocol::{Delivery, JsonlDecoder, Notice, WireClient},
    stream::{Limits, Projection, ProjectionFault, StreamState},
};
use serde_json::{Value, json};

const A: &str = "b4d5db8b-9827-42de-983e-1b7ac183ba49";
const B: &str = "ef78dd70-b2a2-46d3-a325-e45d9c31b330";

fn limits() -> Limits {
    Limits {
        rows: 4,
        tool_calls: 16,
        text_bytes: 4096,
        metadata_bytes: 4096,
    }
}
fn envelope(execution: &str, event: Value) -> Value {
    json!({"type":"gui.event", "session_id":"session-A", "operation_id":"operation-A",
        "execution_id":execution, "hook_id":"hook-A", "source":"typed", "event":event})
}
fn start(execution: &str) -> Value {
    envelope(
        execution,
        json!({"type":"turn_start","model":"fixture-model","thinking_level":"high"}),
    )
}
fn delta(execution: &str, kind: &str, text: &str) -> Value {
    envelope(execution, json!({"type":kind,"text":text}))
}
fn end(execution: &str) -> Value {
    envelope(
        execution,
        json!({"type":"turn_end","stopReason":"stop","tps":null,"tpsSource":null}),
    )
}
fn selected(frame: Value) -> Notice {
    Notice::Event {
        event: frame["event"].clone(),
        frame,
    }
}
fn projection() -> Projection {
    let mut p = Projection::new(7, limits()).unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    p
}

#[test]
fn synthetic_wire_stream_projects_reasoning_text_and_end_without_mirrors() {
    let policy = ["turn_start", "text_delta", "reasoning_delta", "turn_end"]
        .map(|name| (name.into(), Delivery::Mirrored));
    let mut client = WireClient::new(7, 4, policy).unwrap();
    let hello = client.begin_hello().unwrap();
    let notice = client.ingest(
        7,
        json!({"type":"response","id":hello["id"],"ok":true,
        "result":{"protocol_version":1,"data_version":1,"capabilities":[],"operations":[]}}),
    );
    let mut p = Projection::new(7, limits()).unwrap();
    p.apply(7, &notice).unwrap();
    let mut decoder = JsonlDecoder::new(4096).unwrap();
    for frame in [
        start(A),
        delta(A, "reasoning_delta", "Check"),
        delta(A, "text_delta", "same"),
        delta(A, "text_delta", "same"),
        end(A),
    ] {
        let wire = format!("{}\n{frame}\n", frame["event"]);
        for byte in wire.as_bytes() {
            decoder
                .push(&[*byte], |value| {
                    p.apply(7, &client.ingest(7, value)).unwrap();
                })
                .unwrap();
        }
    }
    decoder.finish().unwrap();
    assert_eq!(p.rows().len(), 1);
    let row = &p.rows()[0];
    assert_eq!(row.answer, "samesame");
    assert_eq!(row.reasoning, "Check");
    assert_eq!(row.origin.operation, Some(json!("operation-A")));
    assert!(matches!(&row.state, StreamState::Ended(terminal) if terminal.reason == "stop"));
    assert!(p.fault().is_none());
    // Complete the synthetic wire -> selected notice -> state -> visible buffer
    // path. This is deliberately not a rebinding of historical capture IDs.
    let theme = &litetui_terminal::theme::builtin_themes()[0];
    let mut terminal = ratatui::Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    let rendered = terminal
        .draw(|f| litetui_terminal::ui::draw_stream_projection(f, f.area(), &p, theme, 0))
        .unwrap();
    let text: String = rendered
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("samesame") && text.contains("Check") && text.contains("Stream ended"));
}

#[test]
fn interleaved_executions_and_unknown_input_identity_remain_separate() {
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    let mut b = start(B);
    for name in ["operation_id", "hook_id", "source"] {
        b[name] = Value::Null;
    }
    p.apply(7, &selected(b.clone())).unwrap();
    b["event"] = json!({"type":"text_delta","text":"B"});
    p.apply(7, &selected(b)).unwrap();
    p.apply(7, &selected(delta(A, "text_delta", "A"))).unwrap();
    assert_eq!(p.rows()[0].answer, "A");
    assert_eq!(p.rows()[1].answer, "B");
    assert_eq!(p.rows()[1].origin.operation, None);
    assert_eq!(p.rows()[1].origin.hook, None);
}

#[test]
fn changing_origin_blocks_without_retagging_or_appending() {
    for field in ["session_id", "operation_id", "hook_id", "source"] {
        let mut p = projection();
        p.apply(7, &selected(start(A))).unwrap();
        let original = p.rows()[0].origin.clone();
        let mut changed = delta(A, "text_delta", "wrong");
        changed[field] = json!("changed");
        assert_eq!(
            p.apply(7, &selected(changed)),
            Err(ProjectionFault::OriginChanged)
        );
        assert_eq!(p.rows()[0].origin, original);
        assert!(p.rows()[0].answer.is_empty());
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
        assert_eq!(
            p.apply(7, &selected(end(A))),
            Err(ProjectionFault::OriginChanged)
        );
    }
}

#[test]
fn malformed_and_unattributed_frames_do_not_create_rows() {
    let mut cases = Vec::new();
    let mut missing = start(A);
    missing.as_object_mut().unwrap().remove("execution_id");
    cases.push(missing);
    let mut malformed = start(A);
    malformed["execution_id"] = json!("not-uuid");
    cases.push(malformed);
    let mut no_session = start(A);
    no_session["session_id"] = Value::Null;
    cases.push(no_session);
    let mut bad_operation = start(A);
    bad_operation["operation_id"] = json!(true);
    cases.push(bad_operation);
    let mut bad_model = start(A);
    bad_model["event"]["model"] = json!([]);
    cases.push(bad_model);
    for frame in cases {
        let mut p = projection();
        assert!(p.apply(7, &selected(frame)).is_err());
        assert!(p.rows().is_empty());
    }
}

#[test]
fn missing_start_duplicate_start_and_post_end_are_not_silently_reconciled() {
    let mut p = projection();
    assert_eq!(
        p.apply(7, &selected(delta(A, "text_delta", "orphan"))),
        Err(ProjectionFault::MissingStart)
    );
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    assert_eq!(
        p.apply(7, &selected(start(A))),
        Err(ProjectionFault::DuplicateStart)
    );
    for late in [delta(A, "text_delta", "late"), end(A)] {
        let mut p = projection();
        p.apply(7, &selected(start(A))).unwrap();
        p.apply(7, &selected(end(A))).unwrap();
        assert_eq!(p.apply(7, &selected(late)), Err(ProjectionFault::AfterEnd));
        assert!(matches!(p.rows()[0].state, StreamState::Ended(_)));
        assert!(p.rows()[0].answer.is_empty());
    }
}

#[test]
fn stale_generation_and_disconnect_do_not_complete_active_streams() {
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(6, &selected(delta(A, "text_delta", "old")))
        .unwrap();
    assert!(p.rows()[0].answer.is_empty() && p.fault().is_none());
    p.disconnect();
    assert_eq!(p.rows()[0].state, StreamState::Incomplete);
    assert_eq!(
        p.apply(7, &selected(end(A))),
        Err(ProjectionFault::Disconnected)
    );
}

#[test]
fn excluded_events_and_uncertain_transport_block_instead_of_disappearing() {
    for notice in [
        selected(envelope(A, json!({"type":"tool_start"}))),
        Notice::LegacyEvent(json!({"type":"text_delta","text":"legacy"})),
        Notice::Quarantined(start(A)),
    ] {
        let mut p = projection();
        p.apply(7, &selected(start(A))).unwrap();
        assert!(p.apply(7, &notice).is_err());
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
    }
}

#[test]
fn capacity_limits_preserve_existing_content_and_fail_closed() {
    for limit in [
        Limits {
            rows: 0,
            ..limits()
        },
        Limits {
            text_bytes: 0,
            ..limits()
        },
        Limits {
            metadata_bytes: 0,
            ..limits()
        },
    ] {
        assert!(Projection::new(7, limit).is_err());
    }
    let mut p = Projection::new(
        7,
        Limits {
            rows: 1,
            text_bytes: 4,
            ..limits()
        },
    )
    .unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(7, &selected(delta(A, "text_delta", "🦀"))).unwrap();
    assert_eq!(
        p.apply(7, &selected(delta(A, "reasoning_delta", "x"))),
        Err(ProjectionFault::Capacity)
    );
    assert_eq!(p.rows()[0].answer, "🦀");
    assert!(p.rows()[0].reasoning.is_empty());
    let mut p = Projection::new(
        7,
        Limits {
            rows: 1,
            ..limits()
        },
    )
    .unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    p.apply(7, &selected(start(A))).unwrap();
    assert_eq!(
        p.apply(7, &selected(start(B))),
        Err(ProjectionFault::Capacity)
    );
    assert_eq!(p.rows().len(), 1);
    let mut p = Projection::new(
        7,
        Limits {
            metadata_bytes: 1,
            ..limits()
        },
    )
    .unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    assert_eq!(
        p.apply(7, &selected(start(A))),
        Err(ProjectionFault::Capacity)
    );
    assert!(p.rows().is_empty());
}

#[test]
fn identified_stream_renders_real_content_and_honest_status_in_existing_theme() {
    use litetui_terminal::{theme::builtin_themes, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(
        7,
        &selected(delta(A, "reasoning_delta", "Inspect the actual input")),
    )
    .unwrap();
    p.apply(7, &selected(delta(A, "text_delta", "Actual answer 🦀")))
        .unwrap();
    p.apply(7, &selected(end(A))).unwrap();
    let theme = &builtin_themes()[0];
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
    let rendered = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, theme, 0))
        .unwrap();
    let text: String = rendered
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    for expected in [
        "fixture-model",
        "Inspect the actual input",
        "Actual answer",
        "Thinking",
        "Stream ended",
        "stop",
        "┏",
        "╍",
        "┗",
    ] {
        assert!(text.contains(expected), "missing {expected}: {text}");
    }
    for forbidden in ["mock-model", "Speak", "40.9", "Preview reply", "success"] {
        assert!(!text.contains(forbidden), "invented {forbidden}");
    }
}

#[test]
fn render_escapes_untrusted_controls_preserves_source_and_handles_resize() {
    use litetui_terminal::{theme::builtin_themes, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut p = projection();
    let mut frame = start(A);
    frame["event"]["model"] = json!("model\u{1b}[31m");
    p.apply(7, &selected(frame)).unwrap();
    let input = "safe\u{1b}]52;c;ignored\u{7}\r\u{202e}界e\u{301}👩‍💻\nlast line";
    p.apply(7, &selected(delta(A, "text_delta", input)))
        .unwrap();
    p.disconnect();
    for theme in builtin_themes() {
        for (width, height) in [(1, 1), (11, 3), (20, 9), (40, 20), (100, 30)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            let rendered = terminal
                .draw(|f| ui::draw_stream_projection(f, f.area(), &p, &theme, 0))
                .unwrap();
            for cell in &rendered.buffer.content {
                assert!(
                    !cell
                        .symbol()
                        .chars()
                        .any(|ch| ch.is_control() || ch == '\u{202e}')
                );
                assert!(
                    cell.bg == theme.background || cell.bg == theme.surface,
                    "background must use the selected dark theme: {} {:?}",
                    theme.name,
                    cell.bg
                );
            }
            if width == 100 {
                let text: String = rendered
                    .buffer
                    .content
                    .iter()
                    .map(|cell| cell.symbol())
                    .collect();
                assert!(text.contains("BLOCKED") && text.contains("Incomplete"));
                assert!(text.contains("last line") && text.contains("👩‍💻"));
            }
        }
    }
    assert_eq!(p.rows()[0].answer, input);
}

#[test]
fn terminal_schema_and_budget_fail_before_settling_or_losing_text() {
    for (field, value) in [
        ("stopReason", json!("")),
        ("stopReason", json!(5)),
        ("tps", json!(true)),
        ("tps", json!(-1)),
        ("tpsSource", json!([])),
        ("error", json!({})),
    ] {
        let mut p = projection();
        p.apply(7, &selected(start(A))).unwrap();
        p.apply(7, &selected(delta(A, "text_delta", "retained")))
            .unwrap();
        let mut terminal = end(A);
        terminal["event"][field] = value;
        assert!(p.apply(7, &selected(terminal)).is_err());
        assert_eq!(p.rows()[0].answer, "retained");
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
    }
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    let mut terminal = end(A);
    terminal["event"]["error"] = json!("x".repeat(limits().metadata_bytes));
    assert_eq!(
        p.apply(7, &selected(terminal)),
        Err(ProjectionFault::Capacity)
    );
    assert_eq!(p.rows()[0].state, StreamState::Incomplete);
}

#[test]
fn selected_notice_requires_negotiation_and_matching_envelope() {
    let mut p = Projection::new(7, limits()).unwrap();
    assert_eq!(
        p.apply(7, &selected(start(A))),
        Err(ProjectionFault::NotNegotiated)
    );
    assert_eq!(
        p.apply(7, &Notice::Hello(json!({}))),
        Err(ProjectionFault::NotNegotiated)
    );
    let mut p = projection();
    let mismatched = Notice::Event {
        frame: start(A),
        event: json!({"type":"turn_end"}),
    };
    assert!(p.apply(7, &mismatched).is_err());
    assert!(p.rows().is_empty());
}

#[test]
fn narrow_wrapping_keeps_graphemes_whole_and_scroll_reaches_the_last_line() {
    use litetui_terminal::{theme::builtin_themes, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    let input = format!(
        "{}👩‍💻\n{}\nFINAL ANSWER",
        "x".repeat(11),
        "middle\n".repeat(30)
    );
    p.apply(7, &selected(delta(A, "text_delta", &input)))
        .unwrap();
    let theme = &builtin_themes()[0];
    let mut terminal = Terminal::new(TestBackend::new(20, 14)).unwrap();
    let top = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, theme, 0))
        .unwrap();
    assert!(top.buffer.content.iter().any(|cell| cell.symbol() == "👩‍💻"));
    let bottom = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, theme, usize::MAX))
        .unwrap();
    let text: String = bottom
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("FINAL ANSWER"));
    assert_eq!(p.rows()[0].answer, input);
}

#[test]
fn every_bidi_control_is_visible_as_an_escape_not_active_formatting() {
    use litetui_terminal::{theme::builtin_themes, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let controls = [
        0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068,
        0x2069,
    ];
    let input: String = controls
        .iter()
        .map(|n| format!("before{}after\n", char::from_u32(*n).unwrap()))
        .collect();
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(7, &selected(delta(A, "text_delta", &input)))
        .unwrap();
    let theme = &builtin_themes()[0];
    let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
    let rendered = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, theme, 0))
        .unwrap();
    let text: String = rendered
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    for n in controls {
        assert!(
            text.contains(&format!("before\\u{{{n:x}}}after")),
            "missing visible escape {n:x}"
        );
        assert!(!text.contains(char::from_u32(n).unwrap()));
    }
    assert_eq!(p.rows()[0].answer, input);
}

#[test]
fn arabic_ligature_width_cannot_push_assistant_borders_out_of_the_viewport() {
    use litetui_terminal::{theme::builtin_themes, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut p = projection();
    let mut frame = start(A);
    frame["event"]["model"] = json!("لا");
    p.apply(7, &selected(frame)).unwrap();
    p.apply(7, &selected(delta(A, "reasoning_delta", "لا")))
        .unwrap();
    p.apply(7, &selected(delta(A, "text_delta", "لا"))).unwrap();
    let mut terminal_event = end(A);
    terminal_event["event"]["stopReason"] = json!("لا");
    p.apply(7, &selected(terminal_event)).unwrap();
    let theme = &builtin_themes()[0];
    let mut terminal = Terminal::new(TestBackend::new(20, 30)).unwrap();
    let rendered = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, theme, 0))
        .unwrap();
    let mut body_rows = 0;
    for y in 1..30 {
        match rendered.buffer[(0, y)].symbol() {
            "│" => {
                body_rows += 1;
                assert_eq!(rendered.buffer[(19, y)].symbol(), "│", "row {y}");
            }
            "╭" => assert_eq!(rendered.buffer[(19, y)].symbol(), "╮"),
            "╰" => assert_eq!(rendered.buffer[(19, y)].symbol(), "╯"),
            _ => {}
        }
    }
    assert!(body_rows > 0);
}

#[test]
fn aggregate_metadata_fits_exactly_then_one_more_byte_blocks_atomically() {
    let one_start: usize = [
        A,
        "session-A",
        "operation-A",
        "hook-A",
        "typed",
        "fixture-model",
        "high",
    ]
    .into_iter()
    .map(str::len)
    .sum();
    let mut p = Projection::new(
        7,
        Limits {
            metadata_bytes: one_start * 2 + "stop".len(),
            ..limits()
        },
    )
    .unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(7, &selected(start(B))).unwrap();
    p.apply(7, &selected(end(A))).unwrap();
    let before = p.rows()[1].origin.clone();
    let mut over = end(B);
    over["event"]["stopReason"] = json!("X");
    assert_eq!(p.apply(7, &selected(over)), Err(ProjectionFault::Capacity));
    assert!(
        matches!(&p.rows()[0].state, StreamState::Ended(terminal) if terminal.reason == "stop")
    );
    assert_eq!(p.rows()[1].origin, before);
    assert_eq!(p.rows()[1].state, StreamState::Incomplete);
}

#[test]
fn a_global_fault_keeps_observed_ends_and_invalidates_every_active_row() {
    const C: &str = "3c9ac130-942a-4f4c-b739-2cb1c6bbc658";
    let mut p = projection();
    p.apply(7, &selected(start(A))).unwrap();
    p.apply(7, &selected(end(A))).unwrap();
    for (execution, text) in [(B, "kept-B"), (C, "kept-C")] {
        p.apply(7, &selected(start(execution))).unwrap();
        p.apply(7, &selected(delta(execution, "text_delta", text)))
            .unwrap();
    }
    let fault = selected(envelope(B, json!({"type":"unsupported"})));
    assert_eq!(p.apply(7, &fault), Err(ProjectionFault::UnsupportedEvent));
    assert!(
        matches!(&p.rows()[0].state, StreamState::Ended(terminal) if terminal.reason == "stop")
    );
    for (row, text) in p.rows()[1..].iter().zip(["kept-B", "kept-C"]) {
        assert_eq!(row.state, StreamState::Incomplete);
        assert_eq!(row.answer, text);
    }
    assert_eq!(
        p.apply(7, &selected(end(B))),
        Err(ProjectionFault::UnsupportedEvent)
    );
}
