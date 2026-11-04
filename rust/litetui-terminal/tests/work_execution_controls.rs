use litetui_terminal::{
    tool_mode::Profile, work_execution_controls as ex, work_lifecycle as life,
    work_protocol as inspection,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn all_fixtures() -> Vec<Value> {
    serde_json::from_str::<Value>(include_str!(
        "../../../tests/fixtures/work_execution_controls.json"
    ))
    .unwrap()["cases"]
        .as_array()
        .unwrap()
        .clone()
}
fn fixtures() -> Vec<Value> {
    all_fixtures()
        .into_iter()
        .filter(|c| c["result"]["status"] == "published")
        .collect()
}
fn h(raw: &str) -> String {
    format!("{:x}", Sha256::digest(raw.as_bytes()))
}
fn caps() -> ex::Capabilities {
    let mut ops = ex::OPERATIONS
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    ops.extend([life::CAPTURE, life::ACTIVATE, life::SETTLED].map(str::to_owned));
    ex::Capabilities::negotiate(&ops).unwrap().unwrap()
}
fn setup(c: &Value) -> (ex::Edit, ex::Review, life::Capture) {
    let id = life::Identity::parse(&c["capture"]["identity"]).unwrap();
    let capture = life::Capture::parse_execution(&c["capture"], &id, Some(&caps())).unwrap();
    let i = inspection::Identity {
        session: id.session,
        agent: id.agent,
        backend: id.backend,
        model: id.model,
    };
    let snapshot = inspection::Snapshot::parse(&c["snapshot"], &i).unwrap();
    let action = match c["review"]["action"].as_str().unwrap() {
        "job_run" => ex::Action::JobRun,
        "goal_start" => ex::Action::GoalStart,
        "goal_resume" => ex::Action::GoalResume,
        _ => panic!("unknown fixture"),
    };
    let row = if action == ex::Action::JobRun {
        let section = if c["review"]["section"] == "loops" {
            inspection::Section::Loops
        } else {
            inspection::Section::Jobs
        };
        let mut p = c["snapshot"].clone();
        p["kind"] = json!("work_page");
        p["section"] = json!(section.key());
        p["offset"] = json!(0);
        p["total"] = json!(1);
        p["next_offset"] = Value::Null;
        p["rows"] = c["selected"][section.key()].clone();
        Some(
            inspection::Page::parse(&p, &snapshot, section, 0, 32)
                .unwrap()
                .rows
                .remove(0),
        )
    } else {
        None
    };
    let edit = ex::Edit::new(
        &snapshot,
        action,
        row.as_ref(),
        c["validate_command"]["draft"].clone(),
    )
    .unwrap();
    let review =
        ex::Review::parse(&c["review"], &edit, &capture.instance, capture.provider).unwrap();
    (edit, review, capture)
}
fn result(c: &Value, r: &ex::Review) -> ex::Receipt {
    ex::Receipt::parse(&c["result"], r, "apply-execution").unwrap()
}
fn stop(c: &Value, r: &ex::Review) -> ex::StopReceipt {
    ex::StopReceipt::parse(&c["stop_result"], r, "apply-execution", "execution-stop").unwrap()
}
fn rewrite_document(v: &mut Value, f: impl FnOnce(&mut Value)) {
    let mut d: Value = serde_json::from_str(v["document"].as_str().unwrap()).unwrap();
    f(&mut d);
    let raw = serde_json::to_string(&d).unwrap();
    v["document_sha256"] = json!(h(&raw));
    v["document"] = json!(raw);
}
fn rewrite_raw(v: &mut Value, f: impl FnOnce(&mut Value)) {
    let mut d: Value = serde_json::from_str(v["json"].as_str().unwrap()).unwrap();
    f(&mut d);
    let raw = serde_json::to_string(&d).unwrap();
    v["sha256"] = json!(h(&raw));
    v["json"] = json!(raw);
}
fn event(e: &Value) -> Value {
    let mut envelope = if matches!(e["type"].as_str(), Some("work_admission" | "work_settled")) {
        e["admission"]["origin"].clone()
    } else {
        json!({"session_id":e["identity"]["session_id"],"operation_id":null,"hook_id":null,"source":"work","execution_id":null})
    };
    envelope["type"] = json!("gui.event");
    envelope["event_id"] = json!(format!(
        "00000000-0000-4000-8000-{:012x}",
        e["sequence"].as_u64().unwrap()
    ));
    envelope["event"] = e.clone();
    envelope
}
#[test]
fn actual_source_four_actions_exact_review_launch_stop_readback_and_shutdown() {
    let cases = fixtures();
    assert!(cases.len() >= 4);
    for c in cases {
        let (edit, r, capture) = setup(&c);
        let a = result(&c, &r);
        let rb = ex::Readback::parse_launch(&c["readback"], &a).unwrap();
        let t = stop(&c, &r);
        let sb = ex::Readback::parse_stop(&c["stop_readback"], &r, &t).unwrap();
        let e = ex::Shutdown::parse(&c["shutdown"]).unwrap();
        e.verify_invocation(
            &r,
            "apply-execution",
            Some(&a),
            std::slice::from_ref(&t),
            Some("execution-stop"),
        )
        .unwrap();
        assert_eq!(rb.document, sb.document);
        let mut q = edit.validate_request(&capture.instance).unwrap();
        q["id"] = c["validate_command"]["id"].clone();
        assert_eq!(q, c["validate_command"]);
        let mut q = r.request();
        q["id"] = c["apply_command"]["id"].clone();
        assert_eq!(q, c["apply_command"]);
        let mut q = a.readback_request().unwrap();
        q["id"] = c["readback_command"]["id"].clone();
        assert_eq!(q, c["readback_command"]);
        let mut q = t.readback_request().unwrap();
        q["id"] = c["stop_readback_command"]["id"].clone();
        assert_eq!(q, c["stop_readback_command"]);
    }
}
#[test]
fn genuine_work_ledger_links_early_events_and_accepts_later_progress() {
    for c in fixtures() {
        let (_, r, capture) = setup(&c);
        let a = result(&c, &r);
        let d = a.document.as_ref().unwrap();
        let mut ledger = life::Ledger::new(capture, Profile::Strict);
        ledger.activate(&c["activation"]).unwrap();
        assert!(ledger.verify_execution_document(d).is_err());
        for e in c["events"].as_array().unwrap() {
            ledger.observe(&event(e), None).unwrap();
        }
        ledger.verify_execution_document(d).unwrap();
        ledger.verify_execution_lifecycle(d, &a.lifecycle).unwrap();
        let t = stop(&c, &r);
        ledger.verify_execution_lifecycle(d, &t.lifecycle).unwrap();
        let mut wrong = d.clone();
        wrong.input.id = "00000000-0000-4000-8000-000000000123".into();
        assert!(ledger.verify_execution_document(&wrong).is_err());
        let mut wrong = a.lifecycle.clone();
        if let Some(a) = wrong.admission.as_mut() {
            a.id = "00000000-0000-4000-8000-000000000123".into();
        }
        assert!(ledger.verify_execution_lifecycle(d, &wrong).is_err());
    }
}
#[test]
fn capabilities_and_capture_activation_require_exact_independent_opt_in() {
    let base = [life::CAPTURE, life::ACTIVATE, life::SETTLED]
        .map(str::to_owned)
        .to_vec();
    for bits in 0..16 {
        let mut ops = base.clone();
        for (n, op) in ex::OPERATIONS.iter().enumerate() {
            if bits & (1 << n) != 0 {
                ops.push((*op).into());
            }
        }
        let out = ex::Capabilities::negotiate(&ops);
        if bits == 0 {
            assert!(out.unwrap().is_none())
        } else if bits == 15 {
            assert!(out.unwrap().is_some());
            ops.push(ex::STOP.into());
            assert!(ex::Capabilities::negotiate(&ops).is_err())
        } else {
            assert!(out.is_err())
        }
    }
    assert!(ex::Capabilities::negotiate(&ex::OPERATIONS.map(str::to_owned)).is_err());
    let c = &fixtures()[0];
    let (_, _, cap) = setup(c);
    assert!(life::Capture::parse(&c["capture"], &cap.identity).is_err());
    for bad in [json!(null), json!(false), json!(1.0), json!(2)] {
        let mut v = c["capture"].clone();
        v["execution_version"] = bad.clone();
        assert!(life::Capture::parse_execution(&v, &cap.identity, Some(&caps())).is_err());
        let mut a = c["activation"].clone();
        a["execution_version"] = bad;
        assert!(
            life::Ledger::new(cap.clone(), Profile::Strict)
                .activate(&a)
                .is_err()
        );
    }
    let mut missing = c["activation"].clone();
    missing.as_object_mut().unwrap().remove("execution_version");
    assert!(
        life::Ledger::new(cap.clone(), Profile::Strict)
            .activate(&missing)
            .is_err()
    );
    let mut legacy = c["capture"].clone();
    legacy.as_object_mut().unwrap().remove("execution_version");
    legacy.as_object_mut().unwrap().remove("execution_receipt");
    let old = life::Capture::parse(&legacy, &cap.identity).unwrap();
    assert!(
        life::Ledger::new(old, Profile::Strict)
            .activate(&c["activation"])
            .is_err()
    );
}
#[test]
fn review_binds_action_epoch_raw_numbers_payload_and_only_declared_timestamps() {
    for c in fixtures() {
        let (edit, r, cap) = setup(&c);
        for (key, bad) in [
            ("work_instance", json!("0".repeat(64))),
            ("inspection_instance", json!(cap.instance)),
            ("version", json!(1.0)),
            ("execution_version", json!(true)),
            ("producer_generation", json!(1.0)),
            ("target_id", json!("foreign")),
            ("materialize", json!(0)),
            ("unknown", json!(null)),
        ] {
            let mut v = c["review"].clone();
            v[key] = bad;
            assert!(
                ex::Review::parse(&v, &edit, &cap.instance, cap.provider).is_err(),
                "{} {key}",
                c["name"]
            );
        }
        let mut v = r.wire.clone();
        rewrite_raw(&mut v["after_template"], |row| {
            row["tool_profile"] = json!("autonomous")
        });
        assert!(ex::Review::parse(&v, &edit, &cap.instance, cap.provider).is_err());
        let mut v = r.wire.clone();
        let raw = "{\"id\":\"one\",\"id\":\"two\"}";
        v["before"] = json!({"json":raw,"sha256":h(raw)});
        assert!(ex::Review::parse(&v, &edit, &cap.instance, cap.provider).is_err());
        if r.action == ex::Action::GoalResume {
            let mut v = r.wire.clone();
            rewrite_raw(&mut v["after_template"], |row| {
                row["future_extension"]["keep"][2] = json!(0)
            });
            assert!(ex::Review::parse(&v, &edit, &cap.instance, cap.provider).is_err());
        }
    }
}
#[test]
fn launch_receipt_rejects_rehashed_provider_refs_cadence_and_unreviewed_changes() {
    for c in fixtures() {
        let (_, r, _) = setup(&c);
        for fault in [
            "provider",
            "producer",
            "input",
            "payload",
            "clock",
            "unreviewed",
        ] {
            let mut v = c["result"].clone();
            rewrite_document(&mut v, |d| match fault {
                "provider" => d["provider"] = json!("claude"),
                "producer" => d["producer"]["generation"] = json!(99),
                "input" => {
                    d["input"]["producer_id"] = json!("00000000-0000-4000-8000-000000000123")
                }
                "payload" => d["payload"]["text"] = json!("changed"),
                "clock" => rewrite_raw(&mut d["after"], |a| {
                    let k = if r.action == ex::Action::JobRun {
                        "last_fired_slot"
                    } else {
                        "updated"
                    };
                    a[k] = json!("2026-02-30T11:20:00")
                }),
                _ => rewrite_raw(&mut d["after"], |a| a["future_extension"] = json!(true)),
            });
            assert!(
                ex::Receipt::parse(&v, &r, "apply-execution").is_err(),
                "{} {fault}",
                c["name"]
            );
        }
        if r.edit.section == "loops" {
            let mut v = c["result"].clone();
            rewrite_document(&mut v, |d| {
                rewrite_raw(&mut d["after"], |a| {
                    a["next_run_at"] = json!("2026-11-04T10:55:27")
                })
            });
            assert!(ex::Receipt::parse(&v, &r, "apply-execution").is_err());
        }
    }
}
#[test]
fn stop_and_launch_readbacks_cannot_release_each_other_or_forge_settlement() {
    for c in fixtures() {
        let (_, r, _) = setup(&c);
        let a = result(&c, &r);
        let t = stop(&c, &r);
        assert!(ex::Readback::parse_launch(&c["stop_readback"], &a).is_err());
        assert!(ex::Readback::parse_stop(&c["readback"], &r, &t).is_err());
        for key in ["scope", "stop_id", "result_operation_id", "result_sha256"] {
            let mut rb = c["stop_readback"].clone();
            rb[key] = c["readback"][key].clone();
            assert!(ex::Readback::parse_stop(&rb, &r, &t).is_err(), "{key}");
        }
        let mut t = c["stop_result"].clone();
        t["lifecycle"]["settlement"]["proof"]["callbacks_retired"] = json!(false);
        assert!(ex::StopReceipt::parse(&t, &r, "apply-execution", "execution-stop").is_err());
        let mut rb = c["readback"].clone();
        rb["ledger_sha256"] = Value::Null;
        rb["ledger_revision"] = json!(0);
        assert!(ex::Readback::parse_launch(&rb, &a).is_err());
    }
}
#[test]
fn quit_requires_sent_stop_identity_even_before_response_and_exact_retired_owner() {
    let c = &fixtures()[0];
    let (_, r, _) = setup(c);
    let a = result(c, &r);
    let e = ex::Shutdown::parse(&c["shutdown"]).unwrap();
    e.verify_invocation(&r, "apply-execution", None, &[], Some("execution-stop"))
        .unwrap();
    assert!(
        e.verify_invocation(&r, "apply-execution", None, &[], None)
            .is_err()
    );
    assert!(
        e.verify_invocation(&r, "apply-execution", None, &[], Some("other-stop"))
            .is_err()
    );
    for (key, bad) in [
        ("action_id", Value::Null),
        ("effects_verified", json!(true)),
        ("worker_settled", json!(false)),
        ("stop_ids", json!([])),
        (
            "stop_readbacks_published",
            json!(["00000000-0000-4000-8000-000000000123"]),
        ),
    ] {
        let mut v = c["shutdown"].clone();
        v[key] = bad;
        assert!(ex::Shutdown::parse(&v).is_err(), "{key}")
    }
    let mut v = c["shutdown"].clone();
    v["owners"]["launch"] = json!("pending");
    assert!(ex::Shutdown::parse(&v).is_err());
    let mut v = c["shutdown"].clone();
    v["document_sha256"] = json!("0".repeat(64));
    assert!(
        ex::Shutdown::parse(&v)
            .unwrap()
            .verify_invocation(&r, "apply-execution", Some(&a), &[], Some("execution-stop"))
            .is_err()
    );
    assert!(e.verify_idle(&r.identity, &r.work_instance).is_err());
}
#[test]
fn historical_receipt_has_no_live_epoch_authority_and_keeps_conversation_binding() {
    for c in fixtures() {
        let (_, r, _) = setup(&c);
        let a = result(&c, &r);
        let recovery = json!({"status":"historical","document":a.wire["document"],"document_sha256":a.wire["document_sha256"],"reason":null});
        let mut current = r.identity.clone();
        current.backend = "different-backend".into();
        current.model = "different-model".into();
        ex::Recovery::parse(&recovery, &current).unwrap();
        current.session = "00000000-0000-4000-8000-000000000123".into();
        assert!(ex::Recovery::parse(&recovery, &current).is_err());
        let mut bad = recovery.clone();
        bad["document_sha256"] = json!("0".repeat(64));
        assert!(ex::Recovery::parse(&bad, &r.identity).is_err());
    }
}

#[test]
fn actual_source_stop_before_any_producer_keeps_independent_proofs() {
    let c = all_fixtures()
        .into_iter()
        .find(|c| c["name"] == "early-stop")
        .expect("source early stop fixture");
    let (_, r, _) = setup(&c);
    let a = ex::Receipt::parse(&c["result"], &r, "paused-apply").unwrap();
    let t =
        ex::StopReceipt::parse(&c["stop_result"], &r, "paused-apply", "execution-stop").unwrap();
    ex::Readback::parse_launch(&c["readback"], &a).unwrap();
    ex::Readback::parse_stop(&c["stop_readback"], &r, &t).unwrap();
    let e = ex::Shutdown::parse(&c["shutdown"]).unwrap();
    e.verify_invocation(
        &r,
        "paused-apply",
        Some(&a),
        std::slice::from_ref(&t),
        Some("execution-stop"),
    )
    .unwrap();
    assert!(a.document.is_none());
    assert!(a.lifecycle.producer_phase.is_none());
    let mut erased = c["shutdown"].clone();
    erased["stop_ids"] = json!([]);
    erased["stop_operation_ids"] = json!([]);
    erased["stop_readbacks_published"] = json!([]);
    assert!(
        ex::Shutdown::parse(&erased)
            .unwrap()
            .verify_invocation(&r, "paused-apply", None, &[], Some("execution-stop"))
            .is_err()
    );
}

#[test]
fn historical_receipt_checks_canonical_effects_even_with_a_recomputed_file_hash() {
    for c in fixtures() {
        let (_, r, _) = setup(&c);
        let a = result(&c, &r);
        let mut recovery = json!({"status":"historical","document":a.wire["document"],"document_sha256":a.wire["document_sha256"],"reason":null});
        rewrite_document(&mut recovery, |d| {
            rewrite_raw(&mut d["after"], |row| {
                if r.action == ex::Action::JobRun {
                    row["enabled"] = json!(true);
                } else {
                    row["last_reason"] = json!("invented historical evidence");
                }
            })
        });
        assert!(ex::Recovery::parse(&recovery, &r.identity).is_err());
    }
}
#[test]
fn current_goal_is_independent_and_cannot_rewrite_immutable_launch() {
    let c = fixtures()
        .into_iter()
        .find(|c| c["review"]["action"] == "goal_start")
        .unwrap();
    let (_, r, _) = setup(&c);
    let a = result(&c, &r);
    let mut rb = c["readback"].clone();
    rewrite_raw(&mut rb["current"]["row"], |row| {
        row["id"] = json!("other-goal");
        row["objective"] = json!("A newer independently observed goal");
    });
    let out = ex::Readback::parse_launch(&rb, &a).unwrap();
    assert_eq!(out.document, a.document);
    let mut unavailable = c["readback"].clone();
    unavailable["current"] = json!({"status":"unavailable","row":{"json":null,"sha256":null},"store_revision":null,"reason":"External file unavailable"});
    assert!(ex::Readback::parse_launch(&unavailable, &a).is_ok());
    unavailable["document_sha256"] = json!("0".repeat(64));
    assert!(ex::Readback::parse_launch(&unavailable, &a).is_err());
}

#[test]
fn legacy_static_defaults_expand_only_where_canonical_source_writes_them() {
    for c in fixtures().into_iter().filter(|c| {
        matches!(
            c["review"]["action"].as_str(),
            Some("job_run" | "goal_resume")
        )
    }) {
        let (edit, r, cap) = setup(&c);
        let mut v = r.wire.clone();
        let remove = if r.action == ex::Action::JobRun {
            vec!["new_conversation", "last_fired_slot", "run_count", "label"]
        } else {
            vec!["max_turns", "last_verdict", "no_progress_count"]
        };
        rewrite_raw(&mut v["before"], |row| {
            for key in &remove {
                row.as_object_mut().unwrap().remove(*key);
            }
        });
        if r.action == ex::Action::GoalResume {
            rewrite_raw(&mut v["after_template"], |row| {
                for key in &remove {
                    row.as_object_mut().unwrap().remove(*key);
                }
            });
        }
        ex::Review::parse(&v, &edit, &cap.instance, cap.provider).unwrap();
        if r.action == ex::Action::JobRun {
            rewrite_raw(&mut v["after_template"], |row| {
                row.as_object_mut().unwrap().remove("label");
            });
            assert!(ex::Review::parse(&v, &edit, &cap.instance, cap.provider).is_err());
        }
    }
}

#[test]
fn quit_crosschecks_exact_work_cutoff_even_before_apply_response() {
    for c in all_fixtures() {
        let (_, r, capture) = setup(&c);
        let e = ex::Shutdown::parse(&c["shutdown"]).unwrap();
        let mut ledger = life::Ledger::new(capture, Profile::Strict);
        ledger.activate(&c["activation"]).unwrap();
        assert!(ledger.verify_execution_shutdown(&e, Some(&r)).is_err());
        for source in c["events"].as_array().unwrap() {
            if source["type"] == "work_closing" {
                ledger.begin_close();
            }
            ledger.observe(&event(source), None).unwrap();
        }
        ledger.begin_close();
        ledger.verify_shutdown(&c["work_shutdown"]).unwrap();
        ledger.verify_execution_shutdown(&e, Some(&r)).unwrap();
        assert!(ledger.verify_execution_shutdown(&e, None).is_err());
        if c["result"]["status"] == "published" {
            let a = result(&c, &r);
            let document = a.document.as_ref().unwrap();
            assert!(ledger.execution_admission(document).unwrap().is_some());
            let mut erased = c["shutdown"].clone();
            erased["input_id"] = Value::Null;
            erased["admission_id"] = Value::Null;
            erased["document_sha256"] = Value::Null;
            erased["disposition"] = json!("uncertain");
            let erased = ex::Shutdown::parse(&erased).unwrap();
            ledger.verify_execution_shutdown(&erased, Some(&r)).unwrap();
            assert!(
                erased
                    .verify_invocation(&r, "apply-execution", Some(&a), &[], Some("execution-stop"))
                    .is_err()
            );
            let mut foreign = r.clone();
            foreign.producer_generation += 1;
            assert!(
                ledger
                    .verify_execution_shutdown(&e, Some(&foreign))
                    .is_err()
            );
        }
    }
}
