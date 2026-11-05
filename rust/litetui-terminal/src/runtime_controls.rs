//! Reviewed runtime policy boundaries. No receipt creates a turn or tool authority.
use crate::{
    scheduled_transition::{boolean, digest},
    tool_mode::Profile,
    work_lifecycle as life,
};
use life::{bad, bounded, hash, keys, number, optional, optional_uuid, profile, text, uuid};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, io};

pub const TOOL_LIST: &str = "gui.runtime.native_tool_list";
pub const SNAPSHOT: &str = "gui.runtime.native_snapshot";
pub const VALIDATE: &str = "gui.runtime.native_validate";
pub const APPLY: &str = "gui.runtime.native_apply";
pub const READBACK: &str = "gui.runtime.native_readback";
pub const OPERATIONS: [&str; 4] = [SNAPSHOT, VALIDATE, APPLY, READBACK];
const BODY: usize = 262_144;
const MAX_N: u64 = 9_007_199_254_740_991;
const BASE: &[&str] = &[
    "version",
    "runtime_controls_version",
    "work_instance",
    "session_id",
    "agent_id",
    "backend",
    "model",
];
fn object(v: &Value, extra: &[&str]) -> io::Result<()> {
    bounded(v, BODY)?;
    let mut expected = BASE.to_vec();
    expected.extend(extra);
    keys(v, &expected)
}
fn count(v: &Value, key: &str) -> io::Result<u64> {
    number(v, key, false)?
        .checked_add(0)
        .filter(|n| *n <= MAX_N)
        .ok_or_else(|| bad("Runtime control counter exceeds exact range"))
}
fn maybe_count(v: &Value, key: &str) -> io::Result<Option<u64>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        count(v, key).map(Some)
    }
}
fn maybe_hash(v: &Value, key: &str) -> io::Result<Option<String>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        hash(v, key).map(Some)
    }
}
fn command_id(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 128)?;
    if s.chars().any(char::is_control) {
        return Err(bad("Invalid runtime control RPC id"));
    }
    Ok(s)
}
fn maybe_id(v: &Value, key: &str) -> io::Result<Option<String>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        command_id(v, key).map(Some)
    }
}
fn version(v: &Value) -> io::Result<()> {
    if number(v, "version", true)? != 1 || number(v, "runtime_controls_version", true)? != 1 {
        return Err(bad("Unsupported runtime controls version"));
    }
    Ok(())
}
macro_rules! wire_enum {
    ($name:ident { $($variant:ident => $wire:literal),+ $(,)? }) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum $name { $($variant),+ }
        impl $name {
            pub fn key(self)->&'static str { match self { $(Self::$variant=>$wire),+ } }
            fn parse(v:&Value,key:&str)->io::Result<Self> { match text(v,key,32)?.as_str() { $($wire=>Ok(Self::$variant)),+, _=>Err(bad(concat!("Invalid runtime ",stringify!($name)))) } }
        }
    };
}
wire_enum!(Status { Applied=>"applied", SessionOnly=>"session_only", Refused=>"refused", Uncertain=>"uncertain" });
wire_enum!(Release { Install=>"install", Unchanged=>"unchanged", Close=>"close" });
wire_enum!(OwnerState { Idle=>"idle", Active=>"active", Terminal=>"terminal" });
wire_enum!(EffectiveTransition { Unchanged=>"unchanged", RestoreSeat=>"restore_seat" });
wire_enum!(Inventory { HostRequest=>"host_request", ClaudeSession=>"claude_session", CodexRuntime=>"codex_runtime" });
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Field {
    Tools,
    Profile,
    Plan,
    ToolSelection,
}
impl Field {
    pub fn key(self) -> &'static str {
        match self {
            Self::Tools => "tools_enabled",
            Self::Profile => "tool_profile",
            Self::Plan => "plan_mode",
            Self::ToolSelection => "tool_selection",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Tools => "Tools",
            Self::Profile => "Authority",
            Self::Plan => "Plan mode",
            Self::ToolSelection => "Individual tools",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Change {
    Tools(bool),
    Profile(Profile),
    Plan(bool),
    ToolSelection { name: String, enabled: bool },
}
impl Change {
    pub fn field(&self) -> Field {
        match self {
            Self::Tools(_) => Field::Tools,
            Self::Profile(_) => Field::Profile,
            Self::Plan(_) => Field::Plan,
            Self::ToolSelection { .. } => Field::ToolSelection,
        }
    }
    pub fn wire(&self) -> Value {
        json!({"field":self.field().key(),"value":match self {Self::Tools(v)|Self::Plan(v)=>json!(v),Self::Profile(v)=>json!(v.as_str()),Self::ToolSelection{name,enabled}=>json!({"name":name,"enabled":enabled})}})
    }
    fn parse(v: &Value) -> io::Result<Self> {
        keys(v, &["field", "value"])?;
        match text(v, "field", 32)?.as_str() {
            "tools_enabled" => Ok(Self::Tools(boolean(v, "value")?)),
            "plan_mode" => Ok(Self::Plan(boolean(v, "value")?)),
            "tool_profile" => Ok(Self::Profile(profile(v, "value")?)),
            "tool_selection" => {
                keys(&v["value"], &["name", "enabled"])?;
                Ok(Self::ToolSelection {
                    name: text(&v["value"], "name", 128)?,
                    enabled: boolean(&v["value"], "enabled")?,
                })
            }
            _ => Err(bad("Unknown runtime control field")),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Binding {
    pub mcp_lifecycle: bool,
    pub identity: life::Identity,
    pub work_instance: String,
    pub provider: life::Provider,
    pub tool_selection: bool,
}
impl Binding {
    pub fn verify(&self, v: &Value) -> io::Result<()> {
        version(v)?;
        let identity = life::Identity {
            session: uuid(v, "session_id")?,
            agent: uuid(v, "agent_id")?,
            backend: text(v, "backend", 64)?,
            model: text(v, "model", 1024)?,
        };
        if identity != self.identity || hash(v, "work_instance")? != self.work_instance {
            return Err(bad("Runtime control changed captured epoch"));
        }
        Ok(())
    }
    pub fn request(&self, op: &str) -> Value {
        let mut v = self.identity.request(op);
        v["version"] = json!(1);
        v["runtime_controls_version"] = json!(1);
        v["work_instance"] = json!(self.work_instance);
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Facts {
    pub tools_enabled: bool,
    pub tools_disabled: Option<Vec<String>>,
    pub settings_profile: Profile,
    pub chosen_profile: Profile,
    pub seat_profile: Profile,
    pub effective_profile: Profile,
    pub requested_cli_profile: Option<Profile>,
    pub plan_mode: bool,
    pub mcp_enabled: bool,
    pub approval_route: String,
    pub cwd: String,
    pub provider: life::Provider,
    pub inventory: Inventory,
    pub wire: Value,
}
impl Facts {
    pub fn parse(v: &Value, provider: life::Provider) -> io::Result<Self> {
        Self::parse_tools(v, provider, false, false)
    }
    /// Parse facts with exactly the extensions negotiated for this binding.
    pub(crate) fn parse_bound(v: &Value, binding: &Binding) -> io::Result<Self> {
        Self::parse_tools(
            v,
            binding.provider,
            binding.tool_selection,
            binding.mcp_lifecycle,
        )
    }
    #[cfg(test)]
    pub(crate) fn parse_mcp(
        v: &Value,
        provider: life::Provider,
        allowed: bool,
    ) -> io::Result<Self> {
        Self::parse_tools(v, provider, false, allowed)
    }
    fn parse_tools(
        v: &Value,
        provider: life::Provider,
        tool_selection: bool,
        allowed: bool,
    ) -> io::Result<Self> {
        let mut expected = vec![
            "tools_enabled",
            "settings_profile",
            "chosen_profile",
            "seat_profile",
            "effective_profile",
            "requested_cli_profile",
            "plan_mode",
            "mcp_enabled",
            "approval_route",
            "cwd",
            "provider",
            "inventory",
        ];
        if tool_selection {
            expected.push("tools_disabled");
        }
        keys(v, &expected)?;
        let tools_disabled = tool_selection
            .then(|| disabled(&v["tools_disabled"]))
            .transpose()?;
        let requested_cli_profile = match v.get("requested_cli_profile") {
            Some(Value::Null) => None,
            Some(_) => Some(profile(v, "requested_cli_profile")?),
            None => return Err(bad("Missing nullable CLI profile")),
        };
        let chosen_profile = profile(v, "chosen_profile")?;
        let seat_profile = profile(v, "seat_profile")?;
        let actual = life::Provider::parse(v, "provider")?;
        let inventory = Inventory::parse(v, "inventory")?;
        let expected = match provider {
            life::Provider::Host => Inventory::HostRequest,
            life::Provider::Claude => Inventory::ClaudeSession,
            life::Provider::Codex => Inventory::CodexRuntime,
        };
        let cwd = text(v, "cwd", 32768)?;
        if actual != provider
            || inventory != expected
            || seat_profile != requested_cli_profile.unwrap_or(chosen_profile)
            || boolean(v, "mcp_enabled")? && (!allowed || provider == life::Provider::Codex)
            || v["approval_route"] != "host"
            || !std::path::Path::new(&cwd).is_absolute()
        {
            return Err(bad(
                "Runtime policy changed provider, seat, route or workspace shape",
            ));
        }
        Ok(Self {
            tools_enabled: boolean(v, "tools_enabled")?,
            tools_disabled,
            settings_profile: profile(v, "settings_profile")?,
            chosen_profile,
            seat_profile,
            effective_profile: profile(v, "effective_profile")?,
            requested_cli_profile,
            plan_mode: boolean(v, "plan_mode")?,
            mcp_enabled: boolean(v, "mcp_enabled")?,
            approval_route: "host".into(),
            cwd,
            provider,
            inventory,
            wire: v.clone(),
        })
    }
    pub fn stable_eq(&self, other: &Self) -> bool {
        let mut left = self.wire.clone();
        let mut right = other.wire.clone();
        left.as_object_mut().unwrap().remove("effective_profile");
        right.as_object_mut().unwrap().remove("effective_profile");
        left == right
    }
    fn changed(&self, change: &Change) -> io::Result<Self> {
        let mut v = self.wire.clone();
        match change {
            Change::Tools(value) => v["tools_enabled"] = json!(value),
            Change::Plan(value) => v["plan_mode"] = json!(value),
            Change::ToolSelection { name, enabled } => {
                let mut names = self
                    .tools_disabled
                    .clone()
                    .ok_or_else(|| bad("Per-tool policy was not negotiated"))?;
                if *enabled {
                    names.retain(|entry| entry != name);
                } else if !names.contains(name) {
                    names.push(name.clone());
                }
                v["tools_disabled"] = json!(names);
            }
            Change::Profile(value) => {
                for k in [
                    "settings_profile",
                    "chosen_profile",
                    "seat_profile",
                    "effective_profile",
                ] {
                    v[k] = json!(value.as_str());
                }
                v["requested_cli_profile"] = Value::Null;
            }
        }
        Self::parse_tools(
            &v,
            self.provider,
            self.tools_disabled.is_some(),
            self.mcp_enabled,
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Owner {
    pub state: OwnerState,
    pub origin: Option<life::Origin>,
    pub wire: Value,
}
impl Owner {
    pub fn parse(v: &Value, binding: &Binding) -> io::Result<Self> {
        keys(v, &["state", "origin"])?;
        let state = OwnerState::parse(v, "state")?;
        let origin = if v["origin"].is_null() {
            None
        } else {
            Some(life::Origin::parse(&v["origin"])?)
        };
        if (state == OwnerState::Idle) != origin.is_none()
            || origin
                .as_ref()
                .is_some_and(|o| o.session != binding.identity.session)
        {
            return Err(bad("Runtime owner changed admitted origin"));
        }
        Ok(Self {
            state,
            origin,
            wire: v.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Policy {
    pub binding: Binding,
    pub generation: u64,
    pub facts: Facts,
}
impl Policy {
    pub fn prompt_binding(&self) -> Value {
        json!({"version":1,"work_instance":self.binding.work_instance,"generation":self.generation})
    }
    pub fn verify_stable(
        &self,
        binding: &Binding,
        generation: u64,
        facts: &Facts,
    ) -> io::Result<()> {
        if self.binding != *binding || self.generation != generation || !self.facts.stable_eq(facts)
        {
            return Err(bad("Runtime control changed published policy"));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub tool_selection: bool,
}
impl Capabilities {
    pub fn negotiate(ops: &[String]) -> io::Result<Option<Self>> {
        let counts = OPERATIONS.map(|op| ops.iter().filter(|s| *s == op).count());
        let selected = ops.iter().filter(|op| op.as_str() == TOOL_LIST).count();
        if counts == [0; 4] && selected == 0 {
            return Ok(None);
        }
        if selected > 1 {
            return Err(bad("Duplicate per-tool capability"));
        }
        if counts != [1; 4]
            || [life::CAPTURE, life::ACTIVATE, life::SETTLED]
                .iter()
                .any(|op| ops.iter().filter(|s| *s == op).count() != 1)
        {
            return Err(bad("Incomplete runtime controls negotiation"));
        }
        Ok(Some(Self {
            tool_selection: selected == 1,
        }))
    }
    pub fn parse_capture(&self, v: &Value, binding: &Binding) -> io::Result<Policy> {
        keys(
            &v["runtime_controls_limits"],
            &["authorizations", "waiters"],
        )?;
        if number(&v["runtime_controls_limits"], "authorizations", true)? != 128
            || number(&v["runtime_controls_limits"], "waiters", true)? != 128
        {
            return Err(bad("Runtime control ownership budgets changed"));
        }
        self.policy(v, binding)
    }
    fn policy(&self, v: &Value, binding: &Binding) -> io::Result<Policy> {
        if number(v, "runtime_controls_version", true)? != 1 {
            return Err(bad("Runtime control opt-in echo changed"));
        }
        if self.tool_selection != binding.tool_selection
            || if self.tool_selection {
                v["tool_selection_version"] != 1
            } else {
                v.get("tool_selection_version").is_some()
            }
        {
            return Err(bad("Per-tool policy opt-in echo changed"));
        }
        let p = &v["runtime_policy"];
        keys(p, &["generation", "facts"])?;
        if count(p, "generation")? != 0 {
            return Err(bad("Fresh runtime capture generation is not zero"));
        }
        Ok(Policy {
            binding: binding.clone(),
            generation: 0,
            // Capture/activation never admit enabled MCP; only a later published
            // lifecycle generation may carry mcp_enabled:true.
            facts: Facts::parse_tools(
                &p["facts"],
                binding.provider,
                binding.tool_selection,
                false,
            )?,
        })
    }
    pub fn parse_activation(
        &self,
        v: &Value,
        binding: &Binding,
        captured: &Policy,
    ) -> io::Result<Policy> {
        let p = self.policy(v, binding)?;
        if p != *captured {
            return Err(bad("Runtime policy changed before activation"));
        }
        Ok(p)
    }
}
fn disabled(v: &Value) -> io::Result<Vec<String>> {
    v.as_array()
        .filter(|names| names.len() <= 1024)
        .ok_or_else(|| bad("Invalid disabled-tool list"))?
        .iter()
        .map(|name| {
            name.as_str()
                .filter(|name| !name.is_empty() && name.len() <= 128)
                .map(str::to_owned)
                .ok_or_else(|| bad("Invalid disabled-tool name"))
        })
        .collect()
}
fn saved(v: &Value, selected: bool) -> io::Result<()> {
    if selected {
        keys(v, &["tools_enabled", "tool_profile", "tools_disabled"])?;
        disabled(&v["tools_disabled"])?;
    } else {
        keys(v, &["tools_enabled", "tool_profile"])?;
    }
    boolean(v, "tools_enabled")?;
    profile(v, "tool_profile")?;
    Ok(())
}
fn revisions(v: &Value) -> io::Result<()> {
    keys(v, &["global", "conversation", "agent"])?;
    for k in ["global", "conversation", "agent"] {
        if v[k] != "absent" {
            hash(v, k)?;
        }
    }
    Ok(())
}
fn system(v: &Value) -> io::Result<()> {
    if v.is_null() {
        return Ok(());
    }
    keys(v, &["row_sha256", "content_sha256"])?;
    hash(v, "row_sha256")?;
    hash(v, "content_sha256")?;
    Ok(())
}
fn plan(v: &Value) -> io::Result<()> {
    keys(v, &["present", "sha256", "text"])?;
    if !boolean(v, "present")? {
        if !v["sha256"].is_null() || !v["text"].is_null() {
            return Err(bad("Absent PLAN contains text/hash"));
        }
        return Ok(());
    }
    let raw = v["text"]
        .as_str()
        .filter(|s| s.len() <= 65536)
        .ok_or_else(|| bad("Invalid PLAN text"))?;
    if format!("{:x}", Sha256::digest(raw.as_bytes())) != hash(v, "sha256")? {
        return Err(bad("PLAN text hash changed"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolRow {
    pub name: String,
    pub authority: String,
    pub description: String,
    pub summary: String,
    pub enabled: bool,
    pub gated: bool,
    pub wire: Value,
}
impl ToolRow {
    fn parse(v: &Value) -> io::Result<Self> {
        keys(
            v,
            &[
                "name",
                "authority",
                "description",
                "summary",
                "enabled",
                "gated",
            ],
        )?;
        let name = text(v, "name", 128)?;
        if name.is_empty() {
            return Err(bad("Empty registered tool name"));
        }
        Ok(Self {
            name,
            authority: text(v, "authority", 65536)?,
            description: text(v, "description", 65536)?,
            summary: text(v, "summary", 65536)?,
            enabled: boolean(v, "enabled")?,
            gated: boolean(v, "gated")?,
            wire: v.clone(),
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolInventory {
    pub rows: Vec<ToolRow>,
    pub dynamic_sources: usize,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub binding: Binding,
    pub generation: u64,
    pub token: String,
    pub facts: Facts,
    pub owner: Owner,
    pub next_profile: Profile,
    pub editable: BTreeMap<Field, Option<String>>,
    pub tools: Option<ToolInventory>,
    pub wire: Value,
}
impl Snapshot {
    pub fn parse_tool_list(v: &Value, binding: &Binding) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "snapshot",
                "tools",
                "editable_reason",
                "dynamic_sources",
            ],
        )?;
        binding.verify(v)?;
        if !binding.tool_selection || v["kind"] != "tool_selection_list" {
            return Err(bad("Per-tool list was not negotiated"));
        }
        let mut snapshot = Self::parse(&v["snapshot"], binding)?;
        let reason = optional(v, "editable_reason", 2048)?;
        if snapshot.facts.provider == life::Provider::Codex && reason.is_none() {
            return Err(bad("Native provider inventory must remain read-only"));
        }
        let rows = v["tools"]
            .as_array()
            .filter(|rows| rows.len() <= 1024)
            .ok_or_else(|| bad("Oversized tool inventory"))?
            .iter()
            .map(ToolRow::parse)
            .collect::<io::Result<Vec<_>>>()?;
        let mut names = std::collections::HashSet::new();
        let off = snapshot
            .facts
            .tools_disabled
            .as_ref()
            .ok_or_else(|| bad("Missing selected tool policy"))?;
        for row in &rows {
            if !names.insert(row.name.clone()) || row.enabled == off.contains(&row.name) {
                return Err(bad("Tool row contradicts captured preference"));
            }
        }
        let dynamic_sources = usize::try_from(count(v, "dynamic_sources")?)
            .map_err(|_| bad("Invalid dynamic inventory size"))?;
        if dynamic_sources > 1024 {
            return Err(bad("Oversized dynamic inventory"));
        }
        snapshot.editable.insert(Field::ToolSelection, reason);
        snapshot.tools = Some(ToolInventory {
            rows,
            dynamic_sources,
        });
        Ok(snapshot)
    }
    pub fn parse(v: &Value, binding: &Binding) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "generation",
                "snapshot",
                "facts",
                "owner",
                "saved",
                "revisions",
                "first_system",
                "plan",
                "profiles",
                "next_profile",
                "editable",
            ],
        )?;
        binding.verify(v)?;
        if v["kind"] != "runtime_controls_snapshot" {
            return Err(bad("Wrong runtime snapshot kind"));
        }
        let facts = Facts::parse_bound(&v["facts"], binding)?;
        saved(&v["saved"], binding.tool_selection)?;
        revisions(&v["revisions"])?;
        system(&v["first_system"])?;
        plan(&v["plan"])?;
        let profiles = v["profiles"]
            .as_array()
            .filter(|p| p.len() == 3)
            .ok_or_else(|| bad("Runtime profile catalog is incomplete"))?;
        let mut choices = Vec::new();
        for p in profiles {
            keys(p, &["name", "label", "summary"])?;
            choices.push(profile(p, "name")?);
            text(p, "label", 128)?;
            text(p, "summary", 2048)?;
        }
        if choices != [Profile::Strict, Profile::Interactive, Profile::Autonomous] {
            return Err(bad("Runtime profile order changed"));
        }
        let current = choices
            .iter()
            .position(|p| *p == facts.settings_profile)
            .unwrap();
        let next_profile = profile(v, "next_profile")?;
        if next_profile != choices[(current + choices.len() - 1) % choices.len()] {
            return Err(bad(
                "Runtime next profile differs from source catalog cycle",
            ));
        }
        keys(
            &v["editable"],
            &["tools_enabled", "tool_profile", "plan_mode"],
        )?;
        let mut editable = BTreeMap::new();
        for f in [Field::Tools, Field::Profile, Field::Plan] {
            editable.insert(f, optional(&v["editable"], f.key(), 2048)?);
        }
        Ok(Self {
            binding: binding.clone(),
            generation: count(v, "generation")?,
            token: hash(v, "snapshot")?,
            facts,
            owner: Owner::parse(&v["owner"], binding)?,
            next_profile,
            editable,
            tools: None,
            wire: v.clone(),
        })
    }
    pub fn validate_request(&self, change: &Change) -> io::Result<Value> {
        if let Change::ToolSelection { name, .. } = change
            && (!self.binding.tool_selection
                || self
                    .tools
                    .as_ref()
                    .is_none_or(|inventory| !inventory.rows.iter().any(|row| &row.name == name)))
        {
            return Err(bad("Select an exact registered tool from a fresh list"));
        }
        if self
            .editable
            .get(&change.field())
            .and_then(Option::as_ref)
            .is_some()
        {
            return Err(bad("Selected runtime control is unavailable"));
        }
        let mut v = self.binding.request(VALIDATE);
        v["snapshot"] = json!(self.token);
        v["generation"] = json!(self.generation);
        v["change"] = change.wire();
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub binding: Binding,
    pub action_id: String,
    pub token: String,
    pub generation: u64,
    pub change: Change,
    pub before: Facts,
    pub after: Facts,
    pub owner_before: Owner,
    pub wire: Value,
}
impl Review {
    pub fn parse(v: &Value, snapshot: &Snapshot, change: &Change) -> io::Result<Self> {
        let mut fields = vec![
            "kind",
            "action_id",
            "review_token",
            "snapshot",
            "generation",
            "change",
            "before",
            "after",
            "owner_before",
            "saved_before",
            "revisions",
            "first_system_before",
            "first_system_after",
            "plan",
            "context_sha256",
            "consequence",
        ];
        if let Change::ToolSelection { name, .. } = change {
            fields.push("tool");
            let row = snapshot
                .tools
                .as_ref()
                .and_then(|inventory| inventory.rows.iter().find(|row| &row.name == name))
                .ok_or_else(|| bad("Missing reviewed tool row"))?;
            if v["tool"] != row.wire {
                return Err(bad("Review changed selected tool metadata"));
            }
        }
        object(v, &fields)?;
        snapshot.binding.verify(v)?;
        snapshot.validate_request(change)?;
        if v["kind"] != "runtime_controls_review"
            || hash(v, "snapshot")? != snapshot.token
            || count(v, "generation")? != snapshot.generation
            || Change::parse(&v["change"])? != *change
        {
            return Err(bad("Runtime Review changed selected snapshot or change"));
        }
        let before = Facts::parse_bound(&v["before"], &snapshot.binding)?;
        let after = Facts::parse_bound(&v["after"], &snapshot.binding)?;
        let owner_before = Owner::parse(&v["owner_before"], &snapshot.binding)?;
        if before != snapshot.facts
            || owner_before != snapshot.owner
            || after != before.changed(change)?
            || v["saved_before"] != snapshot.wire["saved"]
            || v["revisions"] != snapshot.wire["revisions"]
            || v["first_system_before"] != snapshot.wire["first_system"]
            || v["plan"] != snapshot.wire["plan"]
        {
            return Err(bad("Runtime Review changed protected evidence"));
        }
        saved(&v["saved_before"], snapshot.binding.tool_selection)?;
        revisions(&v["revisions"])?;
        system(&v["first_system_before"])?;
        system(&v["first_system_after"])?;
        plan(&v["plan"])?;
        hash(v, "context_sha256")?;
        text(v, "consequence", 4096)?;
        let row_unchanged = matches!(change, Change::Profile(_) | Change::ToolSelection { .. })
            || matches!(change,Change::Tools(value) if *value==before.tools_enabled)
            || matches!(change,Change::Plan(value) if *value==before.plan_mode);
        if (row_unchanged && v["first_system_before"] != v["first_system_after"])
            || v["first_system_before"].is_null() != v["first_system_after"].is_null()
        {
            return Err(bad("Runtime Review changed canonical system-row scope"));
        }
        Ok(Self {
            binding: snapshot.binding.clone(),
            action_id: uuid(v, "action_id")?,
            token: hash(v, "review_token")?,
            generation: snapshot.generation,
            change: change.clone(),
            before,
            after,
            owner_before,
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> Value {
        json!({"type":APPLY,"review":self.wire})
    }
    pub fn detail_lines(&self) -> Vec<String> {
        let choice = match &self.change {
            Change::Tools(enabled) => format!(
                "Turn tools {} for this conversation",
                if *enabled { "on" } else { "off" }
            ),
            Change::Profile(profile) => format!(
                "Change authority from {} to {}",
                self.before.settings_profile.as_str(),
                profile.as_str()
            ),
            Change::ToolSelection { name, enabled } => format!(
                "{} host tool {name} for this conversation",
                if *enabled { "Enable" } else { "Disable" }
            ),
            Change::Plan(enabled) => format!(
                "Turn Plan mode {} for this session",
                if *enabled { "on" } else { "off" }
            ),
        };
        let mut lines = vec![
            choice,
            self.wire["consequence"]
                .as_str()
                .expect("verified consequence")
                .to_owned(),
        ];
        if self.change == Change::Tools(true) {
            lines.push(format!("Tools will run in {}", self.after.cwd));
        }
        if let Some(text) = self.wire["plan"]["text"].as_str() {
            lines.push("Plan prompt".into());
            lines.extend(text.split('\n').map(str::to_owned));
        }
        // Keep the complete review evidence inspectable. Show the prompt once,
        // with its actual line breaks, and keep the common policy fields together.
        lines.push(String::new());
        for (name, value) in self.wire.as_object().expect("verified Review") {
            match name.as_str() {
                "consequence" => {}
                "plan" => lines.push(format!(
                    "Plan file · present {} · SHA-256 {}",
                    value["present"], value["sha256"]
                )),
                _ => lines.push(format!("{name}  {value}")),
            }
        }
        lines
    }
    fn saved_after(&self) -> Value {
        let mut v = self.wire["saved_before"].clone();
        match &self.change {
            Change::Tools(value) => v["tools_enabled"] = json!(value),
            Change::Profile(value) => v["tool_profile"] = json!(value.as_str()),
            Change::Plan(_) => {}
            Change::ToolSelection { .. } => {
                v["tools_disabled"] = self.after.wire["tools_disabled"].clone()
            }
        }
        v
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Receipt {
    pub review: Review,
    pub binding: Binding,
    pub apply_operation_id: String,
    pub status: Status,
    pub generation_after: Option<u64>,
    pub effect_attempted: bool,
    pub changed: bool,
    pub runtime_after: Option<Facts>,
    pub owner_after: Option<Owner>,
    pub reason: Option<String>,
    pub wire: Value,
}
fn optional_facts(v: &Value, key: &str, binding: &Binding) -> io::Result<Option<Facts>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        Facts::parse_bound(&v[key], binding).map(Some)
    }
}
fn optional_owner(v: &Value, key: &str, binding: &Binding) -> io::Result<Option<Owner>> {
    if v.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        Owner::parse(&v[key], binding).map(Some)
    }
}
fn invocation(v: &Value, r: &Review, apply: &str) -> io::Result<()> {
    r.binding.verify(v)?;
    if uuid(v, "action_id")? != r.action_id
        || hash(v, "review_token")? != r.token
        || command_id(v, "apply_operation_id")? != apply
        || count(v, "generation_before")? != r.generation
    {
        return Err(bad("Runtime reply changed reviewed invocation"));
    }
    Ok(())
}
impl Receipt {
    pub fn parse(v: &Value, r: &Review, apply: &str) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "action_id",
                "review_token",
                "apply_operation_id",
                "generation_before",
                "generation_after",
                "status",
                "effect_attempted",
                "changed",
                "runtime_after",
                "owner_after",
                "persistence",
                "saved_after",
                "revisions_after",
                "first_system_after",
                "reason",
                "readback_token",
                "close_required",
            ],
        )?;
        invocation(v, r, apply)?;
        if v["kind"] != "runtime_controls_result" {
            return Err(bad("Wrong runtime result kind"));
        }
        let status = Status::parse(v, "status")?;
        let generation_after = maybe_count(v, "generation_after")?;
        let effect_attempted = boolean(v, "effect_attempted")?;
        let changed = boolean(v, "changed")?;
        let close = boolean(v, "close_required")?;
        let runtime_after = optional_facts(v, "runtime_after", &r.binding)?;
        let owner_after = optional_owner(v, "owner_after", &r.binding)?;
        let reason = optional(v, "reason", 2048)?;
        if changed && !effect_attempted {
            return Err(bad(
                "Runtime result claims change without an attempted effect",
            ));
        }
        let persistence = text(v, "persistence", 32)?;
        if !v["saved_after"].is_null() {
            saved(&v["saved_after"], r.binding.tool_selection)?;
        }
        if !v["revisions_after"].is_null() {
            revisions(&v["revisions_after"])?;
        }
        system(&v["first_system_after"])?;
        hash(v, "readback_token")?;
        match status {
            Status::Applied | Status::SessionOnly => {
                let next = r
                    .generation
                    .checked_add(1)
                    .filter(|n| *n <= MAX_N)
                    .ok_or_else(|| bad("Runtime generation overflow"))?;
                if generation_after != Some(next)
                    || runtime_after.as_ref() != Some(&r.after)
                    || owner_after.as_ref() != Some(&r.owner_before)
                    || close
                    || v["first_system_after"] != r.wire["first_system_after"]
                    || v["saved_after"].is_null()
                    || v["revisions_after"].is_null()
                {
                    return Err(bad("Runtime result lacks exact canonical after proof"));
                }
                if !effect_attempted
                    && (changed
                        || r.before != r.after
                        || r.wire["first_system_before"] != r.wire["first_system_after"]
                        || v["saved_after"] != r.wire["saved_before"]
                        || v["revisions_after"] != r.wire["revisions"])
                {
                    return Err(bad("Runtime no-op claims an unattempted mutation"));
                }
                if status == Status::SessionOnly {
                    if persistence != "failed"
                        || reason.is_none()
                        || !effect_attempted
                        || v["saved_after"] != r.wire["saved_before"]
                        || v["revisions_after"] != r.wire["revisions"]
                    {
                        return Err(bad(
                            "Session-only runtime result lacks unchanged saved evidence",
                        ));
                    }
                } else {
                    let expected = if matches!(r.change, Change::Plan(_)) {
                        "not_applicable"
                    } else {
                        "saved"
                    };
                    if persistence != expected
                        || reason.is_some()
                        || v["saved_after"] != r.saved_after()
                    {
                        return Err(bad("Runtime result changed selected saved values"));
                    }
                    for key in ["global", "agent"] {
                        if v["revisions_after"][key] != r.wire["revisions"][key] {
                            return Err(bad(
                                "Runtime control wrote an unrelated settings destination",
                            ));
                        }
                    }
                    if matches!(r.change, Change::Plan(_))
                        && v["revisions_after"] != r.wire["revisions"]
                    {
                        return Err(bad("Plan mode wrote durable settings"));
                    }
                }
            }
            Status::Refused => {
                if generation_after.is_some()
                    || effect_attempted
                    || changed
                    || runtime_after.is_some()
                    || owner_after.is_some()
                    || !v["saved_after"].is_null()
                    || !v["revisions_after"].is_null()
                    || !v["first_system_after"].is_null()
                    || persistence != "not_attempted"
                    || reason.is_none()
                    || close
                {
                    return Err(bad(
                        "Refused runtime control claims effects or after observations",
                    ));
                }
            }
            Status::Uncertain => {
                if generation_after.is_some()
                    || !effect_attempted
                    || persistence != "uncertain"
                    || reason.is_none()
                    || !close
                {
                    return Err(bad("Uncertain runtime control lacks required closure"));
                }
            }
        }
        Ok(Self {
            review: r.clone(),
            binding: r.binding.clone(),
            apply_operation_id: apply.into(),
            status,
            generation_after,
            effect_attempted,
            changed,
            runtime_after,
            owner_after,
            reason,
            wire: v.clone(),
        })
    }
    pub fn readback_request(&self) -> io::Result<Value> {
        let mut v = self.binding.request(READBACK);
        v["action_id"] = json!(self.review.action_id);
        v["review_token"] = json!(self.review.token);
        v["apply_operation_id"] = json!(self.apply_operation_id);
        v["readback_token"] = self.wire["readback_token"].clone();
        v["result_sha256"] = json!(digest(&self.wire)?);
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Readback {
    pub binding: Binding,
    pub status: Status,
    pub current: Option<Facts>,
    pub owner: Option<Owner>,
    pub verified: bool,
    pub release: Release,
    pub generation_after: Option<u64>,
    pub effective_transition: Option<EffectiveTransition>,
    pub wire: Value,
}
impl Readback {
    pub fn parse(v: &Value, a: &Receipt) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "action_id",
                "review_token",
                "apply_operation_id",
                "readback_operation_id",
                "result_sha256",
                "generation_before",
                "generation_after",
                "status",
                "verified",
                "current",
                "owner",
                "effective_transition",
                "saved",
                "revisions",
                "first_system",
                "reason",
                "release",
            ],
        )?;
        invocation(v, &a.review, &a.apply_operation_id)?;
        let status = Status::parse(v, "status")?;
        let generation_after = maybe_count(v, "generation_after")?;
        command_id(v, "readback_operation_id")?;
        if v["kind"] != "runtime_controls_readback"
            || hash(v, "result_sha256")? != digest(&a.wire)?
            || status != a.status
            || generation_after != a.generation_after
        {
            return Err(bad("Runtime readback changed its exact result"));
        }
        let current = optional_facts(v, "current", &a.binding)?;
        let owner = optional_owner(v, "owner", &a.binding)?;
        let verified = boolean(v, "verified")?;
        let release = Release::parse(v, "release")?;
        let reason = optional(v, "reason", 2048)?;
        let effective_transition = if v["effective_transition"].is_null() {
            None
        } else {
            Some(EffectiveTransition::parse(v, "effective_transition")?)
        };
        if !v["saved"].is_null() {
            saved(&v["saved"], a.binding.tool_selection)?;
        }
        if !v["revisions"].is_null() {
            revisions(&v["revisions"])?;
        }
        system(&v["first_system"])?;
        if !verified {
            if release != Release::Close || reason.is_none() || effective_transition.is_some() {
                return Err(bad(
                    "Unverified runtime readback must close without policy installation",
                ));
            }
        } else {
            match status {
                Status::Applied | Status::SessionOnly => {
                    let facts = current
                        .as_ref()
                        .ok_or_else(|| bad("Verified runtime readback omitted current policy"))?;
                    let observed = owner
                        .as_ref()
                        .ok_or_else(|| bad("Verified runtime readback omitted owner"))?;
                    if release != Release::Install
                        || !facts.stable_eq(&a.review.after)
                        || v["saved"] != a.wire["saved_after"]
                        || v["revisions"] != a.wire["revisions_after"]
                        || v["first_system"] != a.wire["first_system_after"]
                        || reason != a.reason
                    {
                        return Err(bad(
                            "Runtime readback lacks independent selected-state proof",
                        ));
                    }
                    verify_effective(a, facts, observed, effective_transition)?;
                }
                Status::Refused => {
                    if release != Release::Unchanged
                        || reason != a.reason
                        || effective_transition.is_some()
                    {
                        return Err(bad("No-effect runtime readback changed refusal evidence"));
                    }
                    let supplied = [
                        current.is_some(),
                        owner.is_some(),
                        !v["saved"].is_null(),
                        !v["revisions"].is_null(),
                    ];
                    if supplied.iter().any(|x| *x) && !supplied.iter().all(|x| *x)
                        || supplied.iter().all(|x| !*x) && !v["first_system"].is_null()
                    {
                        return Err(bad(
                            "Historical runtime refusal has partial current observations",
                        ));
                    }
                    // Current facts are observations, not R.before equality or adoption authority.
                }
                Status::Uncertain => return Err(bad("Uncertain runtime effects cannot verify")),
            }
        }
        Ok(Self {
            binding: a.binding.clone(),
            status,
            current,
            owner,
            verified,
            release,
            generation_after,
            effective_transition,
            wire: v.clone(),
        })
    }
    /// Candidate only: Management must verify exact observed terminal/settled owner first.
    pub fn policy(&self) -> Option<Policy> {
        if !self.verified || self.release != Release::Install {
            return None;
        }
        Some(Policy {
            binding: self.binding.clone(),
            generation: self.generation_after?,
            facts: self.current.clone()?,
        })
    }
}
fn verify_effective(
    a: &Receipt,
    facts: &Facts,
    owner: &Owner,
    transition: Option<EffectiveTransition>,
) -> io::Result<()> {
    let before = a
        .owner_after
        .as_ref()
        .ok_or_else(|| bad("Runtime result omitted owner sample"))?;
    let old = a
        .runtime_after
        .as_ref()
        .ok_or_else(|| bad("Runtime result omitted effective sample"))?;
    let progressed = match (before.state, owner.state) {
        (OwnerState::Idle, OwnerState::Idle) => false,
        (OwnerState::Active, OwnerState::Active) if before.origin == owner.origin => false,
        (OwnerState::Active | OwnerState::Terminal, OwnerState::Terminal)
            if before.origin == owner.origin =>
        {
            true
        }
        (OwnerState::Active | OwnerState::Terminal, OwnerState::Idle) => true,
        _ => {
            return Err(bad(
                "Runtime readback introduced a new owner behind the admission gate",
            ));
        }
    };
    match transition {
        Some(EffectiveTransition::Unchanged)
            if facts.effective_profile == old.effective_profile =>
        {
            Ok(())
        }
        Some(EffectiveTransition::RestoreSeat)
            if progressed
                && facts.effective_profile == facts.seat_profile
                && facts.effective_profile != old.effective_profile =>
        {
            Ok(())
        }
        _ => Err(bad(
            "Runtime effective profile drift lacks finite owned restoration",
        )),
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Shutdown {
    pub binding: Binding,
    pub published_generation: u64,
    pub wire: Value,
}
fn shutdown_ids(v: &Value) -> io::Result<()> {
    optional_uuid(v, "action_id")?;
    maybe_hash(v, "review_token")?;
    maybe_id(v, "apply_operation_id")?;
    maybe_id(v, "readback_operation_id")?;
    maybe_hash(v, "result_sha256")?;
    maybe_hash(v, "readback_sha256")?;
    if v["action_id"].is_null() != v["review_token"].is_null()
        || v["action_id"].is_null() != v["apply_operation_id"].is_null()
    {
        return Err(bad("Runtime shutdown invocation is partial"));
    }
    if (!v["readback_operation_id"].is_null() && v["result_sha256"].is_null())
        || (!v["readback_sha256"].is_null() && v["readback_operation_id"].is_null())
        || boolean(v, "readback_published")? && v["readback_sha256"].is_null()
    {
        return Err(bad("Runtime shutdown lost publication identity"));
    }
    Ok(())
}
fn joined(v: &Value, key: &str) -> io::Result<()> {
    if !matches!(text(v, key, 16)?.as_str(), "absent" | "joined") {
        return Err(bad("Runtime shutdown retains unjoined owner"));
    }
    Ok(())
}
impl Shutdown {
    pub fn parse(v: &Value, binding: &Binding) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "published_generation",
                "action_id",
                "review_token",
                "apply_operation_id",
                "readback_operation_id",
                "result_sha256",
                "readback_sha256",
                "pending_at_close",
                "readback_published",
                "effect_attempted",
                "disposition",
                "authorization_closed",
                "admission_closed",
                "control_owner",
                "publication_owner",
                "authorization_owners_joined",
                "waiter_owners_joined",
                "remaining_authorizations",
                "remaining_waiters",
                "effects_verified",
            ],
        )?;
        binding.verify(v)?;
        if v["kind"] != "runtime_controls_shutdown" {
            return Err(bad("Wrong runtime shutdown kind"));
        }
        shutdown_ids(v)?;
        boolean(v, "pending_at_close")?;
        let effect = boolean(v, "effect_attempted")?;
        let published = boolean(v, "readback_published")?;
        for key in [
            "authorization_closed",
            "admission_closed",
            "authorization_owners_joined",
            "waiter_owners_joined",
        ] {
            if !boolean(v, key)? {
                return Err(bad(
                    "Runtime shutdown lacks exact admission/owner retirement",
                ));
            }
        }
        if boolean(v, "effects_verified")?
            || count(v, "remaining_authorizations")? != 0
            || count(v, "remaining_waiters")? != 0
        {
            return Err(bad("Runtime shutdown claims effects or retains authority"));
        }
        joined(v, "control_owner")?;
        joined(v, "publication_owner")?;
        let idle = v["action_id"].is_null();
        if idle {
            if v["disposition"] != "idle"
                || effect
                || published
                || v["pending_at_close"] != false
                || v["control_owner"] != "absent"
                || v["publication_owner"] != "absent"
                || ["result_sha256", "readback_sha256", "readback_operation_id"]
                    .iter()
                    .any(|k| !v[*k].is_null())
            {
                return Err(bad("Idle runtime shutdown contains invocation authority"));
            }
        } else {
            if v["control_owner"] != "joined" {
                return Err(bad("Consumed runtime control owner was not joined"));
            }
            match text(v, "disposition", 16)?.as_str() {
                "verified" if published && !v["result_sha256"].is_null() => {}
                "no_effect" if !effect => {}
                "uncertain" if effect => {}
                _ => {
                    return Err(bad(
                        "Runtime shutdown disposition contradicts retained effects",
                    ));
                }
            }
        }
        Ok(Self {
            binding: binding.clone(),
            published_generation: count(v, "published_generation")?,
            wire: v.clone(),
        })
    }
    pub fn verify_idle(&self, generation: u64) -> io::Result<()> {
        if self.wire["disposition"] != "idle" || self.published_generation != generation {
            return Err(bad("Runtime shutdown is not exact idle policy epoch"));
        }
        Ok(())
    }
    pub fn verify_invocation(
        &self,
        r: &Review,
        apply: &str,
        a: Option<&Receipt>,
        v: Option<&Readback>,
        readback_id: Option<&str>,
        generation: u64,
    ) -> io::Result<()> {
        verify_close_invocation(&self.wire, &self.binding, r, apply, a, v, readback_id)?;
        let g = self.published_generation;
        let mut expected = generation;
        if let Some(v) = v {
            if v.verified && v.release == Release::Install {
                expected = v
                    .generation_after
                    .ok_or_else(|| bad("Installed runtime readback omitted generation"))?;
            }
        } else if self.wire["readback_published"] == true
            && self.wire["disposition"] == "verified"
            && let Some(receipt) = a
        {
            expected = receipt.generation_after.unwrap_or(generation);
        }
        if g != expected {
            return Err(bad("Runtime shutdown changed published generation"));
        }
        if let Some(a) = a {
            let expected = if a.status == Status::Refused {
                "no_effect"
            } else if let Some(v) = v {
                if v.verified && matches!(v.release, Release::Install | Release::Unchanged) {
                    "verified"
                } else if !a.effect_attempted {
                    "no_effect"
                } else {
                    "uncertain"
                }
            } else if self.wire["readback_published"] == true
                && self.wire["disposition"] == "verified"
            {
                "verified"
            } else if !a.effect_attempted {
                "no_effect"
            } else {
                "uncertain"
            };
            if self.wire["disposition"] != expected {
                return Err(bad("Runtime shutdown changed consumed outcome"));
            }
        }
        Ok(())
    }
}
fn verify_close_invocation(
    value: &Value,
    binding: &Binding,
    r: &Review,
    apply: &str,
    a: Option<&Receipt>,
    v: Option<&Readback>,
    readback_id: Option<&str>,
) -> io::Result<()> {
    if *binding != r.binding
        || value["action_id"] != r.action_id
        || value["review_token"] != r.token
        || value["apply_operation_id"] != apply
    {
        return Err(bad("Runtime shutdown changed known reviewed invocation"));
    }
    if value["readback_operation_id"].as_str() != readback_id {
        return Err(bad(
            "Runtime shutdown lost or invented sent readback command",
        ));
    }
    if let Some(a) = a
        && (a.review != *r
            || a.apply_operation_id != apply
            || value["result_sha256"] != digest(&a.wire)?
            || value["effect_attempted"] != a.effect_attempted)
    {
        return Err(bad("Runtime shutdown changed known Apply evidence"));
    }
    if let Some(v) = v
        && (v.binding != *binding
            || value["readback_sha256"] != digest(&v.wire)?
            || value["readback_published"] != true
            || v.wire["readback_operation_id"].as_str() != readback_id)
    {
        return Err(bad("Runtime shutdown erased consumed readback publication"));
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HistoricalShutdown {
    pub binding: Binding,
    pub wire: Value,
}
impl HistoricalShutdown {
    pub fn parse(v: &Value, binding: &Binding) -> io::Result<Self> {
        object(
            v,
            &[
                "kind",
                "action_id",
                "review_token",
                "apply_operation_id",
                "readback_operation_id",
                "result_sha256",
                "readback_sha256",
                "readback_published",
                "disposition",
                "effect_attempted",
                "execution_authority",
                "command_owner",
                "publication_owner",
                "effects_verified",
            ],
        )?;
        binding.verify(v)?;
        shutdown_ids(v)?;
        joined(v, "publication_owner")?;
        if v["kind"] != "runtime_controls_historical_shutdown"
            || v["action_id"].is_null()
            || v["disposition"] != "refused"
            || boolean(v, "effect_attempted")?
            || boolean(v, "execution_authority")?
            || boolean(v, "effects_verified")?
            || v["command_owner"] != "joined"
        {
            return Err(bad(
                "Historical runtime refusal claims execution or unjoined ownership",
            ));
        }
        Ok(Self {
            binding: binding.clone(),
            wire: v.clone(),
        })
    }
    pub fn verify_invocation(
        &self,
        r: &Review,
        apply: &str,
        a: Option<&Receipt>,
        v: Option<&Readback>,
        readback_id: Option<&str>,
    ) -> io::Result<()> {
        verify_close_invocation(&self.wire, &self.binding, r, apply, a, v, readback_id)?;
        if a.is_some_and(|a| a.status != Status::Refused)
            || v.is_some_and(|v| {
                v.status != Status::Refused || v.current.is_some() || v.owner.is_some()
            })
        {
            return Err(bad("Historical control refusal changed current policy"));
        }
        Ok(())
    }
}
/// Strict raw-body entry point for callers retaining JSON bytes. The outer wire
/// decoder must apply equivalent duplicate-key rejection before constructing Value.
pub fn strict_body(raw: &str) -> io::Result<Value> {
    if raw.len() > BODY {
        return Err(bad("Runtime body exceeds byte budget"));
    }
    crate::deferred_control::strict_object(raw)
}
