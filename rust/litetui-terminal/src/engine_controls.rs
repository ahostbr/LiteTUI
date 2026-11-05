//! Owned engine lifecycle: inspected plans are metadata, never readiness or permission.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const INSPECT: &str = "gui.engine.native_inspect";
pub const VALIDATE: &str = "gui.engine.native_validate";
pub const ACTION: &str = "gui.engine.native_action";
pub const READBACK: &str = "gui.engine.native_readback";
pub const OPERATIONS: [&str; 4] = [INSPECT, VALIDATE, ACTION, READBACK];
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text(v: &Value, key: &str, max: usize) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| !s.trim().is_empty() && s.len() <= max)
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid engine {key}")))
}
fn boolean(v: &Value, key: &str) -> io::Result<bool> {
    v[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid engine {key}")))
}
fn number(v: &Value, key: &str) -> io::Result<u64> {
    v[key]
        .as_u64()
        .ok_or_else(|| fail(&format!("Invalid engine {key}")))
}
fn token(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid engine token"));
    }
    Ok(s)
}
fn nullable_text(v: &Value, key: &str, max: usize) -> io::Result<Option<String>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        text(v, key, max).map(Some)
    }
}
fn nullable_token(v: &Value, key: &str) -> io::Result<Option<String>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        token(v, key).map(Some)
    }
}
fn bounded(v: &Value, kind: &str) -> io::Result<()> {
    if v["version"] != 1 || v["kind"] != kind || serde_json::to_vec(v)?.len() > 1048576 {
        return Err(fail("Invalid/oversized engine response"));
    }
    Ok(())
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admission {
    ManagementOnly,
    PromptReady,
    RetainedRecovery,
}
impl Admission {
    pub fn parse(v: &Value) -> io::Result<Self> {
        match v.as_str() {
            Some("management_only") => Ok(Self::ManagementOnly),
            Some("prompt_ready") => Ok(Self::PromptReady),
            Some("retained_recovery") => Ok(Self::RetainedRecovery),
            _ => Err(fail("Unknown engine admission")),
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::ManagementOnly => "Management only; prompts disabled",
            Self::PromptReady => "Connected; prompts available",
            Self::RetainedRecovery => "Owned handle retained; inspect or retry stop",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub instance: String,
}
impl Identity {
    pub fn parse(v: &Value) -> io::Result<Self> {
        let backend = text(v, "backend", 64)?;
        if !matches!(backend.as_str(), "ninfer" | "strata") {
            return Err(fail("Engine controls require NInfer or Strata"));
        }
        Ok(Self {
            session: text(v, "session_id", 128)?,
            agent: text(v, "agent_id", 128)?,
            backend,
            model: text(v, "desired_model", 1024)?,
            instance: token(v, "management_instance")?,
        })
    }
    pub fn verify(&self, v: &Value) -> io::Result<()> {
        if &Self::parse(v)? != self {
            return Err(fail("Engine response changed owned identity"));
        }
        Ok(())
    }
    pub fn command(&self, op: &str) -> Value {
        json!({"type":op,"version":1,"session_id":self.session,"agent_id":self.agent,"backend":self.backend,"desired_model":self.model,"management_instance":self.instance})
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub reconnect: Option<Value>,
    pub local_peer_start: Option<Value>,
    pub identity: Identity,
    pub token: String,
    pub generation: u64,
    pub admission: Admission,
    pub effective: Option<String>,
    pub ownership: Value,
    pub launch: Value,
    pub policy: Value,
    pub start: bool,
    pub stop: bool,
    pub start_reason: String,
    pub stop_reason: String,
}
fn peer_start(v: &Value, identity: &Identity, launch: &Value, generation: u64) -> io::Result<()> {
    use crate::work_lifecycle as life;
    use std::collections::BTreeSet;
    life::keys(
        v,
        &[
            "refused_action_id",
            "generation",
            "selection_id",
            "current",
            "peers",
            "spawn_new_engine",
            "other_engine_ownership",
            "resource_checks",
            "sha256",
        ],
    )?;
    crate::resume::conversation_id(&text(v, "refused_action_id", 36)?)?;
    if v["generation"] != generation
        || v["selection_id"] != launch["selection_id"]
        || generation == 0
        || v["spawn_new_engine"] != true
        || v["other_engine_ownership"] != "unverified"
        || v["resource_checks"] != "required"
    {
        return Err(fail(
            "Peer confirmation lost its original Start or resource limits",
        ));
    }
    let rows = v["peers"]
        .as_array()
        .filter(|rows| !rows.is_empty() && rows.len() <= 8)
        .ok_or_else(|| fail("Expected 1 to 8 other local seats"))?;
    let mut ids = BTreeSet::new();
    let mut pids = BTreeSet::new();
    for row in std::iter::once(&v["current"]).chain(rows) {
        life::keys(row, &["agent_id", "name", "backend", "pid", "born_us"])?;
        let agent = text(row, "agent_id", 128)?;
        let name = text(row, "name", 256)?;
        let backend = text(row, "backend", 64)?;
        if agent
            .chars()
            .chain(name.chars())
            .chain(backend.chars())
            .any(char::is_control)
            || !ids.insert(agent)
            || !pids.insert(number(row, "pid")?)
            || row["pid"] == 0
            || number(row, "born_us")? == 0
        {
            return Err(fail("Ambiguous or incomplete local process identity"));
        }
    }
    if v["current"]["agent_id"] != identity.agent || v["current"]["backend"] != identity.backend {
        return Err(fail(
            "Peer confirmation changed the current LiteTUI identity",
        ));
    }
    let mut body = v.clone();
    body.as_object_mut().unwrap().remove("sha256");
    if token(v, "sha256")? != crate::scheduled_transition::digest(&body)? {
        return Err(fail("Local peer confirmation hash changed"));
    }
    Ok(())
}
fn reconnect_plan(v: &Value, id: &Identity) -> io::Result<()> {
    crate::work_lifecycle::keys(
        v,
        &[
            "endpoint",
            "model",
            "start_process",
            "load_model",
            "acquire_handle",
            "selection_id",
        ],
    )?;
    let endpoint = text(v, "endpoint", 512)?;
    let (host, port) = endpoint
        .strip_prefix("https://")
        .or_else(|| endpoint.strip_prefix("http://"))
        .and_then(|s| s.rsplit_once(':'))
        .ok_or_else(|| fail("Missing connect-only host or port"))?;
    if !matches!(host, "127.0.0.1" | "localhost" | "[::1]")
        || !port
            .parse::<u16>()
            .is_ok_and(|n| n > 0 && n.to_string() == port)
        || v["model"] != id.model
        || ["start_process", "load_model", "acquire_handle"]
            .iter()
            .any(|k| v[*k] != false)
    {
        return Err(fail(
            "Connect-only review changed endpoint, model or lifecycle scope",
        ));
    }
    let mut body = v.clone();
    body.as_object_mut().unwrap().remove("selection_id");
    if token(v, "selection_id")? != crate::scheduled_transition::digest(&body)? {
        return Err(fail("Connect-only selection hash changed"));
    }
    Ok(())
}
impl Snapshot {
    pub fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        bounded(v, "engine_snapshot")?;
        identity.verify(v)?;
        if v["readiness_checked"] != false
            || v["resources_checked"] != false
            || v["lifecycle"]["starting"] != false
            || v["lifecycle"]["stopping"] != false
            || v["lifecycle"]["worker_settled"] != true
        {
            return Err(fail("Engine inspection is not settled metadata"));
        }
        let admission = Admission::parse(&v["admission"])?;
        let effective = nullable_text(v, "effective_model", 1024)?;
        check_model(admission, &effective, identity)?;
        let ownership = &v["ownership"];
        let handle = nullable_token(ownership, "handle_token")?;
        if !matches!(
            (ownership["kind"].as_str(), handle.is_some()),
            (Some("retained_handle"), true) | (Some("unowned"), false)
        ) {
            return Err(fail("Contradictory engine handle ownership"));
        }
        if admission == Admission::RetainedRecovery && handle.is_none() {
            return Err(fail("Recovery admission lost its retained handle"));
        }
        nullable_text(ownership, "host", 4096)?;
        if !(ownership["pid"].is_null() || ownership["pid"].as_u64().is_some_and(|n| n > 0)) {
            return Err(fail("Invalid engine process metadata"));
        }
        let launch = &v["launch"];
        if !launch.is_null() {
            token(launch, "selection_id")?;
            for key in ["model", "executable", "cwd"] {
                text(launch, key, 4096)?;
            }
            for key in ["artifact", "config", "derived_config", "endpoint"] {
                nullable_text(launch, key, 4096)?;
            }
            if !(launch["context"].is_null() || launch["context"].as_u64().is_some_and(|n| n > 0))
                || number(launch, "concurrency")? == 0
                || !matches!(
                    launch["endpoint_policy"].as_str(),
                    Some("loopback_ephemeral" | "fixed_loopback")
                )
                || !launch["load_shape"].is_object()
                || serde_json::to_vec(&launch["load_shape"])?.len() > 16384
            {
                return Err(fail("Invalid engine launch shape"));
            }
        }
        let start = boolean(&v["actions"]["start"], "eligible")?;
        let stop = boolean(&v["actions"]["stop"], "eligible")?;
        let reason = |key: &str| -> io::Result<String> {
            if v["actions"][key]["eligible"] == true && v["actions"][key]["reason"].is_null() {
                return Ok(String::new());
            }
            v["actions"][key]["reason"]
                .as_str()
                .filter(|s| s.len() <= 4096)
                .map(str::to_owned)
                .ok_or_else(|| fail("Invalid engine action reason"))
        };
        let start_reason = reason("start")?;
        let stop_reason = reason("stop")?;
        if (!start && start_reason.is_empty())
            || (!stop && stop_reason.is_empty())
            || (start
                && (launch.is_null()
                    || launch["model"] != identity.model
                    || handle.is_some()
                    || admission != Admission::ManagementOnly))
            || (stop && handle.is_none())
        {
            return Err(fail("Contradictory engine action eligibility"));
        }
        if !v["tool_policy"].is_object() {
            return Err(fail("Missing engine policy"));
        }
        let local_peer_start = v.get("local_peer_start").cloned();
        if let Some(peers) = &local_peer_start {
            if !start {
                return Err(fail("Ineligible engine proposed second-instance consent"));
            }
            peer_start(peers, identity, launch, number(v, "generation")?)?;
        }
        let reconnect = v.get("reconnect").cloned();
        if let Some(plan) = &reconnect {
            if admission != Admission::ManagementOnly || handle.is_some() {
                return Err(fail("Connect-only action claimed owned or active endpoint"));
            }
            reconnect_plan(plan, identity)?;
        }
        Ok(Self {
            reconnect,
            local_peer_start,
            identity: identity.clone(),
            token: token(v, "snapshot")?,
            generation: number(v, "generation")?,
            admission,
            effective,
            ownership: ownership.clone(),
            launch: launch.clone(),
            policy: v["tool_policy"].clone(),
            start,
            stop,
            start_reason,
            stop_reason,
        })
    }
    pub fn request(&self, action: Action) -> io::Result<Value> {
        if !(match action {
            Action::Start => self.start,
            Action::Stop => self.stop,
            Action::Reconnect => self.reconnect.is_some(),
        }) {
            return Err(fail("Selected engine action is unavailable"));
        }
        let mut v = self.identity.command(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["generation"] = json!(self.generation);
        v["action"] = json!(action.as_str());
        v["selection_id"] = if action == Action::Start {
            self.launch["selection_id"].clone()
        } else {
            Value::Null
        };
        v["handle_token"] = if action == Action::Stop {
            self.ownership["handle_token"].clone()
        } else {
            Value::Null
        };
        v["consent_scope"] = json!(action.scope());
        if action == Action::Reconnect {
            v["type"] = json!("gui.engine.native_reconnect_validate");
            v["selection_id"] = self.reconnect.as_ref().expect("eligible")["selection_id"].clone();
        }
        if action == Action::Start
            && let Some(peers) = &self.local_peer_start
        {
            v["type"] = json!("gui.engine.native_peer_validate");
            v["consent_scope"] = json!("one_start_with_local_peers");
            v["peer_sha256"] = peers["sha256"].clone();
        }
        Ok(v)
    }
}
fn check_model(
    admission: Admission,
    effective: &Option<String>,
    identity: &Identity,
) -> io::Result<()> {
    if (admission == Admission::PromptReady
        && effective.as_deref() != Some(identity.model.as_str()))
        || (admission != Admission::PromptReady && effective.is_some())
    {
        return Err(fail("Engine effective model contradicts admission"));
    }
    Ok(())
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Start,
    Stop,
    Reconnect,
}
impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Reconnect => "reconnect",
        }
    }
    fn scope(self) -> &'static str {
        match self {
            Self::Start => "one_start",
            Self::Stop => "one_stop",
            Self::Reconnect => "one_reconnect",
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub snapshot: Snapshot,
    pub action: Action,
    pub token: String,
    pub action_id: String,
    pub timeout_ms: u64,
    pub summary: Value,
}
impl Review {
    pub fn parse(v: &Value, snapshot: Snapshot, action: Action) -> io::Result<Self> {
        bounded(v, "engine_review")?;
        snapshot.identity.verify(v)?;
        let request = snapshot.request(action)?;
        for key in [
            "snapshot",
            "generation",
            "action",
            "selection_id",
            "handle_token",
            "consent_scope",
        ] {
            if v[key] != request[key] {
                return Err(fail("Engine review differs from selection"));
            }
        }
        let expected_peers = if action == Action::Start {
            snapshot.local_peer_start.as_ref()
        } else {
            None
        };
        if v.get("local_peer_start") != expected_peers
            || v.get("peer_sha256") != request.get("peer_sha256")
        {
            return Err(fail(
                "Engine review changed the local seats requiring confirmation",
            ));
        }
        let summary = match action {
            Action::Start => &snapshot.launch,
            Action::Stop => &snapshot.ownership,
            Action::Reconnect => snapshot.reconnect.as_ref().expect("validated reconnect"),
        };
        if &v["summary"] != summary {
            return Err(fail("Engine review changed inspected plan"));
        }
        let action_id = text(v, "action_id", 36)?;
        crate::resume::conversation_id(&action_id)?;
        let timeout_ms = number(v, "timeout_ms")?;
        if !(1..=1200000).contains(&timeout_ms) {
            return Err(fail("Engine action timeout exceeds native bound"));
        }
        Ok(Self {
            snapshot,
            action,
            token: token(v, "review")?,
            action_id,
            timeout_ms,
            summary: v["summary"].clone(),
        })
    }
    pub fn request(&self) -> io::Result<Value> {
        let mut v = self.snapshot.request(self.action)?;
        v["type"] = json!(ACTION);
        v["review"] = json!(self.token);
        v["action_id"] = json!(self.action_id);
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt {
    pub review: Review,
    pub generation: u64,
    pub status: String,
    pub admission: Admission,
    pub effective: Option<String>,
    pub handle: Option<String>,
    pub shutdown: Value,
    pub error: Option<String>,
    pub error_code: Option<String>,
    peer_accepted: Option<bool>,
}
fn shutdown(v: &Value) -> io::Result<()> {
    if v.is_null() {
        return Ok(());
    }
    for key in ["owned", "attempted", "main_exited", "retained"] {
        boolean(v, key)?;
    }
    if !matches!(v["tree"].as_str(), Some("unknown" | "confirmed"))
        || !(v["pid"].is_null() || v["pid"].as_u64().is_some_and(|n| n > 0))
    {
        return Err(fail("Malformed engine shutdown evidence"));
    }
    nullable_text(v, "error", 4096)?;
    if v["owned"] == false
        && (v["attempted"] != false || v["main_exited"] != false || v["retained"] != false)
    {
        return Err(fail("Unowned engine claims shutdown"));
    }
    Ok(())
}
impl Receipt {
    pub fn parse(v: &Value, review: Review) -> io::Result<Self> {
        bounded(v, "engine_action")?;
        review.snapshot.identity.verify(v)?;
        let generation = review
            .snapshot
            .generation
            .checked_add(1)
            .ok_or_else(|| fail("Engine generation exhausted"))?;
        if v["action_id"] != review.action_id
            || v["review"] != review.token
            || v["action"] != review.action.as_str()
            || v["snapshot"] != review.snapshot.token
            || v["consumed_generation"] != review.snapshot.generation
            || number(v, "generation")? != generation
        {
            return Err(fail("Engine receipt changed reviewed operation"));
        }
        for key in [
            "worker_settled",
            "cancel_requested",
            "spawn_attempted",
            "mutated",
            "close_required",
        ] {
            boolean(v, key)?;
        }
        if v["close_required"] == true || v["worker_settled"] != true {
            return Err(fail(
                "Engine lifecycle/ownership did not settle; closing connection",
            ));
        }
        let status = text(v, "status", 32)?;
        if !matches!(
            status.as_str(),
            "refused" | "started" | "stopped" | "retained" | "cancelled" | "failed" | "connected"
        ) {
            return Err(fail("Unknown engine outcome"));
        }
        let admission = Admission::parse(&v["admission"])?;
        let effective = nullable_text(v, "effective_model", 1024)?;
        check_model(admission, &effective, &review.snapshot.identity)?;
        let handle = nullable_token(v, "handle_token")?;
        shutdown(&v["shutdown"])?;
        if (status == "started"
            && (review.action != Action::Start
                || admission != Admission::PromptReady
                || handle.is_none()
                || v["spawn_attempted"] != true))
            || (status == "stopped"
                && (review.action != Action::Stop
                    || admission != Admission::ManagementOnly
                    || handle.is_some()
                    || v["shutdown"]["owned"] != true
                    || v["shutdown"]["main_exited"] != true
                    || v["shutdown"]["retained"] != false))
            || (admission == Admission::RetainedRecovery && handle.is_none())
            || (status == "retained" && admission != Admission::RetainedRecovery)
            || (review.action == Action::Stop && v["spawn_attempted"] != false)
        {
            return Err(fail("Contradictory engine lifecycle evidence"));
        }
        if review.action == Action::Reconnect {
            if v.get("reconnect") != Some(&review.summary)
                || v["spawn_attempted"] != false
                || handle.is_some()
                || !v["shutdown"].is_null()
                || (status == "connected") != (admission == Admission::PromptReady)
                || !matches!(
                    status.as_str(),
                    "connected" | "refused" | "cancelled" | "failed"
                )
            {
                return Err(fail(
                    "Reconnect receipt changed the endpoint or claimed engine process effects",
                ));
            }
        } else if status == "connected" || v.get("reconnect").is_some() {
            return Err(fail("Unexpected connect-only receipt"));
        }
        let error = nullable_text(v, "error", 4096)?;
        let error_code = nullable_text(v, "error_code", 4096)?;
        if (admission == Admission::PromptReady
            && !matches!(status.as_str(), "started" | "connected")
            && review.snapshot.admission != Admission::PromptReady)
            || (matches!(status.as_str(), "started" | "connected")
                && (v["mutated"] != true
                    || v["cancel_requested"] != false
                    || error.is_some()
                    || error_code.is_some()))
            || (status == "stopped" && error.is_some())
        {
            return Err(fail("Engine outcome cannot establish prompt readiness"));
        }

        if (matches!(status.as_str(), "refused" | "failed")
            || (status == "retained" && v["shutdown"]["retained"] != true))
            && error.is_none()
        {
            return Err(fail("Engine failure lacks a visible reason"));
        }
        let peers = review
            .snapshot
            .local_peer_start
            .as_ref()
            .filter(|_| review.action == Action::Start);
        let peer_accepted = if let Some(peers) = peers {
            let accepted = boolean(v, "peer_accepted")?;
            if v["peer_sha256"] != peers["sha256"]
                || (!accepted
                    && (v["spawn_attempted"] == true
                        || v["mutated"] == true
                        || status == "started"))
            {
                return Err(fail("Engine spawn lacked the exact one-use peer consent"));
            }
            Some(accepted)
        } else {
            if v.get("peer_sha256").is_some() || v.get("peer_accepted").is_some() {
                return Err(fail("Unexpected peer authorization"));
            }
            None
        };
        Ok(Self {
            peer_accepted,
            review,
            generation,
            status,
            admission,
            effective,
            handle,
            shutdown: v["shutdown"].clone(),
            error,
            error_code,
        })
    }
    pub fn readback_request(&self) -> Value {
        let mut v = self.review.snapshot.identity.command(READBACK);
        v["action_id"] = json!(self.review.action_id);
        v["review"] = json!(self.review.token);
        v["generation"] = json!(self.generation);
        v
    }
    pub fn verify_readback(&self, v: &Value) -> io::Result<()> {
        bounded(v, "engine_readback")?;
        self.review.snapshot.identity.verify(v)?;
        if v["action_id"] != self.review.action_id
            || v["review"] != self.review.token
            || number(v, "generation")? != self.generation
            || Admission::parse(&v["admission"])? != self.admission
            || nullable_text(v, "effective_model", 1024)? != self.effective
            || nullable_token(v, "handle_token")? != self.handle
            || v["worker_settled"] != true
            || v["shutdown"] != self.shutdown
        {
            return Err(fail("Engine readback contradicts lifecycle receipt"));
        }
        if v.get("reconnect")
            != (self.review.action == Action::Reconnect).then_some(&self.review.summary)
        {
            return Err(fail(
                "Connect-only readback changed its reviewed destination",
            ));
        }
        if self.peer_accepted
            != v.get("peer_accepted")
                .map(|_| boolean(v, "peer_accepted"))
                .transpose()?
            || v.get("peer_sha256")
                != self
                    .peer_accepted
                    .and(self.review.snapshot.local_peer_start.as_ref())
                    .map(|p| &p["sha256"])
        {
            return Err(fail(
                "Independent engine readback lost the peer warning decision",
            ));
        }
        if boolean(v, "connected")? != (self.admission == Admission::PromptReady)
            || nullable_text(v, "loaded_model", 1024)? != self.effective
        {
            return Err(fail("Engine connection/model readback is unproven"));
        }
        Ok(())
    }
    pub fn message(&self) -> String {
        if self.error_code.as_deref() == Some("resource_confirmation_required") {
            return "Another local LiteTUI seat blocked Start. Press R to inspect it and review a separate Start.".into();
        }
        if let Some(error) = &self.error {
            format!("{}: {error}", self.status)
        } else if self.status == "stopped" && self.shutdown["tree"] == "unknown" {
            "Main engine process exited; descendant resource release is unconfirmed.".into()
        } else {
            format!("Engine {}. {}", self.status, self.admission.label())
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Config(crate::engine_config::Intent),
    Read,
    Validate { snapshot: String, action: Action },
    Execute { review: String },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Config(Box<crate::engine_config::Update>),
    Snapshot(Box<Snapshot>),
    Review(Box<Review>),
    Settled {
        admission: Admission,
        message: String,
    },
    Rejected(String),
}
#[derive(Default)]
pub struct View {
    config: Option<crate::engine_config_view::View>,
    open: bool,
    waiting: bool,
    executing: bool,
    snapshot: Option<Snapshot>,
    review: Option<Review>,
    intent: Option<Intent>,
    selected: usize,
    scroll: usize,
    last_page: usize,
    visible: bool,
    area: Rect,
    pressed: Option<(u16, u16)>,
    message: String,
}
impl View {
    pub fn open(&mut self) {
        self.open = true;
        if !self.waiting {
            self.review = None;
            self.intent = Some(Intent::Read);
            self.waiting = true;
            self.message = "Inspecting source engine ownership and launch plan...".into();
        }
        self.hide();
    }
    pub fn owns_input(&self) -> bool {
        self.open
    }
    pub fn pending(&self) -> bool {
        self.waiting
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn invalidate(&mut self) {
        self.scroll = 0;
        self.hide();
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.pressed = None;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.config = None;
        self.review = None;
        if self.intent.take().is_some() {
            self.waiting = false;
            self.executing = false;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        *self = Self::default();
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        self.executing = false;
        self.hide();
        match update {
            Update::Config(result) => {
                if self.open
                    && let Some(config) = self.config.as_mut()
                {
                    let refresh = config.observe(*result);
                    if refresh {
                        self.waiting = true;
                        self.intent = Some(Intent::Config(crate::engine_config::Intent::Read));
                    }
                }
            }
            Update::Snapshot(snapshot) => {
                self.snapshot = Some(*snapshot);
                self.review = None;
                self.message =
                    "Inspection is metadata; readiness and resources have not been checked.".into();
                self.scroll = 0;
            }
            Update::Review(review) => {
                if self.open {
                    self.review = Some(*review);
                    self.scroll = 0;
                    self.message="Review the complete source plan. Confirmation applies to this action once.".into();
                }
            }
            Update::Settled { admission, message } => {
                self.review = None;
                self.snapshot = None;
                self.message = format!("{message}\n{}", admission.label());
            }
            Update::Rejected(message) => {
                if let Some(config) = self.config.as_mut() {
                    config.observe(crate::engine_config::Update::Rejected(message));
                    return;
                }
                self.review = None;
                self.snapshot = None;
                self.message = message;
            }
        }
    }
    pub fn accepts_config_paste(&self) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.config.as_ref().is_some_and(|c| c.accepts_paste())
    }
    pub fn config_paste(&mut self, text: &str) {
        if self.accepts_config_paste()
            && let Some(c) = self.config.as_mut()
        {
            c.paste(text);
        }
    }
    pub fn config_notice(&mut self, text: String) {
        if let Some(c) = self.config.as_mut() {
            c.notice = text;
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if let Some(config) = self.config.as_mut() {
            if self.open && self.visible && !self.waiting {
                match config.key(key) {
                    crate::engine_config_view::Action::Back => {
                        self.config = None;
                        self.open();
                    }
                    crate::engine_config_view::Action::Send(intent) => {
                        self.intent = Some(Intent::Config(intent));
                        self.waiting = true;
                        self.hide();
                    }
                    crate::engine_config_view::Action::Stay => {}
                }
            }
            return;
        }
        if self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && key.kind == KeyEventKind::Press
            && key.code == KeyCode::Char('c')
            && key.modifiers.is_empty()
        {
            self.config = Some(crate::engine_config_view::View::default());
            self.waiting = true;
            self.intent = Some(Intent::Config(crate::engine_config::Intent::Read));
            self.hide();
            return;
        }

        if !self.open
            || key.kind != KeyEventKind::Press
            || !(key.modifiers.is_empty()
                || key.modifiers == KeyModifiers::SHIFT && key.code == KeyCode::Char('R'))
        {
            return;
        }
        if key.code == KeyCode::Esc {
            self.close();
            return;
        }
        if self.waiting {
            return;
        }
        match key.code {
            KeyCode::Char('a') if self.visible && self.review.is_none() => {
                if let Some(snap) = &self.snapshot
                    && snap.reconnect.is_some()
                {
                    self.intent = Some(Intent::Validate {
                        snapshot: snap.token.clone(),
                        action: Action::Reconnect,
                    });
                    self.waiting = true;
                    self.message =
                        "Reviewing the selected endpoint for connect-only attachment.".into();
                    self.hide();
                }
            }
            KeyCode::Char('r' | 'R') | KeyCode::F(5) => self.open(),
            KeyCode::Up | KeyCode::Down | KeyCode::Tab if self.review.is_none() => {
                self.selected = 1 - self.selected;
                self.scroll = 0;
                self.hide();
            }
            KeyCode::PageDown if self.visible => {
                self.scroll = (self.scroll + 1).min(self.last_page);
                self.hide();
            }
            KeyCode::PageUp => {
                self.scroll = self.scroll.saturating_sub(1);
                self.hide();
            }
            KeyCode::Enter if self.visible => {
                if let Some(review) = &self.review {
                    if self.scroll != self.last_page {
                        return;
                    }
                    self.intent = Some(Intent::Execute {
                        review: review.token.clone(),
                    });
                    self.executing = true;
                    self.message =
                        "Waiting for source lifecycle settlement and independent readback..."
                            .into();
                } else if let Some(snapshot) = &self.snapshot {
                    let action = if self.selected == 0 {
                        Action::Start
                    } else {
                        Action::Stop
                    };
                    if snapshot.request(action).is_err() {
                        self.message = if action == Action::Start {
                            snapshot.start_reason.clone()
                        } else {
                            snapshot.stop_reason.clone()
                        };
                        return;
                    }
                    self.intent = Some(Intent::Validate {
                        snapshot: snapshot.token.clone(),
                        action,
                    });
                    self.message = "Validating the exact source selection...".into();
                } else {
                    return;
                }
                self.waiting = true;
                self.hide();
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if self.config.is_some() {
            return;
        }
        if !self.open || self.waiting {
            return;
        }
        match event.kind {
            MouseEventKind::ScrollDown
                if self.visible && self.area.contains((event.column, event.row).into()) =>
            {
                self.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE))
            }
            MouseEventKind::ScrollUp if self.area.contains((event.column, event.row).into()) => {
                self.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE))
            }
            MouseEventKind::Down(MouseButton::Left) => {
                self.pressed = Some((event.column, event.row));
                self.visible = false;
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take() == Some((event.column, event.row))
                    && self.area.contains((event.column, event.row).into())
                    && self.review.is_none()
                    && event.row == self.area.y + 2
                {
                    self.selected = usize::from(event.column >= self.area.x + self.area.width / 2);
                    self.scroll = 0;
                }
                self.hide();
            }
            _ => self.hide(),
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.open {
            return;
        }
        if self.area != area {
            self.scroll = 0;
            self.last_page = 0;
            self.hide();
        }
        let style = Style::default().fg(theme.foreground).bg(theme.surface);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Block::default()
                .title(" Owned engine ")
                .borders(Borders::ALL)
                .style(style),
            area,
        );
        if area.width < 42 || area.height < 18 {
            if let Some(config) = self.config.as_mut() {
                config.clear_layout();
            }
            frame.render_widget(
                Paragraph::new("Resize to 42x18\nEsc: close engine panel").style(style),
                area.inner(ratatui::layout::Margin::new(1, 1)),
            );
            return;
        }
        self.area = area;
        if let Some(config) = self.config.as_mut() {
            config.draw(frame, area, theme, self.waiting);
            self.visible = true;
            return;
        }
        let body = Rect::new(area.x + 2, area.y + 3, area.width - 4, area.height - 6);
        if let Some(review) = &self.review {
            frame.render_widget(
                Paragraph::new(format!("Confirm {} once", review.action.as_str())).style(style),
                Rect::new(area.x + 2, area.y + 2, area.width - 4, 1),
            );
        } else {
            let middle = area.x + area.width / 2;
            for (index, label, x, width) in [
                (0, "Start", area.x + 2, middle - area.x - 2),
                (1, "Stop", middle, area.x + area.width - middle - 2),
            ] {
                frame.render_widget(
                    Paragraph::new(format!(
                        "{} {label}",
                        if self.selected == index { ">" } else { " " }
                    ))
                    .style(style),
                    Rect::new(x, area.y + 2, width, 1),
                );
            }
        }
        let details = if let Some(review) = &self.review {
            format!(
                "{} / {}\nAction: {} ({})\n{}",
                review.snapshot.identity.backend,
                display(&review.snapshot.identity.model),
                review.action.as_str(),
                if review.action == Action::Start && review.snapshot.local_peer_start.is_some() {
                    "one_start_with_local_peers"
                } else {
                    review.action.scope()
                },
                {
                    let plan =
                        display(&serde_json::to_string_pretty(&review.summary).unwrap_or_default());
                    if let Some(peers) = review
                        .snapshot
                        .local_peer_start
                        .as_ref()
                        .filter(|_| review.action == Action::Start)
                    {
                        let seat = |v: &Value| {
                            format!(
                                "{} · {} · agent {} · PID {} · born {} µs",
                                display(v["name"].as_str().unwrap()),
                                display(v["backend"].as_str().unwrap()),
                                display(v["agent_id"].as_str().unwrap()),
                                v["pid"],
                                v["born_us"]
                            )
                        };
                        let others = peers["peers"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(seat)
                            .collect::<Vec<_>>()
                            .join("\n");
                        format!(
                            "SECOND-INSTANCE CONFIRMATION\nCurrent LiteTUI: {}\nOther local LiteTUI seats:\n{}\n\nStart will attempt a new engine process for {} and may load a second set of weights into VRAM. Memory, port and competing-process checks still apply. Other seats' engine ownership is unknown.\n\n{}",
                            seat(&peers["current"]),
                            others,
                            display(&review.snapshot.identity.model),
                            plan
                        )
                    } else {
                        plan
                    }
                }
            )
        } else if let Some(snapshot) = &self.snapshot {
            format!(
                "{} / {}\n{}\nStart: {}\nStop: {}\nOwnership: {}\nPlan: {}",
                snapshot.identity.backend,
                display(&snapshot.identity.model),
                snapshot.admission.label(),
                if snapshot.local_peer_start.is_some() {
                    "requires separate local-seat confirmation".into()
                } else if snapshot.start {
                    "eligible".into()
                } else {
                    display(&snapshot.start_reason)
                },
                if snapshot.stop {
                    "eligible".into()
                } else {
                    display(&snapshot.stop_reason)
                },
                display(&snapshot.ownership.to_string()),
                display(&snapshot.launch.to_string())
            )
        } else {
            String::new()
        };
        let content = format!("{}\n\n{}", display(&self.message), details);
        let lines = crate::ui::wrap(&content, body.width as usize);
        let height = usize::from(body.height).max(1);
        self.last_page = lines.len().saturating_sub(1) / height;
        self.scroll = self.scroll.min(self.last_page);
        let page = lines
            .into_iter()
            .skip(self.scroll * height)
            .take(height)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(Paragraph::new(page).style(style), body);
        let footer = if self.waiting {
            if self.executing {
                "Action pending | Esc hides only"
            } else {
                "Waiting | Esc cancels review"
            }
        } else if self.review.is_some() {
            "PgUp/PgDn | Enter confirm | Esc cancel"
        } else if self
            .snapshot
            .as_ref()
            .is_some_and(|s| s.reconnect.is_some())
        {
            "Esc | a attach | c config | Enter review"
        } else {
            "Esc | ↑↓ select | Enter review | c config"
        };
        frame.render_widget(
            Paragraph::new(footer).style(style),
            Rect::new(area.x + 1, area.y + area.height - 2, area.width - 2, 1),
        );
        self.visible = true;
    }
}
fn display(s: &str) -> String {
    terminal_text(s)
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

#[derive(Clone, Debug)]
pub struct ShutdownIdentity {
    pub instance: String,
    pub session: String,
    pub backend: String,
}
pub fn verify_shutdown(result: &Value, expected: Option<&ShutdownIdentity>) -> io::Result<()> {
    if result["ok"] != true {
        return Err(fail("Runtime shutdown was not acknowledged"));
    }
    let proof = &result["engine_shutdown"];
    if proof.is_null() {
        return if expected.is_none() {
            Ok(())
        } else {
            Err(fail("Engine shutdown lacks joined ownership proof"))
        };
    }
    if serde_json::to_vec(proof)?.len() > 16384
        || proof["version"] != 1
        || proof["worker_settled"] != true
        || proof["resource_release_verified"] != false
    {
        return Err(fail("Invalid engine shutdown settlement"));
    }
    token(proof, "management_instance")?;
    crate::resume::conversation_id(&text(proof, "session_id", 128)?)?;
    if !matches!(proof["backend"].as_str(), Some("ninfer" | "strata")) {
        return Err(fail("Unknown shutdown engine"));
    }
    if let Some(expected) = expected
        && (proof["management_instance"] != expected.instance
            || proof["session_id"] != expected.session
            || proof["backend"] != expected.backend)
    {
        return Err(fail("Engine shutdown proof changed startup ownership"));
    }
    shutdown(&proof["shutdown"])?;
    if proof["shutdown"]["retained"] != false
        || (proof["shutdown"]["owned"] == true && proof["shutdown"]["main_exited"] != true)
    {
        return Err(fail("Engine shutdown retained unconfirmed ownership"));
    }
    Ok(())
}
