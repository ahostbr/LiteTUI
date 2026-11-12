"""Claude authority boundary: mandatory native hooks and guarded host MCP.

Native activity never executes through the host tool loop. Unknown native tools,
background launches and unsafe host control-plane tools are denied explicitly.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from litetui import native_runtime_controls as controls
from litetui import paths, tool_policy

NATIVE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "AskUserQuestion", "TodoWrite"]
HOST_TOOLS = frozenset({"chrome", "pccontrol", "studio", "listen", "harness", "convo_search", "skill", "view_image", "subagent", "small_task"})


def spec_digest(spec):
    """What a host tool IS to the model: its name, description and argument schema."""
    body = {key: spec.get(key) for key in ("name", "description", "parameters")} if isinstance(spec, dict) else None
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False, default=str).encode("utf-8")).hexdigest()


#: What an unreviewed Claude compaction under the native Work frontend tells Claude when it asks for
#: something the frontend cannot host. The Rust frontend only accepts an approval or a question from
#: an active chat turn or a reviewed action's execution; this summary turn is neither, so raising one
#: would be an out-of-order event for it. The conversation's own store files (STEP 1) need no approval.
UNATTENDED_APPROVAL = ("This unattended compaction was not started from a reviewed action, so the native frontend "
                       "cannot show an approval for it. Only the conversation's own store files can be written here.")
UNATTENDED_QUESTION = "Questions cannot be asked during an unattended compaction; write the summary with what you have."
UNATTENDED_HOST = ("[denied] Host tools are unavailable during an unattended compaction, which has no reviewed "
                   "action to attribute them to; use the native tools.")


def _host_result(text, *, error=True, images=()):
    # The SDK consumes is_error, while existing host callers consume isError.
    # Every refusal and dispatched result must preserve the same verdict for both.
    content = [{"type": "text", "text": text}]
    content.extend({"type": "image", "data": image, "mimeType": "image/png"} for _, image in images)
    return {"content": content, "isError": error, "is_error": error}


@dataclass(eq=False)
class HostRequest:
    """One bridged host call, parked until the reviewed worker (the only task admitted) runs it."""
    name: str
    args: Any
    spec: dict
    future: asyncio.Future


class HostLane:
    """The hand-off from the SDK's tool task to the reviewed worker that owns one bound turn.

    The SDK invokes a bridged host tool on a task of its own. During a reviewed action only the
    captured worker may enter the app's tool door, so the call is queued through the session's event
    stream and the worker runs it in order. `disclosed` is the name -> spec digest the review bound;
    nothing outside it runs, and every request is settled when the turn ends."""

    def __init__(self, turn, session, disclosed):
        self.turn, self.session, self.disclosed = turn, session, dict(disclosed)
        self.pending = set()
        self.closed = False

    def submit(self, request):
        if self.closed or not self.session.deliver(request):
            return False
        self.pending.add(request)
        return True

    def close(self):
        self.closed = True
        for request in list(self.pending):
            if not request.future.done():
                request.future.set_result(_host_result("[denied] The reviewed compaction turn ended before this "
                                                       "host tool ran; nothing was executed."))
        self.pending.clear()


def _deadlines(app=None):
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
    from litetui.tool_approval import approval_timeout_s

    # T1049-B (clock C2): the SAME deadline the rpc approval uses, so the two cannot
    # drift when a supervised child's is lifted. Unbounded (None) still needs a
    # number for the SDK hook.
    # ponytail: a day stands in for "no deadline"; a real None if the SDK takes one
    base = approval_timeout_s() or 86400.0
    if app is not None:
        # Dijkstra K1(b): a seat that relays to its SPAWNER waits relay_approval_timeout_s
        # (600 s by default), so this deadline must outlast it or it cuts the relay.
        from litetui import approval_relay, seat_authority
        if seat_authority.confirm_route(app) == "spawner":
            base = max(base, approval_relay.timeout_s(app) + 60.0)
    decision = base + 15.0
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
        self._turn = None
        self._observed = {}
        self._deciding = set()
        self._pending = {}
        #: name -> spec digest of the host tools this session's SDK server bridges (fixed at open).
        self.host_tools = {}
        self._lane = None
        self._unattended = False

    def bind_turn(self, origin, *, unattended=False):
        """Own one turn's callbacks. `unattended`: an unreviewed compaction under the Work frontend, whose
        approvals and questions the frontend cannot host (see UNATTENDED_APPROVAL), so they refuse in words."""
        if self._turn is not None:
            raise ValueError("Claude tool bridge already belongs to a turn")
        self._turn = (self.backend.session, origin)
        self._unattended = unattended
        return self._turn

    def bind_host_lane(self, turn, disclosed):
        """Let the worker consuming `turn`'s events run the host tools the review disclosed."""
        if self._turn is not turn or self._lane is not None:
            raise ValueError("Claude host lane needs the bound turn, once")
        self._lane = HostLane(turn, self.backend.session, disclosed)
        return self._lane

    def observed(self, turn, tool_id, name, args):
        if (self._turn is turn and isinstance(tool_id, str) and tool_id and isinstance(args, dict)
                and len(self._observed) < controls.LIMITS["authorizations"]):
            self._observed[tool_id] = (name, copy.deepcopy(args))

    def completed(self, turn, tool_id):
        if self._turn is not turn:
            return
        self._observed.pop(tool_id, None)
        record = self._pending.pop(tool_id, None)
        if record:
            lifetime, question, _ = record
            lifetime.close()
            if question:
                question.close()

    def finish_turn(self, turn):
        if turn is not None and self._turn is turn:
            if self._lane is not None and self._lane.turn is turn:
                self._lane.close()
                self._lane = None
            for tool_id in list(self._pending):
                self.completed(turn, tool_id)
            self._observed.clear()
            self._turn = None
            self._unattended = False

    def _owns_turn(self, turn):
        return (turn is not None and self._turn is turn and self.backend.session is turn[0]
                and self.current())


    def current(self):
        return (not self.cancelled.is_set() and not self.app._stop_requested
                and self.app.backend is self.backend and self.app.convo_id == self.conversation_id
                and self.backend.segment_id == self.segment_id)

    def stop(self):
        self.cancelled.set()
        self.finish_turn(self._turn)
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

    def _action_origin(self):
        """The execution a bound turn belongs to, for a reviewed compaction's event attribution."""
        return self._turn[1] if self._turn is not None else None

    async def pre_tool(self, data, tool_id, context):
        from litetui import conversation_action
        # The SDK calls this on its own task; the scope attributes the approval it may raise.
        with conversation_action.claude_event_scope(self.app, self._action_origin()):
            return await self._pre_tool(data, tool_id, context)

    async def _pre_tool(self, data, tool_id, context):
        name, args = data.get("tool_name", ""), data.get("tool_input", {})
        guarded, turn = controls.enabled(self.app), self._turn
        origin = None
        if guarded:
            if (not self._owns_turn(turn) or not isinstance(tool_id, str) or not tool_id
                    or tool_id in self._deciding or tool_id in self._pending):
                return self._denied("Stale or unowned Claude callback")
            # SDK callbacks have no native turn id. Only a tool-use event from
            # this stream proves an exact execution owner for a mid-turn change.
            if self._observed.get(tool_id) == (name, args):
                origin = turn[1]
            self._deciding.add(tool_id)
        try:
            async with controls.authorization(self.app, origin=origin) as ticket:
                if guarded and (ticket is None or not self._owns_turn(turn)):
                    return self._denied("Claude callback expired before authorization")
                return await self._authorize(name, args, tool_id, ticket, turn)
        except controls.AuthorizationClosed as exc:
            return self._denied(str(exc))
        finally:
            if guarded:
                self._deciding.discard(tool_id)

    @staticmethod
    def _denied(reason):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}

    async def _authorize(self, name, args, tool_id, ticket, turn):
        plain = name.removeprefix("mcp__litetui__")
        reason = None
        if not self.current() or not (ticket.tools_enabled if ticket else self.app.tools_enabled):
            reason = "Claude tool call cancelled, stale, or tools are off"
        elif not isinstance(args, dict):
            reason = "Invalid tool arguments"
        elif self._unattended and name == "AskUserQuestion":
            reason = UNATTENDED_QUESTION
        elif plain in (ticket.disabled if ticket else self.app.settings.tools_disabled or ()):
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
                reason = await self._decide(name, policy_args, policy, ticket)
        if not self.current() or ticket and not self._owns_turn(turn):
            reason = reason or "Claude callback expired"
        if reason:
            return self._denied(reason)
        if ticket and not name.startswith("mcp__litetui__"):
            question_args = copy.deepcopy(args) if name == "AskUserQuestion" else None
            self._pending[tool_id] = (controls.retain_authorization(self.app),
                                      controls.retain_authorization(self.app) if question_args is not None else None,
                                      question_args)
        # Questions must reach can_use_tool to provide structured answers. Other
        # classified calls have already passed LiteTUI approval exactly once.
        decision = "ask" if name == "AskUserQuestion" else "allow"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}

    async def _decide(self, name, args, policy, ticket):
        """Authorize one native call under a deadline this side owns.

        The await runs in a task we hold for two reasons: `stop()` can cancel a
        decision blocked on a human, and `wait_for` can put a bound on one
        nobody is ever going to answer. Both outcomes are the same answer —
        a denial with a reason — because the one thing the hook must never do
        is fail to reply.
        """
        decision_s, _ = _deadlines(self.app)
        # An unattended compaction's approval would be an event the frontend cannot place: refuse it in words.
        refusal_only = {"allow_prompt": False, "prompt_refusal": UNATTENDED_APPROVAL} if self._unattended else {}
        decision = self.app._authorize_action(
            name, args, policy,
            profile=ticket.profile if ticket else self.app._active_tool_profile,
            workspace=self.workspace, stop_on_denial=False, **refusal_only,
        )
        task = asyncio.ensure_future(controls.child_call(self.app, decision))
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
        from litetui import conversation_action
        with conversation_action.claude_event_scope(self.app, self._action_origin()):
            return await self._can_use_tool(name, arguments, context)

    async def _can_use_tool(self, name, arguments, context):
        from claude_agent_sdk import PermissionResultDeny
        if controls.enabled(self.app):
            turn = self._turn
            if self._unattended:
                return PermissionResultDeny(message=UNATTENDED_QUESTION)
            if name != "AskUserQuestion" or not self._owns_turn(turn) or controls._state(self.app).closing:
                return PermissionResultDeny(message="Unapproved or expired native request")
            tool_id = getattr(context, "tool_use_id", None)
            record = self._pending.get(tool_id) if isinstance(tool_id, str) else None
            if record is None or record[1] is None or record[2] != arguments:
                return PermissionResultDeny(message="Question does not match its authorized tool call")
            lifetime, question, expected = record
            self._pending[tool_id] = (lifetime, None, expected)
            try:
                answer = await question.run(self._answer_question, name, arguments)
            except controls.AuthorizationClosed:
                return PermissionResultDeny(message="Question authorization expired")
            if not self._owns_turn(turn):
                return PermissionResultDeny(message="Question callback expired")
            return answer
        return await self._answer_question(name, arguments)

    async def _answer_question(self, name, arguments):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        ticket = controls.current_authorization(self.app)
        if name != "AskUserQuestion" or not self.current() or not (ticket.tools_enabled if ticket else self.app.tools_enabled):
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
        _, hook_s = _deadlines(self.app)
        # What this session bridges is fixed here, and it is what a compaction review discloses.
        self.host_tools = {name: spec_digest(inventory[name]) for name in sorted(HOST_TOOLS & inventory.keys())
                           } if tools and self.app.tools_enabled else {}
        from litetui import harness
        # Native Bash runs inside claude.exe, not through harness.tool_process_env: give that process the seat's id.
        seat_id = getattr(getattr(self.app, "seat", None), "agent_id", None)
        return {
            "env": harness.claude_process_env(seat_id),
            "tools": [n for n in NATIVE_TOOLS if n not in disabled] if self.app.tools_enabled else [],
            "permission_mode": "default",
            "can_use_tool": self.can_use_tool,
            # An explicit timeout, because the default is 60s and the decision
            # behind this hook is allowed to take five minutes — see _deadlines.
            "hooks": {"PreToolUse": [HookMatcher(hooks=[self.pre_tool], timeout=hook_s)]},
            "mcp_servers": {"litetui": create_sdk_mcp_server(name="litetui", tools=tools)} if tools and self.app.tools_enabled else {},
        }

    async def host_call(self, name, args, spec):
        from litetui import conversation_action
        if conversation_action.compact_pending(self.app):
            # A host tool runs through the app's own tool door, which only a captured worker may
            # enter during a reviewed action; the SDK's task is not that worker.
            return await self._reviewed_host_call(name, spec, args)
        if self._unattended:
            return _host_result(UNATTENDED_HOST)
        turn = self._turn
        guarded = controls.enabled(self.app)
        try:
            # MCP supplies no tool-use id. The host call is independent of
            # any earlier native preflight, and must obtain its own ticket.
            async with controls.authorization(self.app) as ticket, self._host_lock:
                if guarded and (ticket is None or ticket.state.closing or not self._owns_turn(turn)):
                    raise controls.AuthorizationClosed("Claude host callback expired")
                return await self._host_call(name, args, spec, ticket)
        except controls.AuthorizationClosed as exc:
            return _host_result(str(exc))

    async def _reviewed_host_call(self, name, spec, args):
        """The SDK's side of a host call during a reviewed compaction: authorize nothing, run nothing.

        It checks the call against what the review disclosed, parks it on the turn's lane and waits;
        the reviewed worker runs it (`serve_host_call`) under its own execution. A call outside the
        summary turn, outside the disclosed inventory or after the turn ended executes nothing."""
        lane, turn = self._lane, self._turn
        if lane is None or lane.turn is not turn or not self._owns_turn(turn):
            return _host_result("[denied] Host tools run only inside the reviewed compaction's summary turn; "
                                "use the native tools.")
        if lane.disclosed.get(name) != spec_digest(spec):
            return _host_result(f"[denied] Host tool {name} was not part of the compaction review; "
                                "use the native tools.")
        async with self._host_lock:
            request = HostRequest(name, args, spec, asyncio.get_running_loop().create_future())
            if not lane.submit(request):
                return _host_result("[denied] The reviewed compaction turn is not accepting host tools.")
            return await request.future

    async def serve_host_call(self, lane, request):
        """Run one parked host call on the CALLING task, which must be the reviewed worker.

        The worker's own execution admits the tool, so the existing door (`_execute_tool`) attributes
        its approvals to the root compaction execution and registers the tool future for the action's
        join and Stop. Whatever happens, the SDK waiting on `request.future` is answered."""
        lane.pending.discard(request)
        future = request.future
        if future.done():
            return  # the SDK gave up before the worker reached it
        try:
            result = await self._serve(lane, request)
        except asyncio.CancelledError:
            if not future.done():
                future.set_result(_host_result("[denied] Host tool cancelled with the compaction; "
                                               "it may not have completed."))
            raise
        except Exception as exc:  # noqa: BLE001 - a failed host tool is a tool result, not a failed compaction
            result = _host_result(f"[error] Host tool failed: {type(exc).__name__}")
        if not future.done():
            future.set_result(result)

    async def _serve(self, lane, request):
        turn, name = lane.turn, request.name
        # Disclosure again at the door: the schema the model was shown is the one the review bound.
        live = {s["function"]["name"]: s["function"] for s in self.app._all_tools() if s.get("type") == "function"}
        if (lane.disclosed.get(name) != spec_digest(request.spec) or name not in live
                or spec_digest(live[name]) != lane.disclosed[name]):
            return _host_result(f"[denied] Host tool {name} changed since the compaction review; "
                                "use the native tools.")
        guarded = controls.enabled(self.app)
        try:
            async with controls.authorization(self.app) as ticket:
                if (guarded and (ticket is None or ticket.state.closing)) or not self._owns_turn(turn):
                    raise controls.AuthorizationClosed("Claude host callback expired")
                return await self._host_call(name, request.args, request.spec, ticket)
        except controls.AuthorizationClosed as exc:
            return _host_result(str(exc))

    async def _host_call(self, name, args, spec, ticket):
        # Optional transitive dependency of the claude extra; it ships no stubs
        # and pulling a types package in for one validate() call is not worth it.
        import jsonschema  # type: ignore[import-untyped]
        error = None
        if not self.current() or not (ticket.tools_enabled if ticket else self.app.tools_enabled):
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
            return _host_result(error)
        names = {s["function"]["name"] for s in self.app._all_tools() if s.get("type") == "function"}
        if not self.current() or name not in names:
            return _host_result("Host inventory changed or callback expired")
        # Serialize host calls and scope the shared image staging buffer to
        # this result; never borrow images from an unrelated tool call.
        previous = self.app._pending_tool_images
        self.app._pending_tool_images = []
        try:
            text, ok = await self.app._execute_tool(name, dict(args))
            return _host_result(text, error=not ok, images=self.app._pending_tool_images)
        finally:
            self.app._pending_tool_images = previous
