//! Explicit generic host observations, not tool authority or task completion.
use litetui_terminal::{
    protocol::{Delivery, JsonlDecoder, Notice, WireClient},
    stream::{Limits, Projection, ProjectionFault, StreamState, ToolObservationState},
    theme::builtin_themes,
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

const A: &str = "b4d5db8b-9827-42de-983e-1b7ac183ba49";
const B: &str = "ef78dd70-b2a2-46d3-a325-e45d9c31b330";
const T: &str = "22222222-2222-4222-8222-222222222222";
const U: &str = "33333333-3333-4333-8333-333333333333";
const V: &str = "44444444-4444-4444-8444-444444444444";

fn limits() -> Limits {
    Limits {
        rows: 4,
        tool_calls: 16,
        text_bytes: 4096,
        metadata_bytes: 4096,
    }
}
fn selected(execution: &str, event: Value) -> Notice {
    let frame = json!({"type":"gui.event", "execution_id":execution,
        "session_id":"session", "operation_id":null, "hook_id":null,
        "source":null, "event":event});
    Notice::Event { frame, event }
}
fn start() -> Value {
    json!({"type":"turn_start", "model":null, "thinking_level":null})
}
fn end() -> Value {
    json!({"type":"turn_end", "stopReason":"stop", "tps":null, "tpsSource":null})
}
fn request(id: &str, round: u64, slot: u64) -> Value {
    json!({"type":"generic_tool_requested", "observation_id":id,
        "round":round, "slot":slot, "provider_call_id":"call_0", "name":"powershell"})
}
fn returned(id: &str, result: &str, ok: bool) -> Value {
    json!({"type":"generic_tool_returned", "observation_id":id, "result":result, "ok":ok})
}
fn projection(limits: Limits) -> Projection {
    let mut p = Projection::new(7, limits).unwrap();
    p.apply(7, &Notice::Hello(json!({}))).unwrap();
    p.apply(7, &selected(A, start())).unwrap();
    p
}
fn apply(p: &mut Projection, execution: &str, event: Value) {
    p.apply(7, &selected(execution, event)).unwrap();
}

#[test]
fn explicitly_identified_tool_pair_is_accepted_without_inventing_completion() {
    let mut p = projection(limits());
    apply(&mut p, A, request(T, 0, 0));
    assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Requested);
    apply(&mut p, A, returned(T, "Background task started", true));
    apply(&mut p, A, end());
    assert!(p.fault().is_none());
    assert_eq!(
        p.rows()[0].tools[0].state,
        ToolObservationState::Returned {
            result: "Background task started".into(),
            ok: true,
        }
    );
    assert!(matches!(p.rows()[0].state, StreamState::Ended(_)));
}

#[test]
fn repeated_provider_ids_coordinates_and_equal_results_are_distinct_observations() {
    let mut p = projection(limits());
    apply(&mut p, B, start());
    for (execution, id, round) in [(A, T, 3), (A, U, 0), (B, V, 0)] {
        apply(&mut p, execution, request(id, round, 7));
        apply(&mut p, execution, returned(id, "same result", false));
    }
    let tools = &p.rows()[0].tools;
    assert_eq!(tools.len(), 2);
    assert_eq!(tools[0].id.as_str(), T);
    assert_eq!(tools[1].id.as_str(), U);
    assert_eq!((tools[0].round, tools[1].round, tools[1].slot), (3, 0, 7));
    assert_eq!(tools[0].provider_call_id, tools[1].provider_call_id);
    assert_eq!(tools[0].state, tools[1].state);
    assert_eq!(p.rows()[1].tools[0].id.as_str(), V);
}

#[test]
fn duplicate_ids_remain_invalid_after_return_and_across_executions() {
    for returned_first in [false, true] {
        for duplicate_execution in [A, B] {
            let mut p = projection(limits());
            apply(&mut p, B, start());
            apply(&mut p, A, request(T, 0, 0));
            if returned_first {
                apply(&mut p, A, returned(T, "retained", true));
            }
            assert_eq!(
                p.apply(7, &selected(duplicate_execution, request(T, 9, 9))),
                Err(ProjectionFault::DuplicateToolRequest)
            );
            assert_eq!(p.rows()[0].tools.len(), 1);
            assert!(p.rows()[1].tools.is_empty());
            if returned_first {
                assert!(
                    matches!(&p.rows()[0].tools[0].state, ToolObservationState::Returned { result, .. } if result == "retained")
                );
            } else {
                assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
            }
        }
    }
}

