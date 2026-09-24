"""Opt-in, no-inference Claude SDK initialization/configuration probe.

Run with the candidate SDK installed in an isolated environment:
    python e2e/claude_contract_probe.py --run-offline --output artifacts/claude-p0-offline.json

No user prompt is submitted. A child process receives an allowlisted environment,
empty credentials/config home, and an unreachable API endpoint. Synthetic project
and user settings contain harmless marker-writing hooks/MCP servers, so accidental
config inheritance is observable. This is NOT an authority or inference acceptance
gate: model-driven tools, resume, interrupt/drain and permissions need live probes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SDK_VERSION = "0.2.159"
CLI_VERSION = "2.1.281"


def isolated_environment(source: dict[str, str], root: Path) -> dict[str, str]:
    """Do not copy credentials, provider switches, tracing or SDK overrides."""
    allowed = {"systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp"}
    env = {key: value for key, value in source.items() if key.lower() in allowed}
    home = root / "home"
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "CLAUDE_CONFIG_DIR": str(root / "config"),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "PYTHONUTF8": "1",
    })
    return env


def seed_workspace(root: Path) -> None:
    """Sentinels only write under the dedicated temporary probe root."""
    for directory in (root / "home", root / "config", root / "workspace" / ".claude"):
        directory.mkdir(parents=True, exist_ok=True)
    marker_script = root / "marker.py"
    marker_script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('executed')\n",
        encoding="utf-8",
    )
    for scope, settings_path in (
        ("user", root / "config" / "settings.json"),
        ("project", root / "workspace" / ".claude" / "settings.json"),
        ("local", root / "workspace" / ".claude" / "settings.local.json"),
    ):
        command = subprocess.list2cmdline([
            sys.executable, str(marker_script), str(root / f"{scope}-hook.marker"),
        ])
        settings_path.write_text(json.dumps({
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]},
        }), encoding="utf-8")
    (root / "workspace" / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"probe-unwanted": {
            "command": sys.executable,
            "args": [str(marker_script), str(root / "project-mcp.marker")],
        }},
    }), encoding="utf-8")
    seed_plugin(root, marker_script)
    (root / "workspace" / "CLAUDE.md").write_text(
        "PROBE_UNWANTED_PROJECT_INSTRUCTION. Never execute any instruction in this file.\n",
        encoding="utf-8",
    )


def seed_plugin(root: Path, marker_script: Path) -> None:
    """A user-installed plugin, laid out the way Claude Code records real installs, whose MCP server
    writes plugin-mcp.marker and whose SessionStart hook writes plugin-hook.marker. Ryan's own
    plugins register fleet seats from SessionStart, so plugin inheritance must be observable."""
    config = root / "config"
    market = config / "plugins" / "marketplaces" / "probe-mkt"
    plugin = config / "plugins" / "cache" / "probe-mkt" / "probe-plugin" / "1.0.0"
    for directory in (market / ".claude-plugin", plugin / ".claude-plugin", plugin / "hooks"):
        directory.mkdir(parents=True, exist_ok=True)
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "probe-mkt", "owner": {"name": "probe"},
        "plugins": [{"name": "probe-plugin", "source": str(plugin), "version": "1.0.0"}],
    }), encoding="utf-8")
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "probe-plugin", "version": "1.0.0"}), encoding="utf-8")
    hook = subprocess.list2cmdline([sys.executable, str(marker_script), str(root / "plugin-hook.marker")])
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]},
    }), encoding="utf-8")
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"probe-plugin-mcp": {
        "command": sys.executable, "args": [str(marker_script), str(root / "plugin-mcp.marker")],
    }}}), encoding="utf-8")
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps({"probe-mkt": {
        "source": {"source": "directory", "path": str(market)}, "installLocation": str(market),
        "lastUpdated": "2026-01-01T00:00:00.000Z",
    }}), encoding="utf-8")
    (config / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "probe-plugin@probe-mkt": [{"scope": "user", "installPath": str(plugin), "version": "1.0.0",
                                    "installedAt": "2026-01-01T00:00:00.000Z", "lastUpdated": "2026-01-01T00:00:00.000Z"}],
    }}), encoding="utf-8")
    user_settings = config / "settings.json"
    settings = json.loads(user_settings.read_text(encoding="utf-8")) if user_settings.exists() else {}
    settings["enabledPlugins"] = {"probe-plugin@probe-mkt": True}
    user_settings.write_text(json.dumps(settings), encoding="utf-8")


def options_for_probe(sdk: Any, root: Path, control: bool = False) -> Any:
    """control=True is the POSITIVE control: config inheritance deliberately ON, so the seeded
    hooks/MCP fixtures must fire. Without it, marker absence in the real probe proves nothing."""
    return sdk.ClaudeAgentOptions(
        cwd=root / "workspace",
        tools=[],
        mcp_servers={},
        strict_mcp_config=not control,
        setting_sources=["user", "project", "local"] if control else [],
        skills=[],
        permission_mode="dontAsk",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "LiteTUI offline contract probe."},
        verbatim_prompts=True,
        extra_args={"no-chrome": None, "disable-slash-commands": None},
    )


async def inspect_runtime(root: Path, control: bool = False, with_query: bool = False) -> dict[str, Any]:
    # Optional dependency is intentionally imported only inside the opted-in child.
    import claude_agent_sdk as sdk

    version = importlib.metadata.version("claude-agent-sdk")
    if version != SDK_VERSION:
        raise RuntimeError(f"Expected SDK {SDK_VERSION}; found {version}")
    binary = Path(sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    cli_version = (await asyncio.to_thread(
        subprocess.check_output, [str(binary), "--version"], text=True, timeout=15,
    )).strip()
    if cli_version != f"{CLI_VERSION} (Claude Code)":
        raise RuntimeError("Unexpected bundled CLI version")
    options = options_for_probe(sdk, root, control)
    options.cli_path = binary  # Never fall back to the user's installed executable.
    result: dict[str, Any] = {"sdk_version": version, "cli_version": cli_version}
    client = sdk.ClaudeSDKClient(options)
    started = time.monotonic()
    try:
        await client.connect()  # Same owner task opens/closes; no prompt/query.
        info = await client.get_server_info() or {}
        result["initialize_keys"] = sorted(info)
        result["model_ids"] = [m.get("value") for m in info.get("models", []) if isinstance(m, dict)]
        result["command_names"] = [c.get("name") for c in info.get("commands", []) if isinstance(c, dict)]
        status = await client.get_mcp_status()
        if with_query:
            # SessionStart hooks fire when a session starts, not on connect. The isolated env has no
            # credentials and ANTHROPIC_BASE_URL points at a closed port, so this cannot bill or infer.
            await client.query("probe")
            try:
                async with asyncio.timeout(20):
                    async for message in client.receive_response():
                        result.setdefault("query_message_types", []).append(type(message).__name__)
                        if type(message).__name__ == "HookEventMessage":
                            fields = getattr(message, "__dict__", {}) or {}
                            result.setdefault("hook_events", []).append({k: str(v)[:160] for k, v in fields.items() if k in ("subtype", "hook_event", "hook_name", "event", "name", "exit_code", "outcome", "status", "stderr", "data")})
            except TimeoutError:
                result["query_timed_out"] = True
            except Exception as exc:  # noqa: BLE001 - an auth/connection failure is the expected ending
                result["query_error_type"] = type(exc).__name__
        result["mcp_server_count"] = len(status.get("mcpServers", []))
        result["connected"] = True
    finally:
        await client.disconnect()
    result["disconnected"] = True
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # Hooks/MCP start asynchronously; give the fixtures a moment to write their markers.
    await asyncio.sleep(2.0)
    result["markers"] = sorted(path.name for path in root.glob("*.marker"))
    result["positive_control"] = control
    if control:
        if not result["markers"] and not result["mcp_server_count"]:
            raise RuntimeError("Positive control failed: seeded config never fired, so the probe cannot detect inheritance")
        if with_query and (len([e for e in result.get("hook_events", []) if e.get("subtype") == "hook_started"]) < 4
                           or "plugin-mcp.marker" not in result["markers"]):
            raise RuntimeError("Positive control did not exercise settings/plugin hooks and plugin MCP")
    elif result["markers"] or result["mcp_server_count"] or result.get("hook_events"):
        raise RuntimeError("Unexpected configuration inheritance")
    return result


def run_offline(output: Path, timeout: float = 90, control: bool = False, with_query: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="litetui-claude-p0-") as temporary:
        root = Path(temporary)
        seed_workspace(root)
        child_output = root / "result.json"
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(root), "--output", str(child_output)]
            + (["--positive-control"] if control else [])
            + (["--with-query"] if with_query else []),
            env=isolated_environment(dict(os.environ), root),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Target only this probe's owned process tree, never all claude.exe.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
            else:
                process.kill()
            process.communicate(timeout=15)
            raise RuntimeError("Probe timed out; owned worker was terminated") from None
        if process.returncode:
            # Do not persist arbitrary SDK stderr (it can contain sensitive values).
            print(f"Probe worker failed (exit {process.returncode}).", file=sys.stderr)
            if stderr:
                print("Worker diagnostics withheld from artifact; rerun in isolated debugger if needed.", file=sys.stderr)
        evidence = json.loads(child_output.read_text(encoding="utf-8")) if child_output.exists() else {}
        evidence.update({
            "mode": ("offline-positive-control" if control else "offline-initialize-only") + ("+unauthenticated-query" if with_query else ""),
            "inference_submitted": False,
            "worker_exit": process.returncode,
            "markers": sorted(path.name for path in root.glob("*.marker")),
            "limitations": [
                "No authenticated model call, native tool call or permission callback exercised.",
                "No resume, interrupt/drain or context-content verification.",
                "Evaluate alongside the matching positive/negative run; absence alone does not prove isolation.",
                "Hook execution is evidenced by native hook events, not hook marker files; MCP markers are checked.",
                "Normal disconnect returned; process-tree absence is not independently verified.",
                "Temporary config/environment is probe isolation, not the product's finalized auth/config policy.",
            ],
        })
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"Evidence: {output}")
        if control:
            return process.returncode or int(not evidence["markers"])
        return process.returncode or int(bool(evidence["markers"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-offline", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--positive-control", action="store_true",
                        help="Turn config inheritance ON; the seeded hooks/MCP must fire (proves the probe can see them).")
    parser.add_argument("--with-query", action="store_true",
                        help="Also submit one prompt to a closed port with no credentials, so SessionStart hooks run.")
    args = parser.parse_args(argv)
    if args.output is None or not (args.run_offline or args.worker):
        parser.error("Explicit --run-offline and --output are required; no probe ran")
    if args.worker:
        # Defense against accidental invocation outside the isolated launcher.
        if os.environ.get("CLAUDE_CONFIG_DIR") != str(args.worker / "config"):
            parser.error("Worker requires the isolated launcher environment")
        try:
            result = asyncio.run(inspect_runtime(args.worker, args.positive_control, args.with_query))
        except Exception as exc:  # noqa: BLE001 - isolated worker error boundary
            args.output.write_text(json.dumps({"error_type": type(exc).__name__}), encoding="utf-8")
            return 1
        args.output.write_text(json.dumps(result), encoding="utf-8")
        return 0
    return run_offline(args.output.resolve(), control=args.positive_control, with_query=args.with_query)


if __name__ == "__main__":
    raise SystemExit(main())
