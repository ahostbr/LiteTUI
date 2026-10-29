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
import json, os, subprocess, sys, time, uuid
from pathlib import Path
root = Path(os.environ['LITETUI_DATA_ROOT'])
(root.parent/'python-started').write_text('runtime activated')
mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')
gui_enabled = False
operation = None
execution = None
identified = {'turn_start','turn_end','text_delta','reasoning_delta','native_text_snapshot',
              'native_activity','native_usage','native_event','generic_tool_requested','generic_tool_returned'}
def emit(value):
    global execution
    print(json.dumps(value), flush=True)
    if gui_enabled and value['type'] not in {'response','error'}:
        if value['type'] == 'turn_start':
            execution = str(uuid.uuid4())
        envelope = dict(type='gui.event',event_id=str(uuid.uuid4()),
                        session_id='controlled-conversation',operation_id=operation,event=value)
        if value['type'] in identified:
            envelope.update(execution_id=execution,hook_id=None,source='rpc')
        if mode == 'management_wrong_origin' and value['type'] == 'text_delta':
            envelope['operation_id'] = 'unrelated-prompt'
        print(json.dumps(envelope), flush=True)
ready = dict(type='ready', protocol_version=1, launch_status='ready', backend='claude', model='claude-sonnet-5-5')
ready.update(conversation_id='controlled-conversation', thinking_level='low', context_length=200000,
             management_protocols=[1], capabilities=['hello','state'])
settings = json.loads((root/'settings.json').read_text())
for marker in ['LITETUI_OWNER', 'LITETUI_SPAWN_IDENTITY', 'LITEHARNESS_SPAWNED_BY']:
    assert marker not in os.environ, marker
profile = sys.argv[sys.argv.index('--tool-profile')+1]
ready.update(tools_enabled=settings['tools_enabled'], mcp_enabled=settings['mcp_enabled'],
             tool_profile=profile, tool_profile_requested=profile,
             approval_route='host' if os.environ.get('LITETUI_APPROVAL_HOST') == '1' else 'refuse',
             cwd=os.getcwd())

if mode.startswith('policy_'):
    field = mode.removeprefix('policy_')
    ready[field] = {'tools_enabled':False,'tool_profile':'autonomous',
                   'approval_route':'spawner','mcp_enabled':True}[field]
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
pending = None
approval_sequence = 0
prompt_count = 0
abort_count = 0
for line in sys.stdin:
    command = json.loads(line)
    with (root/'all-commands.jsonl').open('a') as out:
        out.write(json.dumps(command)+'\n')
    if not command['type'].startswith('gui.'):
        with (root/'commands.jsonl').open('a') as out:
            out.write(json.dumps(command)+'\n')
    else:
        with (root/'management-commands.jsonl').open('a') as out:
            out.write(json.dumps(command)+'\n')
    if command['type'] == 'gui.hello':
        if mode == 'management_hello_timeout':
            continue
        if mode == 'management_activation_race':
            emit(dict(type='usage', usage=dict(context_tokens=1)))
        gui_enabled = True
        emit(dict(type='response',id=command['id'],ok=True,
                  result=dict(protocol_version=2 if mode == 'management_bad_version' else 1,
                              data_version=1,capabilities=['hello','state'],operations=['gui.hello','gui.state'])))
        continue
    if command['type'] == 'gui.state':
        state = dict(session_id='controlled-conversation',busy=False,mode='normal',
                     tool_profile=profile,model=dict(model=ready['model'],backend=ready['backend']),
                     ownership=dict(writable=True,data_version=1),schedules_paused=False,
                     conversations=[],jobs=[],tasks=[],active_work=[],pending_approvals=[],
                     pending_questions=[],pending_model_loads=[],settings={'private_fixture':'do-not-display'})
        if mode == 'management_state_busy':
            state['busy'] = True
        if mode == 'management_state_identity':
            state['session_id'] = 'another-conversation'
        if mode == 'management_state_malformed':
            state['ownership']['writable'] = 'true'
        emit(dict(type='response',id=command['id'],ok=True,result=state))
        continue
    if command['type'] == 'prompt':
        operation = command['id']
    if command['type'] == 'shutdown':
        if pending:
            if mode.startswith('question'):
                emit(dict(type='user_input_resolved',id=pending,cancelled=True))
            else:
                emit(dict(type='tool_approval_closed',id=pending,outcome='cancelled',reason='cancelled'))
        emit(dict(type='response',id=command['id'],ok=True,result=dict(ok=True)))
        break
    if command['type'] == 'abort' and mode.startswith('cancel'):
        abort_count += 1
        ack = dict(type='response',id=command['id'],ok=True,
                   result=dict(stopped=True,cancelled_asks=[pending] if mode == 'cancel_question' else []))
        if mode == 'cancel_hang' or (mode == 'cancel_force' and abort_count == 1):
            emit(ack)
            continue
        # Exercise terminal-before-closure and receipt-after-terminal ordering.
        emit(dict(type='turn_end',stopReason='cancelled',error='CancelledError' if mode == 'cancel_force' else None))
        if pending:
            if mode == 'cancel_question':
                emit(dict(type='user_input_resolved',id=pending,cancelled=True,reason='abort'))
            else:
                emit(dict(type='tool_approval_closed',id=pending,outcome='denied',reason='cancelled'))
            pending = None
        emit(ack)
        continue
    if command['type'] == 'answer':
        assert command['ask_id'] == pending and command['id'] != pending
        assert command['action'] in ['submit', 'chat', 'cancel']
        if command['action'] == 'submit':
            assert command['answers'] == [dict(selected=[0, 1],note='both'), dict(selected=[],note='fixture-secret')]
        elif command['action'] == 'chat':
            assert command['answers'] == [dict(selected=[1],note='discuss'), dict(selected=[],note='')]
        else:
            assert command['answers'] == []
        ack = dict(type='response',id=command['id'],ok=True,result=dict(answered=pending,id_key='ask_id'))
        if mode == 'question_cancelled':
            emit(dict(type='user_input_resolved',id=pending,cancelled=True))
            emit(dict(type='turn_end',stopReason='approval',error='Fixture cancelled'))
            emit(dict(type='response',id=command['id'],ok=False,error='Unknown ask'))
        else:
            if approval_sequence % 2:
                emit(ack)
            emit(dict(type='text_delta',text='Fixture question handled; no tool executed',card_index=0))
            emit(dict(type='turn_end',stopReason='stop',error=None))
            if not approval_sequence % 2:
                emit(ack)
        pending = None
        continue
    if command['type'] == 'approve':
        assert command['approval_id'] == pending and command['id'] != pending
        assert command['remember'] is False and type(command['allow']) is bool
        ack = dict(type='response',id=command['id'],ok=True,result=dict(approved=pending,allow=command['allow'],id_key='approval_id'))
        closure = dict(type='tool_approval_closed',id=pending,outcome='approved' if command['allow'] else 'denied',reason='answered')
        # Turn one ACKs before closure; turn two ACKs after the sole turn_end.
        if not command['allow']:
            emit(ack)
        emit(closure)
        pending = None
        if mode == 'approval_stop':
            emit(dict(type='turn_end',stopReason='approval',error='Fixture approval stopped; nothing executed'))
        else:
            emit(dict(type='text_delta',text='Fixture decision observed; nothing executed',card_index=0))
            emit(dict(type='turn_end',stopReason='stop',error=None))
        if command['allow']:
            emit(ack)
        continue
    emit(dict(type='response',id=command['id'],ok=True,result=dict(turn='accepted')))
    emit(dict(type='turn_start',model='claude-sonnet-5-5'))
    prompt_count += 1
    if mode.startswith('cancel') and prompt_count == 1:
        if mode == 'cancel_question':
            pending = 'cancel-ask'
            emit(dict(type='user_input_requested',id=pending,questions=[dict(
                label='Scope',question='Fixture?',options=[],selected=[],note='',answered=False,multiSelect=False)]))
        elif mode == 'cancel_approval':
            pending = 'cancel-tool'
            emit(dict(type='tool_approval_requested',id=pending,tool='write',input={},profile='strict',why='fixture',timeout_s=None))
        else:
            emit(dict(type='text_delta',text='Partial fixture output',card_index=0))
        continue
    if mode == 'refused_without_request':
        emit(dict(type='turn_end',stopReason='approval',error='No approval route'))
        continue
    if mode.startswith('approval') and not (mode == 'approval_stop' and approval_sequence == 2):
        pending = 'fixture-approval-' + str(approval_sequence)
        approval_sequence += 1
        emit(dict(type='tool_approval_requested',id=pending,tool='write',input={'path':'never-executed.txt'},profile='strict',why='fixture only',timeout_s=None))
        continue
    if mode.startswith('question'):
        pending = 'fixture-question-' + str(approval_sequence)
        approval_sequence += 1
        questions = [
            dict(label='Scope',question='Which files?',options=[
                dict(title='Source',description='Implementation'),
                dict(title='Tests',description='Regression coverage'),
            ],selected=[],note='',answered=False,multiSelect=True),
            dict(label='Token',question='Fixture only',options=[],selected=[],note='',answered=False,multiSelect=False,isSecret=True),
        ]
        emit(dict(type='user_input_requested',id=pending,questions=questions))
        continue
    if mode == 'timeout':
        continue
    emit(dict(type='reasoning_delta',text='wrong prefix',reconcile='append',card_index=0))
    emit(dict(type='reasoning_delta',text='correct trace',reconcile='replace',card_index=0))
    emit(dict(type='text_delta',text=('x'*1048576 if mode == 'bigtext' else 'Hello'),card_index=0))
    emit(dict(type='native_activity',provider='claude',kind='tool_use',id='fixture-tool',name='Read',complete=True,input={'path':'fixture-only'},result=None,is_error=None,card_index=0))
    emit(dict(type='text_delta',text='After tool',card_index=1))
    emit(dict(type='native_activity',provider='claude',kind='tool_result',id='fixture-tool',name='Read',complete=True,input=None,result='Fixture failure, nothing executed',is_error=True,card_index=0))
    emit(dict(type='usage',usage=dict(context_tokens=4000,max_context_tokens=(0 if mode == 'bad_usage_state' else 8000))))
    emit(dict(type='turn_end',stopReason='stop',error=None,tps=(-1 if mode == 'bad_rate_state' else 12.3)))
    if mode == 'management_unpaired_idle':
        print(json.dumps(dict(type='usage',usage=dict(context_tokens=2))),flush=True)
    if mode == 'truncated':
        sys.stdout.write('{"type":'); sys.stdout.flush(); break
    if mode.startswith('unknown'):
        wire = json.dumps(dict(type='user_input_requested'))+'\n'
        if mode == 'unknown_split':
            sys.stdout.write(wire[:5]); sys.stdout.flush(); time.sleep(.1); wire=wire[5:]
        sys.stdout.write(wire); sys.stdout.flush()
