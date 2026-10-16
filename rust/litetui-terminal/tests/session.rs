//! Offline owner integration. No pipe, subprocess, command write or authority.
use litetui_terminal::{
    protocol::{ClientError, ClientFault, Delivery, Notice},
    session::{BuildError, Session, Termination, Update},
    stream::{Limits, ProjectionFault, StreamState, ToolObservationState},
    transport::{Inbound, IngressError, IngressLimits, IngressStatus, Push},
};
use serde_json::{Value, json};

const A: &str = "b4d5db8b-9827-42de-983e-1b7ac183ba49";
const T: &str = "22222222-2222-4222-8222-222222222222";
fn ingress_limits() -> IngressLimits {
    IngressLimits {
        frame_bytes: 4096,
        queued_frames: 16,
        queued_bytes: 16384,
    }
}
fn view_limits() -> Limits {
    Limits {
        rows: 4,
        tool_calls: 4,
        text_bytes: 4096,
        metadata_bytes: 4096,
    }
}
fn policy() -> impl IntoIterator<Item = (String, Delivery)> {
    [
        "turn_start",
        "text_delta",
        "reasoning_delta",
        "turn_end",
        "generic_tool_requested",
        "generic_tool_returned",
    ]
    .map(|kind| (kind.into(), Delivery::Mirrored))
}
fn session(limits: IngressLimits) -> Session {
    Session::new(7, limits, 4, view_limits(), policy()).unwrap()
}
fn bytes(frames: &[Value]) -> Vec<u8> {
    frames
        .iter()
        .flat_map(|frame| format!("{frame}\n").into_bytes())
        .collect()
}
fn send(s: &mut Session, frames: &[Value]) {
    s.push(7, &bytes(frames)).unwrap();
}
fn hello(s: &mut Session) -> Value {
    let command = s.begin_hello().unwrap();
    json!({"type":"response", "id":command["id"], "ok":true,
        "result":{"protocol_version":1,"data_version":1,"capabilities":[],"operations":[]}})
}
fn negotiated(limits: IngressLimits) -> Session {
    let mut s = session(limits);
    let reply = hello(&mut s);
    send(&mut s, &[reply]);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Hello(_),
            projection_fault: None
        }
    ));
    assert!(s.ready_for_reads());
    s
}
fn event(event: Value) -> Value {
    json!({"type":"gui.event", "session_id":"session", "execution_id":A,
        "operation_id":null,"hook_id":null,"source":null,"event":event})
}
fn start() -> Value {
    event(json!({"type":"turn_start","model":null,"thinking_level":null}))
}
fn delta(text: &str) -> Value {
    event(json!({"type":"text_delta","text":text}))
}
fn end() -> Value {
    event(json!({"type":"turn_end","stopReason":"stop","tps":null,"tpsSource":null}))
}
fn tool() -> Value {
    event(json!({"type":"generic_tool_requested","observation_id":T,
    "round":0,"slot":0,"provider_call_id":"call_0","name":"fixture"}))
}
fn activate(s: &mut Session) {
    for frame in [start(), tool()] {
        send(s, &[frame]);
        assert!(matches!(
            s.poll_one(),
            Update::Notice {
                projection_fault: None,
                ..
            }
        ));
    }
}
fn response(id: Value) -> Value {
    json!({"type":"response","id":id,"ok":true,"result":{}})
}

#[test]
fn construction_and_registration_failures_are_not_session_termination() {
    assert!(matches!(
        Session::new(
            7,
            IngressLimits {
                frame_bytes: 0,
                ..ingress_limits()
            },
            4,
            view_limits(),
            policy()
        ),
        Err(BuildError::Ingress(IngressError::InvalidLimit))
    ));
    assert!(matches!(
        Session::new(7, ingress_limits(), 0, view_limits(), policy()),
        Err(BuildError::Wire(ClientError::InvalidLimit))
    ));
    assert!(matches!(
        Session::new(
            7,
            ingress_limits(),
            4,
            Limits {
                tool_calls: 0,
                ..view_limits()
            },
            policy()
        ),
        Err(BuildError::Projection(ProjectionFault::InvalidLimit))
    ));
    let mut s = session(ingress_limits());
    assert_eq!(s.generation(), 7);
    assert_eq!(s.register_read(), Err(ClientError::NotReady));
    let reply = hello(&mut s);
    assert_eq!(s.begin_hello(), Err(ClientError::HelloAlreadyStarted));
    send(&mut s, &[reply]);
    s.poll_one();
    for _ in 0..4 {
        s.register_read().unwrap();
    }
    assert_eq!(s.register_read(), Err(ClientError::PendingLimit));
    assert!(s.termination().is_none());
    assert_eq!(s.poll_one(), Update::Idle);
}

