"""Claude authority boundary: mandatory native hooks and guarded host MCP.

Native activity never executes through the host tool loop. Unknown native tools,
background launches and unsafe host control-plane tools are denied explicitly.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from litetui import paths, tool_policy

NATIVE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "AskUserQuestion", "TodoWrite"]
HOST_TOOLS = frozenset({"chrome", "pccontrol", "studio", "listen", "harness", "convo_search", "skill", "view_image"})


def _deadlines():
    """(local decision deadline, hook timeout) — the second always outlives the first.

    🔴 THE MANDATORY GATE HAD A DEADLINE IT DID NOT SET AND COULD NOT SEE. An
    SDK HookMatcher defaults to 60 seconds, while the approval `pre_tool` waits
    on is a Textual modal with no timeout at all and an RPC path that waits
    `APPROVAL_TIMEOUT_S` (300s). A human taking seventy seconds over a Bash
    confirm therefore outlived the gate, and what the CLI does with an
    abandoned PreToolUse hook is not observable from this side — so the
    difference between "denied" and "fell through to the permission rules"
    was decided by something nobody here had measured.

    Both numbers are derived from the approval timeout and from each other so
    they cannot drift apart: the hook is given room to outlast our own
    deadline, and our deadline always answers first. An expiry is a DENIAL
    with a stated reason, never a fall-through.
    """
    from litetui.tool_approval import APPROVAL_TIMEOUT_S

    decision = APPROVAL_TIMEOUT_S + 15.0
    return decision, decision + 45.0


def native_policy(name, arguments):
    if name in {"Read", "Glob", "Grep", "TodoWrite", "AskUserQuestion"}:
        return tool_policy.READ_POLICY, arguments
    if name in {"WebFetch", "WebSearch"}:
        return tool_policy.NETWORK_READ_POLICY, arguments
    if name in {"Write", "Edit"}:
        return tool_policy.WRITE_POLICY, {**arguments, "path": arguments.get("file_path")}
    if name == "Bash":
        return tool_policy.SHELL_POLICY, arguments
    return None, arguments


class ClaudeTools:
    def __init__(self, app, backend, segment_id, *, workspace=None):
        self.app = app
        self.backend = backend
        self.segment_id = segment_id
        self.conversation_id = app.convo_id
        # The root a native write is judged against. `Path.cwd()` was wrong in
        # a way that does not look wrong: tool_policy.classify_write splits
        # WORKSPACE_WRITE from EXTERNAL_WRITE by containment, so the process's
        # launch directory silently decided which authority a write needed,
        # and every host tool was judged against paths.ROOT instead. The
        # segment's workspace is the honest answer; ROOT is the floor.
        self.workspace = Path(workspace) if workspace else paths.ROOT
        self.cancelled = threading.Event()
        self._host_lock = asyncio.Lock()
        self._decisions = set()

    def current(self):
        return (not self.cancelled.is_set() and not self.app._stop_requested
                and self.app.backend is self.backend and self.app.convo_id == self.conversation_id
                and self.backend.segment_id == self.segment_id)

    def stop(self):
        self.cancelled.set()
        from litetui.ask_user_question import cancel_pending_asks
        from litetui.tool_approval import DENIED
        cancel_pending_asks(self.app)
        for future in getattr(self.app, "_approval_waiters", {}).values():
            if not future.done():
                future.set_result(DENIED)
        # `_approval_waiters` is the RPC registry (tool_approval.py) — a Textual
        # approval modal is in no registry at all, so the two lines above could
        # not reach a decision blocked on one. The hook then never answered,
        # the SDK could not drain, and the interrupt reported "delivery is
        # uncertain" because of a dialog nobody had closed. Cancelling the
        # decision we own settles it from our side.
        for task in list(self._decisions):
            task.cancel()

    async def pre_tool(self, data, tool_id, context):
        name, args = data.get("tool_name", ""), data.get("tool_input", {})
        plain = name.removeprefix("mcp__litetui__")
        reason = None
        if not self.current() or not self.app.tools_enabled:
            reason = "Claude tool call cancelled, stale, or tools are off"
        elif not isinstance(args, dict):
            reason = "Invalid tool arguments"
        elif plain in (self.app.settings.tools_disabled or ()):
            # The user switched this tool off. `_execute_tool` enforces the same
            # list, but only host calls reach it — a native tool never does, and
            # withholding the name from `sdk_options` does not cover a model
            # imitating its own transcript. Both mechanisms, for the reason
            # app.py gives for having two.
            reason = f"{plain} is switched off"
        elif name.startswith("mcp__litetui__"):
            if plain not in HOST_TOOLS:
                reason = "Host tool is not in the approved bridge inventory"
            # Host handler validates and authorizes immediately before execution.
        else:
            policy, policy_args = native_policy(name, args)
            if policy is None:
                reason = f"Unmapped native tool {name} is disabled (agents/background work are not enabled)"
            elif args.get("run_in_background"):
                reason = "Native background work is not enabled"
            else:
                reason = await self._decide(name, policy_args, policy)
        if not self.current():
            reason = reason or "Claude callback expired"
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        # Questions must reach can_use_tool to provide structured answers. Other
        # classified calls have already passed LiteTUI approval exactly once.
        decision = "ask" if name == "AskUserQuestion" else "allow"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}

    async def _decide(self, name, args, policy):
        """Authorize one native call under a deadline this side owns.

        The await runs in a task we hold for two reasons: `stop()` can cancel a
        decision blocked on a human, and `wait_for` can put a bound on one
        nobody is ever going to answer. Both outcomes are the same answer —
        a denial with a reason — because the one thing the hook must never do
        is fail to reply.
        """
        decision_s, _ = _deadlines()
        task = asyncio.ensure_future(self.app._authorize_action(
            name, args, policy,
            profile=self.app._active_tool_profile,
            workspace=self.workspace, stop_on_denial=False,
        ))
        self._decisions.add(task)
        try:
            refusal = await asyncio.wait_for(task, decision_s)
        except TimeoutError:
            return f"No decision for {name} within {decision_s:.0f}s; denied"
        except asyncio.CancelledError:
            # 🔴 WHICH TASK WAS CANCELLED DECIDES THE ANSWER, AND A BARE
            # `return` HERE IS A BUG. stop() cancels the inner decision, and
            # answering is right: the hook must reply or the SDK cannot drain.
            # But this same clause catches the teardown of the hook callback
            # ITSELF, and suppressing that makes this coroutine un-cancellable
            # — it reports a tidy denial to a caller that has already gone.
            # `cancelling()` is the only thing that tells the two apart.
            # Measured: without this check, a caller's own wait_for timeout was
            # converted into a passing denial (Python 3.11 wait_for returns
            # fut.result() when the task swallows its cancellation).
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # An approval modal may still be on screen — closing it needs the
            # host approval API, which is deliberately untouched here.
            return "Claude tool call cancelled while awaiting approval"
        finally:
            self._decisions.discard(task)
        return refusal[0] if refusal else None

    async def can_use_tool(self, name, arguments, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if name != "AskUserQuestion" or not self.current() or not self.app.tools_enabled:
            return PermissionResultDeny(message="Unapproved or expired native request")
        from litetui import ask_user_question
        from litetui.question_result import (
            capture_answers,
            native_answers,
            question_lifetime,
            question_slot,
        )
        questions = arguments.get("questions") if isinstance(arguments, dict) else None
        try:
            adapted = {"questions": [{
                "label": q.get("header", "Question"), "question": q["question"],
                "multiSelect": q.get("multiSelect", False),
                "options": [{"title": o["label"], "description": o.get("description", "")} for o in q["options"]],
            } for q in questions]}
        except (AttributeError, KeyError, TypeError):
            # A question nobody can render is a question nobody can answer.
            # Raising here is still fail-closed — the SDK turns it into a
            # control error — but it puts raw exception text on the wire and
            # gives the user no reason they can act on. State it instead, and
            # state nothing the payload supplied.
            return PermissionResultDeny(message="Question payload is missing required question/option fields")
        if not adapted["questions"]:
            return PermissionResultDeny(message="Question payload contained no questions")
        try:
            with question_lifetime(self.cancelled), capture_answers() as captured:
                async with question_slot(self.app):
                    await asyncio.to_thread(ask_user_question.run, adapted, self.app)
        except Exception:  # noqa: BLE001 - a failed prompt must deny, never escape
            return PermissionResultDeny(message="Question could not be presented")
        if not captured or not self.current():
            return PermissionResultDeny(message="Question cancelled or disconnected")
        answers = native_answers([q["question"] for q in questions], captured[-1])
        if len(answers) != len(questions):
            return PermissionResultDeny(message="Question not explicitly submitted in full")
        return PermissionResultAllow(updated_input={**arguments, "answers": {key: ", ".join(value["answers"]) for key, value in answers.items()}})

    def sdk_options(self):
        from claude_agent_sdk import HookMatcher, create_sdk_mcp_server, tool
        inventory = {s["function"]["name"]: s["function"] for s in self.app._all_tools() if s.get("type") == "function"}
        tools = []
        for name in sorted(HOST_TOOLS & inventory.keys()):
            spec = inventory[name]

            async def invoke(args, name=name, spec=spec):
                return await self.host_call(name, args, spec)

            tools.append(tool(name, spec.get("description", name), spec.get("parameters", {"type": "object"}))(invoke))
        disabled = frozenset(self.app.settings.tools_disabled or ())
        _, hook_s = _deadlines()
        return {
            "tools": [n for n in NATIVE_TOOLS if n not in disabled] if self.app.tools_enabled else [],
            "permission_mode": "default",
            "can_use_tool": self.can_use_tool,
            # An explicit timeout, because the default is 60s and the decision
            # behind this hook is allowed to take five minutes — see _deadlines.
            "hooks": {"PreToolUse": [HookMatcher(hooks=[self.pre_tool], timeout=hook_s)]},
            "mcp_servers": {"litetui": create_sdk_mcp_server(name="litetui", tools=tools)} if tools and self.app.tools_enabled else {},
        }

    async def host_call(self, name, args, spec):
        # Optional transitive dependency of the claude extra; it ships no stubs
        # and pulling a types package in for one validate() call is not worth it.
        import jsonschema  # type: ignore[import-untyped]
        error = None
        if not self.current() or not self.app.tools_enabled:
            error = "Host callback cancelled, stale, or tools off"
        elif name not in HOST_TOOLS:
            error = "Host tool not bridged"
        elif not isinstance(args, dict):
            error = "Host arguments must be an object"
        else:
            try:
                jsonschema.validate(args, spec.get("parameters", {"type": "object"}))
            except jsonschema.ValidationError:
                error = "Invalid host arguments"
        if error:
            return {"content": [{"type": "text", "text": error}], "isError": True}
        async with self._host_lock:
            names = {s["function"]["name"] for s in self.app._all_tools() if s.get("type") == "function"}
            if not self.current() or name not in names:
                return {"content": [{"type": "text", "text": "Host inventory changed or callback expired"}], "isError": True}
            # Serialize host calls and scope the shared image staging buffer to
            # this result; never borrow images from an unrelated tool call.
            previous = self.app._pending_tool_images
            self.app._pending_tool_images = []
            try:
                text, ok = await self.app._execute_tool(name, dict(args))
                content = [{"type": "text", "text": text}]
                for _, image in self.app._pending_tool_images:
                    content.append({"type": "image", "data": image, "mimeType": "image/png"})
                return {"content": content, "isError": not ok}
            finally:
                self.app._pending_tool_images = previous
