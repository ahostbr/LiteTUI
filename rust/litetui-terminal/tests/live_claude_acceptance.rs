#![cfg(windows)]
//! Explicitly opt-in, PAID real-Claude acceptance run; never part of ordinary/fixture tests.
//!
//! One tools-enabled Strict conversation driven through the native frontend:
//! turn 1 reads `token.txt` with Claude's Read tool, the reviewed F6 compaction runs (the summary
//! turn may write the conversation's own memory store), and turn 3 recalls the token with tools
//! off. Every approval and the settled compaction witness are logged for audit.
#[path = "support/acceptance_filter.rs"]
mod acceptance_filter;
#[path = "support/paid_startup.rs"]
mod paid_startup;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    approvals, conversation_compact as r,
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use std::{
    collections::{BTreeMap, hash_map::RandomState},
    fs,
    hash::BuildHasher,
    io::{self, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const OVERALL: Duration = Duration::from_secs(6 * 60);
const TURN: Duration = Duration::from_secs(120);
const COMPACTION: Duration = Duration::from_secs(180);
const HINT: &str = "Keep the exact token you read from token.txt.";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    /// Startup and the F6 review steps: no approval is expected, so any request is denied.
    Idle,
    Turn1,
    Compaction,
    Turn3,
}

struct Ctx {
    startup: paid_startup::Startup,
    deadline: Instant,
    phase: Phase,
    token: String,
    /// Normalised acceptable spellings of the workspace and of the memory store.
    workspace: Vec<String>,
    store: Vec<String>,
    audit: PathBuf,
    cards: BTreeMap<usize, String>,
    legacy: String,
    witness: Option<Witness>,
    approvals: Vec<String>,
}

#[derive(Debug)]
struct Witness {
    outcome: r::Outcome,
    segments: u64,
    receipt: Option<r::ClaudeReceipt>,
}

impl Ctx {
    fn say(&self, line: impl AsRef<str>) {
        let line = line.as_ref();
        println!("{line}");
        let _ = std::io::stdout().flush();
        if let Ok(mut file) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.audit)
        {
            let _ = writeln!(file, "{line}");
        }
    }
    fn answer(&self) -> String {
        self.cards
            .values()
            .rev()
            .find(|text| !text.trim().is_empty())
            .cloned()
            .unwrap_or_else(|| self.legacy.clone())
    }
    fn reset_answer(&mut self) {
        self.cards.clear();
        self.legacy.clear();
    }
}

fn fail(message: impl Into<String>) -> io::Error {
    io::Error::other(message.into())
}

fn norm(path: &str) -> String {
    let slashed = path.replace('\\', "/");
    slashed
        .strip_prefix("//?/")
        .unwrap_or(&slashed)
        .to_ascii_lowercase()
        .trim_end_matches('/')
        .to_owned()
}

/// Both the lexical and the canonical spelling, so a junction or a `\\?\` prefix cannot hide a match.
fn spellings(path: &Path) -> Vec<String> {
    let mut all = vec![norm(&path.to_string_lossy())];
    if let Ok(canonical) = path.canonicalize() {
        let canonical = norm(&canonical.to_string_lossy());
        if !all.contains(&canonical) {
            all.push(canonical);
        }
    }
    all
}

fn clean(path: &str) -> Option<String> {
    let path = norm(path);
    (!path.split('/').any(|part| part == "..")).then_some(path)
}

/// The store paths a Write/Edit may touch: the three memory documents and `memories/**`,
/// the same set the source's tool policy calls SELF_STORE.
fn in_store(path: &str, store: &[String]) -> bool {
    let Some(path) = clean(path) else {
        return false;
    };
    store.iter().any(|root| {
        path.strip_prefix(&format!("{root}/")).is_some_and(|rest| {
            matches!(rest, "memory.md" | "soul.md" | "handoff.md")
                || rest
                    .strip_prefix("memories/")
                    .is_some_and(|f| !f.is_empty())
        })
    })
}