#[test]
fn unknown_cross_execution_and_duplicate_returns_never_guess_a_join() {
    for (execution, id) in [(A, U), (B, T)] {
        let mut p = projection(limits());
        apply(&mut p, B, start());
        apply(&mut p, A, request(T, 0, 0));
        assert_eq!(
            p.apply(7, &selected(execution, returned(id, "wrong", true))),
            Err(ProjectionFault::MissingToolRequest)
        );
        assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
        assert!(p.rows()[1].tools.is_empty());
    }
    let mut p = projection(limits());
    apply(&mut p, A, request(T, 0, 0));
    apply(&mut p, A, returned(T, "first", false));
    assert_eq!(
        p.apply(7, &selected(A, returned(T, "second", true))),
        Err(ProjectionFault::DuplicateToolReturn)
    );
    assert_eq!(
        p.rows()[0].tools[0].state,
        ToolObservationState::Returned {
            result: "first".into(),
            ok: false
        }
    );
}

#[test]
fn strict_request_schema_rejects_missing_wrong_and_non_integer_coordinates() {
    for field in [
        "observation_id",
        "round",
        "slot",
        "provider_call_id",
        "name",
    ] {
        for bad in [
            None,
            Some(Value::Null),
            Some(json!(true)),
            Some(json!([])),
            Some(json!({})),
        ] {
            let mut event = request(T, 0, 0);
            match bad {
                None => {
                    event.as_object_mut().unwrap().remove(field);
                }
                Some(value) => {
                    event[field] = value;
                }
            }
            let mut p = projection(limits());
            assert!(
                matches!(
                    p.apply(7, &selected(A, event)),
                    Err(ProjectionFault::Malformed(_))
                ),
                "{field}"
            );
            assert!(p.rows()[0].tools.is_empty());
        }
    }
    for raw in ["-1", "0.0", "1e0", "18446744073709551616", "\"1\""] {
        for field in ["round", "slot"] {
            let mut event = request(T, 0, 0);
            event[field] = serde_json::from_str(raw).unwrap();
            let mut p = projection(limits());
            assert!(p.apply(7, &selected(A, event)).is_err(), "{field}={raw}");
            assert!(p.rows()[0].tools.is_empty());
        }
    }
    let mut event = request(T, u64::MAX, u64::MAX);
    event["name"] = json!("");
    event["provider_call_id"] = json!("");
    let mut p = projection(limits());
    apply(&mut p, A, event);
    apply(&mut p, A, returned(T, "", false));
    assert_eq!(p.rows()[0].tools[0].round, u64::MAX);
    assert_eq!(
        p.rows()[0].tools[0].state,
        ToolObservationState::Returned {
            result: String::new(),
            ok: false
        }
    );
}

#[test]
fn both_tool_events_require_canonical_uuid4_and_return_types() {
    for id in [
        "",
        "unknown",
        "22222222-2222-5222-8222-222222222222",
        "22222222-2222-4222-7222-222222222222",
        "B4D5DB8B-9827-42DE-983E-1B7AC183BA49",
    ] {
        for event in [request(id, 0, 0), returned(id, "bad", true)] {
            let mut p = projection(limits());
            apply(&mut p, A, request(T, 0, 0));
            assert!(matches!(
                p.apply(7, &selected(A, event)),
                Err(ProjectionFault::Malformed(_))
            ));
            assert_eq!(p.rows()[0].tools.len(), 1);
            assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
        }
    }
    for field in ["observation_id", "result", "ok"] {
        for bad in [
            None,
            Some(Value::Null),
            Some(json!(1)),
            Some(json!([])),
            Some(json!({})),
        ] {
            let mut event = returned(T, "safe", false);
            match bad {
                None => {
                    event.as_object_mut().unwrap().remove(field);
                }
                Some(value) => {
                    event[field] = value;
                }
            }
            let mut p = projection(limits());
            apply(&mut p, A, request(T, 0, 0));
            assert!(p.apply(7, &selected(A, event)).is_err());
            assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
        }
    }
}

