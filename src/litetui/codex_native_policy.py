"""Apply shared host policy at Codex's synchronous native tool hook boundary."""

import json
from dataclasses import dataclass

from litetui import hook_host, sanitize, tool_policy
from litetui import native_runtime_controls as controls
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


@dataclass
class _Pending:
    context: dict | None
    name: str
    args: dict
    profile: str | None
    outcome: object
    permission: object


class NativePolicy:
    def __init__(self, app):
        self.app = app
        self.pending = {}
        self.deciding = set()
        self.turn = None

    def bind_turn(self, thread_id, turn_id, origin):
        if self.turn is not None:
            raise ValueError("Codex policy already belongs to a turn")
        self.turn = (thread_id, turn_id, origin)

    def take_permission(self, params, method):
        key = (params.get("threadId"), params.get("turnId"), params.get("itemId"))
        if not isinstance(key[2], str) or not key[2]:
            return None
        record = self.pending.get(key)
        if record is None:
            return None
        if method == "item/tool/requestUserInput" and record.name != "request_user_input":
            return None
        if method == "item/commandExecution/requestApproval" and record.name != "bash":
            return None
        if method == "item/fileChange/requestApproval" and record.name != "apply_patch":
            return None
        continuation, record.permission = record.permission, None
        return continuation

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
        if canonical == 'bash':
            args = dict(args)
            if 'command' not in args and isinstance(args.get('cmd'), str):
                args['command'] = args['cmd']
            if name == 'write_stdin' and 'command' not in args:
                args['command'] = str(args.get('chars', ''))
        if kind == "PostToolUse":
            # The native hook payload does not include an exit/success status.
            # Wait for the authoritative item completion before reporting one.
            return {}
        if kind != "PreToolUse":
            return {}
        turn = self.turn
        guarded = controls.enabled(self.app)
        if guarded:
            if (turn is None or turn[2] is None or key[:2] != turn[:2]
                    or not isinstance(key[2], str) or not key[2]
                    or key in self.deciding or key in self.pending):
                return denial("Stale or unowned Codex tool request")
            self.deciding.add(key)
        try:
            async with controls.authorization(self.app, origin=turn[2] if turn else None) as ticket:
                if guarded and (ticket is None or self.turn is not turn):
                    return denial("Codex turn expired before authorization")
                return await self._authorize(name, canonical, aliases, policy, args, key, turn, ticket)
        except controls.AuthorizationClosed as exc:
            return denial(str(exc))
        finally:
            if guarded:
                self.deciding.discard(key)

    async def _authorize(self, name, canonical, aliases, policy, args, key, turn, ticket):
        if getattr(self.app, "_stop_requested", False):
            return denial("Turn stopped by user")
        enabled = ticket.tools_enabled if ticket else self.app.tools_enabled
        if not enabled:
            return denial("Tools are disabled in LiteTUI")
        if aliases.intersection(ticket.disabled if ticket else self.app.settings.tools_disabled or ()):
            return denial(f"Tool {canonical} is disabled in LiteTUI")
        profile = ticket.profile if ticket else getattr(self.app, "_active_tool_profile", None)
        registered = self.app.plugins.policy_for(name)
        options = {"workspace": workspace(self.app)}
        if ticket:
            options["profile"] = ticket.profile
        refusal = await self.app._authorize_action(canonical, args, registered or policy, **options)
        if refusal:
            return denial(str(refusal[0]))
        captured = None
        if getattr(self.app, "hook_config", None) is not None and not getattr(
            self.app, "_hooks_suppressed", False
        ):
            captured = ticket.context if ticket else hook_host.context(self.app)
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
        if getattr(self.app, "_stop_requested", False) or ticket and self.turn is not turn:
            return denial("Codex turn expired before authorization completed")
        if captured is not None or ticket:
            self.pending[key] = _Pending(captured, canonical, dict(args), profile,
                                         controls.retain_authorization(self.app),
                                         controls.retain_authorization(self.app))
        return {}

    async def completed(self, payload):
        item = payload.get("item", {})
        key = (payload.get("threadId"), payload.get("turnId"), item.get("id"))
        pending = self.pending.pop(key, None)
        if pending is None:
            return
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
        await self._settle(pending, output=output, ok=ok,
                           cancelled=item.get("status") in ("cancelled", "interrupted"))

    async def _settle(self, record, *, output, ok, cancelled):
        if record.permission:
            record.permission.close()
        if record.context is None:
            if record.outcome:
                record.outcome.close()
            return
        data = {"tool": record.name, "args": record.args, "result": output,
                "ok": ok, "cancelled": cancelled}
        kwargs = {"captured": record.context, "profile": record.profile}
        if record.outcome:
            await record.outcome.run(hook_host.dispatch, self.app, "tool_after", data, **kwargs)
        else:
            await hook_host.dispatch(self.app, "tool_after", data, **kwargs)

    async def finish(self):
        self.turn = None
        pending, self.pending = self.pending, {}
        try:
            while pending:
                _, record = pending.popitem()
                await self._settle(record, output="", ok=False, cancelled=True)
        finally:
            for record in pending.values():
                if record.permission:
                    record.permission.close()
                if record.outcome:
                    record.outcome.close()