fn is_token_file(path: &str, workspace: &[String]) -> bool {
    let Some(path) = clean(path) else {
        return false;
    };
    path == "token.txt"
        || workspace
            .iter()
            .any(|root| path == format!("{root}/token.txt"))
}

/// `Claude · Read`, `mcp__x__Read` and `Read` all name the Read tool.
fn tool_word(tool: &str) -> String {
    tool.split(|c: char| !c.is_ascii_alphanumeric())
        .rfind(|part| !part.is_empty())
        .unwrap_or("")
        .to_ascii_lowercase()
}

fn target(request: &approvals::Request) -> String {
    let input: Value = serde_json::from_str(&request.input).unwrap_or(Value::Null);
    for key in ["file_path", "path", "notebook_path"] {
        if let Some(path) = input[key].as_str() {
            return path.to_owned();
        }
    }
    let rest: String = request
        .input
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    format!("(no path) {}", rest.chars().take(90).collect::<String>())
}

fn judge(ctx: &Ctx, request: &approvals::Request) -> (bool, String) {
    let path = target(request);
    let tool = tool_word(&request.tool);
    let allow = match ctx.phase {
        Phase::Turn1 => tool == "read" && is_token_file(&path, &ctx.workspace),
        Phase::Compaction => {
            matches!(tool.as_str(), "write" | "edit") && in_store(&path, &ctx.store)
        }
        Phase::Idle | Phase::Turn3 => false,
    };
    (allow, path)
}

fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

/// Answer the visible approval at the front of the queue, once it has been painted.
fn approve(runtime: &Interactive, app: &mut Demo, ctx: &mut Ctx) -> io::Result<()> {
    loop {
        let Some(pending) = app.live.as_ref().and_then(|chat| chat.approvals.current()) else {
            return Ok(());
        };
        if pending.waiting {
            return Ok(());
        }
        let request = pending.request.clone();
        draw(app);
        let (allow, path) = judge(ctx, &request);
        let approvals = &mut app.live.as_mut().unwrap().approvals;
        approvals.choose(allow);
        let decision = approvals
            .take_decision()
            .ok_or_else(|| fail("no visible approval decision"))?;
        if decision.id != request.id {
            return Err(fail("approval decision names another request"));
        }
        let line = format!(
            "[approval] phase={:?} tool={} path={} decision={} id={}",
            ctx.phase,
            request.tool,
            path,
            if allow { "ALLOW" } else { "DENY" },
            request.id
        );
        ctx.say(&line);
        ctx.approvals.push(line);
        runtime.approve(decision)?;
    }
}

