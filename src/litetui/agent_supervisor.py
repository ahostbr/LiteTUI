"""Owned headless child transport. No shell, fallback or prompt before handshake.

This transport is not yet the full launcher: caller must obtain admission and
create the child's conversation/worktree first. Tree containment and headed
transport must be integrated before exposing spawn_agent as a production tool.
"""
import asyncio
from collections import deque
import json
import os
from litetui.agent_launcher import LaunchBlocked, validate_handshake, validate_process_identity


class AgentProcess:
    def __init__(self):
        self.process = None
        self.ready = False
        self.diagnostics = deque(maxlen=100)
        self._stderr = None

    @property
    def returncode(self):
        return self.process.returncode if self.process else None

    async def start(self, argv, *, cwd, env=None):
        if self.process is not None:
            raise LaunchBlocked('Child process already started')
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)
        self._stderr = asyncio.create_task(self._drain_errors())

    async def _drain_errors(self):
        try:
            while chunk := await self.process.stderr.read(4096):
                self.diagnostics.append(chunk.decode('utf-8', errors='replace'))
        except Exception as exc:
            self.diagnostics.append(str(exc))

    async def receive(self, *, timeout):
        try:
            raw = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not raw:
                raise LaunchBlocked('Child disconnected before expected event')
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError('Expected event object')
            return event
        except (TimeoutError, ValueError) as exc:
            raise LaunchBlocked(f'Child event unavailable or malformed: {exc}') from exc

    async def handshake(self, spec, *, timeout=30, **expected):
        try:
            event = await self.receive(timeout=timeout)
            validate_handshake(spec, event, **expected)
            validate_process_identity(event, owned_pid=self.process.pid)
            if self.process.returncode is not None:
                raise LaunchBlocked('Child exited during readiness handshake')
            self.ready = True
            return event
        except BaseException:
            await self.close()
            raise

    async def send_prompt(self, text):
        if not self.ready or self.process.returncode is not None:
            raise LaunchBlocked('Child is not ready for a prompt')
        self.process.stdin.write((json.dumps({'type': 'prompt', 'text': text}) + '\n').encode('utf-8'))
        await self.process.stdin.drain()

    async def close(self, *, timeout=2):
        self.ready = False
        if self.process is None:
            return True
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout)
        except TimeoutError:
            # Popen owns this exact process handle; never kill a discovered PID.
            if self.process.returncode is None:
                self.process.kill()
            try:
                await asyncio.wait_for(self.process.wait(), timeout)
            except TimeoutError:
                return False
        if self._stderr:
            try:
                await asyncio.wait_for(self._stderr, timeout)
            except TimeoutError:
                self.diagnostics.append('Child stderr cleanup timed out')
        return self.process.returncode is not None
