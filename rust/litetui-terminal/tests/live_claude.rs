#![cfg(windows)]
//! Explicitly opt-in real-provider smoke; never part of ordinary/fixture tests.
use litetui_terminal::live::{Interactive, Observation, Options};
use std::{
    io::Write,
    path::PathBuf,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[test]
#[ignore = "PAID Claude smoke; requires LITETUI_REAL_CLAUDE_SMOKE=YES and LITETUI_TEST_PYTHON"]
fn real_claude_two_turns_keep_conversation_and_exit_the_owned_job() {
    assert_eq!(
        std::env::var("LITETUI_REAL_CLAUDE_SMOKE").as_deref(),
        Ok("YES"),
        "explicit real-provider opt-in required"
    );
    let python = PathBuf::from(std::env::var_os("LITETUI_TEST_PYTHON").expect("explicit Python"));
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let root = PathBuf::from("C:/Projects/.scratch/T0565").join(format!(
        "interactive-smoke-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    println!("Real Claude multi-turn data: {}", root.display());
    let mut runtime = Interactive::start(Options {
        launch: Default::default(),
        resume: None,
        python,
        source,
        data_root: root,
        backend: "claude".into(),
        model: "claude-sonnet-5-5".into(),
        prompt: String::new(),
        timeout: Duration::from_secs(90),
        tools_enabled: false,
        tool_profile: Some(litetui_terminal::tool_mode::Profile::Strict),
        workspace: None,
    });
    let deadline = Instant::now() + Duration::from_secs(150);
    let mut answers = Vec::new();
    let mut answer = String::new();
    let mut failure = None;
    let mut state = litetui_terminal::runtime_state::State::default();
    let mut saw_ready_facts = false;
    let mut saw_usage = false;
    while answers.len() < 2 && Instant::now() < deadline {
        match runtime.events.recv_timeout(Duration::from_secs(1)) {
            Ok(Observation::State(update)) => {
                println!("[state] {update:?}");
                state.apply(update);
                let fields = state.footer_fields();
                println!("[footer] {fields:?}");
                saw_ready_facts |= fields.iter().any(|f| {
                    f.key == litetui_terminal::footer::FieldKey::Model
                        && f.text == "claude-sonnet-5-5"
                });
                saw_usage |= fields.iter().any(|f| {
                    f.key == litetui_terminal::footer::FieldKey::Context && !f.text.contains('—')
                });
            }
            Ok(Observation::History(_)) => panic!("Unrequested restored history"),
            Ok(
                Observation::Controls(_)
                | Observation::Queue(_)
                | Observation::Commands(_)
                | Observation::Theme(_)
                | Observation::SavedHistory(_)
                | Observation::Settings(_)
                | Observation::Backend(_)
                | Observation::BackendChanged(_)
                | Observation::Browser(_)
                | Observation::ConversationChanged(_)
                | Observation::ConversationUnverified(_)
                | Observation::Opened(_),
            ) => {
                panic!("Unrequested model control response")
            }
            Ok(Observation::Engine(_) | Observation::EngineAdmission(_)) => {
                panic!("Unexpected engine operation in Claude smoke")
            }
            Ok(Observation::Submission { .. }) => {}
            Ok(Observation::Ready) => {
                assert!(saw_ready_facts, "validated facts precede Ready");
                println!("Ready; submit turn 1");
                runtime.submit("Remember the word ORANGE for this conversation. Reply with exactly ORANGE. Do not use tools.".into()).unwrap();
            }
            Ok(Observation::Content { card, kind, text }) => {
                assert_eq!(card, 0, "tools-disabled smoke expects one card");
                if !kind.reasoning() {
                    if kind.replaces() {
                        answer = text;
                    } else {
                        answer.push_str(&text);
                    }
                }
            }
            Ok(Observation::Activity(_)) => panic!("Unexpected tool in tools-disabled smoke"),
            Ok(Observation::Approval(_))
            | Ok(Observation::ApprovalSettled { .. })
            | Ok(Observation::Question { .. }) => {
                failure =
                    Some("Unexpected approval in tools-disabled smoke; no answer sent".into());
                break;
            }
            Ok(Observation::Text(text)) => {
                print!("{text}");
                std::io::stdout().flush().unwrap();
                answer.push_str(&text);
            }
            Ok(Observation::Snapshot(text)) => {
                println!("[replacement] {text}");
                answer = text;
            }
            Ok(
                Observation::Cancelled(reason)
                | Observation::Refused(reason)
                | Observation::Failed(reason)
                | Observation::ApprovalStopped(reason),
            ) => {
                failure = Some(format!("Turn stopped for approval: {reason}"));
                break;
            }
            Ok(Observation::Complete) => {
                println!("\nTurn {} completed", answers.len() + 1);
                answers.push(std::mem::take(&mut answer));
                if answers.len() == 1 {
                    runtime
                        .submit(
                            "Reply with only the word I asked you to remember. Do not use tools."
                                .into(),
                        )
                        .unwrap();
                }
            }
            Ok(Observation::Reasoning(text)) => println!("[reasoning] {text}"),
            Ok(Observation::ReasoningSnapshot(text)) => println!("[reasoning replacement] {text}"),
            Ok(Observation::Information(info)) => println!("[runtime] {info}"),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(error) => {
                failure = Some(error.to_string());
                break;
            }
        }
    }
    let _ = runtime.quit();
    let cleanup = runtime.shutdown();
    println!("Cleanup: {cleanup:?}");
    cleanup.unwrap();
    assert!(failure.is_none(), "{failure:?}");
    assert_eq!(answers.len(), 2);
    assert!(saw_usage, "expected real context usage facts");
    assert!(
        answers
            .iter()
            .all(|answer| answer.to_ascii_uppercase().contains("ORANGE")),
        "{answers:?}"
    );
}
