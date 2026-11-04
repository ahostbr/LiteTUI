//! Read-only diagnostics and cached MCP observations for the current Work instance.
use crate::{theme::Theme, ui, work_lifecycle as work, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

pub const OPERATION: &str = "gui.inspection.native_status";
const MAX_SERVERS: usize = 32;
const MAX_COUNTER: u64 = 9_007_199_254_740_991;

pub fn command(identity: &Identity, work_instance: &str) -> Value {
    let mut value = identity.request();
    value["type"] = json!(OPERATION);
    value["work_instance"] = json!(work_instance);
    value
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Persistence {
    Ok,
    Error,
    Unavailable,
}

impl Persistence {
    fn parse(value: &Value) -> io::Result<Self> {
        match value.as_str() {
            Some("ok") => Ok(Self::Ok),
            Some("error") => Ok(Self::Error),
            Some("unavailable") => Ok(Self::Unavailable),
            _ => Err(work::bad("Invalid persistence status")),
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Ok => "OK",
            Self::Error => "Error",
            Self::Unavailable => "Unavailable",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Server {
    pub name: String,
    pub state: String,
    pub transport: Option<String>,
    pub tool_count: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mcp {
    pub enabled: Option<bool>,
    pub available: bool,
    pub complete: bool,
    pub remaining: Option<u64>,
    pub servers: Vec<Server>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub runtime_version: String,
    pub protocol_version: u64,
    pub conversation_persistence: Persistence,
    pub settings_persistence: Persistence,
    pub mcp: Mcp,
}

fn nullable_count(value: &Value) -> io::Result<Option<u64>> {
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_u64()
        .filter(|&n| n <= MAX_COUNTER)
        .map(Some)
        .ok_or_else(|| work::bad("Invalid cached MCP count"))
}

impl Mcp {
    fn parse(value: &Value) -> io::Result<Self> {
        work::keys(
            value,
            &["enabled", "available", "complete", "remaining", "servers"],
        )?;
        let enabled = match value["enabled"] {
            Value::Null => None,
            Value::Bool(b) => Some(b),
            _ => return Err(work::bad("Invalid MCP setting")),
        };
        let available = value["available"]
            .as_bool()
            .ok_or_else(|| work::bad("Invalid MCP availability"))?;
        let complete = value["complete"]
            .as_bool()
            .ok_or_else(|| work::bad("Invalid MCP completeness"))?;
        let remaining = nullable_count(&value["remaining"])?;
        let rows = value["servers"]
            .as_array()
            .filter(|rows| rows.len() <= MAX_SERVERS)
            .ok_or_else(|| work::bad("MCP server list exceeds the inspection limit"))?;
        if complete != (remaining == Some(0))
            || !available && (complete || remaining.is_some() || !rows.is_empty())
        {
            return Err(work::bad("Inconsistent MCP cache availability"));
        }
        let mut names = HashSet::new();
        let mut servers = Vec::with_capacity(rows.len());
        for row in rows {
            work::keys(row, &["name", "transport", "state", "tool_count"])?;
            let name = work::text(row, "name", 96)?;
            if !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                || !names.insert(name.clone())
            {
                return Err(work::bad("Invalid or repeated cached MCP server name"));
            }
            let transport = match row["transport"].as_str() {
                Some(s @ ("http" | "stdio")) => Some(s.to_owned()),
                None if row["transport"].is_null() => None,
                _ => return Err(work::bad("Unknown cached MCP server transport")),
            };
            let state = match row["state"].as_str() {
                Some(
                    s @ ("failed" | "connected" | "orphan" | "excluded" | "disabled"
                    | "not started (lazy)" | "stopped"),
                ) => s.to_owned(),
                _ => return Err(work::bad("Unknown cached MCP server state")),
            };
            servers.push(Server {
                name,
                state,
                transport,
                tool_count: nullable_count(&row["tool_count"])?,
            });
        }
        Ok(Self {
            enabled,
            available,
            complete,
            remaining,
            servers,
        })
    }
}

impl Snapshot {
    pub fn parse(value: &Value, identity: &Identity, work_instance: &str) -> io::Result<Self> {
        work::bounded(value, 32768)?;
        work::keys(
            value,
            &[
                "version",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "diagnostics",
                "mcp",
            ],
        )?;
        if value["version"].as_u64() != Some(1)
            || value["kind"] != "runtime_inspection"
            || value["session_id"] != identity.session
            || value["agent_id"] != identity.agent
            || value["backend"] != identity.backend
            || value["model"] != identity.model
            || value["work_instance"] != work_instance
        {
            return Err(work::bad(
                "Runtime status belongs to a different Work instance or protocol",
            ));
        }
        let diagnostics = &value["diagnostics"];
        work::keys(
            diagnostics,
            &[
                "runtime_version",
                "protocol_version",
                "conversation_persistence",
                "settings_persistence",
            ],
        )?;
        let runtime_version = work::text(diagnostics, "runtime_version", 64)?;
        if !runtime_version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._+-".contains(&b))
            || diagnostics["protocol_version"].as_u64() != Some(1)
        {
            return Err(work::bad(
                "Invalid runtime or protocol version in diagnostics",
            ));
        }
        Ok(Self {
            runtime_version,
            protocol_version: 1,
            conversation_persistence: Persistence::parse(&diagnostics["conversation_persistence"])?,
            settings_persistence: Persistence::parse(&diagnostics["settings_persistence"])?,
            mcp: Mcp::parse(&value["mcp"])?,
        })
    }

    fn lines(&self) -> Vec<String> {
        let enabled = match self.mcp.enabled {
            Some(true) => "On",
            Some(false) => "Off",
            None => "Unknown",
        };
        let mut lines = vec![
            format!("Runtime version   {}", self.runtime_version),
            format!("Protocol version  {}", self.protocol_version),
            format!(
                "Conversation persistence   {}",
                self.conversation_persistence.label()
            ),
            format!(
                "Settings persistence       {}",
                self.settings_persistence.label()
            ),
            String::new(),
            "MCP servers".into(),
            format!("MCP setting   {enabled}"),
        ];
        if !self.mcp.available {
            lines.push("Cached server status is unavailable.".into());
        } else {
            if self.mcp.servers.is_empty() && self.mcp.complete {
                lines.push("No cached servers.".into());
            }
            for server in &self.mcp.servers {
                let transport = server.transport.as_deref().unwrap_or("transport unknown");
                let tools = match server.tool_count {
                    Some(1) => "1 tool".into(),
                    Some(n) => format!("{n} tools"),
                    None => "tool count unknown".into(),
                };
                lines.push(format!(
                    "{}   {} · {transport} · {tools}",
                    server.name, server.state
                ));
            }
            if !self.mcp.complete {
                lines.push(match self.mcp.remaining {
                    Some(n) => format!("{n} additional server entries omitted."),
                    None => "Partial server list. Additional entry count is unknown.".into(),
                });
            }
        }
        lines
    }
}

#[derive(Default)]
pub struct View {
    open: bool,
    waiting: bool,
    lines: Vec<String>,
    wrapped: Vec<String>,
    width: Option<u16>,
    body: Option<Rect>,
    scroll: usize,
}

impl View {
    pub fn is_open(&self) -> bool {
        self.open
    }
    pub fn open(&mut self) {
        self.open = true;
        self.waiting = true;
        self.replace(vec!["Reading runtime status…".into()]);
    }
    pub fn close(&mut self) {
        *self = Self::default();
    }
    pub fn hide(&mut self) {
        self.body = None;
    }
    fn replace(&mut self, lines: Vec<String>) {
        self.lines = lines;
        self.wrapped.clear();
        self.width = None;
        self.scroll = 0;
        self.hide();
    }
    pub fn apply(&mut self, snapshot: Snapshot) {
        if self.open && self.waiting {
            self.waiting = false;
            self.replace(snapshot.lines());
        }
    }
    pub fn reject(&mut self, reason: &str) {
        if self.open && self.waiting {
            self.waiting = false;
            self.replace(vec![format!("Runtime status unavailable. {reason}")]);
        }
    }
    fn scroll_by(&mut self, amount: isize) {
        if let Some(body) = self.body {
            self.scroll = self
                .scroll
                .saturating_add_signed(amount)
                .min(self.wrapped.len().saturating_sub(usize::from(body.height)));
        }
    }
    pub fn key(&mut self, event: KeyEvent) -> bool {
        if !self.open || !event.modifiers.is_empty() || event.kind == KeyEventKind::Release {
            return false;
        }
        if event.code == KeyCode::Esc && event.kind == KeyEventKind::Press {
            self.close();
            return false;
        }
        let Some(body) = self.body else { return false };
        if self.waiting {
            return false;
        }
        match event.code {
            KeyCode::F(5) if event.kind == KeyEventKind::Press => {
                self.open();
                return true;
            }
            KeyCode::Down => self.scroll_by(1),
            KeyCode::Up => self.scroll_by(-1),
            KeyCode::PageDown => self.scroll_by(isize::try_from(body.height).unwrap_or(1)),
            KeyCode::PageUp => self.scroll_by(-isize::try_from(body.height).unwrap_or(1)),
            KeyCode::End => self.scroll_by(isize::MAX),
            KeyCode::Home => self.scroll = 0,
            _ => {}
        }
        false
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !event.modifiers.is_empty()
            || !self
                .body
                .is_some_and(|body| body.contains(Position::new(event.column, event.row)))
        {
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => self.scroll_by(-3),
            MouseEventKind::ScrollDown => self.scroll_by(3),
            _ => {}
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.hide();
        if !self.open {
            return;
        }
        let area = area.intersection(frame.area());
        if area.width < 24 || area.height < 7 {
            frame.render_widget(Clear, area);
            frame.render_widget(
                Paragraph::new("Resize to read status, or press Esc.")
                    .block(Block::bordered().title(" Runtime status ")),
                area,
            );
            return;
        }
        let width = area.width.saturating_sub(2).min(108);
        let height = area.height.saturating_sub(2).min(36);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let block = Block::bordered()
            .title(" Runtime status ")
            .style(Style::default().fg(theme.foreground).bg(theme.panel));
        let inner = block.inner(rect);
        let body = Rect::new(inner.x, inner.y, inner.width, inner.height - 1);
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);
        if self.width != Some(body.width) {
            self.wrapped = self
                .lines
                .iter()
                .flat_map(|line| ui::wrap(&ui::display_safe(line), usize::from(body.width)))
                .collect();
            self.width = Some(body.width);
        }
        self.scroll = self
            .scroll
            .min(self.wrapped.len().saturating_sub(usize::from(body.height)));
        let shown = self
            .wrapped
            .iter()
            .skip(self.scroll)
            .take(usize::from(body.height))
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(Paragraph::new(shown), body);
        self.body = Some(body);
        let help = if inner.width >= 44 {
            "↑ ↓ / PgUp PgDn scroll · F5 refresh · Esc Settings"
        } else {
            "↑ ↓ scroll · F5 · Esc"
        };
        frame.render_widget(
            Paragraph::new(help).style(Style::default().fg(theme.title)),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}
