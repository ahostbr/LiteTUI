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
fn pending() -> (Management, Value) {
    let mut m = Management::new(Some(ID.into()));
    let ready = json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":ID,"backend":"claude","model":"model-A","tool_profile":"strict"});
    assert!(matches!(
        m.accept(ready.clone()).unwrap(),
        Step::Bootstrap(_)
    ));
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
    let Step::Write(history)=m.accept(json!({"type":"response","id":state["id"],"ok":true,"result":{
        "session_id":ID,"busy":false,"mode":"normal","tool_profile":"strict","model":{"model":"model-A","backend":"claude"},
        "ownership":{"writable":true,"data_version":1},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],
        "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]}})).unwrap() else {panic!()};
    assert_eq!(history["type"], "gui.conversations.read");
    assert_eq!(history["session_id"], ID);
    assert_ne!(state["id"], history["id"]);
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