#[test]
fn fragmented_hello_and_fifo_notices_have_one_owner_and_no_implicit_write() {
    let mut s = session(ingress_limits());
    let wire = bytes(&[hello(&mut s)]);
    for byte in &wire[..wire.len() - 1] {
        s.push(7, &[*byte]).unwrap();
        assert_eq!(s.poll_one(), Update::Idle);
        assert!(!s.ready_for_reads());
    }
    s.push(7, b"\n").unwrap();
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Hello(_),
            projection_fault: None
        }
    ));
    let id = s.register_read().unwrap();
    let wrapped = start();
    send(
        &mut s,
        &[wrapped["event"].clone(), wrapped, response(id.to_value())],
    );
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::IgnoredMirror(_),
            projection_fault: None
        }
    ));
    assert!(s.projection().rows().is_empty());
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Event { .. },
            projection_fault: None
        }
    ));
    assert_eq!(s.projection().rows().len(), 1);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Response { matched: true, .. },
            projection_fault: None
        }
    ));
    assert_eq!(s.pending_count(), 0);
    assert_eq!(s.poll_one(), Update::Idle);
}

#[test]
fn terminal_ingress_blocks_registration_before_accepted_responses_drain() {
    for overflow in [false, true] {
        let mut s = negotiated(IngressLimits {
            queued_frames: 1,
            ..ingress_limits()
        });
        activate(&mut s);
        let id = s.register_read().unwrap();
        s.register_read().unwrap();
        send(&mut s, &[response(id.to_value())]);
        let reason = if overflow {
            let error = IngressError::FrameQueueFull { limit: 1 };
            assert_eq!(s.push(7, b"{}\n"), Err(error.clone()));
            Termination::Ingress(error)
        } else {
            s.finish(7).unwrap();
            Termination::Eof
        };
        assert!(!s.ready_for_reads());
        assert_eq!(s.register_read(), Err(ClientError::NotReady));
        assert_eq!(s.begin_hello(), Err(ClientError::NotReady));
        assert!(s.termination().is_none());
        assert!(matches!(
            s.poll_one(),
            Update::Notice {
                notice: Notice::Response { matched: true, .. },
                projection_fault: None
            }
        ));
        assert_eq!(s.pending_count(), 1);
        assert_eq!(s.projection().rows()[0].state, StreamState::Streaming);
        assert_eq!(s.poll_one(), Update::Terminated(reason.clone()));
        assert_eq!(s.termination(), Some(&reason));
        assert_eq!(s.projection().rows()[0].state, StreamState::Incomplete);
        assert_eq!(
            s.projection().rows()[0].tools[0].state,
            ToolObservationState::Incomplete
        );
        assert_eq!(s.pending_count(), 1);
        assert_eq!(s.poll_one(), Update::Closed);
    }
}

#[test]
fn wire_fault_blocks_new_requests_but_continues_pending_response_drain() {
    let mut s = negotiated(ingress_limits());
    activate(&mut s);
    let id = s.register_read().unwrap();
    send(&mut s, &[json!({"type":"unknown"})]);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Fault { .. },
            projection_fault: Some(_)
        }
    ));
    assert_eq!(
        s.wire_fault(),
        Some(&ClientFault::UnknownEvent("unknown".into()))
    );
    assert_eq!(s.register_read(), Err(ClientError::NotReady));
    assert!(s.termination().is_none());
    send(&mut s, &[response(id.to_value())]);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Response { matched: true, .. },
            projection_fault: Some(_)
        }
    ));
    assert_eq!(s.pending_count(), 0);
    s.finish(7).unwrap();
    assert_eq!(s.poll_one(), Update::Terminated(Termination::Eof));
    assert_eq!(
        s.wire_fault(),
        Some(&ClientFault::UnknownEvent("unknown".into()))
    );
}

