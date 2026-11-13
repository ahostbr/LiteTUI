//! Captured browser-monitor destinations, explicit review and joined owned cleanup.
use crate::{theme::Theme, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const OPERATIONS: [&str; 5] = [
    "gui.monitor.native_snapshot",
    "gui.monitor.native_review",
    "gui.monitor.native_apply",
    "gui.monitor.native_readback",
    "gui.monitor.native_close",
];
fn bad() -> io::Error {
    io::Error::other("Monitor review, output inventory or owned readback changed")
}
fn keys(v: &Value, expected: &[&str]) -> io::Result<()> {
    let map = v.as_object().ok_or_else(bad)?;
    if map.len() != expected.len() || expected.iter().any(|key| !map.contains_key(*key)) {
        return Err(bad());
    }
    Ok(())
}
fn text(v: &Value, limit: usize) -> io::Result<&str> {
    v.as_str()
        .filter(|s| s.len() <= limit && !s.chars().any(char::is_control))
        .ok_or_else(bad)
}
fn hash(v: &Value) -> io::Result<&str> {
    text(v, 64)?
        .as_bytes()
        .iter()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        .then_some(())
        .ok_or_else(bad)?;
    let s = text(v, 64)?;
    if s.len() != 64 {
        return Err(bad());
    }
    Ok(s)
}
fn path(v: &Value) -> io::Result<&str> {
    let s = text(v, 4096)?;
    if !std::path::Path::new(s).is_absolute() {
        return Err(bad());
    }
    Ok(s)
}
fn artifact(v: &Value) -> io::Result<()> {
    keys(v, &["path", "sha256", "bytes"])?;
    path(&v["path"])?;
    let bytes = v["bytes"]
        .as_u64()
        .filter(|n| *n <= 16 * 1024 * 1024)
        .ok_or_else(bad)?;
    if v["sha256"].is_null() {
        if bytes != 0 {
            return Err(bad());
        }
    } else {
        hash(&v["sha256"])?;
    }
    Ok(())
}
fn base(id: &Identity, instance: &str) -> Value {
    json!({"version":1,"session_id":id.session,"agent_id":id.agent,"backend":id.backend,"model":id.model,"work_instance":instance})
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot(Value);
impl Snapshot {
    fn parse(v: &Value) -> io::Result<Self> {
        if serde_json::to_vec(v)?.len() > 262144 {
            return Err(bad());
        }
        keys(
            v,
            &[
                "action",
                "arguments",
                "workspace",
                "config",
                "allowed",
                "reason",
                "targets",
                "outputs",
                "revision",
                "sidecars",
                "seed",
            ],
        )?;
        let action = text(&v["action"], 16)?;
        if !matches!(action, "targets" | "init" | "demo" | "sweep") {
            return Err(bad());
        }
        text(&v["arguments"], 8192)?;
        path(&v["workspace"])?;
        artifact(&v["config"])?;
        let allowed = v["allowed"].as_bool().ok_or_else(bad)?;
        let reason = text(&v["reason"], 1024)?;
        if allowed != reason.is_empty() {
            return Err(bad());
        }
        let targets = v["targets"]
            .as_array()
            .filter(|a| a.len() <= 16)
            .ok_or_else(bad)?;
        let mut names = std::collections::HashSet::new();
        for target in targets {
            keys(
                target,
                &["name", "url", "url_sha256", "screenshot", "timeout_ms"],
            )?;
            let name = text(&target["name"], 64)?;
            if name.is_empty()
                || !names.insert(name.to_ascii_lowercase())
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            {
                return Err(bad());
            }
            let url = text(&target["url"], 2048)?;
            if !url.starts_with("http://") && !url.starts_with("https://") {
                return Err(bad());
            }
            hash(&target["url_sha256"])?;
            target["screenshot"].as_bool().ok_or_else(bad)?;
            target["timeout_ms"]
                .as_u64()
                .filter(|n| (1..=60000).contains(n))
                .ok_or_else(bad)?;
        }
        let outputs = v["outputs"].as_array().ok_or_else(bad)?;
        let count = match action {
            "targets" => 0,
            "sweep" => 2 + targets.iter().filter(|t| t["screenshot"] == true).count(),
            _ => 1,
        };
        if outputs.len() != count {
            return Err(bad());
        }
        let mut paths = std::collections::HashSet::new();
        let mut total = 0;
        for output in outputs {
            artifact(output)?;
            if !paths.insert(path(&output["path"])?.to_ascii_lowercase()) {
                return Err(bad());
            }
            total += output["bytes"].as_u64().ok_or_else(bad)?;
        }
        if total > 64 * 1024 * 1024 {
            return Err(bad());
        }
        let sidecars = v["sidecars"].as_array().ok_or_else(bad)?;
        if sidecars.len() != outputs.len()
            || sidecars.iter().zip(outputs).any(|(s, o)| {
                s.as_str() != Some(&format!("{}.lock", o["path"].as_str().unwrap_or_default()))
            })
        {
            return Err(bad());
        }
        if action == "init" {
            let seed = v["seed"]
                .as_str()
                .filter(|s| s.len() <= 65536)
                .ok_or_else(bad)?;
            serde_json::from_str::<Value>(seed)?;
            if outputs[0] != v["config"] || allowed != v["config"]["sha256"].is_null() {
                return Err(bad());
            }
        } else if !v["seed"].is_null() {
            return Err(bad());
        }
        if action == "sweep" && allowed == targets.is_empty() {
            return Err(bad());
        }
        let revision = hash(&v["revision"])?;
        let mut document = v.clone();
        document.as_object_mut().ok_or_else(bad)?.remove("revision");
        if format!("{:x}", Sha256::digest(serde_json::to_vec(&document)?)) != revision {
            return Err(bad());
        }
        Ok(Self(v.clone()))
    }
    fn eligible(&self) -> bool {
        self.0["allowed"] == true && self.0["action"] != "targets"
    }
    fn lines(&self) -> String {
        let v = &self.0;
        let mut content = format!(
            "Monitor {}\nWorkspace: {}\nConfig: {}\nConfig SHA-256: {}\n{}\n",
            v["action"].as_str().unwrap_or_default(),
            path(&v["workspace"]).unwrap_or_default(),
            path(&v["config"]["path"]).unwrap_or_default(),
            v["config"]["sha256"]
                .as_str()
                .unwrap_or("missing / unused for demo"),
            v["reason"].as_str().unwrap_or_default()
        );
        for t in v["targets"].as_array().into_iter().flatten() {
            content.push_str(&format!(
                "\n{}  {}\nURL hash: {}\nTimeout {} ms; screenshot {}\n",
                t["name"].as_str().unwrap_or_default(),
                t["url"].as_str().unwrap_or_default(),
                t["url_sha256"].as_str().unwrap_or_default(),
                t["timeout_ms"],
                t["screenshot"]
            ));
        }
        for o in v["outputs"].as_array().into_iter().flatten() {
            content.push_str(&format!(
                "\nOutput: {}\nBefore: {} bytes, {}\n",
                o["path"].as_str().unwrap_or_default(),
                o["bytes"],
                o["sha256"].as_str().unwrap_or("missing")
            ));
        }
        if self.eligible() {
            content.push_str("\nCreates missing destination directories and .lock coordination files beside these outputs. Changed existing outputs refuse publication.\n");
        }
        content.push_str(match v["action"].as_str().unwrap_or_default() {
            "sweep" => "\nUses the existing Chrome bridge. Opens a new tab for each exact configured URL, reads page text and captures requested screenshots. No browser launch. Cancellation joins this helper but cannot undo browser tabs or remote requests. Log appends; baselines and screenshots may change. URLs shown and logged omit credentials and query values.\n",
            "demo" => "\nUses fixed fake probes without a browser or network. Appends sample metrics to the reviewed log.\n",
            "init" => "\nCreates the selected JSON config only if absent. No browser or network. Exact seed:\n",
            _ => "\nRead-only target inventory.\n"
        });
        if let Some(seed) = v["seed"].as_str() {
            content.push_str(seed);
        }
        crate::live::terminal_text(&content)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    snapshot: Snapshot,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Active {
    review: Review,
    apply_id: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    Snapshot(String),
    Review(Snapshot),
    Apply(Review),
    Readback(Active),
    Close(Active),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot(Snapshot),
    Review(Review),
    Applied(Active),
    Readback(Active, Value),
    Closed(Value),
}
impl Request {
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut v = base(id, instance);
        let op = match self {
            Self::Snapshot(arguments) => {
                text(&json!(arguments), 8192)?;
                v["arguments"] = json!(arguments);
                0
            }
            Self::Review(snapshot) => {
                v["arguments"] = snapshot.0["arguments"].clone();
                v["revision"] = snapshot.0["revision"].clone();
                1
            }
            Self::Apply(review) => {
                v["token"] = json!(review.token);
                2
            }
            Self::Readback(active) | Self::Close(active) => {
                v["token"] = json!(active.review.token);
                v["apply_id"] = json!(active.apply_id);
                if matches!(self, Self::Close(_)) { 4 } else { 3 }
            }
        };
        v["type"] = json!(OPERATIONS[op]);
        Ok(v)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command_id: &str,
    ) -> io::Result<Update> {
        if serde_json::to_vec(v)?.len() > 524288 {
            return Err(bad());
        }
        let snapshot = Snapshot::parse(&v["snapshot"])?;
        let mut expected = base(id, instance);
        expected["snapshot"] = snapshot.0.clone();
        match self {
            Self::Snapshot(arguments) => {
                expected["kind"] = json!("monitor_snapshot");
                if expected != *v || snapshot.0["arguments"] != arguments {
                    return Err(bad());
                }
                Ok(Update::Snapshot(snapshot))
            }
            Self::Review(before) => {
                let token = hash(&v["token"])?;
                expected["kind"] = json!("monitor_review");
                expected["token"] = json!(token);
                if expected != *v || snapshot != before || !snapshot.eligible() {
                    return Err(bad());
                }
                Ok(Update::Review(Review {
                    snapshot,
                    token: token.into(),
                }))
            }
            request => {
                let (active, kind) = match request {
                    Self::Apply(review) => (
                        Active {
                            review,
                            apply_id: command_id.into(),
                        },
                        "monitor_applied",
                    ),
                    Self::Readback(active) => (active, "monitor_readback"),
                    Self::Close(active) => (active, "monitor_closed"),
                    _ => unreachable!(),
                };
                if snapshot != active.review.snapshot {
                    return Err(bad());
                }
                let status = text(&v["status"], 16)?;
                if !matches!(
                    status,
                    "running" | "completed" | "failed" | "cancelled" | "uncertain"
                ) {
                    return Err(bad());
                }
                let cleanup = v["cleanup_verified"].as_bool().ok_or_else(bad)?;
                let rows = v["rows"].as_array().ok_or_else(bad)?;
                let targets = snapshot.0["targets"].as_array().ok_or_else(bad)?;
                let row_count = if snapshot.0["action"] == "init" {
                    0
                } else {
                    targets.len()
                };
                if (!rows.is_empty() && rows.len() != row_count)
                    || (status == "completed" && rows.len() != row_count)
                {
                    return Err(bad());
                }
                for (row, target) in rows.iter().zip(targets) {
                    keys(
                        row,
                        &[
                            "name",
                            "url",
                            "ok",
                            "status",
                            "text_chars",
                            "expect_met",
                            "regression",
                            "error",
                        ],
                    )?;
                    let ok = row["ok"].as_bool().ok_or_else(bad)?;
                    if row["name"] != target["name"]
                        || row["url"] != target["url"]
                        || row["status"] != if ok { "ok" } else { "error" }
                    {
                        return Err(bad());
                    }
                    row["expect_met"].as_bool().ok_or_else(bad)?;
                    row["text_chars"]
                        .as_u64()
                        .filter(|n| *n <= 100_000_000)
                        .ok_or_else(bad)?;
                    if !matches!(
                        text(&row["regression"], 16)?,
                        "none" | "baseline" | "stable" | "changed"
                    ) || !matches!(
                        text(&row["error"], 128)?,
                        "" | "Probe failed; see the selected browser target."
                    ) {
                        return Err(bad());
                    }
                }
                let artifacts = v["artifacts"].as_array().ok_or_else(bad)?;
                let outputs = snapshot.0["outputs"].as_array().ok_or_else(bad)?;
                if (status == "running" && (cleanup || !artifacts.is_empty()))
                    || (status != "running" && artifacts.len() != outputs.len())
                    || (status == "completed" && !cleanup)
                    || (kind == "monitor_closed" && !cleanup)
                {
                    return Err(bad());
                }
                for (artifact_row, output) in artifacts.iter().zip(outputs) {
                    artifact(artifact_row)?;
                    if artifact_row["path"] != output["path"] {
                        return Err(bad());
                    }
                }
                if status == "completed"
                    && snapshot.0["action"] == "init"
                    && (artifacts[0]["sha256"]
                        != format!(
                            "{:x}",
                            Sha256::digest(snapshot.0["seed"].as_str().ok_or_else(bad)?.as_bytes())
                        ))
                {
                    return Err(bad());
                }
                expected["kind"] = json!(kind);
                expected["token"] = json!(active.review.token);
                expected["apply_id"] = json!(active.apply_id);
                for name in ["status", "rows", "artifacts", "cleanup_verified"] {
                    expected[name] = v[name].clone();
                }
                if expected != *v {
                    return Err(bad());
                }
                Ok(match kind {
                    "monitor_applied" => Update::Applied(active),
                    "monitor_closed" => Update::Closed(v.clone()),
                    _ => Update::Readback(active, v.clone()),
                })
            }
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    arguments: String,
    snapshot: Option<Snapshot>,
    review: Option<Review>,
    active: Option<Active>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
}
impl View {
    pub fn open(&mut self, arguments: &str) {
        *self = Self {
            open: true,
            waiting: true,
            arguments: arguments.into(),
            notice: "Reading monitor targets and exact output destinations…".into(),
            ..Self::default()
        };
    }
    pub fn hide(&mut self) {
        self.seen.clear();
    }
    pub fn update(&mut self, result: Result<Update, String>) -> Option<Request> {
        self.waiting = false;
        self.page = 0;
        self.seen.clear();
        self.review = None;
        match result {
            Err(reason) => {
                self.notice = format!(
                    "Inspection failed: {reason}. An owned attempt stays held; Esc retries joined Close, Quit settles it."
                );
            }
            Ok(Update::Snapshot(snapshot)) => {
                self.snapshot = Some(snapshot);
                self.notice.clear();
            }
            Ok(Update::Review(review)) => {
                self.snapshot = Some(review.snapshot.clone());
                self.review = Some(review);
                self.notice.clear();
            }
            Ok(Update::Applied(active)) => {
                self.active = Some(active.clone());
                self.waiting = true;
                return Some(Request::Readback(active));
            }
            Ok(Update::Readback(active, value)) => {
                self.active = Some(active);
                self.notice = Self::outcome(&value);
            }
            Ok(Update::Closed(value)) => {
                self.active = None;
                self.notice = Self::outcome(&value);
                self.open = false;
            }
        }
        None
    }
    fn outcome(v: &Value) -> String {
        let mut s = format!(
            "Monitor {}. Helper joined: {}.\n",
            v["status"].as_str().unwrap_or_default(),
            v["cleanup_verified"]
        );
        for row in v["rows"].as_array().into_iter().flatten() {
            s.push_str(&format!(
                "{}: {}, expected text {}, regression {}\n",
                row["name"].as_str().unwrap_or_default(),
                row["status"].as_str().unwrap_or_default(),
                row["expect_met"],
                row["regression"].as_str().unwrap_or_default()
            ));
        }
        for artifact in v["artifacts"].as_array().into_iter().flatten() {
            s.push_str(&format!(
                "Read back {} bytes: {}\nSHA-256: {}\n",
                artifact["bytes"],
                artifact["path"].as_str().unwrap_or_default(),
                artifact["sha256"].as_str().unwrap_or("missing")
            ));
        }
        s
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        let request = match key.code {
            KeyCode::Esc => {
                if let Some(active) = self.active.clone() {
                    Some(Request::Close(active))
                } else {
                    self.open = false;
                    self.review = None;
                    None
                }
            }
            KeyCode::F(5) | KeyCode::Char('r') => Some(if let Some(active) = self.active.clone() {
                Request::Readback(active)
            } else {
                Request::Snapshot(self.arguments.clone())
            }),
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                None
            }
            KeyCode::PageUp | KeyCode::Left => {
                self.page = self.page.saturating_sub(1);
                None
            }
            KeyCode::Enter
                if self.active.is_none()
                    && !self.seen.is_empty()
                    && self.seen.iter().all(|seen| *seen) =>
            {
                if let Some(review) = self.review.take() {
                    Some(Request::Apply(review))
                } else {
                    self.snapshot
                        .clone()
                        .filter(Snapshot::eligible)
                        .map(Request::Review)
                }
            }
            _ => None,
        };
        if request.is_some() {
            self.waiting = true;
            self.seen.clear();
        }
        request
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::bordered()
                .title(" Browser monitor ")
                .style(Style::default().fg(theme.text()).bg(theme.screen())),
            area,
        );
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.hide();
            return;
        }
        let mut content = self.notice.clone();
        if let Some(snapshot) = &self.snapshot {
            content.push_str("\n\n");
            content.push_str(&snapshot.lines());
        }
        let lines = crate::ui::wrap(&content, body.width as usize);
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        if self.geometry != Some((body.width, body.height)) || self.seen.len() != self.pages {
            self.geometry = Some((body.width, body.height));
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        let action = if self.waiting {
            "Waiting for owned source"
        } else if self.active.is_some() {
            "R refresh  Esc cancel / close and join"
        } else if self.review.is_some() {
            "Enter confirm  Esc cancel"
        } else {
            "Enter review  R refresh  Esc return"
        };
        frame.render_widget(
            Paragraph::new(
                lines
                    .iter()
                    .skip(self.page * height)
                    .take(height)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            body,
        );
        frame.render_widget(
            Paragraph::new(format!(
                "PgUp/PgDn {}/{}  {}",
                self.page + 1,
                self.pages,
                action
            )),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    fn id() -> Identity {
        Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "fixture".into(),
        }
    }
    fn snapshot() -> Snapshot {
        let root = std::env::temp_dir().join("monitor-fixture");
        let output = root
            .join("monitor-log.jsonl")
            .to_string_lossy()
            .into_owned();
        let mut v = json!({"action":"demo","arguments":"demo","workspace":root.to_string_lossy(),"config":{"path":root.join("monitor-targets.json").to_string_lossy(),"sha256":null,"bytes":0},"allowed":true,"reason":"","targets":[{"name":"example","url":"https://example.test/?[redacted]","url_sha256":"c".repeat(64),"screenshot":false,"timeout_ms":30000}],"outputs":[{"path":output,"sha256":null,"bytes":0}],"sidecars":[format!("{output}.lock")],"seed":null});
        v["revision"] = json!(format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&v).unwrap())
        ));
        Snapshot::parse(&v).unwrap()
    }
    fn response(snapshot: &Snapshot, kind: &str) -> Value {
        let mut v = base(&id(), "instance");
        v["snapshot"] = snapshot.0.clone();
        v["kind"] = json!(kind);
        v
    }
    fn reviewed() -> Review {
        Review {
            snapshot: snapshot(),
            token: "a".repeat(64),
        }
    }
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn paint(view: &mut View, width: u16, height: u16) {
        Terminal::new(TestBackend::new(width, height))
            .unwrap()
            .draw(|frame| view.draw(frame, frame.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
    }
    #[test]
    fn exact_snapshot_and_review_reject_drift() {
        let sample = snapshot();
        let v = response(&sample, "monitor_snapshot");
        assert!(
            Request::Snapshot("demo".into())
                .response(&v, &id(), "instance", "snapshot")
                .is_ok()
        );
        for (key, value) in [("extra", json!(0)), ("work_instance", json!("foreign"))] {
            let mut changed = v.clone();
            changed[key] = value;
            assert!(
                Request::Snapshot("demo".into())
                    .response(&changed, &id(), "instance", "snapshot")
                    .is_err()
            );
        }
        let mut changed = v.clone();
        changed["snapshot"]["outputs"][0]["path"] =
            json!(std::env::temp_dir().join("other").to_string_lossy());
        assert!(
            Request::Snapshot("demo".into())
                .response(&changed, &id(), "instance", "snapshot")
                .is_err()
        );
        let mut review = response(&sample, "monitor_review");
        review["token"] = json!("a".repeat(64));
        assert!(
            Request::Review(sample.clone())
                .response(&review, &id(), "instance", "review")
                .is_ok()
        );
        review["snapshot"]["revision"] = json!("b".repeat(64));
        assert!(
            Request::Review(sample)
                .response(&review, &id(), "instance", "review")
                .is_err()
        );
    }
    #[test]
    fn every_page_and_fresh_confirm_are_required_twice() {
        let mut view = View::default();
        view.open("demo");
        view.update(Ok(Update::Snapshot(snapshot())));
        assert!(view.key(key(KeyCode::Enter)).is_none());
        paint(&mut view, 40, 10);
        assert!(view.pages > 1);
        assert!(view.key(key(KeyCode::Enter)).is_none());
        for _ in 1..view.pages {
            view.key(key(KeyCode::PageDown));
            paint(&mut view, 40, 10);
        }
        assert!(matches!(
            view.key(key(KeyCode::Enter)),
            Some(Request::Review(_))
        ));
        view.update(Ok(Update::Review(reviewed())));
        assert!(view.key(key(KeyCode::Enter)).is_none());
        for _ in 0..view.pages {
            paint(&mut view, 40, 10);
            view.key(key(KeyCode::PageDown));
        }
        view.hide();
        assert!(view.key(key(KeyCode::Enter)).is_none());
        for page in 0..view.pages {
            view.page = page;
            paint(&mut view, 40, 10);
        }
        let mut repeated = key(KeyCode::Enter);
        repeated.kind = KeyEventKind::Repeat;
        assert!(view.key(repeated).is_none());
        assert!(matches!(
            view.key(key(KeyCode::Enter)),
            Some(Request::Apply(_))
        ));
    }
    #[test]
    fn applied_attempt_keeps_close_authority_when_readback_fails() {
        let mut view = View::default();
        view.open("demo");
        let active = Active {
            review: reviewed(),
            apply_id: "apply".into(),
        };
        assert_eq!(
            view.update(Ok(Update::Applied(active.clone()))),
            Some(Request::Readback(active.clone()))
        );
        view.update(Err("controlled lost readback".into()));
        assert_eq!(view.key(key(KeyCode::Esc)), Some(Request::Close(active)));
        assert!(view.open);
    }
    #[test]
    fn terminal_outcome_requires_exact_artifact_inventory_and_joined_cleanup() {
        let review = reviewed();
        let active = Active {
            review: review.clone(),
            apply_id: "apply".into(),
        };
        let mut v = response(&review.snapshot, "monitor_readback");
        v["token"] = json!(review.token);
        v["apply_id"] = json!(active.apply_id);
        v["status"] = json!("failed");
        v["rows"] = json!([]);
        v["artifacts"] = review.snapshot.0["outputs"].clone();
        v["cleanup_verified"] = json!(true);
        assert!(
            Request::Readback(active.clone())
                .response(&v, &id(), "instance", "readback")
                .is_ok()
        );
        for (field, value) in [
            ("artifacts", json!([])),
            ("apply_id", json!("foreign")),
            ("extra", json!(true)),
        ] {
            let mut changed = v.clone();
            changed[field] = value;
            assert!(
                Request::Readback(active.clone())
                    .response(&changed, &id(), "instance", "readback")
                    .is_err()
            );
        }
        v["kind"] = json!("monitor_closed");
        v["cleanup_verified"] = json!(false);
        assert!(
            Request::Close(active)
                .response(&v, &id(), "instance", "close")
                .is_err()
        );
    }
    #[test]
    fn actual_source_monitor_fixtures_validate_full_lifecycle() {
        let Some(root) = std::env::var_os("LITETUI_MONITOR_FIXTURES") else {
            return;
        };
        for action in ["init", "demo", "sweep"] {
            let case: Value = serde_json::from_slice(
                &std::fs::read(std::path::Path::new(&root).join(format!("{action}.json"))).unwrap(),
            )
            .unwrap();
            let source = &case["snapshot"];
            let identity = Identity {
                session: source["session_id"].as_str().unwrap().into(),
                agent: source["agent_id"].as_str().unwrap().into(),
                backend: source["backend"].as_str().unwrap().into(),
                model: source["model"].as_str().unwrap().into(),
            };
            let instance = source["work_instance"].as_str().unwrap();
            let Update::Snapshot(snapshot) = Request::Snapshot(action.into())
                .response(source, &identity, instance, "monitor-snapshot")
                .unwrap()
            else {
                panic!()
            };
            let Update::Review(review) = Request::Review(snapshot)
                .response(&case["review"], &identity, instance, "monitor-review")
                .unwrap()
            else {
                panic!()
            };
            let Update::Applied(active) = Request::Apply(review)
                .response(&case["apply"], &identity, instance, "monitor-apply")
                .unwrap()
            else {
                panic!()
            };
            assert!(matches!(
                Request::Readback(active.clone())
                    .response(&case["readback"], &identity, instance, "monitor-readback")
                    .unwrap(),
                Update::Readback(_, _)
            ));
            assert!(matches!(
                Request::Close(active)
                    .response(&case["closed"], &identity, instance, "monitor-close")
                    .unwrap(),
                Update::Closed(_)
            ));
        }
    }
}
