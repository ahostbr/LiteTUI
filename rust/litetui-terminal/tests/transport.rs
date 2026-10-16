//! Offline borrowed-byte ingress, not OS pipes or a real child lifecycle.
use litetui_terminal::{
    protocol::{Delivery, JsonlDecoder, Notice, WireClient},
    stream::{Limits as ProjectionLimits, Projection, StreamState, ToolObservationState},
    transport::{Inbound, IngressError, IngressLimits, IngressStatus, Push},
};
use serde_json::{Value, json};

fn limits() -> IngressLimits {
    IngressLimits {
        frame_bytes: 4096,
        queued_frames: 16,
        queued_bytes: 16384,
    }
}
fn inbound(limits: IngressLimits) -> Inbound {
    Inbound::new(7, limits).unwrap()
}
fn drain(queue: &mut Inbound) -> Vec<Vec<u8>> {
    let mut frames = Vec::new();
    while let Some(frame) = queue.pop_frame() {
        frames.push(frame);
    }
    assert_eq!(queue.queued_frames(), 0);
    assert_eq!(queue.queued_bytes(), 0);
    frames
}

#[test]
fn every_fault_split_preserves_the_same_prefix_and_latched_reason() {
    for (bounds, input, expected) in [
        (
            IngressLimits {
                frame_bytes: 3,
                ..limits()
            },
            b"{}\n1234\n{}\n".as_slice(),
            IngressError::FrameTooLarge { limit: 3 },
        ),
        (
            IngressLimits {
                queued_frames: 1,
                ..limits()
            },
            b"{}\n{ }\n{}\n".as_slice(),
            IngressError::FrameQueueFull { limit: 1 },
        ),
        (
            IngressLimits {
                queued_bytes: 3,
                ..limits()
            },
            b"{}\n{ }\n{}\n".as_slice(),
            IngressError::ByteQueueFull { limit: 3 },
        ),
    ] {
        for split in 0..=input.len() {
            let mut q = inbound(bounds);
            let first = q.push(7, &input[..split]);
            assert!(first.is_ok() || first == Err(expected.clone()));
            assert_eq!(q.push(7, &input[split..]), Err(expected.clone()));
            assert_eq!(q.queued_bytes(), 3);
            assert_eq!(q.queued_frames(), 1);
            assert_eq!(q.partial_bytes(), 0);
            assert_eq!(drain(&mut q), [b"{}\n".to_vec()]);
            assert_eq!(q.status(), &IngressStatus::Fault(expected.clone()));
            assert_eq!(q.push(7, b"{}\n"), Err(expected.clone()));
            assert_eq!(q.finish(7), Err(expected.clone()));
        }
    }
}

#[test]
fn stale_push_and_eof_cannot_change_a_current_partial_frame() {
    let mut q = inbound(limits());
    q.push(7, b"{").unwrap();
    assert_eq!(
        q.push(8, b"invalid stale data\n"),
        Ok(Push::StaleGeneration)
    );
    assert_eq!(q.finish(8), Ok(Push::StaleGeneration));
    assert_eq!(q.partial_bytes(), 1);
    assert_eq!(q.status(), &IngressStatus::Open);
    q.push(7, b"}\n").unwrap();
    assert_eq!(drain(&mut q), [b"{}\n".to_vec()]);
}

