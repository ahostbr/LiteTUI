use litetui_terminal::{
    live::{Observation, Options},
    live_chat::Chat,
    management::{Management, Step},
    resume::{self, History},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};
const ID: &str = "12345678-1234-4123-8123-123456789abc";
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn root() -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "litetui-rust-resume-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&p).unwrap();
    p
}
fn transcript() -> Value {
    json!({"session_id":ID,"meta":{"id":ID},"read_only":false,"messages":[
        {"role":"system","content":"HIDDEN-SYSTEM"},
        {"role":"user","content":"old question"},
        {"role":"assistant","content":"old answer","reasoning":"retained reasoning"},
        {"role":"tool","content":"saved result","tool_call_id":"historical-tool"}
    ]})
}
fn negotiating() -> (Management, Value) {
    let mut m = Management::new(Some(ID.into()));
    let ready = json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":ID,"backend":"claude","model":"model-A","tool_profile":"strict"});
    let hello = m.begin(&ready).unwrap();
    let Step::Write(state) = m
        .accept(
            json!({"type":"response","id":hello["id"],"ok":true,"result":{
        "protocol_version":1,"data_version":1,"capabilities":["hello","state"],
        "operations":["gui.hello","gui.state","gui.conversations.read"]}}),
        )
        .unwrap()
    else {
        panic!()
    };
    (m, state["id"].clone())
}
fn state() -> Value {
    json!({"session_id":ID,"busy":false,
        "mode":"normal","tool_profile":"strict","model":{"model":"model-A","backend":"claude"},
        "ownership":{"writable":true,"data_version":1},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],
        "restored_input":{"policy":"hold","held":2},
        "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})
}
fn pending() -> (Management, Value) {
    let (mut m, id) = negotiating();
    let Step::Write(history) = m
        .accept(json!({"type":"response","id":id,"ok":true,"result":state()}))
        .unwrap()
    else {
        panic!()
    };
    assert_eq!(history["type"], "gui.conversations.read");
    assert_eq!(history["session_id"], ID);
    assert_ne!(id, history["id"]);
    (m, history["id"].clone())
}
#[test]
fn resumed_startup_needs_owned_transcript_before_ready_or_prompt() {
    let (mut m, id) = pending();
    assert!(!m.ready());
    assert!(m.prompt("early").is_err());
    let Step::Restored { snapshot, history } = m
        .accept(json!({"type":"response","id":id,"ok":true,"result":transcript()}))
        .unwrap()
    else {
        panic!()
    };
    assert_eq!(snapshot.session, ID);
    assert_eq!(snapshot.held_restored_input, Some(2));
    assert_eq!(history.messages.len(), 3);
    assert!(m.ready());
    m.prompt("fresh-explicit-prompt").unwrap();
}
#[test]
fn failed_or_wrong_transcript_blocks_startup() {
    for case in 0..6 {
        let (mut m, id) = pending();
        let mut value = transcript();
        match case {
            0 => value["session_id"] = json!("other"),
            1 => value["meta"]["id"] = json!("other"),
            2 => value["read_only"] = json!(true),
            3 => value["messages"] = json!([]),
            4 => value["messages"][1]["content"] = json!("x".repeat(40000)),
            _ => {}
        }
        let response = if case == 5 {
            json!({"type":"response","id":id,"ok":false,"error":"lease lost"})
        } else {
            json!({"type":"response","id":id,"ok":true,"result":value})
        };
        assert!(m.accept(response).is_err());
        assert!(!m.ready());
        assert!(m.prompt("never").is_err());
    }
}
#[test]
fn resume_ready_cannot_silently_name_a_fresh_conversation() {
    let mut m = Management::new(Some(ID.into()));
    let ready = json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":"other","backend":"claude","model":"model-A","tool_profile":"strict"});
    assert!(m.begin(&ready).is_err());
    assert!(!m.ready());
}
#[test]
fn saved_messages_retain_order_details_and_structured_content_without_replaying() {
    let mut value = transcript();
    value["messages"][1]["content"] = json!([{"type":"text","text":"structured"},{"type":"image_url","image_url":{"url":"fixture://image"}}]);
    let h = History::parse(&value, ID).unwrap();
    assert_eq!(h.hidden_system_messages, 1);
    assert_eq!(h.messages.len(), 3);
    assert!(h.messages[0].text.contains("fixture://image"));
    assert!(h.messages[1].details.contains("retained reasoning"));
    assert!(h.messages[2].details.contains("historical-tool"));
    assert!(!format!("{h:?}").contains("HIDDEN-SYSTEM"));
    let mut chat = Chat::new("claude".into(), "model-A".into());
    chat.observe(Observation::History(h.clone())).unwrap();
    assert!(chat.rows.is_empty());
    assert!(chat.take_submission().is_none());
    assert!(chat.submit("too early").is_err());
    assert!(chat.observe(Observation::History(h)).is_err());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("new explicit turn").unwrap();
    assert_eq!(chat.take_submission().as_deref(), Some("new explicit turn"));
    assert_eq!(chat.history.as_ref().unwrap().messages[0].role, "user");
}
#[test]
fn saved_display_budget_is_shared_with_new_turns() {
    let mut value = transcript();
    value["messages"] = json!([{"role":"user","content":"x".repeat(32000)}]);
    let mut chat = Chat::new("claude".into(), "model-A".into());
    chat.observe(Observation::History(History::parse(&value, ID).unwrap()))
        .unwrap();
    chat.observe(Observation::Ready).unwrap();
    assert!(chat.submit(&"x".repeat(1000)).is_err());
    assert!(chat.take_submission().is_none());
    assert!(chat.history.is_some());
}
#[test]
fn root_resume_preflight_never_rewrites_settings_or_adopts_an_unmarked_root() {
    let path = root();
    let settings = br#"{"tools_enabled":false,"mcp_enabled":false,"unrelated":"retained"}"#;
    fs::write(path.join("settings.json"), settings).unwrap();
    assert!(resume::validate_root(&path, false).is_err());
    assert!(!path.join(resume::MARKER).exists());
    resume::mark_fresh(&path).unwrap();
    let before = fs::read(path.join(resume::MARKER)).unwrap();
    assert!(resume::mark_fresh(&path).is_err());
    resume::validate_root(&path, false).unwrap();
    assert!(resume::validate_root(&path, true).is_err());
    assert_eq!(fs::read(path.join("settings.json")).unwrap(), settings);
    assert_eq!(fs::read(path.join(resume::MARKER)).unwrap(), before);
    let enabled = br#"{"tools_enabled":false,"mcp_enabled":true,"unrelated":"retained"}"#;
    fs::write(path.join("settings.json"), enabled).unwrap();
    resume::validate_root(&path, false).unwrap();
    assert_eq!(fs::read(path.join("settings.json")).unwrap(), enabled);
    fs::write(
        path.join("settings.json"),
        br#"{"tools_enabled":false,"mcp_enabled":"true"}"#,
    )
    .unwrap();
    assert!(resume::validate_root(&path, false).is_err());

    fs::write(
        path.join(resume::MARKER),
        r#"{"format":"litetui-rust-owned-root","version":2}"#,
    )
    .unwrap();
    assert!(resume::validate_root(&path, false).is_err());
}
#[test]
fn resume_options_require_exact_uuid_and_do_not_invent_selection() {
    let base = [
        "--python",
        "python",
        "--source",
        "source",
        "--data-root",
        "root",
        "--backend",
        "claude",
        "--model",
        "saved-model",
        "--resume",
    ];
    for id in [
        ID,
        "",
        "../escape",
        "1234",
        "12345678-1234-1123-8123-123456789abc",
    ] {
        let args: Vec<String> = base.into_iter().chain([id]).map(str::to_owned).collect();
        let result = Options::parse(&args);
        if id == ID {
            let o = result.unwrap();
            assert_eq!(o.resume.as_deref(), Some(ID));
            assert!(o.tool_profile.is_none());
        } else {
            assert!(result.is_err());
        }
    }
}
#[test]
fn saved_history_renders_as_saved_and_never_claims_current_completion() {
    use litetui_terminal::{demo::Demo, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut value = transcript();
    value["messages"][2]["content"] = json!("saved answer \u{1b}[31m \u{202e}");
    let mut app = Demo::live("claude".into(), "model-A".into());
    app.observe_live(Observation::History(History::parse(&value, ID).unwrap()))
        .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    let mut terminal = Terminal::new(TestBackend::new(100, 45)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains("Saved assistant") && text.contains("old question"));
    assert!(text.contains("retained reasoning") && text.contains("historical-tool"));
    assert!(!text.contains("Completed · stop") && !text.contains("HIDDEN-SYSTEM"));
    assert!(!text.contains('\u{1b}') && !text.contains('\u{202e}'));
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
}

#[test]
fn resume_refuses_unverified_or_invalid_restored_input_policy() {
    for input in [
        Value::Null,
        json!({"policy":"automatic","held":0}),
        json!({"policy":"hold"}),
        json!({"policy":"hold","held":true}),
        json!({"policy":"hold","held":-1}),
        json!({"policy":"hold","held":1001}),
        json!({"policy":"unknown","held":0}),
    ] {
        let (mut m, id) = negotiating();
        let mut value = state();
        if input.is_null() {
            value.as_object_mut().unwrap().remove("restored_input");
        } else {
            value["restored_input"] = input;
        }
        assert!(
            m.accept(json!({"type":"response","id":id,"ok":true,"result":value}))
                .is_err()
        );
        assert!(!m.ready());
        assert!(m.prompt("must-not-send").is_err());
    }
}

#[test]
fn restored_input_is_visible_and_does_not_queue_a_prompt() {
    let (mut m, id) = pending();
    let Step::Restored { snapshot, history } = m
        .accept(json!({"type":"response","id":id,"ok":true,"result":transcript()}))
        .unwrap()
    else {
        panic!()
    };
    let mut chat = Chat::new("claude".into(), "model-A".into());
    chat.observe(Observation::State(
        litetui_terminal::runtime_state::Update::from_management(snapshot),
    ))
    .unwrap();
    chat.observe(Observation::History(history)).unwrap();
    chat.observe(Observation::Ready).unwrap();
    assert!(chat.notice.contains("Restored input held: 2"));
    assert!(chat.take_submission().is_none());
}

#[test]
fn restored_input_count_remains_in_footer_after_the_startup_notice_changes() {
    use litetui_terminal::{demo::Demo, runtime_state::Update, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let (mut m, id) = pending();
    let Step::Restored { snapshot, .. } = m
        .accept(json!({"type":"response","id":id,"ok":true,"result":transcript()}))
        .unwrap()
    else {
        panic!()
    };
    let mut app = Demo::live("claude".into(), "model-A".into());
    app.observe_live(Observation::State(
        Update::from_event(
            &json!({"type":"ready","model":"model-A","conversation_id":ID,"thinking_level":"low"}),
        )
        .unwrap()
        .unwrap(),
    ))
    .unwrap();
    app.observe_live(Observation::State(Update::from_management(snapshot)))
        .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    app.notice = "Another activity".into();
    for width in [40, 120] {
        let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(text.contains("held:2"), "{width}: {text}");
    }
}
