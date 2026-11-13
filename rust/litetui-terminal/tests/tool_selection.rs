use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    runtime_controls as r, runtime_controls_view as view, theme, work_lifecycle as life,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/tool_selection.json")).unwrap()
}
fn binding(c: &Value) -> r::Binding {
    r::Binding {
        identity: life::Identity::parse(&c["capture"]["identity"]).unwrap(),
        work_instance: c["capture"]["instance"].as_str().unwrap().into(),
        provider: life::Provider::Host,
        tool_selection: true,
        mcp_lifecycle: false,
    }
}
fn capabilities() -> r::Capabilities {
    let mut ops = r::OPERATIONS.map(str::to_owned).to_vec();
    ops.extend([life::CAPTURE, life::ACTIVATE, life::SETTLED, r::TOOL_LIST].map(str::to_owned));
    r::Capabilities::negotiate(&ops).unwrap().unwrap()
}
fn change() -> r::Change {
    r::Change::ToolSelection {
        name: "selection_probe".into(),
        enabled: false,
    }
}
fn draw(v: &mut view::View) -> String {
    let mut t = Terminal::new(TestBackend::new(120, 40)).unwrap();
    t.draw(|f| v.draw(f, f.area(), &theme::builtin_themes()[0]))
        .unwrap();
    t.backend()
        .buffer()
        .content
        .chunks(120)
        .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(v: &mut view::View, code: KeyCode) {
    v.key(KeyEvent::new(code, KeyModifiers::NONE));
}
#[test]
fn exported_source_tool_transactions_prove_exact_disabled_lists_and_shutdown() {
    let cases = fixture();
    assert_eq!(cases.as_object().unwrap().len(), 3);
    for (name, c) in cases.as_object().unwrap() {
        let b = binding(c);
        let caps = capabilities();
        let p = caps.parse_capture(&c["capture"], &b).unwrap();
        caps.parse_activation(&c["activation"], &b, &p).unwrap();
        let s = r::Snapshot::parse_tool_list(&c["listing"], &b).unwrap();
        assert_eq!(
            s.facts.tools_disabled.as_ref().unwrap(),
            &["missing-plugin", "missing-plugin"]
        );
        s.validate_request(&change()).unwrap();
        let review = r::Review::parse(&c["review"], &s, &change()).unwrap();
        assert!(
            review
                .detail_lines()
                .join("\n")
                .contains("Exact description 雪")
        );
        let receipt = r::Receipt::parse(&c["result"], &review, "apply-selection").unwrap();
        assert_eq!(
            receipt.status,
            match name.as_str() {
                "success" => r::Status::Applied,
                "save-failure" => r::Status::SessionOnly,
                _ => r::Status::Refused,
            }
        );
        let read = r::Readback::parse(&c["readback"], &receipt).unwrap();
        r::Shutdown::parse(&c["shutdown"], &b)
            .unwrap()
            .verify_invocation(
                &review,
                "apply-selection",
                Some(&receipt),
                Some(&read),
                Some("runtime-readback"),
                0,
            )
            .unwrap();
    }
}
#[test]
fn unnegotiated_or_forged_tool_authority_is_refused() {
    let c = &fixture()["success"];
    let b = binding(c);
    let caps = capabilities();
    let s = r::Snapshot::parse_tool_list(&c["listing"], &b).unwrap();
    let mut legacy = b.clone();
    legacy.tool_selection = false;
    assert!(r::Snapshot::parse_tool_list(&c["listing"], &legacy).is_err());
    assert!(r::Snapshot::parse(&c["listing"]["snapshot"], &legacy).is_err());
    assert!(caps.parse_capture(&c["capture"], &legacy).is_err());
    for field in ["tool_selection_version", "runtime_policy"] {
        let mut bad = c["capture"].clone();
        bad.as_object_mut().unwrap().remove(field);
        assert!(caps.parse_capture(&bad, &b).is_err());
    }
    let mut bad = c["listing"].clone();
    bad["tools"][0]["enabled"] = json!(!bad["tools"][0]["enabled"].as_bool().unwrap());
    assert!(r::Snapshot::parse_tool_list(&bad, &b).is_err());
    for field in ["name", "authority", "description", "summary"] {
        let mut bad = c["review"].clone();
        bad["tool"][field] = json!("forged");
        assert!(r::Review::parse(&bad, &s, &change()).is_err());
    }
    for field in ["tools_enabled", "mcp_enabled"] {
        let mut bad = c["review"].clone();
        bad["after"][field] = json!(!bad["after"][field].as_bool().unwrap());
        assert!(r::Review::parse(&bad, &s, &change()).is_err());
    }
    let mut bad = c["review"].clone();
    bad["after"]["tools_disabled"] = json!(["selection_probe"]);
    assert!(r::Review::parse(&bad, &s, &change()).is_err());
    assert!(
        s.validate_request(&r::Change::ToolSelection {
            name: "mcp__unknown".into(),
            enabled: true
        })
        .is_err()
    );
}
#[test]
fn python_tool_list_tick_applies_at_once_through_the_sources_checks() {
    // Python's /tools (tool_list.py): a tick is written the moment it is made, no Save and
    // no review page. The runtime's Review and Apply still run, unseen, then the list re-reads.
    let c = &fixture()["success"];
    let b = binding(c);
    let s = r::Snapshot::parse_tool_list(&c["listing"], &b).unwrap();
    let index = s
        .tools
        .as_ref()
        .unwrap()
        .rows
        .iter()
        .position(|row| row.name == "selection_probe")
        .unwrap();
    let mut v = view::View::default();
    v.open(r::Field::ToolSelection);
    assert!(matches!(
        v.take_intent(),
        Some(view::Intent::Read(r::Field::ToolSelection))
    ));
    v.apply(view::Update::Snapshot {
        field: r::Field::ToolSelection,
        snapshot: Box::new(s.clone()),
    });
    let frame = draw(&mut v);
    assert!(frame.contains("Tools") && frame.contains(litetui_terminal::tool_list_view::GLOBAL));
    assert!(frame.contains("powershell") && !frame.contains("selection_probe"));
    assert!(frame.contains("Close") && frame.contains("Dock to side"));
    assert!(!frame.contains("Review"), "no review page");
    // Focus: the Switch, then the scroll region, then each tool (Textual's DOM order).
    for _ in 0..index + 2 {
        key(&mut v, KeyCode::Tab);
    }
    // Focusing a tool scrolls it into view, as Textual's focus does.
    assert!(draw(&mut v).contains("selection_probe"));
    key(&mut v, KeyCode::Char(' '));
    assert!(
        matches!(v.take_intent(),Some(view::Intent::Review{change:ref got,..}) if *got==change())
    );
    let review = r::Review::parse(&c["review"], &s, &change()).unwrap();
    v.apply(view::Update::Review(Box::new(review.clone())));
    assert!(matches!(v.take_intent(), Some(view::Intent::Apply(_))));
    let receipt = r::Receipt::parse(&c["result"], &review, "apply-selection").unwrap();
    let read = r::Readback::parse(&c["readback"], &receipt).unwrap();
    v.apply(view::Update::Settled(Box::new(read)));
    assert!(matches!(
        v.take_intent(),
        Some(view::Intent::Read(r::Field::ToolSelection))
    ));
    // A refused change re-reads the list once; a refused read does not loop.
    v.apply(view::Update::Snapshot {
        field: r::Field::ToolSelection,
        snapshot: Box::new(s.clone()),
    });
    key(&mut v, KeyCode::Char(' '));
    assert!(v.take_intent().is_some());
    v.apply(view::Update::Rejected("stale".into()));
    assert!(matches!(
        v.take_intent(),
        Some(view::Intent::Read(r::Field::ToolSelection))
    ));
    v.apply(view::Update::Rejected("still stale".into()));
    assert!(v.take_intent().is_none());
    assert!(draw(&mut v).contains("still stale"));
    key(&mut v, KeyCode::Esc);
    assert!(!v.owns_input());
}
#[test]
fn global_switch_stays_separate_and_readonly_inventory_cannot_toggle() {
    let c = &fixture()["success"];
    let b = binding(c);
    let mut listing = c["listing"].clone();
    listing["editable_reason"] = json!("Provider inventory is fixed");
    let s = r::Snapshot::parse_tool_list(&listing, &b).unwrap();
    let mut v = view::View::default();
    v.open(r::Field::ToolSelection);
    v.take_intent();
    v.apply(view::Update::Snapshot {
        field: r::Field::ToolSelection,
        snapshot: Box::new(s),
    });
    draw(&mut v);
    key(&mut v, KeyCode::Tab);
    key(&mut v, KeyCode::Tab);
    key(&mut v, KeyCode::Char(' '));
    assert!(v.take_intent().is_none());
    // Back to the Switch (first focus): Enter flips every tool, the separate global field.
    key(&mut v, KeyCode::BackTab);
    key(&mut v, KeyCode::BackTab);
    key(&mut v, KeyCode::Enter);
    assert!(matches!(
        v.take_intent(),
        Some(view::Intent::Review {
            change: r::Change::Tools(true),
            ..
        })
    ));
}
