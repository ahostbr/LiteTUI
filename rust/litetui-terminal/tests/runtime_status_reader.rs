use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    inspection::{self, Persistence, Snapshot, View},
    theme::builtin_themes,
    work_protocol::Identity,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

const INSTANCE: &str = "f928dc2c-86cb-4d06-b407-7da6687a9c3c";
fn identity() -> Identity {
    Identity {
        session: "ffb6413f-bf06-4905-8f1f-dc0999b1d2ff".into(),
        agent: "4450558c-5638-4b5d-8a14-cfdbf8a83869".into(),
        backend: "host".into(),
        model: "fixture".into(),
    }
}

fn wire() -> Value {
    let mut value = inspection::command(&identity(), INSTANCE);
    assert_eq!(
        value.as_object_mut().unwrap().remove("type"),
        Some(json!(inspection::OPERATION))
    );
    value["kind"] = json!("runtime_inspection");
    value["diagnostics"] = json!({"runtime_version":"1.2.3+local", "protocol_version":1,
        "conversation_persistence":"ok", "settings_persistence":"error"});
    value["mcp"] = json!({"enabled":false, "available":true, "complete":false, "remaining":1, "servers":[
        {"name":"local-a", "transport":"stdio", "state":"stopped", "tool_count":0},
        {"name":"cache", "transport":null, "state":"orphan", "tool_count":null}]});
    value
}
fn parse(v: &Value) -> std::io::Result<Snapshot> {
    Snapshot::parse(v, &identity(), INSTANCE)
}

#[test]
fn exact_request_identity_and_bounded_allowlists() {
    let good = wire();
    let read = parse(&good).unwrap();
    assert_eq!(read.conversation_persistence, Persistence::Ok);
    assert_eq!(read.settings_persistence, Persistence::Error);
    assert_eq!(read.mcp.servers.len(), 2);
    assert_eq!(read.mcp.servers[0].tool_count, Some(0));
    assert_eq!(read.mcp.servers[1].transport, None);
    for (path, bad) in [
        ("/version", json!(true)),
        ("/session_id", json!("other")),
        ("/model", json!("other")),
        ("/agent_id", json!("other")),
        ("/backend", json!("other")),
        ("/work_instance", json!("old")),
        ("/diagnostics/runtime_version", json!("1.2\nsecret")),
        ("/diagnostics/runtime_version", json!("x".repeat(65))),
        ("/diagnostics/protocol_version", json!(1.0)),
        (
            "/diagnostics/conversation_persistence",
            json!({"error":"raw text"}),
        ),
        ("/diagnostics/settings_persistence", json!("C:/not/an/enum")),
        ("/mcp/servers/0/name", json!("env/token")),
        ("/mcp/servers/0/name", json!("x".repeat(97))),
        ("/mcp/servers/0/state", json!("secret detail")),
        ("/mcp/servers/0/transport", json!("http://private.example")),
        ("/mcp/servers/0/tool_count", json!(-1)),
        ("/mcp/servers/0/tool_count", json!(true)),
        ("/mcp/remaining", json!(9007199254740992u64)),
        ("/mcp/enabled", json!(1)),
        ("/mcp/servers/1/name", json!("local-a")),
    ] {
        let mut v = good.clone();
        *v.pointer_mut(path).unwrap() = bad;
        assert!(parse(&v).is_err(), "{path}");
    }
    let mut extra = good.clone();
    extra["mcp"]["servers"][1]["url"] = json!("private");
    assert!(parse(&extra).is_err());
    let mut extra = good.clone();
    extra["diagnostics"]["data_root"] = json!("private");
    assert!(parse(&extra).is_err());
    let mut extra = good;
    extra["extra"] = json!("x".repeat(32768));
    assert!(parse(&extra).is_err());
}

#[test]
fn unavailable_empty_partial_and_complete_status_remain_distinct() {
    let mut v = wire();
    for enabled in [Value::Null, json!(true), json!(false)] {
        v["mcp"] = json!({"enabled":enabled,"available":false,"complete":false,"remaining":null,"servers":[]});
        assert!(!parse(&v).unwrap().mcp.available);
    }
    v["mcp"]["remaining"] = json!(1);
    assert!(parse(&v).is_err());
    v["mcp"]["remaining"] = json!(0);
    v["mcp"]["complete"] = json!(true);
    assert!(parse(&v).is_err());
    v["mcp"]["available"] = json!(true);
    assert!(parse(&v).unwrap().mcp.complete);
    v["mcp"]["remaining"] = Value::Null;
    assert!(parse(&v).is_err());
    v["mcp"]["complete"] = json!(false);
    assert_eq!(parse(&v).unwrap().mcp.remaining, None);
    let row = json!({"name":"s","state":"not started (lazy)","transport":"http","tool_count":null});
    v["mcp"]["servers"] = json!(vec![row; 33]);
    assert!(parse(&v).is_err());
}

fn draw(view: &mut View, w: u16, h: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    let theme = builtin_themes().remove(0);
    terminal
        .draw(|frame| view.draw(frame, frame.area(), &theme))
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(w as usize)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

#[test]
fn navigation_refresh_refusal_and_late_results_stay_inside_the_reader() {
    let data = parse(&wire()).unwrap();
    let mut view = View::default();
    view.open();
    assert!(draw(&mut view, 80, 13).contains("Reading runtime status"));
    assert!(!view.key(key(KeyCode::F(5))));
    view.apply(data.clone());
    let top = draw(&mut view, 80, 13);
    assert!(top.contains("Runtime version") && top.contains("1.2.3+local"));
    assert!(top.contains("Settings persistence") && top.contains("Error"));
    view.key(key(KeyCode::End));
    let bottom = draw(&mut view, 80, 13);
    assert!(bottom.contains("orphan") && bottom.contains("1 additional server"));
    assert!(!view.key(KeyEvent::new_with_kind(
        KeyCode::F(5),
        KeyModifiers::NONE,
        KeyEventKind::Repeat
    )));
    view.hide();
    assert!(!view.key(key(KeyCode::F(5))));
    draw(&mut view, 80, 13);
    assert!(view.key(key(KeyCode::F(5))));
    view.reject("Read cancelled.\x1b");
    let rejected = draw(&mut view, 80, 13);
    assert!(rejected.contains("Read cancelled.\\u{1b}") && !rejected.contains("orphan"));
    assert!(view.key(key(KeyCode::F(5))));
    view.key(key(KeyCode::Esc));
    view.apply(data);
    assert!(!view.is_open() && !draw(&mut view, 80, 13).contains("Runtime version"));
}
