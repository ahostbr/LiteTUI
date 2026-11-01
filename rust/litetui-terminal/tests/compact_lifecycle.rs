//! Manual compaction owns maintenance, never an invented prompt/stream.
use litetui_terminal::{
    conversation_compact as c, history as h, history_view as hv,
    live::{Observation, Turn},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const ROOT: &str = "42345678-1234-4123-8123-123456789abc";
const WAKE: &str = "52345678-1234-4123-8123-123456789abc";
fn sha(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn review() -> c::Review {
    let selection = c::Selection {
        history_key: hv::ReadKey {
            target: hv::Target::Current,
            generation: 1,
        },
        identity: h::Identity {
            from_session: SESSION.into(),
            session: SESSION.into(),
            agent: AGENT.into(),
            backend: "custom".into(),
            model: "test".into(),
            scope: h::Scope::Current,
            catalog: None,
        },
        history_snapshot: "a".repeat(64),
        context_sha256: "b".repeat(64),
        messages: 7,
    };
    let config = json!({"keep_recent":2,"max_tokens":1024,"max_tool_iters":4,"thinking_level":"default","tools_enabled":false,"wake_after_compact":true,"clear_screen_after_compact":false});
    let plan = json!({"messages":7,"system_messages":1,"head_messages":4,"tail_messages":2,"head_context_sha256":"c".repeat(64),"tail_context_sha256":"d".repeat(64),"prompt_sha256":"e".repeat(64),"config_sha256":sha(&serde_json::to_string(&config).unwrap())});
    let mut value = selection.command(c::SNAPSHOT);
    for(k,v)in json!({"kind":"conversation_compact_snapshot","snapshot":"f".repeat(64),"eligible":true,"reason":null,"plan":plan,"config":config}).as_object().unwrap(){value[k]=v.clone();}
    let snapshot = c::Snapshot::parse(&value, &selection).unwrap();
    let mut value = snapshot.request("");
    for(k,v)in json!({"kind":"conversation_compact_review","plan":plan,"config":config,"review":"1".repeat(64),"action_id":ACTION,"action":"compact","root_execution_id":ROOT,"hint":"","hint_sha256":sha(""),"request_prompt_sha256":"e".repeat(64),"effects":{"provider_execution":true,"provider_rewind":false,"tools_enabled":false,"wake_after_compact":true,"clear_screen_after_compact":false}}).as_object().unwrap(){value[k]=v.clone();}
    c::Review::parse(&value, snapshot, "").unwrap()
}
fn ready() -> (Turn, String) {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"custom","model":"test"}),"custom","test").unwrap();
    let op = turn.submit_compact(&review()).unwrap();
    (turn, op)
}
fn event(op: &str, execution: &str, mut value: Value) -> Value {
    value["compact_origin"] =
        json!({"session_id":SESSION,"operation_id":op,"source":"rpc","execution_id":execution});
    value
}
fn root(op: &str, phase: &str) -> Value {
    event(
        op,
        ROOT,
        json!({"type":"compact_maintenance","action_id":ACTION,"aftercare_id":ROOT,"phase":phase,"kind":"compaction","root":true,"after_execution_id":null}),
    )
}
fn wake(op: &str) -> Value {
    event(
        op,
        WAKE,
        json!({"type":"turn_start","model":"test","compact_segment":{"action_id":ACTION,"index":0,"previous_execution_id":null}}),
    )
}
fn ack(op: &str) -> Value {
    let mut value = review().request();
    for(k,v)in json!({"kind":"conversation_compact_apply","operation_id":op,"status":"accepted","close_required":false,"reason":null,"refusal_history":null}).as_object().unwrap(){value[k]=v.clone();}
    json!({"type":"response","id":op,"ok":true,"result":value})
}
fn accept(turn: &mut Turn, value: Value) -> Observation {
    turn.accept(&value, "custom", "test").unwrap()
}
#[test]
fn root_without_stream_or_prompt_is_not_user_turn_completion() {
    for early_ack in [true, false] {
        let (mut t, op) = ready();
        if early_ack {
            assert!(matches!(
                accept(&mut t, ack(&op)),
                Observation::Information(_)
            ));
        }
        assert!(matches!(
            accept(&mut t, root(&op, "start")),
            Observation::RewriteAftercare {
                running: true,
                previous_outcome: None,
                ..
            }
        ));
        assert!(
            t.accept(
                &event(
                    &op,
                    ROOT,
                    json!({"type":"text_delta","text":"not a user reply"})
                ),
                "custom",
                "test"
            )
            .is_err()
        );
        assert!(matches!(
            accept(&mut t, root(&op, "end")),
            Observation::RewriteAftercare { running: false, .. }
        ));
        if !early_ack {
            assert!(matches!(
                accept(&mut t, ack(&op)),
                Observation::Information(_)
            ));
        }
        assert!(t.submit("not before independent readback").is_err());
        assert!(t.accept(&ack(&op), "custom", "test").is_err());
    }
}
#[test]
fn genuine_wake_requires_root_end_and_uses_first_stream_index_zero() {
    let (mut t, op) = ready();
    accept(&mut t, ack(&op));
    assert!(t.accept(&wake(&op), "custom", "test").is_err());
    accept(&mut t, root(&op, "start"));
    assert!(t.accept(&wake(&op), "custom", "test").is_err());
    accept(&mut t, root(&op, "end"));
    assert!(matches!(
        accept(&mut t, wake(&op)),
        Observation::RewriteSegment {
            index: 0,
            previous_outcome: None,
            ..
        }
    ));
    accept(
        &mut t,
        event(
            &op,
            WAKE,
            json!({"type":"text_delta","text":"real wake answer"}),
        ),
    );
    assert!(matches!(
        accept(
            &mut t,
            event(
                &op,
                WAKE,
                json!({"type":"turn_end","stopReason":"stop","error":null})
            )
        ),
        Observation::Complete
    ));
    assert!(t.submit("still awaiting exact saved state").is_err());
}
#[test]
fn early_wake_terminal_before_apply_receipt_settles_after_receipt() {
    let (mut t, op) = ready();
    accept(&mut t, root(&op, "start"));
    accept(&mut t, root(&op, "end"));
    accept(&mut t, wake(&op));
    accept(
        &mut t,
        event(&op, WAKE, json!({"type":"text_delta","text":"answer"})),
    );
    assert!(matches!(
        accept(
            &mut t,
            event(
                &op,
                WAKE,
                json!({"type":"turn_end","stopReason":"stop","error":null})
            )
        ),
        Observation::Information(_)
    ));
    assert!(matches!(accept(&mut t, ack(&op)), Observation::Complete));
}
#[test]
fn stop_before_scheduled_root_allows_only_its_cancellation_boundary() {
    for stopped in [true, false] {
        let (mut t, op) = ready();
        let abort = t.abort(&op).unwrap().unwrap();
        accept(
            &mut t,
            json!({"type":"response","id":abort["id"],"ok":true,"result":{"stopped":stopped,"cancelled_asks":[]}}),
        );
        accept(&mut t, ack(&op));
        accept(&mut t, root(&op, "start"));
        accept(&mut t, root(&op, "end"));
        assert!(t.accept(&wake(&op), "custom", "test").is_err());
        let _second = t.abort(&op).unwrap();
        assert!(t.accept(&wake(&op), "custom", "test").is_err());
    }
}
#[test]
fn root_requires_exact_reviewed_execution_and_null_parent_once() {
    for field in [
        "execution",
        "aftercare",
        "action",
        "parent",
        "role",
        "source",
        "session",
        "operation",
    ] {
        let (mut t, op) = ready();
        let mut value = root(&op, "start");
        match field {
            "execution" => value["compact_origin"]["execution_id"] = json!(WAKE),
            "aftercare" => value["aftercare_id"] = json!(WAKE),
            "action" => value["action_id"] = json!(WAKE),
            "parent" => value["after_execution_id"] = json!(WAKE),
            "role" => value["root"] = json!(false),
            "source" => value["compact_origin"]["source"] = json!("user"),
            "session" => value["compact_origin"]["session_id"] = json!(WAKE),
            "operation" => value["compact_origin"]["operation_id"] = json!("foreign"),
            _ => unreachable!(),
        }
        assert!(t.accept(&value, "custom", "test").is_err(), "{field}");
    }
    let (mut t, op) = ready();
    accept(&mut t, root(&op, "start"));
    assert!(t.accept(&root(&op, "start"), "custom", "test").is_err());
    accept(&mut t, root(&op, "end"));
    assert!(t.accept(&root(&op, "end"), "custom", "test").is_err());
    assert!(t.accept(&root(&op, "start"), "custom", "test").is_err());
}
#[test]
fn root_does_not_accept_edit_retry_event_family() {
    let (mut t, op) = ready();
    let mut value = root(&op, "start");
    value["type"] = json!("rewrite_aftercare");
    assert!(t.accept(&value, "custom", "test").is_err());
}
#[test]
fn close_cannot_admit_root_or_wake() {
    let (mut t, op) = ready();
    t.begin_close();
    assert!(t.accept(&root(&op, "start"), "custom", "test").is_err());
    assert!(t.accept(&wake(&op), "custom", "test").is_err());
    assert!(t.submit("no new work").is_err());
}

#[test]
fn stop_after_wake_terminal_still_latches_the_pending_action() {
    let (mut t, op) = ready();
    accept(&mut t, ack(&op));
    accept(&mut t, root(&op, "start"));
    accept(&mut t, root(&op, "end"));
    accept(&mut t, wake(&op));
    accept(
        &mut t,
        event(
            &op,
            WAKE,
            json!({"type":"text_delta","text":"wake complete"}),
        ),
    );
    accept(
        &mut t,
        event(
            &op,
            WAKE,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    );
    let abort = t
        .abort(&op)
        .unwrap()
        .expect("Stop must remain available while the action waits for owned aftercare/readback");
    accept(
        &mut t,
        json!({"type":"response","id":abort["id"],"ok":true,"result":{"stopped":true,"cancelled_asks":[]}}),
    );
    let next = "62345678-1234-4123-8123-123456789abc";
    let maintenance = event(
        &op,
        next,
        json!({"type":"compact_maintenance","action_id":ACTION,"aftercare_id":next,"phase":"start","kind":"compaction","root":false,"after_execution_id":WAKE}),
    );
    assert!(t.accept(&maintenance, "custom", "test").is_err());
}
