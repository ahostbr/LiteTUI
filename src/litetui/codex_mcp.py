"""Project-owned SOTS registrations for LiteTUI's native Codex process only.

Codex reads global TOML, not Claude's .mcp.json. Schema deferral is not process
laziness: native Codex may start these servers when it opens a thread. Never
rewrite the shared config to enforce a host seat's workspace boundary.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

PROJECT_SERVERS = ("SOTS_MCP_CORE", "SOTS_BPGEN", "VibeUE")


def project_config(cwd: Path) -> Path | None:
    """Find the owning ancestor, or a linked worktree's common repository."""
    cwd = cwd.resolve()
    for root in (cwd, *cwd.parents):
        path = root / ".mcp.json"
        if path.is_file():
            return path  # nearest declaration owns scope, even without SOTS
        marker = root / ".git"
        if not marker.exists():
            continue
        # The owning repo is a boundary, not another directory to skip over.
        # Linked worktrees may reuse their common repository's declaration.
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = (root / text.partition(":")[2].strip()).resolve()
                common = gitdir / "commondir"
                if common.is_file():
                    common_root = (gitdir / common.read_text(encoding="utf-8").strip()).resolve().parent
                    path = common_root / ".mcp.json"
                    if path.is_file():
                        return path
        return None
    return None


def _servers(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        servers = doc.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise TypeError("mcpServers must be an object")
        return servers
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        warnings.warn(f"{path}: cannot load project MCP config; SOTS disabled ({exc})",
                      RuntimeWarning, stacklevel=2)
        return None


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"Unsupported MCP config value: {type(value).__name__}")


def config_overrides(cwd: Path) -> tuple[str, ...]:
    path = project_config(cwd)
    if path is None:
        return tuple(f"mcp_servers.{name}.enabled=false" for name in PROJECT_SERVERS)
    servers = _servers(path) or {}
    overrides = []
    for name in PROJECT_SERVERS:
        if name not in servers:
            overrides.append(f"mcp_servers.{name}.enabled=false")
            continue
        entry = servers[name]
        if not isinstance(entry, dict) or not entry.get("command"):
            raise ValueError(f"{path}: {name} needs a stdio command")
        # Claude's autoApprove is not a Codex field. Codex recursively overlays
        # these fields: project command/args/env keys win, undeclared global
        # timeout/env/filter keys remain. This does not promise table replacement.
        # Use the declaring project's cwd, even for a linked worktree.
        cfg = {key: entry[key] for key in (
            "command", "args", "env", "env_vars", "startup_timeout_sec",
            "tool_timeout_sec", "enabled_tools", "disabled_tools",
        ) if key in entry}
        cfg.update(cwd=str(path.parent), enabled=not bool(entry.get("disabled")))
        overrides.append(f"mcp_servers.{name}={_toml(cfg)}")
    return tuple(overrides)