#[test]
fn end_and_global_fault_keep_returns_but_close_every_pending_observation() {
    for disconnect in [false, true] {
        let mut p = projection(limits());
        apply(&mut p, A, request(T, 0, 0));
        apply(&mut p, A, request(U, 0, 1));
        apply(&mut p, A, returned(T, "retained", true));
        apply(&mut p, A, end());
        assert_eq!(p.rows()[0].tools[1].state, ToolObservationState::Incomplete);
        apply(&mut p, B, start());
        apply(&mut p, B, request(V, 0, 0));
        if disconnect {
            p.disconnect();
        } else {
            assert_eq!(
                p.apply(7, &selected(B, json!({"type":"usage"}))),
                Err(ProjectionFault::UnsupportedEvent)
            );
        }
        assert!(matches!(p.rows()[0].state, StreamState::Ended(_)));
        assert!(
            matches!(&p.rows()[0].tools[0].state, ToolObservationState::Returned { result, .. } if result == "retained")
        );
        assert_eq!(p.rows()[1].state, StreamState::Incomplete);
        assert_eq!(p.rows()[1].tools[0].state, ToolObservationState::Incomplete);
        let before = p.rows().to_vec();
        assert!(p.apply(7, &selected(B, returned(V, "late", true))).is_err());
        assert_eq!(p.rows(), before);
    }
}

#[test]
fn tool_events_obey_negotiation_start_origin_and_end_guards() {
    for event in [request(T, 0, 0), returned(T, "x", true)] {
        let mut cold = Projection::new(7, limits()).unwrap();
        assert_eq!(
            cold.apply(7, &selected(A, event.clone())),
            Err(ProjectionFault::NotNegotiated)
        );
        let mut p = projection(limits());
        assert_eq!(
            p.apply(7, &selected(B, event.clone())),
            Err(ProjectionFault::MissingStart)
        );
        let mut p = projection(limits());
        apply(&mut p, A, request(U, 0, 0));
        apply(&mut p, A, end());
        assert_eq!(
            p.apply(7, &selected(A, event.clone())),
            Err(ProjectionFault::AfterEnd)
        );
        for field in ["session_id", "operation_id", "hook_id", "source"] {
            let mut p = projection(limits());
            let mut notice = selected(A, event.clone());
            if let Notice::Event { frame, .. } = &mut notice {
                frame[field] = json!("changed");
            }
            assert_eq!(p.apply(7, &notice), Err(ProjectionFault::OriginChanged));
            assert!(p.rows()[0].tools.is_empty());
        }
        let mut p = projection(limits());
        let mut mismatched = selected(A, event.clone());
        if let Notice::Event { frame, .. } = &mut mismatched {
            frame["event"]["extra"] = json!(1);
        }
        assert!(matches!(
            p.apply(7, &mismatched),
            Err(ProjectionFault::Malformed(_))
        ));
    }
}

#[test]
fn stale_and_mirrored_observations_do_not_mutate_selected_state() {
    let mut p = projection(limits());
    p.apply(6, &selected(A, request(T, 0, 0))).unwrap();
    p.apply(7, &Notice::IgnoredMirror(request(T, 0, 0)))
        .unwrap();
    assert!(p.rows()[0].tools.is_empty());
    apply(&mut p, A, request(T, 0, 0));
    p.apply(6, &selected(A, returned(T, "old", true))).unwrap();
    p.apply(7, &Notice::IgnoredMirror(returned(T, "raw", true)))
        .unwrap();
    assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Requested);
}

#[test]
fn aggregate_call_and_metadata_limits_are_exact_and_do_not_release_on_return() {
    assert!(matches!(
        Projection::new(
            7,
            Limits {
                tool_calls: 0,
                ..limits()
            }
        ),
        Err(ProjectionFault::InvalidLimit)
    ));
    let mut p = projection(Limits {
        tool_calls: 1,
        ..limits()
    });
    apply(&mut p, A, request(T, 0, 0));
    apply(&mut p, A, returned(T, "retained", true));
    apply(&mut p, B, start());
    assert_eq!(
        p.apply(7, &selected(B, request(U, 0, 0))),
        Err(ProjectionFault::Capacity)
    );
    assert!(p.rows()[1].tools.is_empty());
    // Two origins (36-byte execution + 7-byte session), then two tool requests
    // (36-byte ID + 6-byte provider ID + 10-byte name + two 8-byte coordinates).
    let exact = 2 * (36 + 7) + 2 * (36 + 6 + 10 + 16);
    for extra in ["", "x"] {
        let mut p = projection(Limits {
            metadata_bytes: exact,
            ..limits()
        });
        apply(&mut p, B, start());
        apply(&mut p, A, request(T, 0, 0));
        let mut event = request(U, 0, 0);
        event["name"] = json!(format!("powershell{extra}"));
        let result = p.apply(7, &selected(B, event));
        assert_eq!(
            result,
            if extra.is_empty() {
                Ok(())
            } else {
                Err(ProjectionFault::Capacity)
            }
        );
        assert_eq!(p.rows()[1].tools.len(), usize::from(extra.is_empty()));
        assert_eq!(p.rows()[0].tools[0].name, "powershell");
    }
}

