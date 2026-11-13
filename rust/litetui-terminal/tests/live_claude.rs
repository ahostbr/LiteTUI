#![cfg(windows)]
//! Explicitly opt-in real-provider smoke; never part of ordinary/fixture tests.
#[path = "support/paid_startup.rs"]
mod paid_startup;

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
    let root = std::env::var_os("LITETUI_LIVE_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::temp_dir().join("litetui-rust"))
        .join(format!(
            "interactive-smoke-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    println!("Real Claude multi-turn data: {}", root.display());
    let runtime = Interactive::start(Options {
        launch: Default::default(),
        agent: None,
        resume: None,
        seed_from: None,
        owner: None,
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
    let mut startup = paid_startup::Startup::new("claude", "claude-sonnet-5-5");
    let result = runtime.with_cleanup(|runtime| {
        while answers.len() < 2 && Instant::now() < deadline {
            let event = runtime.events.recv_timeout(Duration::from_secs(1));
            if let Ok(event) = &event {
                startup.observe(event)?;
            }
            match event {
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
                Ok(Observation::RuntimePolicy(policy)) => state.install_runtime_policy(policy.facts),
                Ok(Observation::History(_)) => {
                    // Startup::observe accepted only this fresh, empty, owned projection.
                    println!("[startup] validated fresh empty source history");
                }
                Ok(Observation::Theme(litetui_terminal::theme_controls::Update::Snapshot(_))) => {
                    println!("[startup] validated source theme snapshot");
                }
                Ok(event @ (
                    Observation::RuntimeControls(_)
                    | Observation::ScheduledProposal(_)
                    | Observation::ScheduledHeld(_)
                    | Observation::ScheduledLocalNotSent { .. }
                    | Observation::ScheduledConversationChanged(_)
                    | Observation::Compact(_)
                    | Observation::CompactSettled(_)
                    | Observation::Controls(_)
                    | Observation::Queue(_)
                    | Observation::Commands(_)
                    | Observation::Theme(_)
                    | Observation::SavedHistory(_)
                    | Observation::Rewrite(_)
                    | Observation::RewriteSettled(_)
                    | Observation::RewriteSegment { .. }
                    | Observation::RewriteAftercare { .. }
                    | Observation::RewriteActivity { .. }
                    | Observation::RewriteStatus { .. }
                    | Observation::WorkLaunchStop { .. }
                    | Observation::WorkLaunchPending { .. }
                    | Observation::WorkLaunchFinished { .. }
                    | Observation::Work(_)
                    | Observation::WorkExecution(_)
                    | Observation::Settings(_)
                    | Observation::Backend(_)
                    | Observation::BackendChanged(_)
                    | Observation::Browser(_)
                    | Observation::ConversationChanged(_)
                    | Observation::ConversationUnverified(_)
                    | Observation::Opened(_)
                )) => {
                    return Err(std::io::Error::other(format!("Unrequested control observation: {}", paid_startup::describe(&event))));
                }
                Ok(event @ (Observation::Engine(_) | Observation::EngineAdmission(_))) => {
                    return Err(std::io::Error::other(format!("Unexpected engine operation in Claude smoke: {}", paid_startup::describe(&event))));
                }
                Ok(Observation::Submission { .. }) => {}
                // Python's chat-log lines (native_system_lines.py) are presentation only.
                Ok(Observation::SystemLine { .. }
                | Observation::SystemLines(_)
                | Observation::SystemLinesRefused(_)
                | Observation::Markdown { .. }) => {}
                Ok(Observation::Ready) => {
                    if !saw_ready_facts {
                        return Err(std::io::Error::other("validated facts must precede Ready"));
                    }
                    println!("Ready; submit turn 1");
                    runtime.submit("Remember the word ORANGE for this conversation. Reply with exactly ORANGE. Do not use tools.".into())?;
                }
                Ok(Observation::Content { card, kind, text }) => {
                    if card != 0 {
                        return Err(std::io::Error::other(format!("tools-disabled smoke expects one card, received {card}")));
                    }
                    if !kind.reasoning() {
                        if kind.replaces() {
                            answer = text;
                        } else {
                            answer.push_str(&text);
                        }
                    }
                }
                Ok(event @ Observation::Activity(_)) => {
                    return Err(std::io::Error::other(format!("Unexpected tool in tools-disabled smoke: {}", paid_startup::describe(&event))));
                }
                Ok(Observation::Approval(_))
                | Ok(Observation::ApprovalSettled { .. })
                | Ok(Observation::Question { .. }) => {
                    failure =
                        Some("Unexpected approval in tools-disabled smoke; no answer sent".into());
                    break;
                }
                Ok(Observation::Text(text)) => {
                    print!("{text}");
                    std::io::stdout().flush()?;
                    answer.push_str(&text);
                }
                Ok(Observation::Snapshot(text)) => {
                    println!("[replacement] {text}");
                    answer = text;
                }
                Ok(
                    Observation::Cancelled(reason)
                    | Observation::ImageRefused { reason, .. }
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
                            )?;
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
        if let Some(failure) = failure {
            return Err(std::io::Error::other(failure));
        }
        runtime.quit()
    });
    println!("Cleanup/result: {result:?}");
    result.unwrap();
    assert_eq!(answers.len(), 2);
    assert!(saw_usage, "expected real context usage facts");
    assert!(
        answers
            .iter()
            .all(|answer| answer.to_ascii_uppercase().contains("ORANGE")),
        "{answers:?}"
    );
}