#[test]
fn decoder_fault_preserves_valid_prefix_but_never_delivers_the_queued_suffix() {
    for bad in [b"\xff\n".as_slice(), b"not JSON\n".as_slice()] {
        let (mut client, mut p, hello) = stack();
        let mut bytes = serialized(&prefix(hello));
        bytes.extend_from_slice(bad);
        bytes.extend(serialized(&[terminal_event()]));
        let mut q = inbound(limits());
        q.push(7, &bytes).unwrap();
        q.finish(7).unwrap();
        let mut decoder = JsonlDecoder::new(limits().frame_bytes).unwrap();
        let mut failed = false;
        while let Some(frame) = q.pop_frame() {
            if decoder
                .push(&frame, |value| {
                    p.apply(7, &client.ingest(7, value)).unwrap();
                })
                .is_err()
            {
                failed = true;
                break;
            }
        }
        assert!(failed);
        assert_eq!(q.queued_frames(), 1);
        // A complete transport-accepted frame is not necessarily decoder-valid.
        // The suffix is cleanup only, never replayed through a fresh decoder.
        assert_eq!(drain(&mut q).len(), 1);
        assert!(decoder.finish().is_err());
        client.disconnect();
        p.disconnect();
        assert_eq!(p.rows()[0].answer, "accepted prefix");
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
        assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
    }
}

#[test]
fn blank_line_filter_matches_actual_decoder_for_every_ascii_byte() {
    for byte in 0..=127 {
        let input = [byte, b'\n'];
        let mut q = inbound(limits());
        q.push(7, &input).unwrap();
        let mut decoder = JsonlDecoder::new(limits().frame_bytes).unwrap();
        let mut emitted = 0;
        let result = decoder.push(&input, |_| emitted += 1);
        let blank = result.is_ok() && emitted == 0;
        assert_eq!(q.queued_frames(), usize::from(!blank), "ASCII {byte}");
        if !blank {
            assert_eq!(q.pop_frame(), Some(input.to_vec()));
        }
    }
    // Rust's ASCII whitespace convention excludes VT (0x0b); do not broaden
    // the inbox filter beyond the existing decoder by silently dropping it.
}

#[test]
fn explicit_positive_limits_are_required() {
    for value in [
        IngressLimits {
            frame_bytes: 0,
            ..limits()
        },
        IngressLimits {
            queued_frames: 0,
            ..limits()
        },
        IngressLimits {
            queued_bytes: 0,
            ..limits()
        },
    ] {
        assert!(matches!(
            Inbound::new(7, value),
            Err(IngressError::InvalidLimit)
        ));
    }
}

#[test]
fn every_split_preserves_original_utf8_crlf_and_nonblank_bytes() {
    let input = " \t\r\n{\"text\":\"🦀\"}\r\n\n {\"value\":2} \n".as_bytes();
    let expected = vec![
        "{\"text\":\"🦀\"}\r\n".as_bytes().to_vec(),
        b" {\"value\":2} \n".to_vec(),
    ];
    for split in 0..=input.len() {
        let mut q = inbound(limits());
        assert_eq!(q.generation(), 7);
        assert_eq!(q.push(7, &input[..split]), Ok(Push::Accepted));
        assert_eq!(q.push(7, &input[split..]), Ok(Push::Accepted));
        assert_eq!(
            q.queued_bytes(),
            expected.iter().map(Vec::len).sum::<usize>()
        );
        assert_eq!(q.queued_frames(), 2);
        assert_eq!(q.partial_bytes(), 0);
        assert_eq!(q.finish(7), Ok(Push::Accepted));
        assert_eq!(q.status(), &IngressStatus::Eof);
        assert_eq!(drain(&mut q), expected);
    }
    let mut q = inbound(limits());
    for byte in input {
        q.push(7, &[*byte]).unwrap();
    }
    assert_eq!(drain(&mut q), expected);
}

#[test]
fn frame_bound_excludes_lf_but_includes_cr_and_whitespace() {
    let mut q = inbound(IngressLimits {
        frame_bytes: 3,
        ..limits()
    });
    q.push(7, b"{}\r").unwrap();
    assert_eq!(q.partial_bytes(), 3);
    q.push(7, b"\n").unwrap();
    assert_eq!(q.queued_bytes(), 4);
    assert_eq!(drain(&mut q), [b"{}\r\n".to_vec()]);
    for input in [b"{} \r\n".as_slice(), b"    \n".as_slice()] {
        let mut q = inbound(IngressLimits {
            frame_bytes: 3,
            ..limits()
        });
        q.push(7, &input[..3]).unwrap();
        assert_eq!(q.partial_bytes(), 3);
        assert_eq!(
            q.push(7, &input[3..]),
            Err(IngressError::FrameTooLarge { limit: 3 })
        );
        assert_eq!(q.partial_bytes(), 0);
        assert!(drain(&mut q).is_empty());
    }
}