/// Drain the runtime until `done`, answering approvals and refusing unexpected observation kinds.
fn pump(
    runtime: &mut Interactive,
    app: &mut Demo,
    ctx: &mut Ctx,
    limit: Duration,
    done: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let phase_deadline = Instant::now() + limit;
    loop {
        let now = Instant::now();
        if now > ctx.deadline {
            return Err(fail("overall 6 minute deadline"));
        }
        if now > phase_deadline {
            return Err(fail(format!("{:?} exceeded {limit:?}", ctx.phase)));
        }
        let event = match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => event,
            Err(_) => {
                if let Some(finished) = runtime.poll_done() {
                    return Err(finished
                        .err()
                        .unwrap_or_else(|| fail("runtime exited before the run finished")));
                }
                continue;
            }
        };
        ctx.startup.observe(&event)?;
        if matches!(
            &event,
            Observation::Theme(litetui_terminal::theme_controls::Update::Snapshot(_))
        ) {
            ctx.say("[startup] validated source theme snapshot");
        }
        match &event {
            Observation::Content { card, kind, text } if !kind.reasoning() => {
                let entry = ctx.cards.entry(*card).or_default();
                if kind.replaces() {
                    *entry = text.clone();
                } else {
                    entry.push_str(text);
                }
            }
            Observation::Content { .. } | Observation::Reasoning(_) => {}
            Observation::ReasoningSnapshot(_) => {}
            Observation::Text(text) => ctx.legacy.push_str(text),
            Observation::Snapshot(text) => ctx.legacy.clone_from(text),
            Observation::Information(info) => ctx.say(format!("[runtime] {info}")),
            Observation::CompactSettled(changed) => {
                let s = &changed.settlement;
                ctx.say(format!(
                    "[compaction settled] outcome={} reason_code={} reason={:?} history_changed={} \
                     wake_segments={}",
                    s.outcome.as_str(),
                    s.reason_code,
                    s.reason,
                    s.history_changed,
                    s.execution_segments
                ));
                if let Some(receipt) = &s.claude_receipt {
                    ctx.say(format!(
                        "[compaction witness] result_id={} new_segment={} seed_sha256={} \
                         ledger_after={} tool_calls={} store_files={:?}",
                        receipt.result_id,
                        receipt.new_segment,
                        receipt.seed_sha256,
                        receipt.ledger_after,
                        receipt.tool_calls,
                        receipt.store_files
                    ));
                }
                if let Some(text) = s.explanation() {
                    ctx.say(format!("[compaction] {text}"));
                }
                ctx.witness = Some(Witness {
                    outcome: s.outcome,
                    segments: s.execution_segments,
                    receipt: s.claude_receipt.clone(),
                });
            }
            Observation::Failed(reason)
            | Observation::Cancelled(reason)
            | Observation::Refused(reason)
            | Observation::ApprovalStopped(reason)
            | Observation::ImageRefused { reason, .. } => {
                return Err(fail(format!("{:?} stopped: {reason}", ctx.phase)));
            }
            _ => {}
        }
        // The shared rule (proved without a provider by compact_process's acceptance dry run).
        if let Some(why) = acceptance_filter::abort_reason(&event) {
            return Err(fail(format!("{:?}: {why}: {event:?}", ctx.phase)));
        }
        let finished = done(&event);
        app.observe_live(event)?;
        approve(runtime, app, ctx)?;
        if finished {
            return Ok(());
        }
    }
}

fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .compact
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}

fn surface(app: &mut Demo) {
    let actual = draw(app);
    let theme = app.palette().clone();
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|f| app.live.as_mut().unwrap().compact.draw(f, f.area(), &theme))
        .unwrap();
    let expected: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert_eq!(
        actual, expected,
        "Compact review must own the entire visible surface"
    );
}

/// F6 -> fresh saved-history read -> `c`: open the reviewed compaction for the current conversation.
fn select(runtime: &mut Interactive, app: &mut Demo, ctx: &mut Ctx) -> io::Result<()> {
    app.open_history();
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .saved_history
        .take_intent()
        .ok_or_else(|| fail("F6 gave no fresh saved-history read"))?;
    let request = intent.key().clone();
    runtime.history(intent)?;
    pump(
        runtime,
        app,
        ctx,
        TURN,
        |e| matches!(e, Observation::SavedHistory(update) if update.key() == &request),
    )?;
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .saved_history
        .key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
    app.take_history_compact()?;
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .compact
        .take_intent()
        .ok_or_else(|| fail("F6 C opened no compaction"))?;
    let request = intent.key().clone();
    runtime.compact(intent)?;
    pump(
        runtime,
        app,
        ctx,
        TURN,
        |e| matches!(e, Observation::Compact(update) if update.key() == &request),
    )
}

/// Replace the hint, then validate: the source returns the exact review.
fn validate(
    runtime: &mut Interactive,
    app: &mut Demo,
    ctx: &mut Ctx,
    hint: &str,
) -> io::Result<()> {
    surface(app);
    let count = app.live.as_ref().unwrap().compact.draft().chars().count();
    key(app, KeyCode::End);
    for _ in 0..count {
        key(app, KeyCode::Backspace);
    }
    app.live.as_mut().unwrap().compact.paste(hint);
    if app.live.as_ref().unwrap().compact.draft() != hint {
        return Err(fail("the compaction hint was not entered"));
    }
    draw(app);
    key(app, KeyCode::Enter);
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .compact
        .take_intent()
        .ok_or_else(|| fail(format!("Missing validation: {}", draw(app))))?;
    let request = intent.key().clone();
    runtime.compact(intent)?;
    pump(
        runtime,
        app,
        ctx,
        TURN,
        |e| matches!(e, Observation::Compact(update) if update.key() == &request),
    )
}