#[test]
fn results_share_utf8_budget_with_answer_and_reasoning_without_partial_append() {
    for oversized in [false, true] {
        let mut p = projection(Limits {
            text_bytes: 8,
            ..limits()
        });
        apply(&mut p, A, json!({"type":"text_delta", "text":"a"}));
        apply(&mut p, A, json!({"type":"reasoning_delta", "text":"b"}));
        apply(&mut p, A, request(T, 0, 0));
        apply(&mut p, A, returned(T, "🦀", true));
        apply(&mut p, B, start());
        apply(&mut p, B, request(U, 0, 0));
        let result = p.apply(
            7,
            &selected(B, returned(U, if oversized { "éx" } else { "é" }, false)),
        );
        assert_eq!(
            result,
            if oversized {
                Err(ProjectionFault::Capacity)
            } else {
                Ok(())
            }
        );
        assert_eq!(p.rows()[0].answer, "a");
        assert_eq!(p.rows()[0].reasoning, "b");
        assert_eq!(
            p.rows()[0].tools[0].state,
            ToolObservationState::Returned {
                result: "🦀".into(),
                ok: true
            }
        );
        if oversized {
            assert_eq!(p.rows()[1].tools[0].state, ToolObservationState::Incomplete);
        } else {
            assert_eq!(
                p.apply(7, &selected(A, json!({"type":"text_delta", "text":"x"}))),
                Err(ProjectionFault::Capacity)
            );
            assert_eq!(
                p.rows()[1].tools[0].state,
                ToolObservationState::Returned {
                    result: "é".into(),
                    ok: false
                }
            );
        }
    }
}

#[test]
fn synthetic_bytewise_wire_to_tool_state_and_visible_rows() {
    let kinds = [
        "turn_start",
        "text_delta",
        "reasoning_delta",
        "turn_end",
        "generic_tool_requested",
        "generic_tool_returned",
    ];
    let mut client =
        WireClient::new(7, 4, kinds.map(|kind| (kind.into(), Delivery::Mirrored))).unwrap();
    let hello = client.begin_hello().unwrap();
    let mut p = Projection::new(7, limits()).unwrap();
    p.apply(
        7,
        &client.ingest(
            7,
            json!({"type":"response", "id":hello["id"], "ok":true,
        "result":{"protocol_version":1,"data_version":1,"capabilities":[],"operations":[]}}),
        ),
    )
    .unwrap();
    let mut decoder = JsonlDecoder::new(4096).unwrap();
    for event in [
        start(),
        request(T, 0, 0),
        returned(T, "Real supplied result", false),
        json!({"type":"text_delta", "text":"Actual answer"}),
        end(),
    ] {
        let Notice::Event { frame, event } = selected(A, event) else {
            unreachable!()
        };
        for byte in format!("{event}\n{frame}\n").as_bytes() {
            decoder
                .push(&[*byte], |value| {
                    p.apply(7, &client.ingest(7, value)).unwrap();
                })
                .unwrap();
        }
    }
    decoder.finish().unwrap();
    assert_eq!(p.rows()[0].tools.len(), 1);
    let mut terminal = Terminal::new(TestBackend::new(100, 35)).unwrap();
    let frame = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, &builtin_themes()[0], 0))
        .unwrap();
    let text: String = frame.buffer.content.iter().map(|c| c.symbol()).collect();
    for expected in [
        "Actual answer",
        "Tools grouped by execution (not transcript order)",
        "Tool: powershell",
        "Returned · host ok=false",
        "Real supplied result",
        "not task completion",
    ] {
        assert!(text.contains(expected), "{expected}: {text}");
    }
    assert!(
        !text.contains("Arguments")
            && !text.contains("waiting for approval")
            && !text.contains("done")
    );
    assert!(text.find("Actual answer").unwrap() < text.find("Tools grouped").unwrap());
}

