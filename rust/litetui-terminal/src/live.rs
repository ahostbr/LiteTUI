//! Owned Python legacy RPC runtime for one-shot and interactive frontends.
//! Deliberately not GUI reconciliation or the generic identified-stream reducer.
//! No automatic retries, backend fallback, approvals or local model launches.
use crate::{
    protocol::JsonlDecoder,
    transport::{Inbound, IngressLimits},
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

const FRAME_BYTES: usize = 2 * 1024 * 1024;
const PROMPT_ID: &str = "rust-live-prompt";
const SHUTDOWN_ID: &str = "rust-live-shutdown";

#[derive(Debug)]
pub struct Options {
    pub python: PathBuf,
    pub source: PathBuf,
    pub data_root: PathBuf,
    pub backend: String,
    pub model: String,
    pub prompt: String,
    pub timeout: Duration,
}
impl Options {
    pub fn parse(args: &[String]) -> io::Result<Self> {
        let mut fields = std::collections::HashMap::new();
        for pair in args.chunks(2) {
            if pair.len() != 2
                || ![
                    "--python",
                    "--source",
                    "--data-root",
                    "--backend",
                    "--model",
                    "--prompt",
                    "--timeout",
                ]
                .contains(&pair[0].as_str())
                || fields.insert(pair[0].as_str(), pair[1].clone()).is_some()
            {
                return Err(io::Error::other(
                    "Expected unique --live option/value pairs; see --help",
                ));
            }
        }
        let mut required = |name| {
            fields
                .remove(name)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| io::Error::other(format!("Missing {name}")))
        };
        let python = required("--python")?.into();
        let source = required("--source")?.into();
        let data_root = required("--data-root")?.into();
        let backend = required("--backend")?;
        let model = required("--model")?;
        let prompt = fields.remove("--prompt").unwrap_or_default();
        let seconds: u64 = fields
            .remove("--timeout")
            .unwrap_or_else(|| "180".into())
            .parse()
            .map_err(io::Error::other)?;
        if prompt.len() > 65536 {
            return Err(io::Error::other("Prompt exceeds 64 KiB live limit"));
        }
        if !(1..=600).contains(&seconds) || !matches!(backend.as_str(), "claude" | "codex") {
            return Err(io::Error::other(
                "Live scope requires claude/codex and timeout 1..600 seconds",
            ));
        }
        Ok(Self {
            python,
            source,
            data_root,
            backend,
            model,
            prompt,
            timeout: Duration::from_secs(seconds),
        })
    }
}

