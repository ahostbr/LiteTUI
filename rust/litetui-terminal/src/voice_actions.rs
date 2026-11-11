//! Explicit reviewed Voice actions; source owns every probe and effect.
use crate::{
    theme::Theme,
    work_lifecycle::{hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, io};
pub const OPERATION: &str = "gui.voice.native_action";
const PHRASE: &str = "This is the LiteTUI voice test.";
fn bad() -> io::Error {
    io::Error::other("Voice action differs from its reviewed plan or owned runtime")
}
fn text(v: &Value) -> io::Result<&str> {
    v.as_str()
        .filter(|s| s.len() <= 4096 && !s.chars().any(crate::display_control))
        .ok_or_else(bad)
}
fn path(v: &Value) -> io::Result<&str> {
    let s = text(v)?;
    if !std::path::Path::new(s).is_absolute() {
        return Err(bad());
    }
    Ok(s)
}
fn digest(v: &Value) -> io::Result<String> {
    Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(v)?)))
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    task: String,
    revision: String,
    preferences: Value,
    selection: Value,
    run: Option<String>,
    status: String,
    plan: Option<Value>,
    plan_hash: Option<String>,
    result: Option<Value>,
    applied: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    state: State,
    action: &'static str,
}
impl Request {
    pub fn mutates(&self) -> bool {
        matches!(self.action, "review" | "apply" | "close")
    }
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        if !["review", "apply", "status", "close"].contains(&self.action)
            || !matches!(self.state.task.as_str(), "fallback" | "edge" | "stt")
            || (self.action == "review") != self.state.run.is_none()
            || self.action == "apply"
                && (self.state.status != "review"
                    || self.state.applied
                    || self.state.plan_hash.is_none())
        {
            return Err(bad());
        }
        let mut cmd = id.request();
        for (key, val) in [
            ("type", json!(OPERATION)),
            ("work_instance", json!(instance)),
            ("action", json!(self.action)),
            ("run", json!(self.state.run)),
            ("task", json!(self.state.task)),
            ("revision", json!(self.state.revision)),
            ("plan_sha256", json!(self.state.plan_hash)),
            ("values", self.state.selection.clone()),
        ] {
            cmd[key] = val;
        }
        Ok(cmd)
    }
    pub fn response(self, v: &Value, id: &Identity, instance: &str) -> io::Result<State> {
        keys(
            v,
            &[
                "version",
                "kind",
                "scope",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "run",
                "before",
                "task",
                "status",
                "plan",
                "plan_sha256",
                "result",
                "applied",
            ],
        )?;
        for key in ["session_id", "agent_id", "backend", "model"] {
            if v[key] != id.request()[key] {
                return Err(bad());
            }
        }
        let mut state = self.state;
        let token = crate::stream::required_uuid4(v, "run")
            .map_err(|_| bad())?
            .to_string();
        let status = text(&v["status"])?;
        let applied = v["applied"].as_bool().ok_or_else(bad)?;
        if v["version"] != 1
            || v["kind"] != "voice_action"
            || v["scope"] != "device"
            || v["work_instance"] != instance
            || v["before"] != state.revision
            || v["task"] != state.task
            || serde_json::to_vec(v)?.len() > 65536
            || state.run.as_ref().is_some_and(|old| old != &token)
            || !matches!(
                status,
                "preparing" | "review" | "running" | "verified" | "failed" | "cancelled" | "closed"
            )
            || (self.action == "close") != (status == "closed")
            || self.action == "review" && status != "preparing"
            || self.action == "apply" && status != "running"
            || applied != (state.applied || self.action == "apply")
        {
            return Err(bad());
        }
        if self.action == "status"
            && !match state.status.as_str() {
                "preparing" => matches!(status, "preparing" | "review" | "failed" | "cancelled"),
                "running" => matches!(status, "running" | "verified" | "failed" | "cancelled"),
                old => old == status,
            }
        {
            return Err(bad());
        }
        let plan = (!v["plan"].is_null()).then(|| v["plan"].clone());
        let plan_hash = if let Some(plan) = &plan {
            validate_plan(plan, &state)?;
            let actual = hash(v, "plan_sha256")?;
            if digest(plan)? != actual {
                return Err(bad());
            }
            Some(actual)
        } else {
            if !v["plan_sha256"].is_null() {
                return Err(bad());
            }
            None
        };
        if state
            .plan
            .as_ref()
            .is_some_and(|old| Some(old) != plan.as_ref())
            || matches!(status, "review" | "running" | "verified") && plan.is_none()
            || status == "preparing" && plan.is_some()
        {
            return Err(bad());
        }
        let result = (!v["result"].is_null()).then(|| v["result"].clone());
        if let Some(result) = &result {
            let plan = plan.as_ref().ok_or_else(bad)?;
            let kind = match state.task.as_str() {
                "fallback" => "fallback_finished",
                "edge" => "edge_ready",
                _ => "stt_cache_ready",
            };
            if !applied
                || !matches!(status, "verified" | "closed")
                || *result
                    != json!({"kind":kind,"interpreter":plan["interpreter"],"cache":plan["cache"],"model":plan["model"],"dependencies_verified":true})
            {
                return Err(bad());
            }
        }
        if status == "verified" && result.is_none()
            || state
                .result
                .as_ref()
                .is_some_and(|old| Some(old) != result.as_ref())
        {
            return Err(bad());
        }
        state.run = Some(token);
        state.status = status.into();
        state.plan = plan;
        state.plan_hash = plan_hash;
        state.result = result;
        state.applied = applied;
        Ok(state)
    }
}
fn validate_plan(plan: &Value, state: &State) -> io::Result<()> {
    keys(
        plan,
        &[
            "version",
            "action",
            "interpreter",
            "launcher",
            "install",
            "argv",
            "packages",
            "prefix",
            "cache",
            "model",
            "engine",
            "voice",
            "timeout",
            "phrase",
            "fingerprints",
            "modules",
            "network",
        ],
    )?;
    let fallback = state.task == "fallback";
    let stt = state.task == "stt";
    let edge = fallback && state.preferences["engine"] == "edge";
    let packages = if fallback {
        json!([])
    } else if stt {
        json!(["faster-whisper"])
    } else {
        json!(["edge-tts", "playsound==1.2.2"])
    };
    let modules = if stt {
        json!(["faster_whisper"])
    } else if !fallback || edge {
        json!(["edge_tts", "playsound"])
    } else {
        json!(["pyttsx3"])
    };
    let timeout = if fallback {
        state.preferences["timeout"].clone()
    } else if stt {
        json!(600)
    } else {
        json!(240)
    };
    for (key, expected) in [
        ("version", json!(1)),
        ("action", json!(state.task)),
        ("packages", packages.clone()),
        ("modules", modules),
        (
            "model",
            if stt {
                state.preferences["model"].clone()
            } else {
                Value::Null
            },
        ),
        (
            "engine",
            if fallback {
                state.preferences["engine"].clone()
            } else {
                Value::Null
            },
        ),
        (
            "voice",
            if fallback {
                state.preferences["voice"].clone()
            } else {
                Value::Null
            },
        ),
        ("timeout", timeout),
        ("phrase", if fallback { json!(PHRASE) } else { Value::Null }),
        ("network", json!(!fallback || edge)),
    ] {
        if plan[key] != expected {
            return Err(bad());
        }
    }
    let exe = path(&plan["interpreter"])?;
    let launcher = path(&plan["launcher"])?;
    let prefix = path(&plan["prefix"])?;
    if stt {
        path(&plan["cache"])?;
    } else if !plan["cache"].is_null() {
        return Err(bad());
    }
    let install = plan["install"].as_bool().ok_or_else(bad)?;
    if fallback && install || !fallback && launcher != exe {
        return Err(bad());
    }
    let argv = plan["argv"]
        .as_array()
        .filter(|a| a.len() <= 16)
        .ok_or_else(bad)?;
    for arg in argv {
        text(arg)?;
    }
    let mut executables = BTreeSet::from([exe, launcher]);
    if install {
        let first = path(argv.first().ok_or_else(bad)?)?;
        let mut pip = vec![
            json!(exe),
            json!("-E"),
            json!("-B"),
            json!("-m"),
            json!("pip"),
            json!("install"),
            json!("--prefix"),
            json!(prefix),
        ];
        pip.extend(packages.as_array().unwrap().clone());
        let mut uv = vec![
            json!(first),
            json!("pip"),
            json!("install"),
            json!("--python"),
            json!(exe),
            json!("--prefix"),
            json!(prefix),
            json!("--no-python-downloads"),
        ];
        uv.extend(packages.as_array().unwrap().clone());
        let uv_name = std::path::Path::new(first)
            .file_name()
            .and_then(|v| v.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if *argv != pip && (!matches!(uv_name.as_str(), "uv" | "uv.exe") || *argv != uv) {
            return Err(bad());
        }
        executables.insert(first);
    } else if !argv.is_empty() {
        return Err(bad());
    }
    let fingerprints = plan["fingerprints"].as_object().ok_or_else(bad)?;
    if fingerprints
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != executables
    {
        return Err(bad());
    }
    for key in fingerprints.keys() {
        hash(&plan["fingerprints"], key)?;
    }
    Ok(())
}
#[derive(Default)]
pub struct View {
    state: Option<State>,
    waiting: bool,
    visible: bool,
    note: String,
}
impl View {
    pub fn open(&self) -> bool {
        self.state.is_some()
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn start(
        &mut self,
        task: &str,
        revision: String,
        preferences: Value,
        selection: Value,
    ) -> Request {
        let state = State {
            task: task.into(),
            revision,
            preferences,
            selection,
            run: None,
            status: "preparing".into(),
            plan: None,
            plan_hash: None,
            result: None,
            applied: false,
        };
        self.state = Some(state.clone());
        self.waiting = true;
        self.hide();
        self.note.clear();
        Request {
            state,
            action: "review",
        }
    }
    pub fn update(&mut self, result: Result<State, String>) {
        self.waiting = false;
        self.hide();
        match result {
            Ok(state) if state.status == "closed" => *self = Self::default(),
            Ok(state) => {
                self.state = Some(state);
                self.note.clear();
            }
            Err(why) => self.note = crate::live::terminal_text(&why),
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || key.modifiers != KeyModifiers::NONE || self.waiting {
            return None;
        }
        let state = self.state.as_ref()?;
        let action = match key.code {
            KeyCode::F(5) => "status",
            KeyCode::Esc => "close",
            KeyCode::Enter if self.visible && state.status == "review" => "apply",
            _ => return None,
        };
        state.run.as_ref()?;
        let request = Request {
            state: state.clone(),
            action,
        };
        self.waiting = true;
        self.hide();
        Some(request)
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let Some(state) = &self.state else { return };
        let title = match state.task.as_str() {
            "fallback" => "Test fallback speech",
            "edge" => "Install Edge support",
            _ => "Download dictation model",
        };
        let mut lines = vec![title.to_owned(), format!("Status: {}", state.status)];
        if let Some(plan) = &state.plan {
            lines.push(format!(
                "Interpreter: {}",
                plan["interpreter"].as_str().unwrap()
            ));
            if plan["launcher"] != plan["interpreter"] {
                lines.push(format!(
                    "Speech launcher: {}",
                    plan["launcher"].as_str().unwrap()
                ));
            }
            if state.task == "fallback" {
                lines.push(format!(
                    "Fallback: {}   Voice: {}",
                    plan["engine"].as_str().unwrap(),
                    plan["voice"].as_str().unwrap()
                ));
                lines.push(format!("Speak: {PHRASE}"));
                lines.push(format!(
                    "Timeout: {} seconds. LiteSuite routing is disabled.",
                    plan["timeout"]
                ));
            } else {
                lines.push(format!("Packages: {}", plan["packages"]));
                lines.push(format!(
                    "Package prefix: {}",
                    plan["prefix"].as_str().unwrap()
                ));
                lines.push(if plan["install"] == true {
                    format!("Install command: {}", plan["argv"])
                } else {
                    "Dependencies already importable; no install command.".into()
                });
                if state.task == "stt" {
                    lines.push(format!(
                        "Model: {} (CPU, int8)",
                        plan["model"].as_str().unwrap()
                    ));
                    lines.push(format!("Cache: {}", plan["cache"].as_str().unwrap()));
                }
            }
            lines.push(if plan["network"] == true {
                "This action may access the network.".into()
            } else {
                "Local speech action.".into()
            });
        }
        if state.status == "review" {
            lines.push("Enter Apply   Esc cancel without running the action".into());
        } else {
            lines.push("F5 refresh progress   Esc cancel/close and join helper".into());
        }
        if state.result.is_some() {
            lines.push(
                match state.task.as_str() {
                    "fallback" => "Speech child finished; audibility is unverified.",
                    "edge" => "Edge dependencies verified by a fresh interpreter.",
                    _ => "Requested model verified from the local cache without network.",
                }
                .into(),
            );
        }
        if state.task != "fallback" {
            lines.push("Cancellation stops running children; installed packages and cache files may remain.".into());
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone());
        }
        if self.waiting {
            lines.push("Waiting for the source…".into());
        }
        let wrapped = crate::ui::wrap(&lines.join("\n"), usize::from(area.width.saturating_sub(2)));
        self.visible =
            area.width >= 36 && wrapped.len() <= usize::from(area.height.saturating_sub(2));
        let shown = if self.visible {
            wrapped.join("\n")
        } else {
            "Resize to see the complete action review. Esc cancels/joins; Ctrl+Q quits.".into()
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(shown)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Voice action ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Identity, Request, Value) {
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let mut view = View::default();
        let request = view.start(
            "stt",
            "a".repeat(64),
            json!({"engine":"pyttsx3","voice":"","timeout":300,"model":"base.en"}),
            json!({}),
        );
        let exe = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .to_string();
        let prefix = std::env::temp_dir().to_string_lossy().to_string();
        let plan = json!({"version":1,"action":"stt","interpreter":exe,"launcher":exe,"install":false,"argv":[],"packages":["faster-whisper"],"prefix":prefix,"cache":prefix,"model":"base.en","engine":null,"voice":null,"timeout":600,"phrase":null,"fingerprints":{exe:"1".repeat(64)},"modules":["faster_whisper"],"network":true});
        (id, request, plan)
    }
    fn response(id: &Identity, state: &State, status: &str, plan: Option<&Value>) -> Value {
        let mut result = id.request();
        for (key, value) in [
            ("kind", json!("voice_action")),
            ("scope", json!("device")),
            ("work_instance", json!("i")),
            ("run", json!("11b31da4-0b1d-445d-98f6-084bf4d571e4")),
            ("before", json!(state.revision)),
            ("task", json!(state.task)),
            ("status", json!(status)),
            ("plan", json!(plan)),
            ("plan_sha256", json!(plan.map(|p| digest(p).unwrap()))),
            ("result", Value::Null),
            ("applied", json!(false)),
        ] {
            result[key] = value;
        }
        result
    }
    #[test]
    fn review_plan_rejects_wrong_target_packages_cache_and_hash() {
        let (id, request, plan) = fixture();
        let preparing = response(&id, &request.state, "preparing", None);
        let state = request.response(&preparing, &id, "i").unwrap();
        let request = Request {
            state: state.clone(),
            action: "status",
        };
        let ready = response(&id, &state, "review", Some(&plan));
        assert!(request.clone().response(&ready, &id, "i").is_ok());
        for (path, value) in [
            ("/before", json!("b".repeat(64))),
            ("/run", json!("85ed8fd0-9ec4-4517-9b7f-18ae4ec13cb6")),
            ("/plan/model", json!("small.en")),
            ("/plan/packages", json!(["other-package"])),
            ("/plan/cache", json!("relative/cache")),
            ("/plan_sha256", json!("0".repeat(64))),
        ] {
            let mut changed = ready.clone();
            *changed.pointer_mut(path).unwrap() = value;
            assert!(
                request.clone().response(&changed, &id, "i").is_err(),
                "{path}"
            );
        }
    }
    #[test]
    fn apply_requires_painted_complete_review_and_only_verifies_exact_result() {
        use ratatui::{Terminal, backend::TestBackend};
        let (id, request, plan) = fixture();
        let state = request
            .response(
                &response(&id, &fixture().1.state, "preparing", None),
                &id,
                "i",
            )
            .unwrap();
        let ready = response(&id, &state, "review", Some(&plan));
        let state = Request {
            state,
            action: "status",
        }
        .response(&ready, &id, "i")
        .unwrap();
        let mut view = View {
            state: Some(state),
            ..View::default()
        };
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        assert!(view.key(key(KeyCode::Enter)).is_none());
        let theme = crate::theme::builtin_themes().remove(0);
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
        let apply = view.key(key(KeyCode::Enter)).unwrap();
        assert_eq!(apply.command(&id, "i").unwrap()["action"], "apply");
        let mut running = response(&id, &apply.state, "running", Some(&plan));
        running["applied"] = json!(true);
        let state = apply.response(&running, &id, "i").unwrap();
        let poll = Request {
            state: state.clone(),
            action: "status",
        };
        let mut complete = running;
        complete["status"] = json!("verified");
        assert!(poll.clone().response(&complete, &id, "i").is_err());
        complete["result"] = json!({"kind":"stt_cache_ready","interpreter":plan["interpreter"],"cache":plan["cache"],"model":"base.en","dependencies_verified":true});
        assert!(poll.clone().response(&complete, &id, "i").is_ok());
        complete["result"]["model"] = json!("small.en");
        assert!(poll.response(&complete, &id, "i").is_err());
    }
    #[test]
    fn installer_command_cannot_change_prefix_or_add_packages() {
        let (_, request, mut plan) = fixture();
        let exe = plan["interpreter"].clone();
        let prefix = plan["prefix"].clone();
        plan["install"] = json!(true);
        plan["argv"] = json!([
            exe,
            "-E",
            "-B",
            "-m",
            "pip",
            "install",
            "--prefix",
            prefix,
            "faster-whisper"
        ]);
        assert!(validate_plan(&plan, &request.state).is_ok());
        plan["argv"][7] = json!("elsewhere");
        assert!(validate_plan(&plan, &request.state).is_err());
    }
}
