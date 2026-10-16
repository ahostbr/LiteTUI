#![cfg(windows)]
//! Controlled child-process tests, NOT provider evidence. Set
//! LITETUI_TEST_PYTHON to the supported interpreter to run these ignored tests.
use std::{
    fs,
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
const FIXTURE: &str = r#"
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['LITETUI_DATA_ROOT'])
mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')
def emit(value):
    print(json.dumps(value), flush=True)
ready = dict(type='ready', protocol_version=1, launch_status='ready', backend='claude', model='claude-sonnet-5-5')
ready.update(conversation_id='controlled-conversation', thinking_level='low', context_length=200000)
if mode == 'bad_ready_state':
    ready['context_length'] = True
if mode == 'blocked':
    ready['launch_status'] = 'blocked'
    print('fixture stderr retained', file=sys.stderr, flush=True)
if mode == 'grandchild':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root/'grandchild.pid').write_text(str(child.pid))
emit(ready)
if mode == 'no_read':
    time.sleep(120)
for line in sys.stdin:
    command = json.loads(line)
    with (root/'commands.jsonl').open('a') as out:
        out.write(json.dumps(command)+'\n')
    if command['type'] == 'shutdown':
        emit(dict(type='response',id=command['id'],ok=True,result=dict(ok=True)))
        break
    emit(dict(type='response',id=command['id'],ok=True,result=dict(turn='accepted')))
    emit(dict(type='turn_start',model='claude-sonnet-5-5'))
    if mode == 'timeout':
        continue
    emit(dict(type='reasoning_delta',text='wrong prefix',reconcile='append'))
    emit(dict(type='reasoning_delta',text='correct trace',reconcile='replace'))
    emit(dict(type='text_delta',text=('x'*1048576 if mode == 'bigtext' else 'Hello')))
    emit(dict(type='usage',usage=dict(context_tokens=4000,max_context_tokens=(0 if mode == 'bad_usage_state' else 8000))))
    emit(dict(type='turn_end',stopReason='stop',error=None,tps=(-1 if mode == 'bad_rate_state' else 12.3)))
    if mode == 'truncated':
        sys.stdout.write('{"type":'); sys.stdout.flush(); break
    if mode.startswith('unknown'):
        wire = json.dumps(dict(type='user_input_requested'))+'\n'
        if mode == 'unknown_split':
            sys.stdout.write(wire[:5]); sys.stdout.flush(); time.sleep(.1); wire=wire[5:]
        sys.stdout.write(wire); sys.stdout.flush()