/// Page through the review and confirm; returns the key the settlement must carry.
fn apply(runtime: &Interactive, app: &mut Demo) -> io::Result<r::RequestKey> {
    surface(app);
    let mut apply = None;
    for _ in 0..64 {
        draw(app);
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().compact.take_intent() {
            apply = Some(intent);
            break;
        }
        draw(app);
        key(app, KeyCode::PageDown);
    }
    let intent = apply.ok_or_else(|| fail("no visible confirmation after the review pages"))?;
    let request = intent.key().clone();
    if !matches!(intent, r::Intent::Apply { .. }) {
        return Err(fail("the confirmation was not an apply intent"));
    }
    key(app, KeyCode::Enter);
    if app.live.as_mut().unwrap().compact.take_intent().is_some() {
        return Err(fail("a second apply intent appeared"));
    }
    app.live.as_mut().unwrap().prepare_compact(&intent)?;
    if app.live.as_mut().unwrap().take_submission().is_some() {
        return Err(fail("compaction fabricated a user submission"));
    }
    runtime.compact(intent)?;
    Ok(request)
}

fn turn(
    runtime: &mut Interactive,
    app: &mut Demo,
    ctx: &mut Ctx,
    phase: Phase,
    prompt: &str,
) -> io::Result<String> {
    ctx.phase = phase;
    ctx.reset_answer();
    ctx.say(format!("[{phase:?}] submit: {prompt}"));
    app.live.as_mut().unwrap().submit(prompt)?;
    runtime.submit(
        app.live
            .as_mut()
            .unwrap()
            .take_submission()
            .ok_or_else(|| fail("no reserved prompt"))?,
    )?;
    pump(runtime, app, ctx, TURN, |e| {
        matches!(e, Observation::Complete)
    })?;
    let answer = ctx.answer();
    ctx.say(format!("[{phase:?}] answer: {answer}"));
    Ok(answer)
}

