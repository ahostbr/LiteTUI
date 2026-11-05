use litetui_terminal::{
    compaction_settings as edit,
    management::{Management, Step},
    settings_editor as budget, settings_view as view,
};
use serde_json::{Value, json};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
fn data() -> Value {
    serde_json::from_str(include_str!("fixtures/compaction_settings_snapshot.json")).unwrap()
}
fn snapshot() -> edit::Snapshot {
    edit::Snapshot::parse(&data(), SESSION, AGENT, "custom", "test").unwrap()
}
fn state(s: &Value) -> Value {
    let mut settings = serde_json::Map::new();
    let mut metadata = Vec::new();
    for f in edit::FIELDS {
        settings.insert(f.into(), s["fields"][f]["effective"].clone());
        metadata.push(json!({"name":f,"label":f,"type":s["fields"][f]["type"],"source":"settings","read_only":false,"secret":false,"apply":s["fields"][f]["apply_timing"]}));
    }
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":"custom","model":"test"},"ownership":{"data_version":1,"writable":true,"agent_id":AGENT},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"checked":"in driver"},"settings":{"effective":settings,"requested":settings,"deferred":{},"metadata":metadata}})
}
fn reply(m: &mut Management, c: &Value, result: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":result}))
}
fn write(step: Step) -> Value {
    let Step::Write(c) = step else {
        panic!("{step:?}")
    };
    c
}
fn connected() -> Management {
    connected_ops(true)
}
fn connected_ops(available: bool) -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"custom","model":"test","tool_profile":"strict"})).unwrap();
    let mut ops = vec![
        "gui.hello",
        "gui.state",
        budget::READ,
        budget::VALIDATE,
        budget::SAVE,
    ];
    if available {
        ops.extend([edit::READ, edit::VALIDATE, edit::SAVE]);
    }
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":ops})).unwrap());
    reply(&mut m, &c, state(&data())).unwrap();
    m
}
fn request(m: &mut Management, i: edit::Intent) -> Value {
    m.request_settings(view::Intent::Compaction(i)).unwrap()
}
fn review_data(s: &edit::Snapshot, field: &str, value: &Value) -> Value {
    let f = s.field(field).unwrap();
    let mut v = s.request(field, value).unwrap();
    v["kind"] = json!("compaction_settings_review");
    v["scope"] = json!(f.scope);
    v["apply_timing"] = json!(f.apply_timing);
    v["destination"] = s.destinations[&f.destination].clone();
    v["saved_before"] = f.saved.clone();
    v["effective_before"] = f.effective.clone();
    v["review"] = json!("c".repeat(64));
    v["action_id"] = json!("32345678-1234-4123-8123-123456789abc");
    v["resolved_value"] = if field == "compact_thinking_level" {
        value.clone()
    } else {
        Value::Null
    };
    v
}
fn prepare(m: &mut Management, field: &str, value: Value) -> (Value, edit::Review) {
    let c = request(m, edit::Intent::Read { generation: 1 });
    assert_eq!(c["agent_id"], AGENT);
    assert_eq!(c["model"], "test");
    assert_eq!(c["backend"], "custom");
    reply(m, &c, data()).unwrap();
    let c = request(
        m,
        edit::Intent::Validate {
            generation: 2,
            snapshot: "a".repeat(64),
            field: field.into(),
            value: value.clone(),
        },
    );
    let v = review_data(&snapshot(), field, &value);
    let r = edit::Review::parse(&v, &snapshot(), field, &value).unwrap();
    reply(m, &c, v).unwrap();
    (
        request(
            m,
            edit::Intent::Save {
                generation: 3,
                review: Box::new(r.clone()),
            },
        ),
        r,
    )
}
fn receipt(r: &edit::Review, partial: bool) -> Value {
    let f = r.snapshot.field(&r.field).unwrap();
    let effective = if partial {
        f.effective.clone()
    } else {
        r.value.clone()
    };
    let mut revisions = r.snapshot.revisions.clone();
    revisions[&f.destination] = json!("d".repeat(64));
    json!({"kind":"compaction_settings_save","version":1,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":r.snapshot.token,"review":r.token,"action_id":r.action_id,"field":r.field,"value":r.value,"scope":f.scope,"apply_timing":f.apply_timing,"destination":r.destination,"status":if partial{"partial"}else{"saved"},"mutated":true,"close_required":false,"error":null,"persistence":[{"destination":r.destination,"scope":f.scope,"saved":true,"error":null,"revision":"d".repeat(64),"fields":[r.field]}],"runtime":[{"field":r.field,"scope":f.scope,"status":if partial{"failed"}else{"applied"},"action":if partial{"retry"}else{"none"},"requested":r.value,"effective":effective,"reason":if partial{json!("Controlled apply failure")}else{Value::Null}}],"saved":r.value,"effective":effective,"revisions":revisions})
}
fn after(r: &edit::Review, partial: bool) -> Value {
    let mut s = data();
    s["snapshot"] = json!("e".repeat(64));
    s["revisions"] = receipt(r, partial)["revisions"].clone();
    s["fields"][&r.field]["saved"] = r.value.clone();
    if !partial {
        s["fields"][&r.field]["effective"] = r.value.clone();
    }
    if r.field == "compact_thinking_level" {
        s["reasoning"]["saved"] = json!(r.resolved_value);
        if !partial {
            s["reasoning"]["effective"] = json!(r.resolved_value);
        }
    }
    s
}
#[test]
fn each_scope_and_type_waits_for_independent_snapshot_and_runtime_readback() {
    let values = [
        json!(0),
        json!(768),
        json!(2),
        json!("high"),
        json!(true),
        json!(false),
        json!(false),
        json!(60),
    ];
    for (field, value) in edit::FIELDS.into_iter().zip(values) {
        for partial in [false, true] {
            let mut m = connected();
            let (c, r) = prepare(&mut m, field, value.clone());
            assert_eq!(c["type"], edit::SAVE);
            assert!(!m.ready());
            let c = write(reply(&mut m, &c, receipt(&r, partial)).unwrap());
            assert_eq!(c["type"], edit::READ);
            assert_eq!(c["agent_id"], AGENT);
            assert!(!m.ready());
            let s = after(&r, partial);
            let c = write(reply(&mut m, &c, s.clone()).unwrap());
            assert_eq!(c["type"], "gui.state");
            assert_eq!(c["include_conversations"], false);
            assert!(!m.ready());
            let Step::Settings {
                update:
                    view::Update::CompactionEdited {
                        update:
                            edit::Update::Saved {
                                generation,
                                outcome,
                                ..
                            },
                        inspection,
                    },
                policy: Some(_),
            } = reply(&mut m, &c, state(&s)).unwrap()
            else {
                panic!("Missing independent save result")
            };
            assert_eq!(generation, 3);
            assert_eq!(outcome.status, if partial { "partial" } else { "saved" });
            assert_eq!(inspection.rows.len(), edit::FIELDS.len());
            assert!(m.ready());
            assert!(
                m.request_settings(view::Intent::Compaction(edit::Intent::Save {
                    generation: 4,
                    review: Box::new(r)
                }))
                .is_err()
            );
        }
    }
}
#[test]
fn stale_generation_or_superseded_review_cannot_send_a_save() {
    let mut m = connected();
    let (c, r) = prepare(&mut m, "wake_after_compact", json!(true));
    let mut v = receipt(&r, false);
    v["status"] = json!("refused");
    v["mutated"] = json!(false);
    v["error"] = json!("stale review");
    v["persistence"] = json!([]);
    v["runtime"] = json!([]);
    for key in ["saved", "effective", "revisions"] {
        v[key] = Value::Null;
    }
    assert!(matches!(
        reply(&mut m, &c, v).unwrap(),
        Step::Settings {
            update: view::Update::Compaction(edit::Update::Rejected { generation: 3, .. }),
            ..
        }
    ));
    assert!(m.ready());
    assert!(
        m.request_settings(view::Intent::Compaction(edit::Intent::Read {
            generation: 3
        }))
        .is_err()
    );
    let c = request(&mut m, edit::Intent::Read { generation: 4 });
    reply(&mut m, &c, data()).unwrap();
    assert!(
        m.request_settings(view::Intent::Compaction(edit::Intent::Save {
            generation: 5,
            review: Box::new(r)
        }))
        .is_err()
    );
    assert!(m.ready());
}
#[test]
fn exact_full_review_and_snapshot_are_required_before_wire_registration() {
    let mut m = connected();
    let c = request(&mut m, edit::Intent::Read { generation: 1 });
    reply(&mut m, &c, data()).unwrap();
    assert!(
        m.request_settings(view::Intent::Compaction(edit::Intent::Validate {
            generation: 2,
            snapshot: "b".repeat(64),
            field: "wake_after_compact".into(),
            value: json!(true)
        }))
        .is_err()
    );
    assert!(m.ready());
    let c = request(
        &mut m,
        edit::Intent::Validate {
            generation: 3,
            snapshot: "a".repeat(64),
            field: "wake_after_compact".into(),
            value: json!(true),
        },
    );
    let v = review_data(&snapshot(), "wake_after_compact", &json!(true));
    let mut r = edit::Review::parse(&v, &snapshot(), "wake_after_compact", &json!(true)).unwrap();
    reply(&mut m, &c, v).unwrap();
    r.value = json!(false);
    assert!(
        m.request_settings(view::Intent::Compaction(edit::Intent::Save {
            generation: 4,
            review: Box::new(r)
        }))
        .is_err()
    );
    assert!(m.ready());
}
#[test]
fn missing_negotiated_operation_refuses_without_poisoning_ordinary_settings() {
    let mut m = connected_ops(false);
    assert!(
        m.request_settings(view::Intent::Compaction(edit::Intent::Read {
            generation: 1
        }))
        .is_err()
    );
    assert!(m.ready());
    assert_eq!(
        m.request_settings(view::Intent::Read).unwrap()["type"],
        "gui.state"
    );
}
#[test]
fn full_response_budget_includes_padding_and_errors_but_not_general_state() {
    for ok in [true, false] {
        let mut m = connected();
        let c = request(&mut m, edit::Intent::Read { generation: 1 });
        let frame = if ok {
            json!({"type":"response","id":c["id"],"ok":true,"result":data()})
        } else {
            json!({"type":"error","id":c["id"],"ok":false,"error":"refused"})
        };
        assert!(m.accept_wire(frame, edit::MAX_BYTES + 1).is_err());
        assert!(!m.ready());
    }
    let mut m = connected();
    let (c, r) = prepare(&mut m, "clear_screen_after_compact", json!(false));
    let c = write(reply(&mut m, &c, receipt(&r, false)).unwrap());
    let s = after(&r, false);
    let c = write(reply(&mut m, &c, s.clone()).unwrap());
    let frame = json!({"type":"response","id":c["id"],"ok":true,"result":state(&s)});
    assert!(m.accept_wire(frame, edit::MAX_BYTES + 1).is_ok());
    assert!(m.ready());
}
#[test]
fn changed_identity_scope_destinations_or_effective_state_fail_closed() {
    for fault in [
        "peer",
        "agent-revision",
        "destination",
        "saved",
        "model",
        "held",
        "busy",
        "effective",
    ] {
        let mut m = connected();
        let (c, r) = prepare(&mut m, "clear_screen_after_compact", json!(false));
        let c = write(reply(&mut m, &c, receipt(&r, false)).unwrap());
        let mut s = after(&r, false);
        match fault {
            "peer" => s["fields"]["wake_after_compact"]["effective"] = json!(true),
            "agent-revision" => s["revisions"]["agent"] = json!("f".repeat(64)),
            "destination" => s["destinations"]["global"] = json!("C:/other/settings.json"),
            "saved" => s["fields"]["clear_screen_after_compact"]["saved"] = json!(true),
            _ => {}
        }
        let result = reply(&mut m, &c, s.clone());
        if ["peer", "agent-revision", "destination", "saved"].contains(&fault) {
            assert!(result.is_err(), "{fault}");
            continue;
        }
        let c = write(result.unwrap());
        let mut st = state(&s);
        match fault {
            "model" => st["model"]["model"] = json!("other"),
            "held" => st["restored_input"]["held"] = json!(1),
            "busy" => st["busy"] = json!(true),
            "effective" => st["settings"]["effective"]["clear_screen_after_compact"] = json!(true),
            _ => unreachable!(),
        };
        assert!(reply(&mut m, &c, st).is_err(), "{fault}");
        assert!(!m.ready());
    }
}
#[test]
fn shutdown_consumes_save_without_followup_or_success_publication() {
    for phase in ["save", "readback", "state"] {
        let mut m = connected();
        let (mut c, r) = prepare(&mut m, "wake_after_compact", json!(true));
        let s = after(&r, false);
        let response = match phase {
            "save" => receipt(&r, false),
            "readback" => {
                c = write(reply(&mut m, &c, receipt(&r, false)).unwrap());
                s
            }
            "state" => {
                c = write(reply(&mut m, &c, receipt(&r, false)).unwrap());
                c = write(reply(&mut m, &c, s.clone()).unwrap());
                state(&s)
            }
            _ => unreachable!(),
        };
        m.begin_close();
        assert!(
            matches!(
                reply(&mut m, &c, response).unwrap(),
                Step::Settings {
                    update: view::Update::Compaction(edit::Update::Rejected { generation: 3, .. }),
                    policy: None,
                }
            ),
            "{phase}"
        );
        assert!(!m.ready());
    }
}
#[test]
fn unsafe_save_receipt_closes_even_during_shutdown() {
    let mut m = connected();
    let (c, r) = prepare(&mut m, "wake_after_compact", json!(true));
    m.begin_close();
    let mut v = receipt(&r, false);
    v["close_required"] = json!(true);
    v["status"] = json!("failed");
    assert!(reply(&mut m, &c, v).is_err());
}

#[test]
fn shutdown_consumes_inspection_and_review_without_publishing_them() {
    for phase in ["read", "validate"] {
        let mut m = connected();
        let mut c = request(&mut m, edit::Intent::Read { generation: 1 });
        let (response, expected_generation) = if phase == "read" {
            (data(), 1)
        } else {
            reply(&mut m, &c, data()).unwrap();
            c = request(
                &mut m,
                edit::Intent::Validate {
                    generation: 2,
                    snapshot: snapshot().token,
                    field: "wake_after_compact".into(),
                    value: json!(true),
                },
            );
            (
                review_data(&snapshot(), "wake_after_compact", &json!(true)),
                2,
            )
        };
        m.begin_close();
        let Step::Settings {
            update: view::Update::Compaction(edit::Update::Rejected { generation, .. }),
            policy: None,
        } = reply(&mut m, &c, response).unwrap()
        else {
            panic!("{phase} published during shutdown")
        };
        assert_eq!(generation, expected_generation);
        assert!(!m.ready());
    }
}
#[test]
fn shutdown_still_checks_final_runtime_readback() {
    let mut m = connected();
    let (c, r) = prepare(&mut m, "wake_after_compact", json!(true));
    let c = write(reply(&mut m, &c, receipt(&r, false)).unwrap());
    let s = after(&r, false);
    let c = write(reply(&mut m, &c, s.clone()).unwrap());
    let mut response = state(&s);
    response["settings"]["effective"]["wake_after_compact"] = json!(false);
    m.begin_close();
    assert!(reply(&mut m, &c, response).is_err());
}