"#;
fn setup(mode: &str) -> (Command, PathBuf) {
    let python =
        std::env::var_os("LITETUI_TEST_PYTHON").expect("set LITETUI_TEST_PYTHON for process tests");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/live-process-tests")
        .join(format!(
            "{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let source = base.join("source");
    fs::create_dir_all(source.join("src/litetui")).unwrap();
    fs::write(source.join("src/litetui/__init__.py"), "").unwrap();
    fs::write(source.join("src/litetui/cli.py"), FIXTURE).unwrap();
    let root = base.join("data");
    let prompt = if mode == "no_read" {
        "x".repeat(16000)
    } else {
        "Hello".into()
    };
    let mut command = Command::new(env!("CARGO_BIN_EXE_litetui-rs"));
    command
        .env("RUST_LIVE_FIXTURE", mode)
        .args(["--live", "--python"])
        .arg(python)
        .arg("--source")
        .arg(source)
        .arg("--data-root")
        .arg(&root)
        .args([
            "--backend",
            "claude",
            "--model",
            "claude-sonnet-5-5",
            "--prompt",
            &prompt,
            "--timeout",
            "2",
        ]);
    (command, root)
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn streams_once_and_waits_for_shutdown_and_clean_eof() {
    let (mut command, root) = setup("success");
    let result = command.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("Hello"));
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert_eq!(commands.lines().count(), 2);
    assert_eq!(commands.matches("rust-live-prompt").count(), 1);
    assert!(commands.contains("rust-live-shutdown"));
    let settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(root.join("settings.json")).unwrap()).unwrap();
    assert_eq!(settings["tools_enabled"], false);
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn blocked_ready_never_submits_and_preserves_stderr() {
    let (mut command, root) = setup("blocked");
    let result = command.output().unwrap();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("fixture stderr retained"));
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!commands.contains("rust-live-prompt"));
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn complete_turn_never_hides_unknown_or_truncated_suffix() {
    for mode in ["unknown", "unknown_split", "truncated"] {
        let (mut command, _) = setup(mode);
        let result = command.output().unwrap();
        assert!(!result.status.success(), "{mode}");
        assert!(
            !String::from_utf8_lossy(&result.stdout)
                .contains("contained process tree exited successfully")
        );
    }
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn runtime_timeout_is_not_completion_and_shutdown_is_sent() {
    let (mut command, root) = setup("timeout");
    let start = Instant::now();
    let result = command.output().unwrap();
    assert!(!result.status.success());
    assert!(start.elapsed() < Duration::from_secs(8));
    assert!(
        fs::read_to_string(root.join("commands.jsonl"))
            .unwrap()
            .contains("shutdown")
    );
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn malformed_runtime_facts_fail_before_admission_or_completion() {
    for (mode, diagnostic) in [
        ("bad_ready_state", "context_length"),
        ("bad_usage_state", "denominator"),
        ("bad_rate_state", "tps"),
    ] {
        let (mut command, root) = setup(mode);
        let result = command.output().unwrap();
        assert!(!result.status.success(), "{mode}");
        assert!(String::from_utf8_lossy(&result.stderr).contains(diagnostic));
        let output = String::from_utf8_lossy(&result.stdout);
        assert!(!output.contains("Turn ended: stop"));
        if mode == "bad_ready_state" {
            assert!(!output.contains("Ready. Prompt:"));
            let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
            assert!(!commands.contains("rust-live-prompt"));
        }
    }
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON; ~22 seconds"]
fn nonreading_child_does_not_block_prompt_or_cleanup_deadline() {
    let (mut command, _) = setup("no_read");
    // 16 KiB exceeds the stdin pipe buffer but fits Windows' command line.
    let start = Instant::now();
    let result = command.output().unwrap();
    assert!(!result.status.success());
    assert!(start.elapsed() < Duration::from_secs(30));
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON; ~20 seconds"]
fn descendant_without_inherited_pipes_is_still_contained() {
    let (mut command, root) = setup("grandchild");
    let start = Instant::now();
    let result = command.output().unwrap();
    assert!(!result.status.success());
    assert!(start.elapsed() < Duration::from_secs(30));
    let pid: u32 = fs::read_to_string(root.join("grandchild.pid"))
        .unwrap()
        .parse()
        .unwrap();
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if !handle.is_null() {
            let mut code = 0;
            assert_ne!(GetExitCodeProcess(handle, &mut code), 0);
            CloseHandle(handle);
            assert_ne!(code, 259, "grandchild still active");
        }
    }
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn stalled_terminal_does_not_block_child_cleanup_or_deadline() {
    let (mut command, _) = setup("bigtext");
    let mut child = command
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    // Retain, but never consume, the read end of stdout.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(!status.success());
            break;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("blocked terminal prevented deadline");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn interactive_worker_keeps_one_child_for_two_unique_prompt_commands() {
    use litetui_terminal::live::{Interactive, Observation, Options};
    let (command, root) = setup("success");
    let args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let mut options = Options::parse(&args).unwrap();
    options.prompt.clear();
    let mut runtime = Interactive::start(options);
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut completed = 0;
    let mut chat =
        litetui_terminal::live_chat::Chat::new("claude".into(), "claude-sonnet-5-5".into());
    while completed < 2 {
        assert!(Instant::now() < deadline);
        let observation = runtime.events.recv_timeout(Duration::from_secs(5)).unwrap();
        let ready = matches!(observation, Observation::Ready);
        let complete = matches!(observation, Observation::Complete);
        chat.observe(observation).unwrap();
        if ready {
            assert!(
                chat.runtime_state
                    .footer_fields()
                    .iter()
                    .any(|f| f.text == "ctx — / 200,000 max")
            );
            chat.submit("first").unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if complete {
            let fields = chat.runtime_state.footer_fields();
            for text in ["50%", "ctx 4,000 / 8,000", "12.3 tok/s"] {
                assert!(fields.iter().any(|f| f.text == text), "{text}");
            }
            assert_eq!(chat.rows.last().unwrap().reasoning, "correct trace");
            assert_eq!(chat.rows.last().unwrap().answer, "Hello");
            completed += 1;
            if completed == 1 {
                chat.submit("second").unwrap();
                runtime.submit(chat.take_submission().unwrap()).unwrap();
            }
        }
    }
    runtime.quit().unwrap();
    runtime.shutdown().unwrap();
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    let commands: Vec<serde_json::Value> = commands
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(commands.len(), 3);
    assert_ne!(commands[0]["id"], commands[1]["id"]);
    assert_eq!(commands[2]["type"], "shutdown");
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON; ~20 seconds"]
fn ui_failure_always_waits_cleanup_and_preserves_both_failures() {
    use litetui_terminal::live::{Interactive, Observation, Options};
    for mode in ["timeout", "grandchild"] {
        let (command, root) = setup(mode);
        let source = root.parent().unwrap().join("source/src/litetui/cli.py");
        let script = fs::read_to_string(&source).unwrap().replace(
            "mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')",
            &format!("mode = '{mode}'"),
        );
        fs::write(source, script).unwrap();
        let args: Vec<String> = command
            .get_args()
            .skip(1)
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();
        let options = Options::parse(&args).unwrap();
        let runtime = Interactive::start(options);
        let result = runtime.with_cleanup(|runtime| {
            loop {
                match runtime.events.recv_timeout(Duration::from_secs(5)).unwrap() {
                    Observation::Ready => runtime.submit("test".into()).unwrap(),
                    Observation::Information(info) if info == "turn started" => {
                        return Err(std::io::Error::other("injected draw failure"));
                    }
                    _ => {}
                }
            }
        });
        let error = result.unwrap_err().to_string();
        assert!(error.contains("injected draw failure"));
        if mode == "timeout" {
            assert_eq!(error, "injected draw failure");
        } else {
            assert!(error.contains("runtime cleanup"));
            assert!(error.contains("forced termination"));
        }
        assert!(
            fs::read_to_string(root.join("commands.jsonl"))
                .unwrap()
                .contains("shutdown")
        );
    }
}
