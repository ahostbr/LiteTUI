//! Launch intent and reported tool policy are separate; readiness checks both.
use serde_json::Value;
use std::{
    io,
    path::{Path, PathBuf},
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Profile {
    #[default]
    Strict,
    Interactive,
    Autonomous,
}
impl Profile {
    pub fn parse(text: &str) -> io::Result<Self> {
        match text {
            "strict" => Ok(Self::Strict),
            "interactive" => Ok(Self::Interactive),
            "autonomous" => Ok(Self::Autonomous),
            _ => Err(io::Error::other(
                "Tool profile must be strict, interactive, or autonomous",
            )),
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Strict => "strict",
            Self::Interactive => "interactive",
            Self::Autonomous => "autonomous",
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Facts {
    pub enabled: bool,
    pub profile: Profile,
    pub route: String,
}
impl Facts {
    pub fn from_ready(event: &Value) -> io::Result<Option<Self>> {
        Self::from_ready_mcp(event, false)
    }
    fn from_ready_mcp(event: &Value, mcp_enabled: bool) -> io::Result<Option<Self>> {
        if event.get("tools_enabled").is_none()
            && event.get("approval_route").is_none()
            && event.get("mcp_enabled").is_none()
        {
            return Ok(None);
        }
        let bad = || {
            io::Error::other(
                "Missing or malformed runtime tool-policy facts; update the Python source",
            )
        };
        let enabled = event["tools_enabled"].as_bool().ok_or_else(bad)?;
        // Only an exact published MCP lifecycle policy permits a live MCP gate.
        if event["mcp_enabled"].as_bool() != Some(mcp_enabled) {
            return Err(bad());
        }
        let profile = Profile::parse(event["tool_profile"].as_str().ok_or_else(bad)?)?;
        let route = event["approval_route"]
            .as_str()
            .filter(|s| matches!(*s, "own" | "host" | "spawner" | "refuse" | "hand"))
            .ok_or_else(bad)?
            .to_owned();
        Ok(Some(Self {
            enabled,
            profile,
            route,
        }))
    }
    pub fn description(&self) -> String {
        format!(
            "Tools {} · {} · approvals: {}",
            if self.enabled { "on" } else { "off" },
            self.profile.as_str(),
            self.route
        )
    }
}
pub struct Expected {
    pub enabled: bool,
    pub profile: Profile,
    pub workspace: PathBuf,
}
/// Callers choose the ownership path explicitly. Target only checks immutable
/// host constraints; it never installs policy or authorizes a transition.
pub enum ValidationMode<'a> {
    Launch,
    Current(&'a crate::runtime_controls::Policy),
    Target,
}
impl Expected {
    pub fn validate(&self, event: &Value) -> io::Result<()> {
        self.validate_mode(event, ValidationMode::Launch)
    }
    pub fn validate_mode(&self, event: &Value, mode: ValidationMode<'_>) -> io::Result<()> {
        let mcp = match mode {
            ValidationMode::Current(p) => p.binding.mcp_lifecycle && p.facts.mcp_enabled,
            _ => false,
        };
        let facts = Facts::from_ready_mcp(event, mcp)?
            .ok_or_else(|| io::Error::other("Runtime omitted tool-policy facts; no prompt sent"))?;
        let requested = match event.get("tool_profile_requested") {
            Some(Value::Null) => None,
            Some(Value::String(value)) => Some(Profile::parse(value)?),
            _ => {
                return Err(io::Error::other(
                    "Runtime omitted nullable requested CLI profile",
                ));
            }
        };
        let cwd = event["cwd"]
            .as_str()
            .ok_or_else(|| io::Error::other("Runtime workspace missing"))?;
        if facts.route != "host" || !same_workspace(cwd, &self.workspace) {
            return Err(io::Error::other(
                "Runtime workspace or approval route differs from owned host",
            ));
        }
        let expected = match mode {
            ValidationMode::Launch => Some((self.enabled, self.profile, Some(self.profile))),
            ValidationMode::Current(policy) => Some((
                policy.facts.tools_enabled,
                policy.facts.effective_profile,
                policy.facts.requested_cli_profile,
            )),
            ValidationMode::Target => None,
        };
        if expected.is_some_and(|(enabled, profile, cli)| {
            facts.enabled != enabled || facts.profile != profile || requested != cli
        }) {
            return Err(io::Error::other(
                "Runtime policy differs from the exact launch/current boundary; no prompt sent",
            ));
        }
        Ok(())
    }
    pub fn validate_facts(
        &self,
        facts: &crate::runtime_controls::Facts,
        mode: ValidationMode<'_>,
    ) -> io::Result<()> {
        self.validate_mode(&serde_json::json!({
            "tools_enabled":facts.tools_enabled,"tool_profile":facts.effective_profile.as_str(),
            "tool_profile_requested":facts.requested_cli_profile.map(Profile::as_str),
            "mcp_enabled":facts.mcp_enabled,"approval_route":facts.approval_route,"cwd":facts.cwd,
        }), mode)
    }
}

// No filesystem operation on a reported path: a UNC probe could block the supervisor.
fn same_workspace(reported: &str, expected: &Path) -> bool {
    if reported.len() > 32768 || reported.contains('\0') || !Path::new(reported).is_absolute() {
        return false;
    }
    if cfg!(windows) {
        fn key(text: &str) -> String {
            let text = text.replace('/', r"\");
            if let Some(tail) = text.strip_prefix(r"\\?\UNC\") {
                format!(r"\\{tail}")
            } else {
                text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
            }
        }
        key(reported).eq_ignore_ascii_case(&key(&expected.to_string_lossy()))
    } else {
        Path::new(reported) == expected
    }
}