/// The one agent home that holds the conversation's memory documents.
fn find_store(root: &Path) -> io::Result<PathBuf> {
    let homes: Vec<PathBuf> = fs::read_dir(root.join(".agents"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|home| home.join("memory.md").is_file() && home.join("soul.md").is_file())
        .collect();
    match homes.as_slice() {
        [one] => Ok(one.clone()),
        other => Err(fail(format!(
            "expected exactly one memory store under {}/.agents, found {other:?}",
            root.display()
        ))),
    }
}

fn token() -> String {
    let hash = RandomState::new().hash_one((
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    (0..8)
        .map(|i| char::from(b'A' + ((hash >> (i * 6)) % 26) as u8))
        .collect()
}

#[test]
#[ignore = "PAID Claude acceptance; requires LITETUI_REAL_CLAUDE_ACCEPTANCE=YES and LITETUI_TEST_PYTHON"]
fn real_claude_reads_a_file_compacts_with_tools_and_recalls_it() {
    assert_eq!(
        std::env::var("LITETUI_REAL_CLAUDE_ACCEPTANCE").as_deref(),
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
            "claude-acceptance-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    // A sibling of the data root: the runtime creates the data root itself and refuses one that exists.
    let workspace = root.with_file_name(format!(
        "{}-workspace",
        root.file_name().unwrap().to_string_lossy()
    ));
    fs::create_dir_all(&workspace).unwrap();
    let token = token();
    fs::write(workspace.join("token.txt"), &token).unwrap();
    println!("Real Claude acceptance data: {}", root.display());
    let started = Instant::now();
    let mut ctx = Ctx {
        startup: paid_startup::Startup::new("claude", "claude-sonnet-5-5"),
        deadline: started + OVERALL,
        phase: Phase::Idle,
        token: token.clone(),
        workspace: spellings(&workspace),
        store: Vec::new(),
        audit: workspace.join("acceptance-audit.log"),
        cards: BTreeMap::new(),
        legacy: String::new(),
        witness: None,
        approvals: Vec::new(),
    };
    let runtime = Interactive::start(Options {
        launch: Default::default(),
        agent: None,
        resume: None,
        seed_from: None,
        owner: None,
        python,
        source,
        data_root: root.clone(),
        backend: "claude".into(),
        model: "claude-sonnet-5-5".into(),
        prompt: String::new(),
        timeout: TURN,
        tools_enabled: true,
        tool_profile: Some(litetui_terminal::tool_mode::Profile::Strict),
        workspace: Some(workspace.clone()),
    });
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("claude".into(), "claude-sonnet-5-5".into());
        pump(runtime, &mut app, &mut ctx, TURN, |e| {
            matches!(e, Observation::Ready)
        })?;
        let store = find_store(&root)?;
        ctx.store = spellings(&store);
        ctx.say(format!(
            "[setup] token file {} / memory store {}",
            workspace.join("token.txt").display(),
            store.display()
        ));

        let file = workspace.join("token.txt");
        let first = turn(
            runtime,
            &mut app,
            &mut ctx,
            Phase::Turn1,
            &format!(
                "Read token.txt ({}) with the Read tool and reply with only its contents.",
                file.display()
            ),
        )?;
        if !first.to_ascii_uppercase().contains(&token) {
            return Err(fail(format!("turn 1 answer lacks the token: {first:?}")));
        }

        ctx.phase = Phase::Idle;
        select(runtime, &mut app, &mut ctx)?;
        validate(runtime, &mut app, &mut ctx, HINT)?;
        let review = draw(&mut app);
        if !review.contains("Confirm Claude compaction") {
            return Err(fail(format!("not the Claude compaction review: {review}")));
        }
        if !review.contains("Tools enabled: yes") {
            return Err(fail(format!("review lacks 'Tools enabled: yes': {review}")));
        }
        ctx.say("[review] Confirm Claude compaction; Tools enabled: yes");
        ctx.phase = Phase::Compaction;
        let request = apply(runtime, &mut app)?;
        pump(
            runtime,
            &mut app,
            &mut ctx,
            COMPACTION,
            |e| matches!(e, Observation::CompactSettled(changed) if changed.key == request),
        )?;
        let chat = app.live.as_ref().unwrap();
        if chat.rewrite_pending() || chat.busy || chat.compact.owns_input() {
            return Err(fail("the compaction left the frontend busy"));
        }

        let third = turn(
            runtime,
            &mut app,
            &mut ctx,
            Phase::Turn3,
            "What was the token? Reply with only the token. Do not use tools.",
        )?;
        if !third.to_ascii_uppercase().contains(&token) {
            return Err(fail(format!("turn 3 answer lacks the token: {third:?}")));
        }
        runtime.quit()
    });
    ctx.say(format!(
        "[done] elapsed={:?} approvals={} cleanup/result={result:?}",
        started.elapsed(),
        ctx.approvals.len()
    ));
    assert!(
        result.is_ok(),
        "{result:?}; artifacts and audit log at {}",
        root.display()
    );
    let witness = ctx.witness.as_ref().expect("a settled compaction");
    assert_eq!(witness.outcome, r::Outcome::Compacted, "{witness:?}");
    assert_eq!(witness.segments, 0, "wake is off: {witness:?}");
    let receipt = witness.receipt.as_ref().expect("a verified Claude segment");
    assert!(!receipt.new_segment.is_empty() && !receipt.seed_sha256.is_empty());
    println!("token={}", ctx.token);
}
