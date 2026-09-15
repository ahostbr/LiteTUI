"""Apply shared host policy at Codex's synchronous native tool hook boundary."""

import json

from litetui import hook_host, sanitize, tool_policy
from litetui.codex_workspace import workspace


def denial(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def identity(name):
    if name in {"Bash", "shell", "shell_command", "exec_command", "write_stdin"}:
        return (
            "bash",
            {
                name,
                "bash",
                "powershell",
                "shell",
                "shell_command",
                "exec_command",
                "write_stdin",
            },
            tool_policy.SHELL_POLICY,
        )
    if name == "apply_patch":
        return (
            name,
            {name, "write", "edit"},
            tool_policy.ToolPolicy(
                frozenset({tool_policy.WORKSPACE_WRITE, tool_policy.EXTERNAL_WRITE}),
                "Codex file changes",
            ),
        )
    if name in {"spawn_agent", "send_input", "resume_agent", "close_agent"}:
        return (
            name,
            {name, "subagent"},
            tool_policy.ToolPolicy(
                frozenset({tool_policy.PROCESS_EXECUTION}), "Codex agent orchestration"
            ),
        )
    if name in {
        "wait",
        "wait_agent",
        "list_agents",
        "update_plan",
        "request_user_input",
    }:
        return name, {name}, tool_policy.READ_POLICY
    if name in {"view_image", "read_file", "list_dir", "grep_files"}:
        return name, {name}, tool_policy.READ_POLICY
    if name in {"web", "web.run", "web_search"}:
        return name, {name, "web_fetch", "web_search"}, tool_policy.NETWORK_READ_POLICY
    return name, {name}, tool_policy.MCP_UNKNOWN_POLICY


class NativePolicy:
    def __init__(self, app):
        self.app = app
        self.pending = {}

    async def handle(self, event):
        name = str(event.get("tool_name", ""))
        # These calls still execute through _execute_tool, including its hooks.
        if name.startswith("litetui_"):
            return {}
        kind = event.get("hook_event_name")
        key = (event.get("session_id"), event.get("turn_id"), event.get("tool_use_id"))
        canonical, aliases, policy = identity(name)
        args = event.get("tool_input") or {}
        if not isinstance(args, dict):
            args = {"input": args}
        if kind == "PostToolUse":
            # The native hook payload does not include an exit/success status.
            # Wait for the authoritative item completion before reporting one.
            return {}
        if kind != "PreToolUse":
            return {}
        if getattr(self.app, "_stop_requested", False):
            return denial("Turn stopped by user")
        if not self.app.tools_enabled:
            return denial("Tools are disabled in LiteTUI")
        if aliases.intersection(self.app.settings.tools_disabled or ()):
            return denial(f"Tool {canonical} is disabled in LiteTUI")
        profile = getattr(self.app, "_active_tool_profile", None)
        registered = self.app.plugins.policy_for(name)
        refusal = await self.app._authorize_action(
            canonical, args, registered or policy, workspace=workspace(self.app)
        )
        if refusal:
            return denial(str(refusal[0]))
        if getattr(self.app, "hook_config", None) is not None and not getattr(
            self.app, "_hooks_suppressed", False
        ):
            captured = hook_host.context(self.app)
            result = await hook_host.dispatch(
                self.app,
                "tool_before",
                {
                    "tool": canonical,
                    "args": args,
                },
                captured=captured,
                profile=profile,
            )
            if not result.allowed:
                return denial(result.reason)
            self.pending[key] = (captured, canonical, dict(args), profile)
        return {}

    async def completed(self, payload):
        item = payload.get("item", {})
        key = (payload.get("threadId"), payload.get("turnId"), item.get("id"))
        pending = self.pending.pop(key, None)
        if pending is None:
            return
        captured, name, args, profile = pending
        output = item.get(
            "aggregatedOutput", item.get("result", item.get("status", ""))
        )
        output = output if isinstance(output, str) else json.dumps(output)
        output = sanitize.redact_secrets(sanitize.strip_escapes(output))
        ok = (
            item.get("status") not in ("failed", "declined", "cancelled", "interrupted")
            and item.get("exitCode") in (None, 0)
            and item.get("success") is not False
        )
        await hook_host.dispatch(
            self.app,
            "tool_after",
            {
                "tool": name,
                "args": args,
                "result": output,
                "ok": ok,
                "cancelled": item.get("status") in ("cancelled", "interrupted"),
            },
            captured=captured,
            profile=profile,
        )

    async def finish(self):
        pending, self.pending = self.pending, {}
        # Unknown results are never reported as successful. A later native
        # completion cannot fire a second after hook after this drain.
        for captured, name, args, profile in pending.values():
            await hook_host.dispatch(
                self.app,
                "tool_after",
                {
                    "tool": name,
                    "args": args,
                    "result": "",
                    "ok": False,
                    "cancelled": True,
                },
                captured=captured,
                profile=profile,
            )
