//! Invocation-only Python launch options. The retained source owns endpoint,
//! model-file, credentials, hardware and backend readiness decisions.
use std::{collections::HashSet, io};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backend {
    Custom,
    LmStudio,
    LlamaCpp,
    Ninfer,
    Strata,
    Codex,
    Claude,
    Cline,
    Free,
}
impl Backend {
    /// Checked against llm_backend.BACKENDS by the controlled source gate.
    pub const ALL: [Self; 9] = [
        Self::Custom,
        Self::LmStudio,
        Self::LlamaCpp,
        Self::Ninfer,
        Self::Strata,
        Self::Codex,
        Self::Claude,
        Self::Cline,
        Self::Free,
    ];
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Custom => "custom",
            Self::LmStudio => "lmstudio",
            Self::LlamaCpp => "llamacpp",
            Self::Ninfer => "ninfer",
            Self::Strata => "strata",
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::Cline => "cline",
            Self::Free => "free",
        }
    }
    pub fn parse(value: &str) -> io::Result<Self> {
        Self::ALL
            .into_iter()
            .find(|backend| backend.as_str() == value)
            .ok_or_else(|| io::Error::other("Unknown backend; use a source-supported backend name"))
    }
    pub fn local(self) -> bool {
        matches!(
            self,
            Self::Custom | Self::LmStudio | Self::LlamaCpp | Self::Ninfer | Self::Strata
        )
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServerMode {
    Auto,
    Connect,
    Start,
}
impl ServerMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Connect => "connect",
            Self::Start => "start",
        }
    }
    fn parse(value: &str) -> io::Result<Self> {
        match value {
            "auto" => Ok(Self::Auto),
            "connect" => Ok(Self::Connect),
            "start" => Ok(Self::Start),
            _ => Err(io::Error::other(
                "--server-mode must be auto, connect, or start",
            )),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LaunchOptions {
    pub engine_management: bool,
    pub base_url: Option<String>,
    pub server_mode: Option<ServerMode>,
    pub load_model: bool,
    pub context_length: Option<u64>,
    pub max_tokens: Option<u64>,
    pub server_executable: Option<String>,
    pub model_path: Option<String>,
    pub server_command: Option<Vec<String>>,
    pub api_key_env: Option<String>,
    pub server_timeout: u64,
    pub codex_engine: Option<String>,
    pub thinking_level: Option<String>,
}
impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            engine_management: false,
            base_url: None,
            server_mode: None,
            load_model: false,
            context_length: None,
            max_tokens: None,
            server_executable: None,
            model_path: None,
            server_command: None,
            api_key_env: None,
            server_timeout: 600,
            codex_engine: None,
            thinking_level: None,
        }
    }
}
fn text(value: &str, field: &str) -> io::Result<()> {
    if value.trim().is_empty() || value.contains('\0') || value.len() > 65536 {
        return Err(io::Error::other(format!(
            "{field} must be nonempty, NUL-free, and at most 64 KiB"
        )));
    }
    Ok(())
}
fn positive(value: &str, field: &str) -> io::Result<u64> {
    let number = value
        .parse::<u64>()
        .map_err(|_| io::Error::other(format!("{field} must be a positive integer")))?;
    if number == 0 {
        return Err(io::Error::other(format!(
            "{field} must be a positive integer"
        )));
    }
    Ok(number)
}
impl LaunchOptions {
    /// Consume launch flags while preserving legacy option/value pairs verbatim.
    /// In particular a prompt VALUE beginning with a known flag is still a value.
    pub fn extract(args: &[String]) -> io::Result<(Self, Vec<String>)> {
        let mut result = Self::default();
        let mut rest = Vec::new();
        let mut seen = HashSet::new();
        let mut index = 0;
        while index < args.len() {
            let key = match args[index].as_str() {
                "--host" => "--base-url",
                "--reasoning-effort" => "--thinking-level",
                "--start-server" => "--server-mode",
                key => key,
            };
            if !matches!(
                key,
                "--base-url"
                    | "--engine-management"
                    | "--server-mode"
                    | "--load-model"
                    | "--context-length"
                    | "--max-tokens"
                    | "--server-executable"
                    | "--model-path"
                    | "--server-command"
                    | "--api-key-env"
                    | "--server-timeout"
                    | "--codex-engine"
                    | "--thinking-level"
            ) {
                rest.push(args[index].clone());
                index += 1;
                if index < args.len() {
                    rest.push(args[index].clone());
                    index += 1;
                }
                continue;
            }
            if !seen.insert(key) {
                return Err(io::Error::other(format!("Duplicate {key}")));
            }
            if key == "--engine-management" {
                result.engine_management = true;
                index += 1;
                continue;
            }
            if key == "--load-model" {
                result.load_model = true;
                index += 1;
                continue;
            }
            if args[index] == "--start-server" {
                result.server_mode = Some(ServerMode::Start);
                index += 1;
                continue;
            }
            let value = args
                .get(index + 1)
                .ok_or_else(|| io::Error::other(format!("Missing value for {key}")))?;
            text(value, key)?;
            match key {
                "--base-url" => result.base_url = Some(value.clone()),
                "--server-mode" => result.server_mode = Some(ServerMode::parse(value)?),
                "--context-length" => result.context_length = Some(positive(value, key)?),
                "--max-tokens" => result.max_tokens = Some(positive(value, key)?),
                "--server-executable" => result.server_executable = Some(value.clone()),
                "--model-path" => result.model_path = Some(value.clone()),
                "--server-command" => {
                    result.server_command = Some(serde_json::from_str(value).map_err(|_| {
                        io::Error::other("--server-command must be a JSON array of strings")
                    })?)
                }
                "--api-key-env" => result.api_key_env = Some(value.clone()),
                "--server-timeout" => result.server_timeout = positive(value, key)?,
                "--codex-engine" => result.codex_engine = Some(value.clone()),
                "--thinking-level" => result.thinking_level = Some(value.clone()),
                _ => unreachable!(),
            }
            index += 2;
        }
        Ok((result, rest))
    }
    pub fn effective_mode(&self, backend: Backend) -> ServerMode {
        self.server_mode.unwrap_or(if backend.local() {
            ServerMode::Connect
        } else {
            ServerMode::Auto
        })
    }
    /// No filesystem/environment/network lookup: source validation stays authoritative.
    pub fn validate(&self, backend: Backend, model: &str) -> io::Result<()> {
        text(model, "--model")?;
        if let Some(level) = &self.thinking_level {
            text(level, "--thinking-level")?;
            if level.len() > 64 || level.chars().any(char::is_control) {
                return Err(io::Error::other(
                    "Thinking level must be at most 64 bytes without control characters",
                ));
            }
        }
        for (value, field) in [
            (&self.base_url, "--base-url"),
            (&self.server_executable, "--server-executable"),
            (&self.model_path, "--model-path"),
            (&self.api_key_env, "--api-key-env"),
        ] {
            if let Some(value) = value {
                text(value, field)?;
            }
        }
        if self.server_timeout == 0 || self.context_length == Some(0) || self.max_tokens == Some(0)
        {
            return Err(io::Error::other(
                "Server timeout and token counts must be positive",
            ));
        }
        if let Some(engine) = &self.codex_engine
            && (backend != Backend::Codex || !matches!(engine.as_str(), "native" | "http"))
        {
            return Err(io::Error::other(
                "--codex-engine native|http applies only to Codex",
            ));
        }
        let mode = self.effective_mode(backend);
        if self.engine_management
            && (!matches!(backend, Backend::Ninfer | Backend::Strata)
                || mode != ServerMode::Connect
                || self.load_model)
        {
            return Err(io::Error::other(
                "--engine-management requires NInfer/Strata connect mode without automatic start/load",
            ));
        }
        if backend.local() && mode == ServerMode::Auto {
            return Err(io::Error::other(
                "Local auto may discover or start a server; choose connect or explicitly start",
            ));
        }
        if !backend.local()
            && (mode != ServerMode::Auto
                || self.base_url.is_some()
                || self.context_length.is_some()
                || self.server_executable.is_some()
                || self.model_path.is_some()
                || self.server_command.is_some()
                || self.api_key_env.is_some()
                || self.load_model)
        {
            return Err(io::Error::other(
                "This provider owns its runtime; local server/load options do not apply",
            ));
        }
        if matches!(backend, Backend::Claude | Backend::Codex) && self.max_tokens.is_some() {
            return Err(io::Error::other("Claude/Codex own their output limits"));
        }
        if self.load_model && !matches!(backend, Backend::LlamaCpp | Backend::LmStudio) {
            return Err(io::Error::other(
                "--load-model applies only to llama.cpp or LM Studio",
            ));
        }
        if self.context_length.is_some() {
            if matches!(backend, Backend::LlamaCpp | Backend::LmStudio) && !self.load_model {
                return Err(io::Error::other(
                    "--context-length requires explicit --load-model",
                ));
            }
            if backend == Backend::LlamaCpp && mode != ServerMode::Start {
                return Err(io::Error::other(
                    "llama.cpp context changes require --start-server to permit a preset rebuild",
                ));
            }
            if matches!(backend, Backend::Ninfer | Backend::Strata) && mode != ServerMode::Start {
                return Err(io::Error::other(
                    "Engine context is fixed at startup; use --start-server",
                ));
            }
        }
        if self.server_executable.is_some()
            && !matches!(backend, Backend::LlamaCpp | Backend::Ninfer)
        {
            return Err(io::Error::other(
                "--server-executable applies only to llama.cpp or NInfer; custom uses --server-command",
            ));
        }
        if self.model_path.is_some()
            && !(matches!(backend, Backend::Ninfer | Backend::Strata) && mode == ServerMode::Start
                || backend == Backend::LlamaCpp && self.load_model
                || backend == Backend::Custom && self.server_command.is_some())
        {
            return Err(io::Error::other(
                "--model-path requires an explicit supported engine start/model load/custom command",
            ));
        }
        if let Some(command) = &self.server_command {
            if backend != Backend::Custom || mode != ServerMode::Start {
                return Err(io::Error::other(
                    "--server-command requires --backend custom --start-server",
                ));
            }
            if command.is_empty()
                || command.len() > 128
                || command[0].trim().is_empty()
                || command.iter().any(|arg| arg.contains('\0'))
                || command.iter().map(String::len).sum::<usize>() > 65536
            {
                return Err(io::Error::other(
                    "Server command needs an executable and at most 128 NUL-free arguments / 64 KiB",
                ));
            }
        }
        if backend == Backend::Custom && mode == ServerMode::Start && self.server_command.is_none()
        {
            return Err(io::Error::other(
                "Custom server start requires --server-command JSON",
            ));
        }
        if backend == Backend::Ninfer && mode == ServerMode::Start && self.base_url.is_some() {
            return Err(io::Error::other(
                "NInfer startup allocates its port; --base-url is only for connecting",
            ));
        }
        if let Some(name) = &self.api_key_env {
            let mut chars = name.chars();
            let first = chars
                .next()
                .is_some_and(|ch| ch.is_ascii_alphabetic() || ch == '_');
            if backend != Backend::Custom
                || !first
                || !chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
            {
                return Err(io::Error::other(
                    "--api-key-env needs a variable NAME for custom, never a credential value",
                ));
            }
        }
        Ok(())
    }
    /// Source argparse accepts a string; current model capabilities decide support.
    /// An omitted resume level must not replace the retained source-owned choice.
    pub fn thinking_args(&self, backend: Backend, resumed: bool) -> Vec<String> {
        let level = self.thinking_level.as_deref().or({
            if resumed {
                None
            } else if matches!(backend, Backend::Claude | Backend::Codex) {
                Some("low")
            } else {
                Some("default")
            }
        });
        level
            .map(|value| vec!["--thinking-level".into(), value.into()])
            .unwrap_or_default()
    }
    pub fn python_args(&self, backend: Backend, model: &str) -> io::Result<Vec<String>> {
        self.validate(backend, model)?;
        let mut args = vec![
            "--server-mode".into(),
            self.effective_mode(backend).as_str().into(),
            "--server-timeout".into(),
            self.server_timeout.to_string(),
        ];
        for (field, value) in [
            ("--base-url", self.base_url.clone()),
            (
                "--context-length",
                self.context_length.map(|n| n.to_string()),
            ),
            ("--max-tokens", self.max_tokens.map(|n| n.to_string())),
            ("--server-executable", self.server_executable.clone()),
            ("--model-path", self.model_path.clone()),
            ("--api-key-env", self.api_key_env.clone()),
            ("--codex-engine", self.codex_engine.clone()),
        ] {
            if let Some(value) = value {
                args.extend([field.into(), value]);
            }
        }
        if self.engine_management {
            args.push("--engine-management".into());
        }
        if self.load_model {
            args.push("--load-model".into());
        }
        if let Some(command) = &self.server_command {
            args.extend(["--server-command".into(), serde_json::to_string(command)?]);
        }
        Ok(args)
    }
}