/// Events are observations of a single isolated legacy turn, NOT invented IDs
/// or an admission receipt. The prompt ACK alone never completes this state.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ContentKind {
    AnswerDelta,
    AnswerSnapshot,
    ReasoningDelta,
    ReasoningSnapshot,
}
impl ContentKind {
    pub fn replaces(self) -> bool {
        matches!(self, Self::AnswerSnapshot | Self::ReasoningSnapshot)
    }
    pub fn reasoning(self) -> bool {
        matches!(self, Self::ReasoningDelta | Self::ReasoningSnapshot)
    }
}
#[derive(Debug, PartialEq)]
pub enum Observation {
    State(crate::runtime_state::Update),
    Ready,
    Content {
        card: usize,
        kind: ContentKind,
        text: String,
    },
    Activity(crate::native_activity::Activity),
    Text(String),
    Snapshot(String),
    Reasoning(String),
    ReasoningSnapshot(String),
    Information(String),
    Complete,
}
#[derive(Default)]
pub struct Turn {
    ready: bool,
    sent: bool,
    closing: bool,
    sequence: u64,
    command_id: String,
    visible_answers: std::collections::BTreeSet<usize>,
    tools_seen: bool,
    card_count: usize,
    started: bool,
    acknowledged: bool,
    complete: bool,
    content_bytes: usize,
}
impl Turn {
    /// Reserve a new command ID exactly once. Neither a write nor admission.
    pub fn submit(&mut self, prompt: &str) -> io::Result<Value> {
        if !self.ready
            || self.closing
            || (self.sent && !self.complete)
            || prompt.trim().is_empty()
            || prompt.len() > 65536
        {
            return Err(io::Error::other(
                "Runtime is not idle, or prompt is empty/too large",
            ));
        }
        self.command_id = if self.sequence == 0 {
            PROMPT_ID.into()
        } else {
            format!("{PROMPT_ID}-{}", self.sequence)
        };
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| io::Error::other("Command ID exhausted"))?;
        self.sent = true;
        self.started = false;
        self.acknowledged = false;
        self.complete = false;
        self.visible_answers.clear();
        self.tools_seen = false;
        self.card_count = 1;
        self.content_bytes = 0;
        Ok(json!({"type":"prompt","id":self.command_id,"message":prompt,"tool_profile":"strict"}))
    }
    pub fn accept(&mut self, event: &Value, backend: &str, model: &str) -> io::Result<Observation> {
        let fail = |message: &str| io::Error::other(message.to_owned());
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| fail("Missing event type"))?;
        let text = || {
            event
                .get("text")
                .and_then(Value::as_str)
                .ok_or_else(|| fail("Missing event text"))
        };
        match kind {
            "ready" => {
                if self.ready
                    || event["protocol_version"] != 1
                    || event["launch_status"] != "ready"
                    || event["backend"] != backend
                    || event["model"] != model
                {
                    return Err(fail(&format!("Unexpected/blocked runtime ready: {event}")));
                }
                self.ready = true;
                Ok(Observation::Ready)
            }
            "response"
                if self.closing
                    && event["id"] == SHUTDOWN_ID
                    && event["ok"] == true
                    && event["result"]["ok"] == true =>
            {
                Ok(Observation::Information("shutdown acknowledged".into()))
            }
            "response" => {
                if event["id"] != self.command_id
                    || !self.sent
                    || self.acknowledged
                    || event["ok"] != true
                    || event["result"]["turn"] != "accepted"
                {
                    return Err(fail(&format!(
                        "Unexpected/refused prompt response: {event}"
                    )));
                }
                self.acknowledged = true;
                Ok(Observation::Information(
                    "prompt acknowledged (not completion)".into(),
                ))
            }
            "turn_start" if self.sent && !self.started => {
                if event["model"] != model {
                    return Err(fail("Turn model differs from requested model"));
                }
                self.started = true;
                Ok(Observation::Information("turn started".into()))
            }
            "text_delta" | "native_text_snapshot" | "reasoning_delta"
                if self.started && !self.complete =>
            {
                let card = crate::native_activity::card_index(event)?;
                if card.is_none() && self.tools_seen {
                    return Err(fail("Content without native display ownership after tools"));
                }
                if card.is_some_and(|index| index > self.card_count) {
                    return Err(fail("Invalid native card sequence"));
                }
                let reasoning = kind == "reasoning_delta";
                let replace = if reasoning {
                    match event.get("reconcile").and_then(Value::as_str) {
                        Some("append") => false,
                        Some("replace") => true,
                        None if backend != "claude" && event.get("reconcile").is_none() => false,
                        _ => {
                            return Err(fail(
                                "Reasoning needs explicit append/replace reconciliation for native Claude",
                            ));
                        }
                    }
                } else {
                    kind == "native_text_snapshot"
                };
                let text = text()?.to_owned();
                // Answer and reasoning share the aggregate emitted-text budget,
                // including replacements. A trace is never an answer receipt.
                self.content_bytes = self
                    .content_bytes
                    .checked_add(text.len())
                    .filter(|size| *size <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live content budget exceeded"))?;
                if let Some(index) = card {
                    self.card_count = self.card_count.max(index + 1);
                }
                if !reasoning {
                    if !text.is_empty() {
                        self.visible_answers.insert(card.unwrap_or(0));
                    } else if replace {
                        self.visible_answers.remove(&card.unwrap_or(0));
                    }
                }
                let content_kind = match (reasoning, replace) {
                    (true, true) => ContentKind::ReasoningSnapshot,
                    (true, false) => ContentKind::ReasoningDelta,
                    (false, true) => ContentKind::AnswerSnapshot,
                    (false, false) => ContentKind::AnswerDelta,
                };
                Ok(if let Some(card) = card {
                    Observation::Content {
                        card,
                        kind: content_kind,
                        text,
                    }
                } else {
                    match content_kind {
                        ContentKind::ReasoningSnapshot => Observation::ReasoningSnapshot(text),
                        ContentKind::ReasoningDelta => Observation::Reasoning(text),
                        ContentKind::AnswerSnapshot => Observation::Snapshot(text),
                        ContentKind::AnswerDelta => Observation::Text(text),
                    }
                })
            }
            "native_activity" if self.started && !self.complete && backend == "claude" => {
                let activity = crate::native_activity::Activity::from_event(event)?;
                if activity.card >= self.card_count {
                    return Err(fail("Native tool has invalid display ownership"));
                }
                self.content_bytes = self
                    .content_bytes
                    .checked_add(activity.emitted_bytes())
                    .filter(|n| *n <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live content budget exceeded"))?;
                self.tools_seen = true;
                Ok(Observation::Activity(activity))
            }
            "turn_end" if self.started && !self.complete => {
                if self.closing && event["stopReason"] != "stop" {
                    return Ok(Observation::Information(
                        "turn interrupted while quitting; not completed".into(),
                    ));
                }
                if !self.acknowledged
                    || self.visible_answers.is_empty()
                    || event["stopReason"] != "stop"
                    || event.get("error").is_some_and(|v| !v.is_null() && v != "")
                {
                    return Err(fail(&format!(
                        "Turn did not complete successfully: {event}"
                    )));
                }
                self.complete = true;
                Ok(Observation::Complete)
            }
            // These source-defined observations have no authority in this frontend.
            // Print their kind, rather than silently discarding unsupported state.
            "usage" | "native_usage" | "native_event" => Ok(Observation::Information(kind.into())),
            // Interactive approval/question requests remain unsupported; never auto-answer.
            _ => Err(fail(&format!(
                "Unsupported or out-of-order live event: {kind}"
            ))),
        }
    }
}

