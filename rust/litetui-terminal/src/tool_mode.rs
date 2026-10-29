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
        // External MCP is not yet connected by this launcher.
        if event["mcp_enabled"].as_bool() != Some(false) {
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
impl Expected {
    pub fn validate(&self, event: &Value) -> io::Result<()> {
        let facts = Facts::from_ready(event)?.ok_or_else(|| {
            io::Error::other(
                "Runtime omitted tool-policy facts; update the Python source before submitting",
            )
        })?;
        if facts.enabled != self.enabled
            || facts.profile != self.profile
            || facts.route != "host"
            || event["tool_profile_requested"].as_str() != Some(self.profile.as_str())
        {
            return Err(io::Error::other(
                "Runtime tool mode/profile/approval route differs from this launch; no prompt sent",
            ));
        }
        let cwd = event["cwd"]
            .as_str()
            .ok_or_else(|| io::Error::other("Runtime workspace missing"))?;
        if !same_workspace(cwd, &self.workspace) {
            return Err(io::Error::other(
                "Runtime workspace differs from this launch; no prompt sent",
            ));
        }
        Ok(())
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