#[test]
fn projection_only_fault_also_blocks_registration_without_losing_correlated_responses() {
    let mut s = negotiated(ingress_limits());
    activate(&mut s);
    let id = s.register_read().unwrap();
    send(&mut s, &[event(json!({"type":"text_delta","text":false}))]);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Event { .. },
            projection_fault: Some(ProjectionFault::Malformed(_))
        }
    ));
    assert_eq!(s.wire_fault(), None);
    assert!(!s.ready_for_reads());
    assert_eq!(s.register_read(), Err(ClientError::NotReady));
    send(&mut s, &[response(id.to_value())]);
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            notice: Notice::Response { matched: true, .. },
            projection_fault: Some(_)
        }
    ));
    assert_eq!(s.pending_count(), 0);
    assert!(s.termination().is_none());
}

#[test]
fn decode_failure_wins_delivery_termination_but_keeps_ingress_evidence() {
    for prior in ["open", "eof", "overflow"] {
        for bad in [b"not JSON\n".as_slice(), b"\xff\n".as_slice()] {
            let mut s = negotiated(IngressLimits {
                queued_frames: 3,
                ..ingress_limits()
            });
            activate(&mut s);
            let id = s.register_read().unwrap();
            let mut wire = bytes(&[delta("prefix")]);
            wire.extend_from_slice(bad);
            wire.extend(bytes(&[response(id.to_value())]));
            send_bytes_with_prior(&mut s, wire, prior);
            assert!(matches!(
                s.poll_one(),
                Update::Notice {
                    projection_fault: None,
                    ..
                }
            ));
            assert_eq!(s.projection().rows()[0].answer, "prefix");
            assert!(matches!(
                s.poll_one(),
                Update::Terminated(Termination::Decode(_))
            ));
            let reason = s.termination().cloned();
            assert_eq!(s.queued_frames(), 0);
            assert_eq!(s.queued_bytes(), 0);
            assert_eq!(s.partial_bytes(), 0);
            assert_eq!(s.pending_count(), 1); // suffix response never correlated
            assert_eq!(s.projection().rows()[0].state, StreamState::Incomplete);
            assert_eq!(
                s.projection().rows()[0].tools[0].state,
                ToolObservationState::Incomplete
            );
            assert_eq!(
                s.ingress_status(),
                &match prior {
                    "eof" => IngressStatus::Eof,
                    "overflow" => IngressStatus::Fault(IngressError::FrameQueueFull { limit: 3 }),
                    _ => IngressStatus::Fault(IngressError::Aborted),
                }
            );
            s.abort(7);
            assert_eq!(s.termination(), reason.as_ref());
            assert_eq!(s.poll_one(), Update::Closed);
            assert_eq!(s.push(7, b"{}\n"), Err(IngressError::Closed));
            assert_eq!(s.push(8, b"anything\n"), Ok(Push::StaleGeneration));
            assert_eq!(s.finish(8), Ok(Push::StaleGeneration));
            assert_eq!(s.abort(8), Push::StaleGeneration);
            assert_eq!(s.termination(), reason.as_ref());
        }
    }
}
fn send_bytes_with_prior(s: &mut Session, mut wire: Vec<u8>, prior: &str) {
    if prior == "overflow" {
        wire.extend_from_slice(b"{}\n");
        assert_eq!(
            s.push(7, &wire),
            Err(IngressError::FrameQueueFull { limit: 3 })
        );
    } else {
        if prior == "open" {
            wire.extend_from_slice(b"{");
        }
        s.push(7, &wire).unwrap();
        if prior == "eof" {
            s.finish(7).unwrap();
        }
    }
}

#[test]
fn local_abort_discards_queued_prefix_and_partial_without_completing_pending_work() {
    let mut s = negotiated(ingress_limits());
    activate(&mut s);
    let id = s.register_read().unwrap();
    send(&mut s, &[response(id.to_value()), delta("discarded")]);
    s.push(7, b"{").unwrap();
    assert_eq!(s.abort(8), Push::StaleGeneration);
    assert_eq!(s.partial_bytes(), 1);
    assert_eq!(s.abort(7), Push::Accepted);
    assert!(!s.ready_for_reads());
    assert_eq!(s.queued_frames(), 0);
    assert_eq!(s.partial_bytes(), 0);
    assert_eq!(s.pending_count(), 1);
    assert!(s.projection().rows()[0].answer.is_empty());
    assert_eq!(
        s.projection().rows()[0].tools[0].state,
        ToolObservationState::Incomplete
    );
    assert_eq!(s.termination(), Some(&Termination::LocalAbort));
    s.abort(7); // finalization already occurred, notification not yet polled
    assert_eq!(s.poll_one(), Update::Terminated(Termination::LocalAbort));
    assert_eq!(s.poll_one(), Update::Closed);
}

