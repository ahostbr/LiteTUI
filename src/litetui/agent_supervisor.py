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
        self._job = None

    @property
    def returncode(self):
        return self.process.returncode if self.process else None

    async def start_python(self, *, module, args=(), cwd, env=None):
        """Full-agent entry path: containment always precedes module execution."""
        await self.start(python_child_argv(module=module, args=args),
                         cwd=cwd, env=env, gated=True)

    async def start(self, argv, *, cwd, env=None, gated=False):
        if self.process is not None:
            raise LaunchBlocked('Child process already started')
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)
        if os.name == 'nt':
            from litetui import jobkill
            self._job = jobkill.create()
            if not jobkill.assign(self._job, self.process.pid):
                jobkill.close(self._job)
                self._job = None
                await self.close(timeout=.2)
                raise LaunchBlocked('Could not establish child process-tree containment')
        self._stderr = asyncio.create_task(self._drain_errors())
        if gated:
            self.process.stdin.write(b'LITETUI_CONTAINED_V1\n')
            await self.process.stdin.drain()

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

    async def rpc_handshake(self, spec, *, workspace, timeout=30, probe=None):
        """Validate real LiteTUI RPC over the exclusively owned stdout pipe.

        Unlike a shared rendezvous endpoint this transport needs no echoed
        bearer token. Conversation allocation is a separate launcher gate.
        """
        self.ready = False
        try:
            event = await self.receive(timeout=timeout)
            expected = {'type': 'ready', 'launch_status': 'ready',
                        'backend': spec.backend, 'model': spec.model,
                        'cwd': str(workspace), 'tool_profile': spec.tool_profile}
            for key, value in expected.items():
                if event.get(key) != value:
                    raise LaunchBlocked(f'Child effective {key} differs from launch request')
            level = spec.reasoning_effort if spec.reasoning_effort is not None else spec.thinking_level
            if level is not None and event.get('thinking_level') != level:
                raise LaunchBlocked('Child effective thinking differs from launch request')
            validate_process_identity(event, owned_pid=self.process.pid, probe=probe)
            if self.process.returncode is not None:
                raise LaunchBlocked('Child exited during readiness')
            self.ready = True
            return event
        except BaseException:
            await self.close()
            raise

    async def send_prompt(self, text):
        if not self.ready or self.process.returncode is not None:
            raise LaunchBlocked('Child is not ready for a prompt')
        self.process.stdin.write((json.dumps({'type': 'prompt', 'message': text}) + '\n').encode('utf-8'))
        await self.process.stdin.drain()

    async def close(self, *, timeout=2):
        self.ready = False
        if self.process is None:
            return True
        if self._job is not None:
            from litetui import jobkill
            if not jobkill.close(self._job):
                self.diagnostics.append('Child job close failed')
                return False
            self._job = None
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
        if self._stderr and not self._stderr.cancelled():
            try:
                await asyncio.wait_for(self._stderr, timeout)
            except TimeoutError:
                self.diagnostics.append('Child stderr cleanup timed out')
        return self.process.returncode is not None


def python_child_argv(*, module=None, script=None, args=()):
    """Gate execution before imports/forks and bypass the Windows venv shim.

    Capture this installed interpreter's import paths, not a hard-coded source
    checkout. Prompt/credentials are never placed on this command line.
    """
    import sys
    if (module is None) == (script is None):
        raise ValueError('Specify exactly one module or script')
    bootstrap = (
        "import sys,json,runpy; "
        "line=sys.stdin.buffer.readline(); "
        "sys.exit(73) if line != b'LITETUI_CONTAINED_V1\\n' else None; "
        "cfg=json.loads(sys.argv[1]); sys.path[:]=cfg['paths']; "
        "sys.argv=[cfg['target']]+cfg['args']; "
        "runpy.run_module(cfg['target'],run_name='__main__',alter_sys=True) "
        "if cfg['module'] else runpy.run_path(cfg['target'],run_name='__main__')"
    )
    config = {'module': module is not None, 'target': module or str(script),
              'args': list(args), 'paths': [p for p in sys.path if p]}
    return [getattr(sys, '_base_executable', sys.executable), '-S', '-c', bootstrap, json.dumps(config)]
