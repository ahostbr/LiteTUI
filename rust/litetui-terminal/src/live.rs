//! First live vertical path: ONE owned Python child and ONE legacy RPC prompt.
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
        let prompt = required("--prompt")?;
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
#[derive(Debug, PartialEq)]
pub enum Observation {
    Ready,
    Text(String),
    Snapshot(String),
    Information(String),
    Complete,
}
#[derive(Default)]
pub struct Turn {
    sent: bool,
    started: bool,
    acknowledged: bool,
    complete: bool,
    answer_bytes: usize,
}
impl Turn {
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
                if self.sent
                    || event["protocol_version"] != 1
                    || event["launch_status"] != "ready"
                    || event["backend"] != backend
                    || event["model"] != model
                {
                    return Err(fail(&format!("Unexpected/blocked runtime ready: {event}")));
                }
                self.sent = true; // no retries, even if the subsequent write fails
                Ok(Observation::Ready)
            }
            "response"
                if self.complete
                    && event["id"] == SHUTDOWN_ID
                    && event["ok"] == true
                    && event["result"]["ok"] == true =>
            {
                Ok(Observation::Information("shutdown acknowledged".into()))
            }
            "response" => {
                if event["id"] != PROMPT_ID
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
            "text_delta" | "native_text_snapshot" if self.started && !self.complete => {
                let text = text()?.to_owned();
                // Bound aggregate emitted text, including replacements; do not retain history.
                self.answer_bytes = self
                    .answer_bytes
                    .checked_add(text.len())
                    .filter(|size| *size <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live answer budget exceeded"))?;
                Ok(if kind == "text_delta" {
                    Observation::Text(text)
                } else {
                    Observation::Snapshot(text)
                })
            }
            "turn_end" if self.started && !self.complete => {
                if !self.acknowledged
                    || self.answer_bytes == 0
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
            "usage" | "native_usage" | "native_event" | "reasoning_delta" => {
                Ok(Observation::Information(kind.into()))
            }
            // No tool/UI/native-activity implementation is claimed by this slice.
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

pub fn run(options: Options) -> io::Result<()> {
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
    let mut output = Output::new();
    writeln!(
        output,
        "LiteTUI Rust LIVE — one prompt, legacy RPC, tools disabled\nBackend: {} / {}\nData: {}",
        terminal_text(&options.backend),
        terminal_text(&options.model),
        terminal_text(&root.to_string_lossy())
    )?;
    output.flush()?;
    let mut deadline = Instant::now() + options.timeout;
    let result = (|| -> io::Result<()> {
        loop {
            if Instant::now() >= deadline {
                return Err(io::Error::other("Live deadline elapsed; no prompt retry"));
            }
            let chunk = match rx.recv_timeout(Duration::from_millis(100)) {
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
                    return if turn.complete {
                        Ok(())
                    } else {
                        Err(io::Error::other(
                            "Runtime stdout closed before successful turn_end",
                        ))
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
                match turn.accept(&event, &options.backend, &options.model)? {
                    Observation::Ready => {
                        // A faulted chunk must never grant new work, even if its
                        // accepted prefix contains ready. No retransmission.
                        if ingress_result.is_err() {
                            return Err(io::Error::other("Ingress failed before prompt admission"));
                        }
                        writeln!(output, "Ready. Prompt: {}", terminal_text(&options.prompt))?;
                        owned.stdin.as_ref().expect("owned stdin").try_send(json!({"type":"prompt","id":PROMPT_ID,"message":options.prompt,"tool_profile":"strict"})).map_err(io::Error::other)?;
                    }
                    Observation::Text(text) => write!(output, "{}", terminal_text(&text))?,
                    Observation::Snapshot(text) => writeln!(
                        output,
                        "\n[Answer snapshot replaces current answer]\n{}",
                        terminal_text(&text)
                    )?,
                    Observation::Information(kind) => {
                        writeln!(output, "\n[Runtime: {}]", terminal_text(&kind))?
                    }
                    Observation::Complete => {
                        owned.request_shutdown()?;
                        deadline = Instant::now() + Duration::from_secs(20);
                        writeln!(output, "\n[Turn ended: stop]")?;
                    }
                }
                output.flush()?;
            }
            ingress_result.map_err(|e| io::Error::other(format!("{e:?}")))?;
        }
    })();
    // Continue draining while the runtime shuts down; otherwise a full pipe can
    // block the very cleanup we are waiting for. Preserve bounded stderr tail.
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
    // Never indefinitely join a reader held by a surviving provider; report
    // unresolved pipe ownership instead of claiming successful cleanup.
    let tail = tail_rx
        .recv_timeout(Duration::from_secs(2))
        .map_err(|error| {
            io::Error::other(format!(
                "live={result:?}; cleanup={cleanup:?}; runtime pipes did not settle: {error}"
            ))
        })?;
    match (result, cleanup) {
        (Ok(()), Ok(())) => {
            writeln!(
                output,
                "[Python child and contained process tree exited successfully]"
            )?;
            output.finish()
        }
        (result, cleanup) => Err(io::Error::other(format!(
            "live={result:?}; cleanup={cleanup:?}\n{}",
            terminal_text(&String::from_utf8_lossy(&tail))
        ))),
    }
}