#[test]
fn abort_before_draining_terminal_ingress_is_deliberate_abandonment() {
    for overflow in [false, true] {
        let mut s = negotiated(IngressLimits {
            queued_frames: 1,
            ..ingress_limits()
        });
        let id = s.register_read().unwrap();
        send(&mut s, &[response(id.to_value())]);
        if overflow {
            assert!(s.push(7, b"{}\n").is_err());
        } else {
            s.finish(7).unwrap();
        }
        let prior = s.ingress_status().clone();
        s.abort(7);
        assert_eq!(s.ingress_status(), &prior);
        assert_eq!(s.pending_count(), 1);
        assert_eq!(s.poll_one(), Update::Terminated(Termination::LocalAbort));
    }
}

#[test]
fn stale_input_cannot_modify_an_active_partial_or_close_the_owner() {
    let mut s = negotiated(ingress_limits());
    let wire = bytes(&[start()]);
    s.push(7, &wire[..10]).unwrap();
    assert_eq!(s.push(6, b"corruption\n"), Ok(Push::StaleGeneration));
    assert_eq!(s.finish(6), Ok(Push::StaleGeneration));
    assert_eq!(s.abort(6), Push::StaleGeneration);
    assert_eq!(s.partial_bytes(), 10);
    assert_eq!(s.poll_one(), Update::Idle);
    s.push(7, &wire[10..]).unwrap();
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            projection_fault: None,
            ..
        }
    ));
    assert_eq!(s.projection().rows().len(), 1);
}

#[test]
fn accepted_stream_end_is_delivered_before_later_overflow_termination() {
    let mut s = negotiated(IngressLimits {
        queued_frames: 1,
        ..ingress_limits()
    });
    activate(&mut s);
    send(&mut s, &[end()]);
    assert!(s.push(7, b"{}\n").is_err());
    assert!(!s.ready_for_reads());
    assert!(matches!(
        s.poll_one(),
        Update::Notice {
            projection_fault: None,
            ..
        }
    ));
    assert!(matches!(
        s.projection().rows()[0].state,
        StreamState::Ended(_)
    ));
    assert!(matches!(
        s.poll_one(),
        Update::Terminated(Termination::Ingress(_))
    ));
    assert!(matches!(
        s.projection().rows()[0].state,
        StreamState::Ended(_)
    ));
    assert_eq!(
        s.projection().rows()[0].tools[0].state,
        ToolObservationState::Incomplete
    );
}

#[test]
fn inbound_abort_clears_only_partial_and_preserves_fifo_and_original_terminal() {
    for prior in ["open", "eof", "fault"] {
        let mut q = Inbound::new(
            7,
            IngressLimits {
                queued_frames: 1,
                ..ingress_limits()
            },
        )
        .unwrap();
        q.push(7, b"{}\n").unwrap();
        match prior {
            "eof" => {
                q.finish(7).unwrap();
            }
            "fault" => {
                assert!(q.push(7, b"{}\n").is_err());
            }
            _ => {
                q.push(7, b"{").unwrap();
            }
        }
        let before = q.status().clone();
        let partial = q.partial_bytes();
        assert_eq!(q.abort(8), Push::StaleGeneration);
        assert_eq!(q.partial_bytes(), partial);
        assert_eq!(q.abort(7), Push::Accepted);
        assert_eq!(q.partial_bytes(), 0);
        assert_eq!(q.queued_bytes(), 3);
        assert_eq!(q.queued_frames(), 1);
        assert_eq!(
            q.status(),
            &if prior == "open" {
                IngressStatus::Fault(IngressError::Aborted)
            } else {
                before
            }
        );
        assert_eq!(q.pop_frame(), Some(b"{}\n".to_vec()));
        assert_eq!(q.queued_bytes(), 0);
    }
}