"#;
fn setup(mode: &str) -> (Command, PathBuf) {
    setup_with_profile(mode, "strict")
}
fn setup_with_profile(mode: &str, profile: &str) -> (Command, PathBuf) {
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
            "--tool-profile",
            profile,
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
            assert_eq!(
                chat.rows.last().unwrap().cards[0].reasoning,
                "correct trace"
            );
            assert_eq!(chat.rows.last().unwrap().cards[0].answer, "Hello");
            let row = chat.rows.last().unwrap();
            assert_eq!(row.cards[1].answer, "After tool");
            assert_eq!(
                row.items,
                vec![
                    litetui_terminal::live_chat::Item::Card(0),
                    litetui_terminal::live_chat::Item::Tool(0),
                    litetui_terminal::live_chat::Item::Card(1)
                ]
            );
            assert!(matches!(
                row.tools[0].result,
                litetui_terminal::native_activity::ResultState::Returned {
                    is_error: Some(true),
                    ..
                }
            ));
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

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn interactive_approval_commands_are_explicit_and_correlated_across_two_turns() {
    use litetui_terminal::{
        approvals::Event,
        live::{Interactive, Observation, Options},
    };
    let (command, root) = setup("approval");
    let source = root.parent().unwrap().join("source/src/litetui/cli.py");
    fs::write(
        &source,
        fs::read_to_string(&source).unwrap().replace(
            "mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')",
            "mode = 'approval'",
        ),
    )
    .unwrap();
    let args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let mut options = Options::parse(&args).unwrap();
    options.timeout = Duration::from_secs(5);
    let mut runtime = Interactive::start(options);
    let mut chat =
        litetui_terminal::live_chat::Chat::new("claude".into(), "claude-sonnet-5-5".into());
    let mut turns = 0;
    let mut closures = 0;
    while turns < 2 {
        let observation = runtime
            .events
            .recv_timeout(Duration::from_secs(10))
            .unwrap();
        let ready = matches!(observation, Observation::Ready);
        let asked = matches!(observation, Observation::Approval(Event::Requested(_)));
        let complete = matches!(observation, Observation::Complete);
        if matches!(observation, Observation::Approval(Event::Closed { .. })) {
            closures += 1;
        }
        chat.observe(observation).unwrap();
        if ready {
            chat.submit("first").unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if asked {
            // Receiving a request alone cannot send an implicit answer.
            std::thread::sleep(Duration::from_millis(75));
            let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
            assert_eq!(
                log.lines()
                    .filter(|line| line.contains("approval_id"))
                    .count(),
                turns
            );
            chat.approvals.choose(turns == 1);
            runtime
                .approve(chat.approvals.take_decision().unwrap())
                .unwrap();
        }
        if complete {
            assert!(chat.approvals.is_empty());
            turns += 1;
            if turns == 1 {
                chat.submit("second").unwrap();
                runtime.submit(chat.take_submission().unwrap()).unwrap();
            }
        }
    }
    assert_eq!(closures, 2);
    runtime.shutdown().unwrap();
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    let commands: Vec<serde_json::Value> = log
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(commands.len(), 5);
    assert_eq!(commands[1]["allow"], false);
    assert_eq!(commands[3]["allow"], true);
    assert_ne!(commands[1]["id"], commands[3]["id"]);
    assert_ne!(commands[1]["approval_id"], commands[3]["approval_id"]);
    assert_eq!(commands[4]["type"], "shutdown");
    assert!(!root.join("never-executed.txt").exists());
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn one_shot_refuses_approval_without_answering() {
    let (mut command, root) = setup("approval");
    let output = command.output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("cannot answer approval"));
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!log.contains("approval_id"));
    assert!(log.contains("shutdown"));
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn unanswered_unbounded_approval_does_not_disable_runtime_deadline_or_auto_deny() {
    use litetui_terminal::live::{Interactive, Observation, Options};
    let (command, root) = setup("approval");
    let source = root.parent().unwrap().join("source/src/litetui/cli.py");
    fs::write(
        &source,
        fs::read_to_string(&source).unwrap().replace(
            "mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')",
            "mode = 'approval'",
        ),
    )
    .unwrap();
    let args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let mut runtime = Interactive::start(Options::parse(&args).unwrap());
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut requested = false;
    loop {
        assert!(Instant::now() < deadline);
        for observation in runtime.events.try_iter() {
            match observation {
                Observation::Ready => runtime.submit("fixture".into()).unwrap(),
                Observation::Approval(_) => requested = true,
                _ => {}
            }
        }
        if let Some(result) = runtime.poll_done() {
            assert!(requested);
            let error = result.unwrap_err().to_string();
            assert!(error.contains("Live deadline elapsed"));
            assert!(error.contains("cleanup=Ok"));
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!log.contains("approval_id"));
    assert!(log.contains("shutdown"));
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn stopped_approval_keeps_owned_runtime_alive_for_explicit_followups() {
    use litetui_terminal::{
        approvals::Event,
        live::{Interactive, Observation, Options},
        live_chat::{Chat, ReplyState},
    };
    let (command, root) = setup("approval_stop");
    let source = root.parent().unwrap().join("source/src/litetui/cli.py");
    fs::write(
        &source,
        fs::read_to_string(&source).unwrap().replace(
            "mode = os.environ.get('RUST_LIVE_FIXTURE', 'success')",
            "mode = 'approval_stop'",
        ),
    )
    .unwrap();
    let args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let mut runtime = Interactive::start(Options::parse(&args).unwrap());
    let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
    let mut stopped = 0;
    let mut closures = 0;
    loop {
        let observation = runtime
            .events
            .recv_timeout(Duration::from_secs(10))
            .unwrap();
        let ready = matches!(observation, Observation::Ready);
        let asked = matches!(observation, Observation::Approval(Event::Requested(_)));
        let refusal = matches!(observation, Observation::ApprovalStopped(_));
        let complete = matches!(observation, Observation::Complete);
        if matches!(observation, Observation::Approval(Event::Closed { .. })) {
            closures += 1;
        }
        chat.observe(observation).unwrap();
        if asked {
            // The second approval is granted but the source still stops.
            // Its ACK arrives after the sole terminal event.
            chat.approvals.choose(stopped == 1);
            runtime
                .approve(chat.approvals.take_decision().unwrap())
                .unwrap();
        }
        if refusal {
            stopped += 1;
            assert_eq!(closures, stopped);
            assert_eq!(chat.rows.last().unwrap().state, ReplyState::ApprovalStopped);
            assert!(chat.rows.last().unwrap().cards[0].answer.is_empty());
            assert!(!chat.busy && !chat.closed && chat.approvals.is_empty());
            if stopped == 1 {
                // The configured deadline is two seconds. Settled refusal must
                // clear it while the human considers a new explicit prompt.
                std::thread::sleep(Duration::from_millis(2250));
                assert!(runtime.poll_done().is_none());
            }
        }
        if ready || refusal {
            chat.submit(&format!("explicit prompt {stopped}")).unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if complete {
            assert_eq!(stopped, 2, "a refusal was mistaken for success");
            break;
        }
    }
    runtime.shutdown().unwrap();
    chat.finish(None);
    assert_eq!(
        chat.rows.iter().map(|row| row.state).collect::<Vec<_>>(),
        vec![
            ReplyState::ApprovalStopped,
            ReplyState::ApprovalStopped,
            ReplyState::Complete
        ]
    );
    let commands: Vec<serde_json::Value> = fs::read_to_string(root.join("commands.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(commands.len(), 6); // Three prompts, two decisions, one shutdown.
    assert_eq!(
        commands
            .iter()
            .map(|cmd| cmd["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "prompt", "approve", "prompt", "approve", "prompt", "shutdown"
        ]
    );
    assert_eq!(commands[1]["allow"], false);
    assert_eq!(commands[3]["allow"], true);
    assert_ne!(commands[0]["id"], commands[2]["id"]);
    assert_ne!(commands[2]["id"], commands[4]["id"]);
    assert!(!root.join("never-executed.txt").exists());
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn one_shot_approval_stop_without_request_is_failure_with_clean_shutdown() {
    let (mut command, root) = setup("refused_without_request");
    let output = command.output().unwrap();
    assert!(!output.status.success());
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(
        error.contains("Turn stopped for approval: No approval route"),
        "{error}"
    );
    assert!(error.contains("cleanup=Ok"), "{error}");
    assert!(!String::from_utf8_lossy(&output.stdout).contains("[Turn ended: stop]"));
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!log.contains("approval_id"));
    assert!(log.contains("shutdown"));
}

fn question_runtime(mode: &str) -> (litetui_terminal::live::Interactive, PathBuf) {
    use litetui_terminal::live::{Interactive, Options};
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
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn question_actions_cross_owned_process_and_ack_races_keep_three_turns_separate() {
    use litetui_terminal::{
        live::{Observation, TerminalOutcome},
        live_chat::Chat,
        questions::{Action, Answer, Decision, Event},
    };
    use std::collections::BTreeSet;
    let (mut runtime, root) = question_runtime("question");
    let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
    let mut turns = 0;
    while turns < 3 {
        let observation = runtime
            .events
            .recv_timeout(Duration::from_secs(10))
            .unwrap();
        let ready = matches!(observation, Observation::Ready);
        let requested = if let Observation::Question {
            event: Event::Requested(request),
            ..
        } = &observation
        {
            Some(request.id.clone())
        } else {
            None
        };
        let complete = matches!(
            observation,
            Observation::Complete
                | Observation::Question {
                    settled: Some(TerminalOutcome::Complete),
                    ..
                }
        );
        chat.observe(observation).unwrap();
        if ready {
            chat.submit("first").unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if let Some(id) = requested {
            std::thread::sleep(Duration::from_millis(75));
            let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
            assert_eq!(
                log.lines().filter(|line| line.contains("ask_id")).count(),
                turns
            );
            let action = [Action::Submit, Action::Chat, Action::Cancel][turns];
            let answers = match action {
                Action::Submit => vec![
                    Answer {
                        selected: BTreeSet::from([0, 1]),
                        note: "both".into(),
                    },
                    Answer {
                        selected: BTreeSet::new(),
                        note: "fixture-secret".into(),
                    },
                ],
                Action::Chat => vec![
                    Answer {
                        selected: BTreeSet::from([1]),
                        note: "discuss".into(),
                    },
                    Answer {
                        selected: BTreeSet::new(),
                        note: String::new(),
                    },
                ],
                Action::Cancel => Vec::new(),
            };
            runtime
                .answer(Decision {
                    id,
                    action,
                    answers,
                })
                .unwrap();
        }
        if complete {
            assert!(chat.questions.is_empty());
            assert!(!chat.busy);
            turns += 1;
            if turns < 3 {
                chat.submit("follow-up").unwrap();
                runtime.submit(chat.take_submission().unwrap()).unwrap();
            }
        }
    }
    runtime.shutdown().unwrap();
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    let commands: Vec<serde_json::Value> = log
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(commands.len(), 7);
    let answers: Vec<_> = commands.iter().filter(|c| c["type"] == "answer").collect();
    assert_eq!(answers.len(), 3);
    assert_ne!(answers[0]["id"], answers[1]["id"]);
    assert_ne!(answers[0]["ask_id"], answers[1]["ask_id"]);
    assert_eq!(answers[2]["answers"], serde_json::json!([]));
    assert_eq!(commands[6]["type"], "shutdown");
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn source_question_cancellation_before_negative_ack_releases_form_and_turn() {
    use litetui_terminal::{
        live::{Observation, TerminalOutcome},
        live_chat::{Chat, ReplyState},
        questions::{Action, Decision, Event},
    };
    let (mut runtime, _) = question_runtime("question_cancelled");
    let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
    loop {
        let observation = runtime
            .events
            .recv_timeout(Duration::from_secs(10))
            .unwrap();
        let ready = matches!(observation, Observation::Ready);
        let requested = if let Observation::Question {
            event: Event::Requested(request),
            ..
        } = &observation
        {
            Some(request.id.clone())
        } else {
            None
        };
        let settled = matches!(
            &observation,
            Observation::Question {
                settled: Some(TerminalOutcome::ApprovalStopped(_)),
                ..
            }
        );
        chat.observe(observation).unwrap();
        if ready {
            chat.submit("first").unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if let Some(id) = requested {
            runtime
                .answer(Decision {
                    id,
                    action: Action::Cancel,
                    answers: Vec::new(),
                })
                .unwrap();
        }
        if settled {
            assert!(!chat.busy && chat.questions.is_empty());
            assert!(matches!(chat.rows[0].state, ReplyState::ApprovalStopped));
            break;
        }
    }
    runtime.shutdown().unwrap();
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn unanswered_question_keeps_deadline_without_automatic_answer() {
    use litetui_terminal::live::Observation;
    let (mut runtime, root) = question_runtime("question");
    let mut asked = false;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        assert!(Instant::now() < deadline);
        for observation in runtime.events.try_iter() {
            match observation {
                Observation::Ready => runtime.submit("test".into()).unwrap(),
                Observation::Question { .. } => asked = true,
                _ => {}
            }
        }
        if let Some(result) = runtime.poll_done() {
            assert!(asked);
            let error = result.unwrap_err().to_string();
            assert!(error.contains("Live deadline elapsed") && error.contains("cleanup=Ok"));
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!log.contains("ask_id"));
    assert!(log.contains("shutdown"));
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn one_shot_refuses_question_without_answering() {
    let (mut command, root) = setup("question");
    let output = command.output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("cannot answer"));
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert!(!log.contains("ask_id"));
    assert!(log.contains("shutdown"));
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn tool_enabled_launch_checks_effective_policy_before_any_prompt() {
    for field in [
        "tools_enabled",
        "tool_profile",
        "approval_route",
        "mcp_enabled",
    ] {
        let (mut command, root) = setup(&format!("policy_{field}"));
        let workspace = root
            .parent()
            .unwrap()
            .join("source")
            .canonicalize()
            .unwrap();
        command
            .args(["--tools", "on", "--workspace"])
            .arg(&workspace);
        let output = command.output().unwrap();
        assert!(!output.status.success(), "{field}");
        let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
        assert!(!log.contains("rust-live-prompt"), "{field}");
        assert!(log.contains("shutdown"));
    }
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn tool_enabled_launch_passes_explicit_settings_workspace_and_profile() {
    let (mut command, root) = setup_with_profile("success", "interactive");
    let workspace = root.parent().unwrap().join("workspace");
    fs::create_dir(&workspace).unwrap();
    command
        .args(["--tools", "on", "--workspace"])
        .arg(&workspace)
        .env("LITETUI_OWNER", "1")
        .env("LITETUI_SPAWN_IDENTITY", "1")
        .env("LITEHARNESS_SPAWNED_BY", "must-not-inherit");
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let settings: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(root.join("settings.json")).unwrap()).unwrap();
    assert_eq!(settings["tools_enabled"], true);
    assert_eq!(settings["tool_policy_profile"], "interactive");
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    let prompt: serde_json::Value = serde_json::from_str(log.lines().next().unwrap()).unwrap();
    assert_eq!(prompt["tool_profile"], "interactive");
}

#[test]
#[ignore = "real Python authority/store with fake provider; requires LITETUI_AUTHORITY_PYTHON"]
fn rust_decisions_cross_real_python_authority_and_owned_store_before_fixture_writes() {
    use litetui_terminal::{approvals::Event, live::Observation, live_chat::Chat};
    let (runtime, root, workspace) = authority_runtime("authority");
    let result = runtime.with_cleanup(|runtime| {
        let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
        let mut turns = 0;
        while turns < 3 {
            let observation = runtime
                .events
                .recv_timeout(Duration::from_secs(20))
                .map_err(|error| std::io::Error::other(error.to_string()))?;
            let ready = matches!(observation, Observation::Ready);
            let asked = matches!(observation, Observation::Approval(Event::Requested(_)));
            let complete = matches!(
                observation,
                Observation::Complete | Observation::Cancelled(_)
            );
            chat.observe(observation)?;
            if ready {
                chat.submit("cancel the fixture write")?;
                runtime.submit(chat.take_submission().unwrap())?;
            }
            if asked {
                assert!(!workspace.join("controlled-write.txt").exists());
                if turns == 0 {
                    chat.request_stop()?;
                    runtime.abort(chat.take_abort().unwrap())?;
                } else {
                    chat.approvals.choose(turns == 2);
                    runtime.approve(chat.approvals.take_decision().unwrap())?;
                }
            }
            if complete {
                assert_eq!(
                    chat.rows.last().unwrap().state,
                    if turns == 0 {
                        litetui_terminal::live_chat::ReplyState::Cancelled
                    } else {
                        litetui_terminal::live_chat::ReplyState::Complete
                    }
                );
                assert_eq!(workspace.join("controlled-write.txt").exists(), turns == 2);
                assert!(chat.approvals.is_empty());
                turns += 1;
                if turns < 3 {
                    chat.submit(if turns == 1 {
                        "deny the separate fixture write"
                    } else {
                        "allow the separate fixture write"
                    })?;
                    runtime.submit(chat.take_submission().unwrap())?;
                }
            }
        }
        Ok(())
    });
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(
        fs::read_to_string(workspace.join("controlled-write.txt")).unwrap(),
        "controlled fixture"
    );
    let store = fs::read_to_string(root.join("authority-store-path.txt")).unwrap();
    let persisted = fs::read_to_string(store).unwrap();
    assert!(
        persisted.contains("approved")
            && persisted.contains("denied")
            && persisted.contains("cancelled")
    );
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert_eq!(commands.lines().count(), 7);
    assert_eq!(commands.matches(r#""type": "abort""#).count(), 1);
    assert!(commands.lines().last().unwrap().contains("shutdown"));
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn stopped_turns_keep_one_owned_child_and_require_explicit_followup() {
    use litetui_terminal::{
        live::Observation,
        live_chat::{Chat, ReplyState},
    };
    for mode in [
        "cancel_stream",
        "cancel_question",
        "cancel_approval",
        "cancel_force",
    ] {
        let (mut runtime, root) = question_runtime(mode);
        let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
        let mut requested = false;
        let mut forced = false;
        let mut cancelled = false;
        let mut completed = false;
        let deadline = Instant::now() + Duration::from_secs(10);
        while !completed {
            assert!(Instant::now() < deadline, "{mode}");
            let event = runtime.events.recv_timeout(Duration::from_secs(5)).unwrap();
            let ready = matches!(&event, Observation::Ready);
            let trigger = matches!(
                &event,
                Observation::Content { .. }
                    | Observation::Question { .. }
                    | Observation::Approval(_)
            );
            let stop_ack = matches!(&event,Observation::Information(text) if text.starts_with("Stop requested"));
            let stopped = matches!(&event, Observation::Cancelled(_));
            completed = matches!(&event, Observation::Complete);
            chat.observe(event).unwrap();
            if ready {
                chat.submit("first").unwrap();
                runtime.submit(chat.take_submission().unwrap()).unwrap();
            }
            if trigger && !requested {
                chat.request_stop().unwrap();
                runtime.abort(chat.take_abort().unwrap()).unwrap();
                requested = true;
                assert!(chat.busy);
            }
            if stop_ack && mode == "cancel_force" && !forced {
                assert!(chat.busy, "ACK alone must not finish turn");
                chat.request_stop().unwrap();
                runtime.abort(chat.take_abort().unwrap()).unwrap();
                forced = true;
            }
            if stopped {
                cancelled = true;
                assert_eq!(chat.rows[0].state, ReplyState::Cancelled);
                assert!(!chat.busy && chat.approvals.is_empty() && chat.questions.is_empty());
                assert!(
                    chat.take_submission().is_none(),
                    "never replay cancelled prompt"
                );
                if mode == "cancel_stream" {
                    // Settled cancellation releases the old two-second turn deadline.
                    std::thread::sleep(Duration::from_millis(2200));
                }
                // Delayed first-turn intent must not interrupt the second turn.
                chat.submit("explicit follow-up").unwrap();
                runtime.submit(chat.take_submission().unwrap()).unwrap();
                runtime.abort("rust-live-prompt".into()).unwrap();
            }
        }
        runtime.quit().unwrap();
        runtime.shutdown().unwrap();
        assert!(cancelled && requested && (mode != "cancel_force" || forced));
        assert_eq!(chat.rows.len(), 2);
        assert_eq!(chat.rows[1].state, ReplyState::Complete);
        if mode == "cancel_stream" || mode == "cancel_force" {
            assert_eq!(chat.rows[0].cards[0].answer, "Partial fixture output");
        }
        let commands: Vec<serde_json::Value> = fs::read_to_string(root.join("commands.jsonl"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(commands.iter().filter(|c| c["type"] == "prompt").count(), 2);
        assert_eq!(
            commands.iter().filter(|c| c["type"] == "abort").count(),
            if forced { 2 } else { 1 }
        );
        assert!(
            !commands
                .iter()
                .any(|c| c["type"] == "answer" || c["type"] == "approve")
        );
        assert_eq!(commands.last().unwrap()["type"], "shutdown");
    }
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn stop_ack_without_terminal_keeps_deadline_and_never_becomes_completion() {
    use litetui_terminal::{live::Observation, live_chat::Chat};
    let (mut runtime, root) = question_runtime("cancel_hang");
    let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
    let start = Instant::now();
    while let Ok(event) = runtime.events.recv_timeout(Duration::from_secs(5)) {
        let ready = matches!(&event, Observation::Ready);
        let text = matches!(&event, Observation::Content { .. });
        assert!(!matches!(
            &event,
            Observation::Complete | Observation::Cancelled(_)
        ));
        chat.observe(event).unwrap();
        if ready {
            chat.submit("hang").unwrap();
            runtime.submit(chat.take_submission().unwrap()).unwrap();
        }
        if text {
            chat.request_stop().unwrap();
            runtime.abort(chat.take_abort().unwrap()).unwrap();
        }
    }
    assert!(chat.busy);
    assert!(
        runtime
            .shutdown()
            .unwrap_err()
            .to_string()
            .contains("deadline")
    );
    assert!(start.elapsed() < Duration::from_secs(8));
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert_eq!(commands.matches(r#""type": "prompt""#).count(), 1);
    assert!(commands.contains("shutdown"));
}

fn missing_profile_args(command: &Command) -> Vec<String> {
    command
        .get_args()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .chunks(2)
        .filter(|pair| pair[0] != "--tool-profile")
        .flatten()
        .cloned()
        .collect()
}
#[test]
#[ignore = "controlled Python child tripwire, requires LITETUI_TEST_PYTHON"]
fn missing_or_cancelled_profile_never_activates_runtime_or_creates_state() {
    use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
    use litetui_terminal::{
        live::{Interactive, Options},
        profile_picker::{Picker, Selection},
    };
    for cancelled in [false, true] {
        let (command, root) = setup("success");
        let options = Options::parse(&missing_profile_args(&command)).unwrap();
        if cancelled {
            let mut picker = Picker::default();
            assert_eq!(
                picker.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE))),
                Selection::Cancelled
            );
        }
        // Even bypassing the normal frontend, the runtime boundary must refuse.
        let mut runtime = Interactive::start(options);
        assert!(
            runtime
                .shutdown()
                .unwrap_err()
                .to_string()
                .contains("Select a tool profile")
        );
        assert!(runtime.events.try_iter().next().is_none());
        assert!(
            !root.exists(),
            "no runtime settings, hooks, jobs or plugins may activate"
        );
        assert!(!root.parent().unwrap().join("python-started").exists());
    }
}
#[test]
#[ignore = "controlled Python child tripwire, requires LITETUI_TEST_PYTHON"]
fn redirected_missing_profile_fails_before_spawn_but_explicit_profile_runs() {
    let (command, root) = setup("success");
    let missing = missing_profile_args(&command);
    let output = Command::new(command.get_program())
        .arg("--live")
        .args(&missing)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("Redirected launch requires --tool-profile")
    );
    assert!(!root.exists() && !root.parent().unwrap().join("python-started").exists());
    let mut empty_prompt = missing.clone();
    let prompt = empty_prompt
        .iter()
        .position(|arg| arg == "--prompt")
        .unwrap()
        + 1;
    empty_prompt[prompt].clear();
    let invalid = Command::new(command.get_program())
        .arg("--live")
        .args(&empty_prompt)
        .output()
        .unwrap();
    assert!(!invalid.status.success());
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("--live requires --prompt"));
    assert!(!root.exists() && !root.parent().unwrap().join("python-started").exists());
    let output = Command::new(command.get_program())
        .arg("--live")
        .args(&missing)
        .args(["--tool-profile", "strict"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(root.exists() && root.parent().unwrap().join("python-started").exists());
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn management_faults_never_admit_a_prompt_or_dump_snapshot_settings() {
    for mode in [
        "management_bad_version",
        "management_hello_timeout",
        "management_state_busy",
        "management_state_identity",
        "management_state_malformed",
        "management_activation_race",
    ] {
        let (mut command, root) = setup(mode);
        let result = command.output().unwrap();
        assert!(!result.status.success(), "{mode}");
        let commands = fs::read_to_string(root.join("all-commands.jsonl")).unwrap();
        assert!(!commands.contains("rust-live-prompt"), "{mode}");
        assert!(commands.contains("shutdown"), "{mode}");
        assert!(!String::from_utf8_lossy(&result.stdout).contains("do-not-display"));
        assert!(!String::from_utf8_lossy(&result.stderr).contains("do-not-display"));
    }
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn management_reads_precede_prompt_and_selected_envelopes_do_not_double_text() {
    let (mut command, root) = setup("success");
    let result = command.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let commands: Vec<serde_json::Value> = fs::read_to_string(root.join("all-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(
        commands
            .iter()
            .map(|v| v["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["gui.hello", "gui.state", "prompt", "shutdown"]
    );
    let output = String::from_utf8_lossy(&result.stdout);
    assert_eq!(output.matches("After tool").count(), 1);
    assert!(!output.contains("do-not-display"));
}
#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn management_origin_drift_stops_output_and_cleans_up_without_replay() {
    let (mut command, root) = setup("management_wrong_origin");
    let result = command.output().unwrap();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("another prompt"));
    let commands = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert_eq!(commands.matches("rust-live-prompt").count(), 1);
    assert!(commands.contains("shutdown"));
}

#[test]
#[ignore = "controlled Python child, requires LITETUI_TEST_PYTHON"]
fn unpaired_gui_event_while_idle_has_a_bounded_failure_deadline() {
    use litetui_terminal::live::Observation;
    let (mut runtime, root) = question_runtime("management_unpaired_idle");
    let until = Instant::now() + Duration::from_secs(7);
    let mut complete = false;
    let mut outcome = None;
    while Instant::now() < until {
        for event in runtime.events.try_iter() {
            match event {
                Observation::Ready => runtime.submit("fixture".into()).unwrap(),
                Observation::Complete => complete = true,
                _ => {}
            }
        }
        if let Some(result) = runtime.poll_done() {
            outcome = Some(result);
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = runtime.shutdown(); // Also clean up the failing negative control.
    assert!(complete);
    let error = outcome
        .expect("unpaired envelope remained silently idle")
        .unwrap_err()
        .to_string();
    assert!(
        error.contains("GUI envelope deadline") && error.contains("cleanup=Ok"),
        "{error}"
    );
    let log = fs::read_to_string(root.join("commands.jsonl")).unwrap();
    assert_eq!(log.matches("rust-live-prompt").count(), 1);
    assert!(log.contains("shutdown"));
}

fn authority_runtime(mode: &str) -> (litetui_terminal::live::Interactive, PathBuf, PathBuf) {
    use litetui_terminal::live::{Interactive, Options};
    let python = std::env::var_os("LITETUI_AUTHORITY_PYTHON")
        .expect("explicit Python with LiteTUI dependencies");
    // setup itself uses the stdlib fixture interpreter variable.
    let (command, root) = setup("success");
    let source = root.parent().unwrap().join("source");
    let actual = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/litetui")
        .canonicalize()
        .unwrap();
    fs::write(
        source.join("src/litetui/__init__.py"),
        format!(
            "__path__.append({})\n",
            serde_json::to_string(&actual.to_string_lossy()).unwrap()
        ),
    )
    .unwrap();
    fs::write(
        source.join("src/litetui/cli.py"),
        include_str!("fixtures/native_authority_runtime.py")
            .replace("MODE = \"authority\"", &format!("MODE = {mode:?}")),
    )
    .unwrap();
    let workspace = root.parent().unwrap().join("workspace");
    fs::create_dir(&workspace).unwrap();
    let mut args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    args[1] = python.to_string_lossy().into_owned();
    args.extend([
        "--tools".into(),
        "on".into(),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
    ]);
    let mut options = Options::parse(&args).unwrap();
    options.timeout = Duration::from_secs(15);
    let runtime = Interactive::start(options);
    (runtime, root, workspace)
}

#[test]
#[ignore = "real Python management/authority with controlled provider; requires LITETUI_AUTHORITY_PYTHON"]
fn model_and_thinking_controls_reach_source_setters_persist_and_drive_next_turn() {
    use litetui_terminal::{
        live::Observation,
        live_chat::Chat,
        model_controls::{Intent, Kind, Update},
    };
    for mode in ["controls", "controls_refuse"] {
        let (runtime, root, _) = authority_runtime(mode);
        let mut expected_level = String::new();
        let result = runtime.with_cleanup(|runtime| {
            let mut chat = Chat::new("claude".into(), "claude-sonnet-5-5".into());
            let mut phase = 0;
            loop {
                let event = runtime
                    .events
                    .recv_timeout(Duration::from_secs(20))
                    .map_err(std::io::Error::other)?;
                let ready = matches!(&event, Observation::Ready);
                let complete = matches!(&event, Observation::Complete);
                let menu = if let Observation::Controls(Update::Choices(menu)) = &event {
                    Some(menu.clone())
                } else {
                    None
                };
                chat.observe(event)?;
                if ready {
                    runtime.control(Intent::Open(Kind::Model))?;
                    phase = 1;
                }
                if let Some(menu) = menu {
                    match phase {
                        1 => {
                            runtime.control(Intent::Select {
                                revision: menu.revision,
                                value: "claude-opus-5".into(),
                            })?;
                            phase = 2;
                        }
                        2 => {
                            assert_eq!(menu.current, "claude-opus-5");
                            assert_eq!(chat.model, "claude-opus-5");
                            runtime.control(Intent::Open(Kind::Thinking))?;
                            phase = 3;
                        }
                        3 => {
                            expected_level = if mode == "controls" {
                                "high".into()
                            } else {
                                menu.current.clone()
                            };
                            runtime.control(Intent::Select {
                                revision: menu.revision,
                                value: "high".into(),
                            })?;
                            phase = 4;
                        }
                        4 => {
                            assert_eq!(menu.current, expected_level.as_str());
                            if mode == "controls_refuse" {
                                assert!(
                                    menu.message
                                        .unwrap()
                                        .contains("controlled owned-write refusal")
                                );
                            }
                            chat.submit("report receiving state")?;
                            runtime.submit(chat.take_submission().unwrap())?;
                            phase = 5;
                        }
                        _ => panic!("unexpected control response"),
                    }
                }
                if complete {
                    assert_eq!(phase, 5);
                    assert_eq!(
                        chat.rows[0].cards[0].answer,
                        format!("claude-opus-5 / {}", expected_level.as_str())
                    );
                    break;
                }
            }
            Ok(())
        });
        assert!(result.is_ok(), "{result:?}");
        let path = fs::read_to_string(root.join("control-settings-path.txt")).unwrap();
        let settings: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        assert_eq!(settings["execution"]["model"], "claude-opus-5");
        assert_eq!(
            settings["execution"]["thinking_level"],
            expected_level.as_str()
        );
        let commands: Vec<serde_json::Value> = fs::read_to_string(root.join("all-commands.jsonl"))
            .unwrap()
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        for kind in ["gui.models.select", "gui.thinking.set", "prompt"] {
            assert_eq!(commands.iter().filter(|v| v["type"] == kind).count(), 1);
        }
        assert_eq!(commands.last().unwrap()["type"], "shutdown");
    }
}

#[test]
#[ignore = "real CLI and owned saved store with controlled provider; requires LITETUI_AUTHORITY_PYTHON"]
fn saved_conversation_reopens_same_owned_store_before_next_explicit_prompt() {
    saved_conversation_process(SavedInput::None);
}

#[test]
#[ignore = "controlled Python process; no provider"]
fn restored_provider_input_stays_held_before_ready_and_during_an_explicit_turn() {
    saved_conversation_process(SavedInput::Held);
}

#[test]
#[ignore = "controlled Python process; no provider"]
fn held_input_cancellation_is_saved_and_read_back_before_an_explicit_turn() {
    saved_conversation_process(SavedInput::Cancel);
}

#[test]
#[ignore = "controlled Python process; no provider"]
fn held_input_continuation_refuses_then_explicitly_runs_and_waits_for_retirement() {
    saved_conversation_process(SavedInput::Continue);
}

#[test]
#[ignore = "controlled Python process; no provider"]
fn held_input_reconciliation_requires_positive_history_and_never_resends() {
    saved_conversation_process(SavedInput::Reconcile);
}
#[test]
#[ignore = "controlled real Python CLI restart, no provider"]
fn held_claude_input_is_inspected_and_cancelled_without_replay() {
    saved_conversation_process(SavedInput::ClaudeCancel);
}

#[test]
#[ignore = "controlled real Python CLI restart, no provider"]
fn held_claude_uncertainty_is_explicitly_resolved_without_replay() {
    saved_conversation_process(SavedInput::ClaudeResolve);
}

#[derive(Clone, Copy)]
enum SavedInput {
    None,
    ClaudeCancel,
    ClaudeResolve,
    Held,
    Cancel,
    Continue,
    Reconcile,
}

fn saved_conversation_process(mode: SavedInput) {
    let held = !matches!(mode, SavedInput::None);
    let claude_held = matches!(mode, SavedInput::ClaudeCancel | SavedInput::ClaudeResolve);
    let resolve_held = matches!(mode, SavedInput::ClaudeResolve);
    let cancel_held = matches!(mode, SavedInput::Cancel | SavedInput::ClaudeCancel);
    let continue_held = matches!(mode, SavedInput::Continue);
    let reconcile_held = matches!(mode, SavedInput::Reconcile);
    let removed = cancel_held || resolve_held || continue_held || reconcile_held;
    use litetui_terminal::live::{Interactive, Observation, Options};
    use litetui_terminal::live_chat::Chat;
    let python = std::env::var_os("LITETUI_AUTHORITY_PYTHON").expect("dependency Python");
    let (command, root) = setup("success");
    let source = root.parent().unwrap().join("source");
    let actual = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/litetui")
        .canonicalize()
        .unwrap();
    fs::write(
        source.join("src/litetui/__init__.py"),
        format!(
            "__path__.append({})\n",
            serde_json::to_string(&actual.to_string_lossy()).unwrap()
        ),
    )
    .unwrap();
    fs::write(
        source.join("src/litetui/cli.py"),
        include_str!("fixtures/resume_runtime.py"),
    )
    .unwrap();
    if claude_held {
        fs::write(
            source.join("src/litetui/fixture-claude-input"),
            "controlled fixture",
        )
        .unwrap();
    }
    if held && !claude_held {
        fs::write(
            source.join("src/litetui/fixture-held-input"),
            "controlled fixture",
        )
        .unwrap();
    }
    if continue_held {
        fs::write(
            source.join("src/litetui/fixture-continue-input"),
            "controlled fixture",
        )
        .unwrap();
    }
    if reconcile_held {
        fs::write(
            source.join("src/litetui/fixture-reconcile-input"),
            "controlled fixture",
        )
        .unwrap();
    }
    let backend = if held && !claude_held {
        "codex"
    } else {
        "claude"
    };
    let model = if held && !claude_held {
        "gpt-6-astra"
    } else {
        "claude-sonnet-5-5"
    };
    let mut args: Vec<String> = command
        .get_args()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    args[1] = python.to_string_lossy().into_owned();
    for (flag, value) in [("--backend", backend), ("--model", model)] {
        let index = args.iter().position(|arg| arg == flag).unwrap();
        args[index + 1] = value.into();
    }
    let mut session = String::new();
    let mut agent = String::new();
    let mut saved_settings = Vec::new();
    for pass in 0..2 {
        let mut options = Options::parse(&args).unwrap();
        options.timeout = Duration::from_secs(15);
        options.resume = if pass == 0 {
            None
        } else {
            Some(session.clone())
        };
        let runtime = Interactive::start(options);
        runtime
            .with_cleanup(|runtime| {
                let mut chat = Chat::new(backend.into(), model.into());
                let mut history_seen = false;
                loop {
                    let event = runtime
                        .events
                        .recv_timeout(Duration::from_secs(20))
                        .map_err(std::io::Error::other)?;
                    let ready = matches!(&event, Observation::Ready);
                    let done = matches!(&event, Observation::Complete);
                    if let Observation::History(h) = &event {
                        assert_eq!(pass, 1);
                        assert_eq!(h.session, session);
                        assert_eq!(h.messages.len(), 2);
                        assert_eq!(h.messages[0].text, "remember SAVED-CONTEXT");
                        assert_eq!(h.messages[1].text, "Saved first reply");
                        history_seen = true;
                    }
                    chat.observe(event)?;
                    if ready {
                        assert_eq!(history_seen, pass == 1);
                        let snapshot = chat.runtime_state.startup().unwrap();
                        assert_eq!(
                            snapshot.held_restored_input,
                            Some(if held && pass == 1 { 3 } else { 0 })
                        );
                        if held && pass == 1 {
                            assert!(chat.notice.contains("Restored input held: 3"));
                        }
                        if pass == 0 {
                            session = snapshot.session.clone();
                        } else {
                            assert_eq!(snapshot.session, session);
                        }
                        assert!(chat.take_submission().is_none());
                        if pass == 1 {
                            let mut competing = Options::parse(&args).unwrap();
                            competing.resume = Some(session.clone());
                            competing.timeout = Duration::from_secs(10);
                            let error = refused_resume(competing);
                            assert!(error.contains("Resource owned or inaccessible"), "{error}");
                        }
                        if (cancel_held || resolve_held) && pass == 1 {
                            use litetui_terminal::restored_queue::{Intent, Update};
                            runtime.queue(Intent::Open)?;
                            let mut cancelling = false;
                            let mut cancelled_id = String::new();
                            loop {
                                let event = runtime
                                    .events
                                    .recv_timeout(Duration::from_secs(20))
                                    .map_err(std::io::Error::other)?;
                                let menu = match &event {
                                    Observation::Queue(Update::Snapshot(menu)) => {
                                        Some(menu.clone())
                                    }
                                    Observation::Queue(Update::Rejected(error)) => {
                                        panic!("{error}")
                                    }
                                    _ => None,
                                };
                                chat.observe(event)?;
                                if let Some(menu) = menu {
                                    if cancelling {
                                        assert_eq!(menu.entries.len(), 2);
                                        assert!(menu.recovered.is_none() && chat.rows.is_empty());
                                        assert!(!menu.entries.iter().any(|e| e.id == cancelled_id));
                                        assert_eq!(
                                            chat.runtime_state
                                                .startup()
                                                .unwrap()
                                                .held_restored_input,
                                            Some(2)
                                        );
                                        break;
                                    }
                                    assert_eq!(menu.entries.len(), 3);
                                    if claude_held {
                                        assert_eq!(menu.entries[0].provider, "claude");
                                        assert_eq!(menu.entries[0].state, "prepared");
                                        assert!(menu.entries[0].can_cancel);
                                        for entry in &menu.entries[1..] {
                                            assert_eq!(entry.state, "uncertain");
                                            assert!(
                                                !entry.can_cancel
                                                    && !entry.can_continue
                                                    && !entry.can_reconcile
                                            );
                                        }
                                    } else {
                                        assert_eq!(menu.entries[0].id, "saved-queued");
                                    }
                                    assert!(
                                        chat.rows.is_empty(),
                                        "Recovery invented an execution row"
                                    );
                                    cancelled_id =
                                        menu.entries[usize::from(resolve_held)].id.clone();
                                    runtime.queue(if resolve_held {
                                        assert!(menu.entries[1].can_resolve);
                                        Intent::Resolve {
                                            revision: menu.revision,
                                            entry: cancelled_id.clone(),
                                        }
                                    } else {
                                        Intent::Cancel {
                                            revision: menu.revision,
                                            entry: cancelled_id.clone(),
                                        }
                                    })?;
                                    cancelling = true;
                                }
                            }
                        }
                        if continue_held && pass == 1 {
                            continue_saved_input(runtime, &mut chat)?;
                        }
                        if reconcile_held && pass == 1 {
                            reconcile_saved_input(runtime, &mut chat)?;
                        }
                        let prompt = if pass == 0 {
                            "remember SAVED-CONTEXT"
                        } else {
                            "what was saved?"
                        };
                        chat.submit(prompt)?;
                        runtime.submit(chat.take_submission().unwrap())?;
                    }
                    if done {
                        if pass == 1 {
                            assert_eq!(
                                chat.rows.last().unwrap().cards[0].answer,
                                if continue_held {
                                    "Earlier: remember SAVED-CONTEXT | UNSENT queued"
                                } else if reconcile_held {
                                    "Earlier: remember SAVED-CONTEXT | UNSENT sending"
                                } else {
                                    "Earlier: remember SAVED-CONTEXT"
                                }
                            );
                        }
                        break;
                    }
                }
                Ok(())
            })
            .unwrap();
        let launches: Vec<serde_json::Value> = fs::read_to_string(root.join("launches.jsonl"))
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let current = launches.last().unwrap();
        if pass == 0 {
            agent = current["agent"].as_str().unwrap().into();
            saved_settings = fs::read(root.join("settings.json")).unwrap();
        } else {
            assert_eq!(current["agent"], agent);
            assert_eq!(current["thinking"], "high");
            assert_eq!(
                fs::read(root.join("settings.json")).unwrap(),
                saved_settings
            );
            let argv = current["argv"].as_array().unwrap();
            assert!(!argv.iter().any(|v| v == "--thinking-level"));
            assert!(argv.iter().any(|v| v == "--hold-restored-input"));
            assert!(argv.iter().any(|v| v == "--convo"));
        }
    }
    if held {
        let proof: serde_json::Value =
            serde_json::from_slice(&fs::read(root.join("held-input-proof.json")).unwrap()).unwrap();
        assert_eq!(
            proof["policy"],
            serde_json::json!({"policy":"hold","held":if removed {2} else {3}})
        );
        assert_eq!(
            proof["explicit_prompts"],
            if continue_held {
                serde_json::json!(["remember SAVED-CONTEXT", "UNSENT queued", "what was saved?"])
            } else if reconcile_held {
                serde_json::json!([
                    "remember SAVED-CONTEXT",
                    "UNSENT sending",
                    "what was saved?"
                ])
            } else {
                serde_json::json!(["remember SAVED-CONTEXT", "what was saved?"])
            }
        );
        let entries = proof["entries"].as_array().unwrap();
        assert_eq!(entries.len(), if removed { 2 } else { 3 });
        let states = if resolve_held {
            vec!["prepared", "uncertain"]
        } else if claude_held {
            vec!["uncertain", "uncertain"]
        } else if reconcile_held {
            vec!["queued", "accepted"]
        } else if cancel_held || continue_held {
            vec!["sending", "accepted"]
        } else {
            vec!["queued", "sending", "accepted"]
        };
        for (entry, state) in entries.iter().zip(states) {
            assert_eq!(entry["state"], state);
            if claude_held && state == "prepared" {
                assert_eq!(entry["content"], "UNSENT prepared");
            } else if claude_held {
                assert!(matches!(
                    entry["uncertain_from"].as_str(),
                    Some("submitted" | "acknowledged")
                ));
                assert_eq!(
                    entry["content"],
                    format!("UNSENT {}", entry["uncertain_from"].as_str().unwrap())
                );
            } else {
                assert_eq!(entry["revision"], 7);
                assert_eq!(entry["item"]["content"], format!("UNSENT {state}"));
            }
        }
        if claude_held {
            let selected = proof["journal"]["selected_segment"].as_str().unwrap();
            let saved = &proof["journal"]["segments"][selected]["entries"];
            let closed = &saved[usize::from(resolve_held)];
            assert_eq!(closed["state"], "terminal");
            assert_eq!(
                closed["stop_reason"],
                if resolve_held {
                    "user_resolved_without_replay"
                } else {
                    "user_cancelled_before_send"
                }
            );
            assert_eq!(
                closed["content"],
                if resolve_held {
                    "UNSENT submitted"
                } else {
                    "UNSENT prepared"
                }
            );
        }
    }
    let saved_commands = fs::read(root.join("resume-commands.jsonl")).unwrap();
    for (id, model, expected) in [
        (
            "12345678-1234-4123-8123-123456789abc",
            model,
            "Owned conversation lookup refused",
        ),
        (session.as_str(), "wrong-model", "Launch override disagrees"),
    ] {
        let mut options = Options::parse(&args).unwrap();
        options.resume = Some(id.into());
        options.model = model.into();
        let error = refused_resume(options);
        assert!(error.contains(expected), "{error}");
        assert_eq!(
            fs::read(root.join("settings.json")).unwrap(),
            saved_settings
        );
        assert_eq!(
            fs::read(root.join("resume-commands.jsonl")).unwrap(),
            saved_commands
        );
    }
    let transcript = fs::read_to_string(root.join("resume-transcript-path.txt")).unwrap();
    let records: Vec<serde_json::Value> = fs::read_to_string(transcript)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let prompts: Vec<&str> = records
        .iter()
        .filter(|v| v["type"] == "msg" && v["message"]["role"] == "user")
        .map(|v| v["message"]["content"].as_str().unwrap())
        .collect();
    assert_eq!(
        prompts,
        if continue_held {
            vec!["remember SAVED-CONTEXT", "UNSENT queued", "what was saved?"]
        } else if reconcile_held {
            vec![
                "remember SAVED-CONTEXT",
                "UNSENT sending",
                "what was saved?",
            ]
        } else {
            vec!["remember SAVED-CONTEXT", "what was saved?"]
        }
    );
    let commands: Vec<serde_json::Value> = fs::read_to_string(root.join("resume-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(commands.iter().filter(|v| v["type"] == "prompt").count(), 2);
    if reconcile_held {
        let receipts: Vec<serde_json::Value> =
            fs::read_to_string(root.join("reconcile-receipts.jsonl"))
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
        assert_eq!(receipts.len(), 2);
        assert_eq!(receipts[0]["reconciled"], false);
        assert_eq!(receipts[1]["reconciled"], true);
        assert_eq!(
            commands
                .iter()
                .filter(|v| v["type"] == "gui.restored_input.reconcile")
                .count(),
            2
        );
        let methods: Vec<String> = fs::read_to_string(root.join("history-requests.jsonl"))
            .unwrap()
            .lines()
            .map(|line| {
                serde_json::from_str::<serde_json::Value>(line).unwrap()["method"]
                    .as_str()
                    .unwrap()
                    .into()
            })
            .collect();
        assert_eq!(
            methods,
            [
                "thread/read",
                "thread/turns/list",
                "thread/read",
                "thread/turns/list"
            ]
        );
        let delivered: Vec<_> = records
            .iter()
            .filter(|v| v["message"]["codex_delivery"]["id"] == "saved-sending")
            .collect();
        assert_eq!(delivered.len(), 1);
        assert_eq!(
            delivered[0]["message"]["codex_delivery"]["state"],
            "accepted"
        );
    }
    if continue_held {
        let continued: Vec<_> = commands
            .iter()
            .filter(|v| v["type"] == "gui.restored_input.continue")
            .collect();
        let settled: Vec<_> = commands
            .iter()
            .filter(|v| v["type"] == "gui.restored_input.settled")
            .collect();
        assert_eq!(continued.len(), 2);
        assert_eq!(settled.len(), 2);
        assert_ne!(continued[0]["id"], continued[1]["id"]);
        assert_ne!(continued[0]["snapshot"], continued[1]["snapshot"]);
        for (sent, read) in continued.iter().zip(settled.iter()) {
            assert_eq!(sent["id"], read["operation_id"]);
            assert!(sent.get("message").is_none() && sent.get("tool_profile").is_none());
        }
        let proof: serde_json::Value =
            serde_json::from_slice(&fs::read(root.join("queue-continue-proof.json")).unwrap())
                .unwrap();
        assert_eq!(proof["source"], "queued");
        assert_eq!(proof["profile"], "strict");
        assert_eq!(proof["operation"], continued[1]["id"]);
        assert_eq!(proof["delivered"].as_array().unwrap().len(), 1);
        assert_eq!(proof["delivered"][0]["content"], "UNSENT queued");
        let receipts: Vec<serde_json::Value> =
            fs::read_to_string(root.join("continue-receipts.jsonl"))
                .unwrap()
                .lines()
                .map(|l| serde_json::from_str(l).unwrap())
                .collect();
        assert_eq!(receipts[0]["turn"], "refused");
        assert_eq!(receipts[1]["turn"], "accepted");
    }
    if cancel_held || resolve_held {
        assert_eq!(
            commands
                .iter()
                .filter(|v| v["type"]
                    == if resolve_held {
                        "gui.restored_input.resolve"
                    } else {
                        "gui.restored_input.cancel"
                    })
                .count(),
            1
        );
        assert_eq!(
            commands
                .iter()
                .filter(|v| v["type"] == "gui.restored_input.list")
                .count(),
            2
        );
        assert!(
            root.join(if resolve_held {
                "queue-resolve-proof.json"
            } else {
                "queue-cancel-proof.json"
            })
            .exists()
        );
    }
    assert_eq!(
        commands
            .iter()
            .filter(|v| v["type"] == "gui.conversations.read")
            .count(),
        1
    );
    assert_eq!(
        commands.iter().filter(|v| v["type"] == "shutdown").count(),
        2
    );
    // An admitted owned resume still cannot enable the composer without history.
    fs::write(
        root.join("hold-history"),
        "controlled missing read response",
    )
    .unwrap();
    let before = fs::read(root.join("resume-commands.jsonl")).unwrap();
    let mut timeout = Options::parse(&args).unwrap();
    timeout.resume = Some(session.clone());
    // This deadline includes Python imports and owned startup. Four seconds
    // sometimes expired before gui.hello, measuring startup instead of the
    // deliberately missing history response. Keep explicit phase assertions.
    timeout.timeout = Duration::from_secs(15);
    let error = refused_resume(timeout);
    assert!(
        error.contains("deadline elapsed") && error.contains("cleanup=Ok"),
        "{error}"
    );
    let after = fs::read(root.join("resume-commands.jsonl")).unwrap();
    let suffix = String::from_utf8_lossy(&after[before.len()..]);
    assert!(
        suffix.contains("gui.conversations.read") && suffix.contains("shutdown"),
        "Expected history-stage timeout: {error}; commands after hold: {suffix}"
    );
    assert!(!suffix.contains("\"type\": \"prompt\""));
}

fn reconcile_saved_input(
    runtime: &litetui_terminal::live::Interactive,
    chat: &mut litetui_terminal::live_chat::Chat,
) -> std::io::Result<()> {
    use litetui_terminal::{
        live::Observation,
        live_chat::ReplyState,
        restored_queue::{Intent, Update},
    };
    runtime.queue(Intent::Open)?;
    let mut attempts = 0;
    loop {
        let event = runtime
            .events
            .recv_timeout(Duration::from_secs(20))
            .map_err(std::io::Error::other)?;
        assert!(!matches!(
            event,
            Observation::Submission { .. }
                | Observation::Complete
                | Observation::Text(_)
                | Observation::Refused(_)
        ));
        let menu = if let Observation::Queue(Update::Snapshot(m)) = &event {
            Some(m.clone())
        } else {
            None
        };
        chat.observe(event)?;
        if let Some(menu) = menu {
            assert!(!chat.busy && chat.take_submission().is_none());
            if attempts == 2 {
                assert_eq!(menu.entries.len(), 2);
                assert!(!menu.entries.iter().any(|e| e.id == "saved-sending"));
                assert_eq!(chat.rows.len(), 1);
                assert_eq!(chat.rows[0].state, ReplyState::Reconciled);
                break;
            }
            assert!(chat.rows.is_empty());
            let entry = menu
                .entries
                .iter()
                .find(|e| e.id == "saved-sending")
                .unwrap();
            assert!(entry.can_reconcile);
            let intent = Intent::Reconcile {
                revision: menu.revision,
                entry: entry.id.clone(),
            };
            chat.prepare_reconciliation(&intent)?;
            runtime.queue(intent)?;
            attempts += 1;
        }
    }
    Ok(())
}

fn continue_saved_input(
    runtime: &litetui_terminal::live::Interactive,
    chat: &mut litetui_terminal::live_chat::Chat,
) -> std::io::Result<()> {
    use litetui_terminal::{
        live::Observation,
        live_chat::ReplyState,
        restored_queue::{Intent, Update},
    };
    runtime.queue(Intent::Open)?;
    let mut attempts = 0;
    let mut terminal = false;
    loop {
        let event = runtime
            .events
            .recv_timeout(Duration::from_secs(20))
            .map_err(std::io::Error::other)?;
        let menu = if let Observation::Queue(Update::Snapshot(menu)) = &event {
            Some(menu.clone())
        } else {
            None
        };
        if matches!(event, Observation::Refused(_) | Observation::Complete) {
            terminal = true;
        }
        chat.observe(event)?;
        if terminal && menu.is_none() {
            assert!(chat.queue_pending());
            assert!(chat.submit("too early").is_err());
        }
        if let Some(menu) = menu {
            if attempts > 0 {
                assert!(terminal, "readback arrived before the turn settled");
                assert!(!chat.queue_pending());
                assert_eq!(
                    chat.rows.last().unwrap().state,
                    if attempts == 1 {
                        ReplyState::Refused
                    } else {
                        ReplyState::Complete
                    }
                );
            }
            if attempts == 2 {
                assert_eq!(menu.entries.len(), 2);
                assert!(!menu.entries.iter().any(|e| e.id == "saved-queued"));
                break;
            }
            let entry = &menu.entries[0];
            assert_eq!(entry.id, "saved-queued");
            assert!(entry.can_continue);
            let intent = Intent::Continue {
                revision: menu.revision,
                entry: entry.id.clone(),
            };
            chat.prepare_continuation(&intent)?;
            assert!(chat.take_submission().is_none());
            runtime.queue(intent)?;
            attempts += 1;
            terminal = false;
        }
    }
    Ok(())
}

fn refused_resume(options: litetui_terminal::live::Options) -> String {
    use litetui_terminal::live::{Interactive, Observation};
    Interactive::start(options)
        .with_cleanup(|runtime| {
            while let Ok(event) = runtime.events.recv_timeout(Duration::from_secs(20)) {
                if matches!(event, Observation::Ready | Observation::History(_)) {
                    return Err(std::io::Error::other("Unexpected resume activation"));
                }
            }
            Err(std::io::Error::other("Resume correctly never became ready"))
        })
        .unwrap_err()
        .to_string()
}

#[test]
#[ignore = "controlled child launch tripwire; no provider"]
fn unmarked_or_mismatched_resume_root_never_starts_python_or_overwrites_files() {
    use litetui_terminal::{live::Options, resume};
    for mode in ["missing-marker", "bad-settings", "matching-settings"] {
        let (command, root) = setup("success");
        fs::create_dir(&root).unwrap();
        let settings = br#"{"tools_enabled":false,"mcp_enabled":false,"retained":"sentinel"}"#;
        fs::write(root.join("settings.json"), settings).unwrap();
        if mode != "missing-marker" {
            resume::mark_fresh(&root).unwrap();
        }
        let args: Vec<String> = command
            .get_args()
            .skip(1)
            .map(|v| v.to_string_lossy().into_owned())
            .collect();
        let mut options = Options::parse(&args).unwrap();
        options.resume = Some("12345678-1234-4123-8123-123456789abc".into());
        if mode == "bad-settings" {
            fs::write(
                root.join("settings.json"),
                br#"{"tools_enabled":true,"mcp_enabled":true}"#,
            )
            .unwrap();
        }
        let before = fs::read(root.join("settings.json")).unwrap();
        let error = refused_resume(options);
        if mode == "matching-settings" {
            // Positive control reaches the process; its deliberately wrong ready ID refuses.
            assert!(root.parent().unwrap().join("python-started").is_file());
            assert!(error.contains("did not resume"), "{error}");
        } else {
            assert!(!root.parent().unwrap().join("python-started").exists());
        }
        assert_eq!(fs::read(root.join("settings.json")).unwrap(), before);
    }
}
