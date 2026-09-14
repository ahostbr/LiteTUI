"""Codex's official agent loop over its versioned, bidirectional stdio protocol.

Codex owns credentials, inference requests, cache routing and native delegation.
The host forwards user turns once and persists the Codex thread reference beside
the displayed assistant message. No Responses request is reconstructed here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from litetui import sanitize
from litetui.model_transport import ProviderError, _chunk, _usage, collect


class AppServer:
    def __init__(self, *, config_overrides=()):
        self.config_overrides = tuple(config_overrides)
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = asyncio.Queue()
        self.serial = 0
        self.start_lock = asyncio.Lock()
        self.closer = None

    async def start(self):
        async with self.start_lock:
            if self.closer:
                await self.closer
                self.closer = None
            if self.process and self.process.returncode is None:
                return
            if self.reader:
                await self.reader
            # Follow PATH in exactly the same order as the CLI command. Looking
            # for codex.exe first skips npm's earlier shim and can select an old
            # desktop-bundled executable with a different protocol.
            binary = shutil.which("codex") or shutil.which("codex.exe")
            if not binary:
                raise ProviderError(
                    "Codex CLI is required. Install Codex and run `codex login`."
                )
            args = [binary, "app-server"]
            # Windows npm shims are batch files, not executables. Resolve their
            # adjacent JS entrypoint and invoke Node without a command shell.
            if Path(binary).suffix.lower() in (".cmd", ".bat", ".ps1"):
                entry = Path(binary).parent / "node_modules/@openai/codex/bin/codex.js"
                node = shutil.which("node")
                if not node or not entry.is_file():
                    raise ProviderError(
                        "Cannot resolve the installed Codex CLI entrypoint."
                    )
                vendors = list(
                    (entry.parent.parent / "node_modules/@openai").glob(
                        "codex-win32-*/vendor/*/bin/codex.exe"
                    )
                )
                args = (
                    [str(vendors[0]), "app-server"]
                    if len(vendors) == 1
                    else [node, str(entry), "app-server"]
                )
            for override in self.config_overrides:
                args.extend(["-c", override])
            self.events = asyncio.Queue()
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                ),
            )
            self.reader = asyncio.create_task(self._read())
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "litetui",
                        "title": "LiteTUI",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized", "params": {}})

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise ProviderError(
                "Codex app-server stopped. Reconnect before continuing."
            )
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, 120)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(
                                ProviderError(
                                    "Codex app-server rejected the operation. "
                                    + str(message["error"].get("message", ""))[:500]
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    await self.events.put(message)
        except (OSError, ValueError):
            pass
        finally:
            error = ProviderError(
                "Codex app-server connection ended. Reconnect before continuing."
            )
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.events.put(error)

    def shutdown(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            if not self.closer:
                self.closer = asyncio.create_task(self._close_process(self.process))

    async def _close_process(self, process):
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            # This PID was spawned and retained by this instance. Close its own
            # tree only, including CLI-owned MCP children on Windows.
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.kill()
            await process.wait()

    async def close(self):
        self.shutdown()
        if self.closer:
            await self.closer
        if self.reader:
            await self.reader


def user_input(messages):
    """Preserve multimodal user input; history import is explicit, once only."""
    result = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system":
            continue
        if isinstance(content, str) and content:
            text = (
                content
                if message["role"] == "user"
                else f"[{message['role']}]\n{content}"
            )
            result.append({"type": "text", "text": text, "text_elements": []})
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    result.append(
                        {"type": "text", "text": block["text"], "text_elements": []}
                    )
                elif block.get("type") == "image_url":
                    result.append({"type": "image", "url": block["image_url"]["url"]})
        if message.get("tool_calls"):
            result.append(
                {
                    "type": "text",
                    "text": "[prior tool calls]\n" + json.dumps(message["tool_calls"]),
                    "text_elements": [],
                }
            )
    return result


class AppServerTransport:
    def __init__(self, server, app=None):
        self.server = server
        self.app = app
        self.thread_id = None
        self.turn_id = None
        self.lock = asyncio.Lock()
        self.process = None

    async def create(self, **kwargs):
        if (
            self.app is not None
            and not kwargs.get("stream")
            and not kwargs.get("tools")
        ):
            # Title/evaluator side calls must not inherit the conversation's
            # active turn, host tools, or permission state.
            server = AppServer()
            try:
                return await AppServerTransport(server).create(**kwargs)
            finally:
                await server.close()
        stream = AppServerStream(self._stream(kwargs))
        return stream if kwargs.get("stream", False) else await collect(stream)

    async def compact(self):
        async with self.lock:
            reference = next(
                (
                    message.get("provider_metadata", {}).get("app_server_thread_id")
                    for message in reversed(self.app.conversation)
                    if message.get("provider_metadata", {}).get("app_server_thread_id")
                ),
                None,
            )
            if not reference:
                raise ProviderError("Start a Codex conversation before compacting it.")
            await self.server.start()
            if self.thread_id != reference or self.process is not self.server.process:
                await self.server.request("thread/resume", {"threadId": reference})
                self.thread_id, self.process = reference, self.server.process
            await self.server.request("thread/compact/start", {"threadId": reference})
            finished = False
            try:
                while True:
                    event = await self.server.events.get()
                    if isinstance(event, Exception):
                        raise event
                    if "id" in event and "method" in event:
                        await self._server_request(event)
                        continue
                    payload = event.get("params", {})
                    if payload.get("threadId") != reference:
                        continue
                    if event.get("method") == "turn/started":
                        self.turn_id = payload["turn"]["id"]
                    if event.get("method") == "turn/completed":
                        finished = True
                        if payload.get("turn", {}).get("status") != "completed":
                            raise ProviderError(
                                "Codex compaction did not complete; the local transcript was preserved."
                            )
                        return
            finally:
                if not finished and self.turn_id:
                    await self.server.request(
                        "turn/interrupt",
                        {"threadId": reference, "turnId": self.turn_id},
                    )
                self.turn_id = None

    async def _server_request(self, message):
        method, params = message["method"], message.get("params", {})
        result = None
        if method == "item/tool/call":
            name = params.get("tool", "").removeprefix("litetui_")
            if self.app:
                if getattr(self.app, "_stop_requested", False):
                    output, success = "[cancelled] turn stopped", False
                else:
                    output, success = await self.app._execute_tool(
                        name, params.get("arguments", {})
                    )
                output = sanitize.redact_secrets(sanitize.strip_escapes(str(output)))
                if not getattr(self.app, "_rpc", False):
                    sanitize.reset_terminal_modes()
                self.app._rpc_emit(
                    {
                        "type": "tool_result",
                        "name": name,
                        "result": output,
                        "ok": success,
                    }
                )
            else:
                output, success = "Host tools are unavailable in this request.", False
            result = {
                "contentItems": [{"type": "inputText", "text": str(output)}],
                "success": success,
            }
            # The native loop does not reach LiteTUI's between-round image
            # drain. Return actual image content in this tool's response.
            if self.app and getattr(self.app, "_pending_tool_images", None):
                staged, self.app._pending_tool_images = (
                    self.app._pending_tool_images,
                    [],
                )
                result["contentItems"].extend(
                    {"type": "inputImage", "imageUrl": f"data:image/png;base64,{b64}"}
                    for _, b64 in staged
                )
        elif method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        ):
            approved = False
            if self.app and self.app.tools_enabled:
                from litetui.tool_policy import (
                    EXTERNAL_WRITE,
                    NETWORK,
                    PROCESS_EXECUTION,
                    WORKSPACE_WRITE,
                    ToolPolicy,
                )

                if "permissions" in method:
                    requested = params.get("permissions", {})
                    caps = (
                        {EXTERNAL_WRITE} if requested.get("fileSystem") else set()
                    ) | ({NETWORK} if requested.get("network") else set())
                else:
                    caps = {
                        PROCESS_EXECUTION
                        if "commandExecution" in method
                        else WORKSPACE_WRITE
                    }
                denial = await self.app._authorize_action(
                    "codex_approval",
                    params,
                    ToolPolicy(
                        frozenset(caps),
                        "Codex requests permission",
                        confirm_always=True,
                    ),
                )
                approved = denial is None
            result = (
                {
                    "permissions": params.get("permissions", {}) if approved else {},
                    "scope": "turn",
                }
                if "permissions" in method
                else {"decision": "accept" if approved else "cancel"}
            )
        elif method == "item/tool/requestUserInput":
            from litetui.question_result import capture_answers, native_answers

            result = {"answers": {}}
            if self.app:
                questions = params.get("questions", [])
                with capture_answers() as captured:
                    _, success = await self.app._execute_tool(
                        "ask_user_question",
                        {
                            "questions": [
                                {
                                    "label": question.get("header", "Question"),
                                    "question": question["question"],
                                    "multiSelect": False,
                                    "allowFreeText": True,
                                    "isSecret": bool(question.get("isSecret")),
                                    "options": [
                                        {
                                            "title": option["label"],
                                            "description": option.get(
                                                "description", ""
                                            ),
                                        }
                                        for option in question.get("options") or []
                                    ],
                                }
                                for question in questions
                            ]
                        },
                    )
                if success and captured:
                    result["answers"] = native_answers(
                        [question["id"] for question in questions], captured[-1]
                    )
                if not result["answers"]:
                    # Empty/partial UI outcomes must not become an inferred answer.
                    self.app._stop_requested = True
        if result is None:
            await self.server.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "This host does not support this request.",
                    },
                }
            )
        else:
            await self.server.send({"id": message["id"], "result": result})
        if self.app and getattr(self.app, "_stop_requested", False) and self.turn_id:
            await self.server.request(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}
            )

    async def _stream(self, kwargs):
        async with self.lock:
            await self.server.start()
            if self.process is not self.server.process:
                self.thread_id = None
                self.process = self.server.process
            account = await self.server.request("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ProviderError(
                    "Codex requires a ChatGPT subscription login. Run `codex login`."
                )
            messages = kwargs["messages"]
            instructions = "\n\n".join(
                str(m.get("content", "")) for m in messages if m["role"] == "system"
            )
            instructions_digest = hashlib.sha256(instructions.encode()).hexdigest()
            previous_digest = None
            reference, boundary = None, 0
            for index, message in enumerate(messages):
                metadata = message.get("provider_metadata") or {}
                if metadata.get("provider") == "codex" and metadata.get(
                    "app_server_thread_id"
                ):
                    reference, boundary = metadata["app_server_thread_id"], index + 1
                    previous_digest = metadata.get("instructions_digest")
            if reference != self.thread_id or not reference:
                if reference:
                    opened = await self.server.request(
                        "thread/resume", {"threadId": reference}
                    )
                else:
                    tools = [
                        {
                            "type": "function",
                            "name": "litetui_" + spec["function"]["name"],
                            "description": spec["function"].get("description", ""),
                            "inputSchema": spec["function"].get("parameters", {}),
                        }
                        for spec in kwargs.get("tools", [])
                    ]
                    if self.app:
                        names = {tool["name"] for tool in tools}
                        deferred_tools = []
                        for spec in self.app.plugins.deferred_specs():
                            function = spec["function"]
                            name = "litetui_" + function["name"]
                            if name not in names:
                                deferred_tools.append(
                                    {
                                        "type": "function",
                                        "name": name,
                                        "description": function.get("description", ""),
                                        "inputSchema": function.get("parameters", {}),
                                        "deferLoading": True,
                                    }
                                )
                                names.add(name)
                        if deferred_tools:
                            tools.append(
                                {
                                    "type": "namespace",
                                    "name": "litetui",
                                    "description": "Additional LiteTUI host tools available through tool search.",
                                    "tools": deferred_tools,
                                }
                            )
                    from litetui import paths

                    opened = await self.server.request(
                        "thread/start",
                        {
                            "model": kwargs["model"],
                            "allowProviderModelFallback": False,
                            "ephemeral": self.app is None,
                            "cwd": str(paths.ROOT),
                            "approvalPolicy": "on-request",
                            "sandbox": "workspace-write",
                            "developerInstructions": instructions,
                            "dynamicTools": tools,
                        },
                    )
                self.thread_id = opened["thread"]["id"]
            input_items = user_input(messages[boundary:])
            if not input_items:
                raise ProviderError("No new user input for the Codex turn.")
            effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
            model = kwargs["model"]
            if self.app:
                model_info = self.app.backend.models.get(model, {})
                effort = effort or model_info.get("default_reasoning_level", "medium")
            restricted = (
                not self.app
                or not self.app.tools_enabled
                or getattr(self.app, "_active_tool_profile", "scheduled")
                != "autonomous"
            )
            params = {
                "threadId": self.thread_id,
                "input": input_items,
                "model": model,
                "effort": effort or "medium",
                "approvalPolicy": "on-request",
                "sandboxPolicy": {"type": "readOnly"}
                if restricted
                else {
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                },
            }
            if reference and instructions_digest != previous_digest:
                params["additionalContext"] = {
                    "litetui-instructions": {
                        "kind": "application",
                        "value": instructions,
                    }
                }
            started = await self.server.request("turn/start", params)
            self.turn_id = started["turn"]["id"]
            metadata = {
                "provider": "codex",
                "model": model,
                "app_server_thread_id": self.thread_id,
                "instructions_digest": instructions_digest,
            }
            from litetui.codex_tool_ui import CodexToolUI

            tool_ui = CodexToolUI(self.app)
            completed = False
            interrupt_sent = False
            last_message_id = None
            try:
                if self.app:
                    for original_index in range(len(self.app.conversation) - 1, -1, -1):
                        original = self.app.conversation[original_index]
                        if original.get("role") == "user" and any(
                            original is m for m in messages
                        ):
                            original["provider_metadata"] = metadata
                            self.app._edit(original_index, "Codex accepted user turn")
                            break
                yield _chunk(metadata=metadata)
                while True:
                    if (
                        self.app
                        and getattr(self.app, "_stop_requested", False)
                        and not interrupt_sent
                    ):
                        await self.server.request(
                            "turn/interrupt",
                            {"threadId": self.thread_id, "turnId": self.turn_id},
                        )
                        interrupt_sent = True
                    try:
                        event = await asyncio.wait_for(self.server.events.get(), 0.2)
                    except TimeoutError:
                        continue
                    if isinstance(event, Exception):
                        raise event
                    if "id" in event and "method" in event:
                        await self._server_request(event)
                        continue
                    method, payload = event.get("method"), event.get("params", {})
                    if payload.get("threadId") not in (None, self.thread_id):
                        continue
                    if method == "item/agentMessage/delta":
                        item_id = payload.get("itemId")
                        if item_id and last_message_id and item_id != last_message_id:
                            yield _chunk(text="\n\n")
                        last_message_id = item_id or last_message_id
                        yield _chunk(text=payload.get("delta", ""))
                    elif method in (
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/textDelta",
                    ):
                        yield _chunk(reasoning=payload.get("delta", ""))
                    elif method == "thread/tokenUsage/updated":
                        last = (payload.get("tokenUsage") or {}).get("last", {})
                        yield _chunk(
                            usage=_usage(
                                {
                                    "input_tokens": last.get("inputTokens", 0),
                                    "output_tokens": last.get("outputTokens", 0),
                                    "input_tokens_details": {
                                        "cached_tokens": last.get("cachedInputTokens")
                                    },
                                },
                                "codex",
                            )
                        )
                    elif method in ("item/started", "item/completed"):
                        await tool_ui.item(
                            payload.get("item", {}),
                            completed=method == "item/completed",
                        )
                    elif (
                        method == "turn/completed"
                        and payload.get("turn", {}).get("id") == self.turn_id
                    ):
                        completed = True
                        status = payload["turn"].get("status")
                        if status == "failed":
                            raise ProviderError(
                                "Codex could not complete the turn: "
                                + str(
                                    (payload["turn"].get("error") or {}).get(
                                        "message", "unknown error"
                                    )
                                )[:500]
                            )
                        yield _chunk(metadata=metadata)
                        return
            finally:
                tool_ui.finish()
                if not completed and self.turn_id and not interrupt_sent:
                    await self.server.request(
                        "turn/interrupt",
                        {"threadId": self.thread_id, "turnId": self.turn_id},
                    )
                self.turn_id = None


class AppServerStream:
    def __init__(self, iterator):
        self.iterator = iterator

    def __aiter__(self):
        return self.iterator

    async def close(self):
        await self.iterator.aclose()