#[test]
fn queue_frame_count_fault_preserves_prefix_and_latches_after_drain() {
    let mut q = inbound(IngressLimits {
        queued_frames: 2,
        ..limits()
    });
    let error = IngressError::FrameQueueFull { limit: 2 };
    assert_eq!(q.push(7, b"{}\n{ }\n{  }\n{   }\n"), Err(error.clone()));
    assert_eq!(q.status(), &IngressStatus::Fault(error.clone()));
    assert_eq!(q.queued_frames(), 2);
    assert_eq!(q.queued_bytes(), 7);
    assert_eq!(q.partial_bytes(), 0);
    assert_eq!(drain(&mut q), [b"{}\n".to_vec(), b"{ }\n".to_vec()]);
    assert_eq!(q.push(7, b"{}\n"), Err(error.clone()));
    assert_eq!(q.finish(7), Err(error.clone()));
    assert_eq!(q.status(), &IngressStatus::Fault(error));
    assert!(drain(&mut q).is_empty());
}

#[test]
fn queue_byte_charge_includes_lf_and_checks_whole_frame_before_insertion() {
    for budget in [6, 7] {
        let mut q = inbound(IngressLimits {
            queued_bytes: budget,
            ..limits()
        });
        let outcome = q.push(7, b"{}\n{ }\n");
        if budget == 7 {
            assert_eq!(outcome, Ok(Push::Accepted));
            assert_eq!(q.queued_frames(), 2);
            assert_eq!(q.queued_bytes(), 7);
        } else {
            assert_eq!(outcome, Err(IngressError::ByteQueueFull { limit: 6 }));
            assert_eq!(q.queued_frames(), 1);
            assert_eq!(q.queued_bytes(), 3);
        }
        assert_eq!(q.pop_frame(), Some(b"{}\n".to_vec()));
        assert_eq!(q.queued_bytes(), if budget == 7 { 4 } else { 0 });
    }
    let mut q = inbound(IngressLimits {
        queued_bytes: 1,
        ..limits()
    });
    assert_eq!(
        q.push(7, b"{}\n"),
        Err(IngressError::ByteQueueFull { limit: 1 })
    );
    assert_eq!(q.queued_bytes(), 0);
}

#[test]
fn partial_storage_has_its_own_bound_and_pop_releases_capacity() {
    let mut q = inbound(IngressLimits {
        frame_bytes: 4,
        queued_frames: 1,
        queued_bytes: 3,
    });
    q.push(7, b"{}\n1234").unwrap();
    assert_eq!(q.queued_bytes(), 3);
    assert_eq!(q.partial_bytes(), 4);
    assert_eq!(q.pop_frame(), Some(b"{}\n".to_vec()));
    assert_eq!(
        q.push(7, b"\n"),
        Err(IngressError::ByteQueueFull { limit: 3 })
    );
    assert!(drain(&mut q).is_empty());
    let mut q = inbound(IngressLimits {
        queued_frames: 1,
        queued_bytes: 3,
        ..limits()
    });
    for _ in 0..10 {
        q.push(7, b"{}\n").unwrap();
        assert_eq!(q.pop_frame(), Some(b"{}\n".to_vec()));
    }
    assert_eq!(q.status(), &IngressStatus::Open);
}