#[test]
fn tool_rendering_is_safe_theme_aware_and_retains_original_text() {
    let name = "unsafe\x1b[31m\u{202e}لام🦀";
    let provider = "id\u{061c}\x07";
    let result = "result\x1b[2J\u{2066}\nlast 🦀";
    let mut p = projection(limits());
    let mut event = request(T, 0, 0);
    event["name"] = json!(name);
    event["provider_call_id"] = json!(provider);
    apply(&mut p, A, event);
    apply(&mut p, A, returned(T, result, true));
    for theme in builtin_themes() {
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        for (width, height) in [(100, 40), (22, 18), (100, 40)] {
            terminal.backend_mut().resize(width, height);
            terminal.autoresize().unwrap();
            let frame = terminal
                .draw(|f| ui::draw_stream_projection(f, f.area(), &p, &theme, usize::MAX))
                .unwrap();
            let text: String = frame.buffer.content.iter().map(|c| c.symbol()).collect();
            assert!(text.contains("🦀"));
            assert!(
                !text
                    .chars()
                    .any(|c| c.is_control() || ['\u{202e}', '\u{061c}', '\u{2066}'].contains(&c))
            );
            if width == 100 {
                for escaped in ["\\u{1b}", "\\u{202e}", "\\u{61c}", "\\u{7}", "\\u{2066}"] {
                    assert!(text.contains(escaped), "{escaped}");
                }
                let tool_cell = frame
                    .buffer
                    .content
                    .iter()
                    .find(|c| c.symbol() == "T" && c.fg == theme.tool_text)
                    .unwrap();
                assert_eq!(tool_cell.bg, theme.tool_background);
            }
        }
    }
    assert_eq!(p.rows()[0].tools[0].name, name);
    assert_eq!(p.rows()[0].tools[0].provider_call_id, provider);
    assert_eq!(
        p.rows()[0].tools[0].state,
        ToolObservationState::Returned {
            result: result.into(),
            ok: true
        }
    );
}

#[test]
fn requested_and_incomplete_labels_do_not_claim_running_or_success() {
    let mut p = projection(limits());
    apply(&mut p, A, request(T, 0, 0));
    for ended in [false, true] {
        if ended {
            apply(&mut p, A, end());
        }
        let mut terminal = Terminal::new(TestBackend::new(100, 35)).unwrap();
        let frame = terminal
            .draw(|f| ui::draw_stream_projection(f, f.area(), &p, &builtin_themes()[0], 0))
            .unwrap();
        let text: String = frame.buffer.content.iter().map(|c| c.symbol()).collect();
        assert!(text.contains(if ended {
            "Incomplete · return unavailable"
        } else {
            "Requested · dispatch unknown"
        }));
        assert!(!text.contains("running") && !text.contains("success") && !text.contains("done"));
    }
}

#[test]
fn ending_one_execution_does_not_invalidate_another_pending_tool() {
    let mut p = projection(limits());
    apply(&mut p, B, start());
    apply(&mut p, A, request(T, 0, 0));
    apply(&mut p, B, request(U, 0, 0));
    apply(&mut p, A, end());
    assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
    assert_eq!(p.rows()[1].state, StreamState::Streaming);
    assert_eq!(p.rows()[1].tools[0].state, ToolObservationState::Requested);
    apply(&mut p, B, returned(U, "still valid", false));
    assert!(p.fault().is_none());
    assert_eq!(
        p.rows()[1].tools[0].state,
        ToolObservationState::Returned {
            result: "still valid".into(),
            ok: false
        }
    );
}

#[test]
fn terminal_capacity_malformed_terminal_and_transport_fault_preserve_only_returns() {
    // One origin and two requests fit exactly, leaving no terminal metadata.
    let exact = 36 + 7 + 2 * (36 + 6 + 10 + 16);
    for fault in [
        selected(A, end()),
        selected(
            A,
            json!({"type":"turn_end", "stopReason":"stop", "tps":-1, "tpsSource":null}),
        ),
        Notice::Quarantined(json!({"type":"unknown"})),
        Notice::DirectError(json!({"type":"error", "error":"uncertain"})),
    ] {
        let mut p = projection(Limits {
            metadata_bytes: exact,
            ..limits()
        });
        apply(&mut p, A, request(T, 0, 0));
        apply(&mut p, A, request(U, 0, 1));
        apply(&mut p, A, returned(T, "literal refusal", false));
        assert!(p.apply(7, &fault).is_err());
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
        assert_eq!(
            p.rows()[0].tools[0].state,
            ToolObservationState::Returned {
                result: "literal refusal".into(),
                ok: false
            }
        );
        assert_eq!(p.rows()[0].tools[1].state, ToolObservationState::Incomplete);
    }
}