/// Never allow model/provider output to execute terminal control sequences.
pub fn terminal_text(text: &str) -> String {
    text.chars()
        .flat_map(|ch| {
            if (ch.is_control() && !matches!(ch, '\n' | '\t'))
                || matches!(ch, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            {
                format!("\\u{{{:x}}}", ch as u32)
                    .chars()
                    .collect::<Vec<_>>()
            } else {
                vec![ch]
            }
        })
        .collect()
}

fn send(stdin: &mut ChildStdin, value: Value) -> io::Result<()> {
    serde_json::to_writer(&mut *stdin, &value)?;
    stdin.write_all(b"\n")?;
    stdin.flush()
}

struct OwnedChild {
    child: Child,
    job: crate::live_process::Job,
    stdin: Option<mpsc::SyncSender<Value>>,
    reaped: bool,
}
impl OwnedChild {
    fn request_shutdown(&mut self) -> io::Result<()> {
        if let Some(stdin) = self.stdin.take() {
            stdin
                .try_send(json!({"type":"shutdown","id":SHUTDOWN_ID}))
                .map_err(io::Error::other)?;
        }
        Ok(())
    }
    fn close(&mut self) -> io::Result<()> {
        if self.reaped {
            return Ok(());
        }
        let deadline = Instant::now() + Duration::from_secs(20);
        let write = self.request_shutdown(); // bounded queue, never a blocking pipe write
        let mut status = None;
        while Instant::now() < deadline {
            if status.is_none() {
                status = self.child.try_wait()?;
            }
            if let Some(status) = status
                && self.job.active()? == 0
            {
                self.reaped = true;
                if !status.success() {
                    return Err(io::Error::other(format!("Python exited with {status}")));
                }
                return write;
            }
            thread::sleep(Duration::from_millis(50));
        }
        self.job.terminate()?;
        self.child.wait()?;
        self.reaped = true;
        Err(io::Error::other(
            "Runtime job shutdown timed out; forced termination, provider cleanup unproven",
        ))
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        // The job's kill-on-close remains armed even if graceful cleanup fails.
        if !self.reaped {
            let _ = self.close();
        }
    }
}

// Blocking output lives off the supervisor thread. Fail-stop on a full bounded
// queue; a stalled terminal cannot prevent child shutdown or deadline checks.
struct Output {
    tx: Option<mpsc::SyncSender<Vec<u8>>>,
    done: mpsc::Receiver<io::Result<()>>,
}
impl Output {
    fn new() -> Self {
        let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(64);
        let (done_tx, done) = mpsc::channel();
        thread::spawn(move || {
            let result = (|| -> io::Result<()> {
                let mut output = io::stdout().lock();
                while let Ok(bytes) = rx.recv() {
                    output.write_all(&bytes)?;
                    output.flush()?;
                }
                Ok(())
            })();
            let _ = done_tx.send(result);
        });
        Self { tx: Some(tx), done }
    }
    fn finish(mut self) -> io::Result<()> {
        self.tx.take();
        self.done
            .recv_timeout(Duration::from_secs(2))
            .map_err(io::Error::other)?
    }
}
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.tx
            .as_ref()
            .expect("open output")
            .try_send(bytes.to_vec())
            .map_err(io::Error::other)?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

enum Pipe {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    End(bool),
    Error(io::Error),
}
fn reader(mut input: impl Read + Send + 'static, stdout: bool, tx: mpsc::SyncSender<Pipe>) {
    thread::spawn(move || {
        let mut buffer = [0; 8192];
        loop {
            let message = match input.read(&mut buffer) {
                Ok(0) => {
                    let _ = tx.send(Pipe::End(stdout));
                    return;
                }
                Ok(n) if stdout => Pipe::Stdout(buffer[..n].to_vec()),
                Ok(n) => Pipe::Stderr(buffer[..n].to_vec()),
                Err(error) => {
                    let _ = tx.send(Pipe::Error(error));
                    return;
                }
            };
            if tx.send(message).is_err() {
                return;
            }
        }
    });
}

fn drive(options: Options, frontend: &mut impl Frontend) -> io::Result<()> {
    let source = options.source.canonicalize()?;
    if !source.join("src/litetui/cli.py").is_file()
        || !options.python.is_file()
        || !options.data_root.is_absolute()
    {
        return Err(io::Error::other(
            "Expected Python executable, LiteTUI source tree, and absolute NEW data root",
        ));
    }
    // Fail if the final path already exists: never resume/reuse a live seat.
    if let Some(parent) = options.data_root.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::create_dir(&options.data_root)?;
    let root = options.data_root.canonicalize()?;
    // Explicit isolated overrides; all other values come from shipped Settings
    // defaults in settings.load(). NEVER copy or load the user's live settings.
    fs::write(
        root.join("settings.json"),
        serde_json::to_vec_pretty(&json!({
            "backend": options.backend, "default_model": options.model,
            "backend_chosen": true, "user_name_asked": true,
            "tools_enabled": false, "mcp_enabled": false, "seat_name": "RustLive"
        }))?,
    )?;
    let mut command = Command::new(&options.python);
    command
        .current_dir(&source)
        .env("LITETUI_DATA_ROOT", &root)
        .env("PYTHONPATH", source.join("src"))
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .args([
            "-m",
            "litetui.cli",
            "--rpc",
            "--backend",
            &options.backend,
            "--model",
            &options.model,
            "--thinking-level",
            "low",
            "--tool-profile",
            "strict",
            "--mcp-servers",
            "none",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let (job, mut child) = crate::live_process::Job::spawn(&mut command)?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (input_tx, input_rx) = mpsc::sync_channel(2);
    let mut owned = OwnedChild {
        child,
        job,
        stdin: Some(input_tx),
        reaped: false,
    };
    let (tx, rx) = mpsc::sync_channel(8); // bounded borrowed chunks in addition to ingress budget
    let errors = tx.clone();
    thread::spawn(move || {
        while let Ok(command) = input_rx.recv() {
            if let Err(error) = send(&mut stdin, command) {
                let _ = errors.send(Pipe::Error(error));
                break;
            }
        }
    });
    reader(stdout, true, tx.clone());
    reader(stderr, false, tx);
    let mut ingress = Inbound::new(
        1,
        IngressLimits {
            frame_bytes: FRAME_BYTES,
            queued_frames: 256,
            queued_bytes: 4 * FRAME_BYTES,
        },
    )
    .map_err(|e| io::Error::other(format!("{e:?}")))?;
    let mut decoder =
        JsonlDecoder::new(FRAME_BYTES).map_err(|e| io::Error::other(format!("{e:?}")))?;
    let mut turn = Turn::default();
    let mut tail = Vec::new();
    let mut deadline = Some(Instant::now() + options.timeout);
    let result = (|| -> io::Result<()> {
        loop {
            if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                return Err(io::Error::other("Live deadline elapsed; no prompt retry"));
            }
            if !turn.closing
                && let Some(command) = frontend.command()?
            {
                match command {
                    Input::Prompt(prompt) => {
                        let value = turn.submit(&prompt)?;
                        owned
                            .stdin
                            .as_ref()
                            .expect("owned stdin")
                            .try_send(value)
                            .map_err(io::Error::other)?;
                        deadline = Some(Instant::now() + options.timeout);
                    }
                    Input::Quit => {
                        turn.closing = true;
                        deadline = Some(Instant::now() + Duration::from_secs(20));
                        owned.request_shutdown()?;
                    }
                }
            }
            let chunk = match rx.recv_timeout(Duration::from_millis(25)) {
                Ok(chunk) => chunk,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(error) => return Err(io::Error::other(error)),
            };
            let ingress_result = match chunk {
                Pipe::Stdout(bytes) => ingress.push(1, &bytes),
                Pipe::Stderr(bytes) => {
                    tail.extend(bytes);
                    if tail.len() > 65536 {
                        tail.drain(..tail.len() - 65536);
                    }
                    continue;
                }
                Pipe::End(false) => continue,
                Pipe::End(true) => {
                    ingress
                        .finish(1)
                        .map_err(|e| io::Error::other(format!("{e:?}")))?;
                    decoder
                        .finish()
                        .map_err(|e| io::Error::other(format!("{e:?}")))?;
                    return if turn.closing {
                        Ok(())
                    } else {
                        Err(io::Error::other("Runtime stdout closed unexpectedly"))
                    };
                }
                Pipe::Error(error) => return Err(error),
            };
            while let Some(frame) = ingress.pop_frame() {
                let mut value = None;
                decoder
                    .push(&frame, |v| value = Some(v))
                    .map_err(|e| io::Error::other(format!("{e:?}")))?;
                let event = value.expect("complete nonblank frame");
                let observation = turn.accept(&event, &options.backend, &options.model)?;
                // Validate facts before Ready can admit work or Complete can
                // claim success. Never derive them from launch preferences.
                if let Some(update) = crate::runtime_state::Update::from_event(&event)? {
                    frontend.observe(Observation::State(update))?;
                }
                if !turn.closing
                    && matches!(observation, Observation::Ready | Observation::Complete)
                {
                    deadline = None;
                }
                frontend.observe(observation)?;
            }
            // Never admit another command after a fault, including when an
            // accepted prefix contains Ready or Complete. Drain prefix first.
            ingress_result.map_err(|e| io::Error::other(format!("{e:?}")))?;
        }
    })();
    // Failure cleanup still drains both pipes to avoid deadlocking shutdown.
    // Success reaches here only after clean, fully validated stdout EOF.
    let (tail_tx, tail_rx) = mpsc::channel();
    thread::spawn(move || {
        while let Ok(chunk) = rx.recv() {
            if let Pipe::Stderr(bytes) = chunk {
                tail.extend(bytes);
                if tail.len() > 65536 {
                    tail.drain(..tail.len() - 65536);
                }
            }
        }
        let _ = tail_tx.send(tail);
    });
    let cleanup = owned.close();
    let tail = tail_rx
        .recv_timeout(Duration::from_secs(2))
        .map_err(|error| {
            io::Error::other(format!(
                "live={result:?}; cleanup={cleanup:?}; runtime pipes did not settle: {error}"
            ))
        })?;
    match (result, cleanup) {
        (Ok(()), Ok(())) => Ok(()),
        (result, cleanup) => Err(io::Error::other(format!(
            "live={result:?}; cleanup={cleanup:?}\n{}",
            terminal_text(&String::from_utf8_lossy(&tail))
        ))),
    }
}

pub enum Input {
    Prompt(String),
    Quit,
}
trait Frontend {
    fn command(&mut self) -> io::Result<Option<Input>>;
    fn observe(&mut self, observation: Observation) -> io::Result<()>;
}
struct OneShot {
    output: Output,
    prompt: Option<String>,
    next: Option<Input>,
}
impl Frontend for OneShot {
    fn command(&mut self) -> io::Result<Option<Input>> {
        Ok(self.next.take())
    }
    fn observe(&mut self, observation: Observation) -> io::Result<()> {
        match observation {
            Observation::State(_) => {} // Read-only footer facts; no one-shot action.
            Observation::Content { card, kind, text } => writeln!(
                self.output,
                "\n[Card {card} {kind:?}] {}",
                terminal_text(&text)
            )?,
            Observation::Activity(activity) => writeln!(
                self.output,
                "\n[Native display activity] {}",
                terminal_text(&format!("{activity:?}"))
            )?,
            Observation::Ready => {
                let prompt = self
                    .prompt
                    .take()
                    .ok_or_else(|| io::Error::other("Repeated ready"))?;
                writeln!(self.output, "Ready. Prompt: {}", terminal_text(&prompt))?;
                self.next = Some(Input::Prompt(prompt));
            }
            Observation::Text(text) => write!(self.output, "{}", terminal_text(&text))?,
            Observation::Reasoning(text) => {
                writeln!(self.output, "\n[Thinking] {}", terminal_text(&text))?
            }
            Observation::ReasoningSnapshot(text) => writeln!(
                self.output,
                "\n[Thinking snapshot replaces current trace]\n{}",
                terminal_text(&text)
            )?,
            Observation::Snapshot(text) => writeln!(
                self.output,
                "\n[Answer snapshot replaces current answer]\n{}",
                terminal_text(&text)
            )?,
            Observation::Information(kind) => {
                writeln!(self.output, "\n[Runtime: {}]", terminal_text(&kind))?
            }
            Observation::Complete => {
                writeln!(self.output, "\n[Turn ended: stop]")?;
                self.next = Some(Input::Quit);
            }
        }
        Ok(())
    }
}
pub fn run(options: Options) -> io::Result<()> {
    if options.prompt.trim().is_empty() {
        return Err(io::Error::other("--live requires --prompt"));
    }
    let mut frontend = OneShot {
        output: Output::new(),
        prompt: Some(options.prompt.clone()),
        next: None,
    };
    writeln!(
        frontend.output,
        "LiteTUI Rust LIVE — one prompt, legacy RPC, tools disabled\nBackend: {} / {}\nData: {}",
        terminal_text(&options.backend),
        terminal_text(&options.model),
        terminal_text(&options.data_root.to_string_lossy())
    )?;
    drive(options, &mut frontend)?;
    writeln!(
        frontend.output,
        "[Python child and contained process tree exited successfully]"
    )?;
    frontend.output.finish()
}

/// UI owns the receivers, worker owns the real child. Queues are bounded and
/// nonblocking; saturation fails visibly rather than dropping stream deltas.
pub struct Interactive {
    commands: mpsc::SyncSender<Input>,
    pub events: mpsc::Receiver<Observation>,
    done: mpsc::Receiver<io::Result<()>>,
    finished: Option<Result<(), String>>,
}
struct Bridge {
    commands: mpsc::Receiver<Input>,
    events: mpsc::SyncSender<Observation>,
}
impl Frontend for Bridge {
    fn command(&mut self) -> io::Result<Option<Input>> {
        match self.commands.try_recv() {
            Ok(input) => Ok(Some(input)),
            Err(mpsc::TryRecvError::Empty) => Ok(None),
            Err(mpsc::TryRecvError::Disconnected) => Ok(Some(Input::Quit)),
        }
    }
    fn observe(&mut self, observation: Observation) -> io::Result<()> {
        self.events.try_send(observation).map_err(io::Error::other)
    }
}
impl Interactive {
    pub fn start(options: Options) -> Self {
        let (commands, input) = mpsc::sync_channel(2);
        let (events, output) = mpsc::sync_channel(64);
        let (finished, done) = mpsc::channel();
        thread::spawn(move || {
            let mut frontend = Bridge {
                commands: input,
                events,
            };
            let result = drive(options, &mut frontend);
            let _ = finished.send(result);
        });
        Self {
            commands,
            events: output,
            done,
            finished: None,
        }
    }
    /// Cache cleanup outcome so UI polling cannot consume ownership evidence.
    pub fn poll_done(&mut self) -> Option<io::Result<()>> {
        if self.finished.is_none() {
            self.finished = match self.done.try_recv() {
                Ok(result) => Some(result.map_err(|error| error.to_string())),
                Err(mpsc::TryRecvError::Empty) => None,
                Err(mpsc::TryRecvError::Disconnected) => {
                    Some(Err("Runtime worker exited without a cleanup result".into()))
                }
            };
        }
        self.finished
            .clone()
            .map(|result| result.map_err(io::Error::other))
    }
    /// Close even after draw/input failure; keep draining to avoid blocking the
    /// worker's bounded observation queue while awaiting whole-job cleanup.
    pub fn shutdown(&mut self) -> io::Result<()> {
        if let Some(result) = self.poll_done() {
            return result;
        }
        let _ = self.quit();
        let deadline = Instant::now() + Duration::from_secs(45);
        loop {
            for _ in self.events.try_iter() {}
            if let Some(result) = self.poll_done() {
                return result;
            }
            if Instant::now() >= deadline {
                return Err(io::Error::other(
                    "Runtime cleanup result timed out; cleanup unproven",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
    /// Unconditional epilogue for the terminal frontend, including fallible
    /// draw/poll/read paths. Preserve both UI and cleanup errors.
    pub fn with_cleanup(
        mut self,
        operation: impl FnOnce(&mut Self) -> io::Result<()>,
    ) -> io::Result<()> {
        let result = operation(&mut self);
        let cleanup = self.shutdown();
        match (result, cleanup) {
            (Ok(()), result) | (result, Ok(())) => result,
            (Err(ui), Err(cleanup)) => Err(io::Error::other(format!(
                "UI failure: {ui}; runtime cleanup: {cleanup}"
            ))),
        }
    }
    pub fn submit(&self, prompt: String) -> io::Result<()> {
        self.commands
            .try_send(Input::Prompt(prompt))
            .map_err(io::Error::other)
    }
    pub fn quit(&self) -> io::Result<()> {
        self.commands
            .try_send(Input::Quit)
            .map_err(io::Error::other)
    }
}