#[test]
fn complete_blank_lines_do_not_spend_queue_capacity_but_incomplete_blanks_fault() {
    let mut q = inbound(IngressLimits {
        queued_frames: 1,
        queued_bytes: 3,
        ..limits()
    });
    q.push(7, b"{}\n \t\r\x0c\n\n").unwrap();
    assert_eq!(q.queued_frames(), 1);
    assert_eq!(q.queued_bytes(), 3);
    assert_eq!(q.partial_bytes(), 0);
    q.push(7, b" ").unwrap();
    assert_eq!(q.finish(7), Err(IngressError::TruncatedFrame));
    assert_eq!(drain(&mut q), [b"{}\n".to_vec()]);
}

#[test]
fn terminal_status_is_monotonic_and_stale_calls_are_inert_in_every_state() {
    for terminal in ["open", "eof", "fault"] {
        let mut q = inbound(limits());
        q.push(7, b"{}\n").unwrap();
        match terminal {
            "eof" => {
                q.finish(7).unwrap();
            }
            "fault" => {
                q.push(7, b"x").unwrap();
                assert_eq!(q.finish(7), Err(IngressError::TruncatedFrame));
            }
            _ => {}
        }
        let before = q.status().clone();
        assert_eq!(q.push(6, b"stale\n"), Ok(Push::StaleGeneration));
        assert_eq!(q.finish(6), Ok(Push::StaleGeneration));
        assert_eq!(q.status(), &before);
        assert_eq!(drain(&mut q), [b"{}\n".to_vec()]);
        if terminal == "eof" {
            assert_eq!(q.push(7, b""), Err(IngressError::Closed));
            assert_eq!(q.finish(7), Err(IngressError::Closed));
            assert_eq!(q.status(), &IngressStatus::Eof);
        }
    }
}

#[test]
fn frame_fault_precedes_queue_fault_and_count_precedes_byte_fault() {
    let mut q = inbound(IngressLimits {
        frame_bytes: 2,
        queued_frames: 1,
        queued_bytes: 3,
    });
    assert_eq!(
        q.push(7, b"{}\n123\n"),
        Err(IngressError::FrameTooLarge { limit: 2 })
    );
    let mut q = inbound(IngressLimits {
        frame_bytes: 2,
        queued_frames: 1,
        queued_bytes: 3,
    });
    assert_eq!(
        q.push(7, b"{}\n{}\n"),
        Err(IngressError::FrameQueueFull { limit: 1 })
    );
}

#[test]
fn raw_queue_does_not_reencode_or_pretend_to_validate_json_or_utf8() {
    let input = b"\xff\nnot json\n\xc2\xa0\n";
    let mut q = inbound(limits());
    q.push(7, input).unwrap();
    let frames = drain(&mut q);
    assert_eq!(frames.concat(), input);
    let mut decoder = JsonlDecoder::new(4096).unwrap();
    assert!(
        decoder
            .push(&frames[0], |_| panic!("bad UTF8 emitted"))
            .is_err()
    );
    assert_eq!(q.status(), &IngressStatus::Open);
}