#[test]
fn multibyte_tool_metadata_shares_model_terminal_and_integer_operation_budget() {
    // A origin=36+7, model=4 bytes; B origin=36+7+8 integer operation;
    // each tool=36-byte ID+6 provider+4 name+16 coordinates; A terminal=4.
    let exact = (36 + 7 + 4) + (36 + 7 + 8) + 2 * (36 + 6 + 4 + 16) + 4;
    for extra in ["", "x"] {
        let mut p = Projection::new(
            7,
            Limits {
                metadata_bytes: exact,
                ..limits()
            },
        )
        .unwrap();
        p.apply(7, &Notice::Hello(json!({}))).unwrap();
        let mut a = start();
        a["model"] = json!("🦀");
        apply(&mut p, A, a);
        let mut b = selected(B, start());
        if let Notice::Event { frame, .. } = &mut b {
            frame["operation_id"] = json!(42);
        }
        p.apply(7, &b).unwrap();
        let mut req = request(T, 0, 0);
        req["name"] = json!("🦀");
        apply(&mut p, A, req);
        apply(&mut p, A, end());
        let mut req = request(U, 0, 0);
        req["name"] = json!(format!("🦀{extra}"));
        let mut notice = selected(B, req);
        if let Notice::Event { frame, .. } = &mut notice {
            frame["operation_id"] = json!(42);
        }
        assert_eq!(
            p.apply(7, &notice),
            if extra.is_empty() {
                Ok(())
            } else {
                Err(ProjectionFault::Capacity)
            }
        );
        assert_eq!(p.rows()[1].tools.len(), usize::from(extra.is_empty()));
        assert!(matches!(p.rows()[0].state, StreamState::Ended(_)));
    }
}

#[test]
fn metadata_newlines_are_visible_while_results_remain_multiline() {
    let mut p = projection(limits());
    let mut event = request(T, 0, 0);
    event["name"] = json!("first\nReturned · host ok=true");
    event["provider_call_id"] = json!("provider\nsecond");
    apply(&mut p, A, event);
    apply(&mut p, A, returned(T, "line one\nline two", false));
    let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
    let frame = terminal
        .draw(|f| ui::draw_stream_projection(f, f.area(), &p, &builtin_themes()[0], 0))
        .unwrap();
    let text: String = frame.buffer.content.iter().map(|c| c.symbol()).collect();
    assert!(text.contains("first\\u{a}Returned"));
    assert!(text.contains("provider\\u{a}second"));
    assert!(text.contains(T));
    assert!(!text.contains("line one\\u{a}line two"));
    assert!(text.contains("line one") && text.contains("line two"));
    assert_eq!(p.rows()[0].tools[0].name, "first\nReturned · host ok=true");
}

#[test]
fn minimum_width_subrectangle_and_long_graphemes_never_escape_or_lose_tail() {
    use ratatui::{layout::Rect, widgets::Paragraph};
    let mut p = projection(limits());
    let mut event = request(T, 0, 0);
    event["name"] = json!("لا🧑‍💻".repeat(8));
    event["provider_call_id"] = json!("e\u{301}".repeat(15));
    apply(&mut p, A, event);
    apply(&mut p, A, returned(T, "لا🧑‍💻e\u{301}\nTAIL", true));
    for width in [12, 13, 22] {
        let mut terminal = Terminal::new(TestBackend::new(30, 22)).unwrap();
        let area = Rect::new(3, 2, width, 18);
        let frame = terminal
            .draw(|f| {
                f.render_widget(
                    Paragraph::new("X".repeat(30 * 22))
                        .wrap(ratatui::widgets::Wrap { trim: false }),
                    f.area(),
                );
                ui::draw_stream_projection(f, area, &p, &builtin_themes()[0], usize::MAX);
            })
            .unwrap();
        let mut text = String::new();
        for y in 0..22 {
            for x in 0..30 {
                let cell = &frame.buffer[(x, y)];
                if area.contains((x, y).into()) {
                    text.push_str(cell.symbol());
                } else {
                    assert_eq!(cell.symbol(), "X", "outside ({x},{y}) at width {width}");
                }
            }
        }
        assert!(text.contains("TAIL"), "{width}: {text}");
        assert!(
            !text.contains('�'),
            "fitting graphemes must not be replaced"
        );
    }
}
