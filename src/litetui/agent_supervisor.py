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
        self.conversation_id = None
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
        if spec.launch:
            timeout = max(timeout, spec.launch.get('timeout', 600) + 15)
        try:
            event = await self.receive(timeout=timeout)
            expected = {'type': 'ready', 'launch_status': 'ready',
                        'backend': spec.backend, 'model': spec.model,
                        'cwd': str(workspace), 'tool_profile': spec.tool_profile}
            for key, value in expected.items():
                if event.get(key) != value:
                    raise LaunchBlocked(f'Child effective {key} differs from launch request')
            level = spec.reasoning_effort if spec.reasoning_effort is not None else spec.thinking_level
            if level == 'none':
                level = 'off'
            if level is not None and event.get('thinking_level') != level:
                raise LaunchBlocked('Child effective thinking differs from launch request')
            if spec.launch:
                from litetui.custom_backend import api_base
                if spec.launch.get('base_url') and event.get('base_url', '').rstrip('/') != api_base(spec.launch['base_url']):
                    raise LaunchBlocked('Child effective endpoint differs from launch request')
                if spec.launch.get('context_length') and event.get('context_requested') != spec.launch['context_length']:
                    raise LaunchBlocked('Child context request differs from launch request')
                if spec.launch.get('context_length') and (event.get('context_length') or 0) < spec.launch['context_length']:
                    raise LaunchBlocked('Child context capacity is below the launch request')
            from litetui.agent_inbox import _identity
            try:
                conversation_id = _identity(event.get('conversation_id'))
            except ValueError as exc:
                raise LaunchBlocked('Child conversation identity missing or invalid') from exc
            validate_process_identity(event, owned_pid=self.process.pid, probe=probe)
            if self.process.returncode is not None:
                raise LaunchBlocked('Child exited during readiness')
            self.conversation_id = conversation_id
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

    async def collect_turn(self, *, timeout=300, max_output=1000000):
        """Bounded first-turn collector; interactive requests require a relay."""
        started = False
        chunks = []
        size = 0
        try:
            async with asyncio.timeout(timeout):
                while True:
                    event = await self.receive(timeout=timeout)
                    kind = event.get('type')
                    if kind == 'error' or (kind == 'response' and event.get('ok') is False):
                        raise LaunchBlocked(str(event.get('error', 'Child rejected request')))
                    if kind in ('tool_approval_requested', 'user_input_requested', 'model_load_requested'):
                        raise LaunchBlocked('Child requires a human relay')
                    if kind == 'turn_start':
                        if started:
                            raise LaunchBlocked('Unexpected overlapping child turn')
                        started = True
                    elif kind == 'text_delta':
                        text = event.get('text')
                        if not started or not isinstance(text, str):
                            raise LaunchBlocked('Invalid child text event')
                        size += len(text)
                        if size > max_output:
                            raise LaunchBlocked('Child output exceeds collection limit')
                        chunks.append(text)
                    elif kind == 'turn_end':
                        if not started:
                            raise LaunchBlocked('Child completed without a started turn')
                        reason = event.get('stopReason')
                        status = 'completed' if reason == 'stop' else 'cancelled' if reason in ('cancelled', 'cancel') else 'failed'
                        result = {'status': status, 'summary': ''.join(chunks), 'stop_reason': reason}
                        if status == 'failed':
                            detail = event.get('error')
                            result['error'] = (detail if isinstance(detail, str) and detail
                                               else f'Child turn ended: {reason}')[:max_output]
                            if not result['summary']:
                                result['summary'] = result['error']
                        return result
        except TimeoutError as exc:
            raise LaunchBlocked('Child turn timed out') from exc

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


async def finish_child(process, inbox, *, parent, child_id, branch, evidence, notify,
                       timeout=300, data_root=None, launch_outcome=None):
    """Commit outcome after bounded process cleanup, then notify the parent.

    Process termination is not local-model absence; resource settlement must be
    reported separately by the backend coordinator before releasing capacity.
    """
    from litetui.agent_inbox import _identity
    _identity(process.conversation_id)
    cancelled = None
    try:
        outcome = (dict(launch_outcome) if launch_outcome is not None
                   else await process.collect_turn(timeout=timeout))
    except asyncio.CancelledError as exc:
        cancelled = exc
        outcome = {'status': 'cancelled', 'summary': 'Parent cancelled child collection'}
    except Exception as exc:
        outcome = {'status': 'failed', 'summary': f'{type(exc).__name__}: {exc}'}
    async def bounded_cleanup():
        try:
            async with asyncio.timeout(10):
                return await process.close(), None
        except asyncio.CancelledError:
            # An internally cancelled close is unknown cleanup, not parent cancellation.
            return False, 'Child cleanup was cancelled'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    # Repeated caller cancellation must not strand the owned tree or bypass
    # the durable outcome. Keep awaiting the SAME bounded cleanup task.
    cleanup = asyncio.create_task(bounded_cleanup())
    while True:
        try:
            closed, cleanup_error = await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            if cleanup.cancelled():
                closed, cleanup_error = False, 'Child cleanup was cancelled'
                break
    result = {**outcome, 'child_id': child_id, 'conversation_id': process.conversation_id,
              'branch': branch, 'evidence': list(evidence),
              'cleanup': {'state': 'confirmed' if closed else 'unconfirmed',
                          'scope': 'owned process tree only', 'error': cleanup_error,
                          'model_residency': 'not verified'}}
    if data_root is not None:
        from litetui.agent_storage import conversation_evidence
        try:
            result['storage'] = conversation_evidence(data_root, process.conversation_id)
        except (OSError, ValueError) as exc:
            result['storage_error'] = f'{type(exc).__name__}: {exc}'
            if result['status'] == 'completed':
                result['status'] = 'failed'
                result['error'] = result['storage_error']
    try:
        completion = inbox.persist(parent, result)
    except Exception as exc:
        # No durable outcome exists; the registry claim must remain for recovery.
        # Do not mask caller cancellation with a storage failure.
        if cancelled is not None:
            cancelled.add_note(f'Child outcome persistence failed: {type(exc).__name__}')
            raise cancelled from exc
        raise
    if cancelled is not None:
        # Durable pending result is replayable; preserve caller cancellation.
        raise cancelled
    notify({'completion_id': completion, 'result': inbox.get(parent, completion)})
    return completion
