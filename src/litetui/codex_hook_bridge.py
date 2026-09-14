"""Session-scoped, authenticated bridge from native hooks to the host policy."""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from litetui.codex_native_policy import NativePolicy, denial
from litetui.model_transport import ProviderError

LIMIT = 16 * 1024 * 1024


def own_hooks(value, command):
    found = []
    if isinstance(value, dict):
        if value.get("command") == command and "currentHash" in value:
            found.append(value)
        for child in value.values():
            found.extend(own_hooks(child, command))
    elif isinstance(value, list):
        for child in value:
            found.extend(own_hooks(child, command))
    return found


class NativeHookBridge:
    def __init__(self, app):
        self.app = app
        self.policy = NativePolicy(app) if app is not None else None
        self.token = secrets.token_urlsafe(32)
        self.listener = None
        self.clients = set()
        self.actions = set()
        self.original_overrides = ()
        self.original_environment = {}

    async def accept(self, reader, writer):
        self.clients.add(writer)
        action_task = disconnected = worker = None
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(line)
            if not secrets.compare_digest(str(request.get("token", "")), self.token):
                reply = denial("Invalid native tool policy connection")
            elif self.policy is None:
                reply = denial("Tools are unavailable in this side request")
            else:
                action = self.policy.handle(request["event"])
                if hasattr(self.app, "run_worker"):
                    worker = self.app.run_worker(
                        action,
                        group="codex-policy",
                        exclusive=False,
                        exit_on_error=False,
                    )
                    action_task = asyncio.create_task(worker.wait())
                else:
                    action_task = asyncio.create_task(action)
                self.actions.add(action_task)
                disconnected = asyncio.create_task(reader.read(1))
                deadline = asyncio.get_running_loop().time() + 295
                while True:
                    done, _ = await asyncio.wait(
                        (action_task, disconnected),
                        timeout=0.1,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if disconnected in done or getattr(
                        self.app, "_stop_requested", False
                    ):
                        reply = denial("Native tool policy request cancelled")
                        break
                    if action_task in done:
                        reply = action_task.result()
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        reply = denial("Native tool policy request timed out")
                        break
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except Exception:  # noqa: BLE001, S110 - fail closed without logging hook payloads
            # Closing without a valid response makes the helper exit 2.
            pass
        finally:
            if worker is not None:
                worker.cancel()
            pending = [task for task in (action_task, disconnected) if task is not None]
            for task in pending:
                task.cancel()
                self.actions.discard(task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self.clients.discard(writer)
            writer.close()

    def close(self):
        if self.listener:
            self.listener.close()
        for writer in tuple(self.clients):
            writer.close()
        for action in tuple(self.actions):
            action.cancel()

    async def install(self, server):
        self.listener = await asyncio.start_server(
            self.accept, "127.0.0.1", 0, limit=LIMIT
        )
        port = self.listener.sockets[0].getsockname()[1]
        helper = Path(__file__).with_name("codex_hook_helper.py")
        # Resolve the bundled interpreter by putting its directory first. An
        # unquoted executable token works in both cmd and PowerShell hooks.
        executable = Path(sys.executable)
        command = f'{executable.name} "{helper}"'
        handler = (
            '{matcher=".*",hooks=[{type="command",command='
            + json.dumps(command)
            + ",timeout=300}]}"
        )
        original = tuple(server.config_overrides)
        environment = dict(server.environment)
        self.original_overrides = original
        self.original_environment = environment
        ready = False
        try:
            await server.close()
            server.environment.update(
                {
                    "LITETUI_CODEX_HOOK_KEY": self.token,
                    "LITETUI_CODEX_HOOK_PORT": str(port),
                    "PATH": str(executable.parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                }
            )
            # Hook discovery visits each config layer. These additional session
            # groups do not replace the user's source-layer hooks or trust.
            server.config_overrides = original + (
                "features.hooks=true",
                "hooks.PreToolUse=[" + handler + "]",
                "hooks.PostToolUse=[" + handler + "]",
            )
            await server.start()
            hooks = own_hooks(await server.request("hooks/list", {}), command)
            if len(hooks) != 2:
                raise ProviderError(
                    "Codex did not discover the native host policy hooks."
                )
            # Whole-table syntax matters: dotted CLI keys cannot reliably
            # represent the hook identity containing config.toml and backslashes.
            states = ",".join(
                json.dumps(h["key"])
                + "={trusted_hash="
                + json.dumps(h["currentHash"])
                + "}"
                for h in hooks
            )
            await server.close()
            server.config_overrides += ("hooks.state={" + states + "}",)
            await server.start()
            trusted = own_hooks(await server.request("hooks/list", {}), command)
            if len(trusted) != 2 or any(
                h.get("trustStatus") != "trusted" or not h.get("enabled")
                for h in trusted
            ):
                raise ProviderError(
                    "Codex native host policy hooks could not be enabled for this session."
                )
            server.native_bridge = self
            ready = True
        finally:
            if not ready:
                await server.close()
                server.config_overrides = original
                server.environment = environment
                self.close()