const A: &str = "b4d5db8b-9827-42de-983e-1b7ac183ba49";
const T: &str = "22222222-2222-4222-8222-222222222222";
fn envelope(event: Value) -> Value {
    json!({"type":"gui.event", "execution_id":A, "session_id":"session",
        "operation_id":null,"hook_id":null,"source":null,"event":event})
}
fn stack() -> (WireClient, Projection, Value) {
    let mut client = WireClient::new(
        7,
        4,
        [
            "turn_start",
            "text_delta",
            "generic_tool_requested",
            "turn_end",
        ]
        .map(|kind| (kind.into(), Delivery::Mirrored)),
    )
    .unwrap();
    let hello = client.begin_hello().unwrap();
    let reply = json!({"type":"response","id":hello["id"],"ok":true,
        "result":{"protocol_version":1,"data_version":1,"capabilities":[],"operations":[]}});
    let projection = Projection::new(
        7,
        ProjectionLimits {
            rows: 4,
            tool_calls: 4,
            text_bytes: 4096,
            metadata_bytes: 4096,
        },
    )
    .unwrap();
    (client, projection, reply)
}
fn serialized(frames: &[Value]) -> Vec<u8> {
    frames
        .iter()
        .flat_map(|frame| format!("{frame}\n").into_bytes())
        .collect()
}
fn prefix(hello: Value) -> Vec<Value> {
    vec![
        hello,
        envelope(json!({"type":"turn_start","model":null,"thinking_level":null})),
        envelope(json!({"type":"generic_tool_requested","observation_id":T,
            "round":0,"slot":0,"provider_call_id":"call_0","name":"fixture"})),
        envelope(json!({"type":"text_delta","text":"accepted prefix"})),
    ]
}
fn terminal_event() -> Value {
    envelope(json!({"type":"turn_end","stopReason":"stop","tps":null,"tpsSource":null}))
}
fn consume_then_disconnect(q: &mut Inbound, client: &mut WireClient, p: &mut Projection) {
    let mut decoder = JsonlDecoder::new(limits().frame_bytes).unwrap();
    while let Some(frame) = q.pop_frame() {
        decoder
            .push(&frame, |value| {
                let notice = client.ingest(q.generation(), value);
                assert!(!matches!(notice, Notice::StaleGeneration));
                p.apply(q.generation(), &notice).unwrap();
            })
            .unwrap();
    }
    decoder.finish().unwrap();
    assert_ne!(q.status(), &IngressStatus::Open);
    // Ingress termination is explicit even though drained complete frames leave
    // the decoder at a valid boundary. It is not an operation completion ACK.
    client.disconnect();
    p.disconnect();
}

#[test]
fn overflow_and_truncated_eof_deliver_prefix_before_marking_stream_incomplete() {
    for truncated in [false, true] {
        let (mut client, mut p, hello) = stack();
        let mut frames = prefix(hello);
        let mut q = inbound(IngressLimits {
            queued_frames: 4,
            ..limits()
        });
        if truncated {
            let mut bytes = serialized(&frames);
            bytes.extend_from_slice(b"{\"type\":");
            q.push(7, &bytes).unwrap();
            assert_eq!(q.finish(7), Err(IngressError::TruncatedFrame));
        } else {
            frames.push(terminal_event());
            assert_eq!(
                q.push(7, &serialized(&frames)),
                Err(IngressError::FrameQueueFull { limit: 4 })
            );
        }
        consume_then_disconnect(&mut q, &mut client, &mut p);
        assert_eq!(p.rows()[0].answer, "accepted prefix");
        assert_eq!(p.rows()[0].state, StreamState::Incomplete);
        assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
        assert!(!client.ready_for_reads());
    }
}

#[test]
fn accepted_terminal_prefix_is_retained_even_if_later_ingress_faults() {
    let (mut client, mut p, hello) = stack();
    let mut frames = prefix(hello);
    frames.push(terminal_event());
    let mut q = inbound(IngressLimits {
        queued_frames: 5,
        ..limits()
    });
    let mut bytes = serialized(&frames);
    bytes.extend_from_slice(b"{}\n");
    assert_eq!(
        q.push(7, &bytes),
        Err(IngressError::FrameQueueFull { limit: 5 })
    );
    consume_then_disconnect(&mut q, &mut client, &mut p);
    assert!(matches!(p.rows()[0].state, StreamState::Ended(_)));
    assert_eq!(p.rows()[0].tools[0].state, ToolObservationState::Incomplete);
    assert!(p.fault().is_some());
}

#[test]
fn clean_eof_is_not_a_successful_operation_terminal() {
    let (mut client, mut p, hello) = stack();
    let mut q = inbound(limits());
    q.push(7, &serialized(&prefix(hello))).unwrap();
    q.finish(7).unwrap();
    consume_then_disconnect(&mut q, &mut client, &mut p);
    assert_eq!(p.rows()[0].state, StreamState::Incomplete);
    assert_eq!(p.rows()[0].answer, "accepted prefix");
}
