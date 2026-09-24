"""One startup contract for interactive and agent-driven CLI launches."""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from litetui.llm_backend import BackendError


@dataclass
class LaunchOptions:
    base_url: str | None = None
    server_mode: str = 'auto'
    load_model: bool = False
    context_length: int | None = None
    max_tokens: int | None = None
    server_executable: str | None = None
    model_path: str | None = None
    server_command: list[str] | None = None
    api_key_env: str | None = None
    timeout: int = 600

    def overrides(self, settings, backend, model):
        from litetui.custom_backend import api_base
        values = {}
        if backend == 'claude' and any((
            self.base_url, self.context_length, self.max_tokens, self.server_executable,
            self.model_path, self.server_command, self.api_key_env, self.load_model,
            self.server_mode != 'auto',
        )):
            raise ValueError('Claude owns its runtime, endpoint and context; local server/load/sampling options are unsupported')
        if backend == 'ninfer':
            from litetui import gpu_gate
            if not gpu_gate.is_rtx_5090():
                raise ValueError('NInfer requires an RTX 5090; no backend fallback was used')
            if self.server_mode == 'start':
                if self.base_url:
                    raise ValueError('NInfer startup allocates its port; use --base-url only when connecting')
                values['ninfer_host'] = ''
        for name in ('base_url', 'server_executable', 'model_path', 'api_key_env'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip() or '\x00' in value):
                raise ValueError(f'{name} must be a nonempty string')
        if type(self.load_model) is not bool:
            raise ValueError('load_model must be a boolean')
        if backend == 'llamacpp' and self.server_mode in ('connect', 'start'):
            values['llama_attach_hosts'] = []
        if self.server_mode not in ('auto', 'connect', 'start'):
            raise ValueError('Unknown server mode')
        for name in ('context_length', 'max_tokens', 'timeout'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f'{name} must be a positive integer')
        if self.load_model and not model:
            raise ValueError('--load-model requires --model')
        if self.load_model and backend not in ('llamacpp', 'lmstudio'):
            raise ValueError('--load-model is supported by llama.cpp and LM Studio; other engines select models at server startup')
        if self.server_command is not None and (backend != 'custom' or self.server_mode != 'start'):
            raise ValueError('--server-command requires --backend custom --start-server')
        if self.server_command is not None and (not self.server_command or any(not isinstance(x, str) or '\x00' in x for x in self.server_command)):
            raise ValueError('--server-command must be a nonempty JSON array of strings')
        if self.api_key_env and backend != 'custom':
            raise ValueError('--api-key-env applies only to the custom backend')
        if backend == 'codex' and any((self.base_url, self.context_length, self.model_path, self.server_executable, self.max_tokens)):
            raise ValueError('Codex manages its endpoint, context and output limits; local server options do not apply')
        if self.base_url:
            base = api_base(self.base_url)
            field = {'custom': 'custom_base_url', 'llamacpp': 'llama_host',
                     'lmstudio': 'lm_host', 'ninfer': 'ninfer_host'}[backend]
            values[field] = base if backend == 'custom' else base.removesuffix('/v1')
            if backend == 'llamacpp':
                values['llama_attach_hosts'] = []
        if self.context_length:
            if backend in ('llamacpp', 'lmstudio') and not self.load_model:
                raise ValueError('--context-length requires --load-model for llama.cpp/LM Studio; changing context reloads the model')
            if backend == 'ninfer' and self.server_mode != 'start':
                raise ValueError('NInfer context is fixed at startup; use --start-server --context-length')
            field = {'custom': 'custom_context_length', 'ninfer': 'ninfer_max_context',
                     'llamacpp': 'default_context_length', 'lmstudio': 'default_context_length'}[backend]
            values[field] = self.context_length
        if self.max_tokens:
            values.update(max_tokens_tools=self.max_tokens, max_tokens_chat=self.max_tokens)
        if self.server_executable:
            if backend not in ('llamacpp', 'ninfer', 'custom'):
                raise ValueError('--server-executable applies to llama.cpp, NInfer or custom')
            if backend == 'custom':
                raise ValueError('Use --server-command JSON for a custom executable and its arguments')
            values['llama_executable' if backend == 'llamacpp' else 'ninfer_executable'] = self.server_executable
        if self.model_path:
            if backend == 'ninfer':
                if self.server_mode != 'start':
                    raise ValueError('--model-path for NInfer requires --start-server')
                values['ninfer_artifact'] = self.model_path
            elif backend == 'llamacpp':
                if not self.load_model:
                    raise ValueError('--model-path requires --load-model for llama.cpp')
                path = Path(self.model_path).resolve()
                if not path.is_file():
                    raise ValueError(f'Model file does not exist: {path}')
                values['llama_models_dirs'] = [*settings.llama_models_dirs, str(path.parent)]
            elif backend != 'custom':
                raise ValueError('--model-path applies to llama.cpp, NInfer or a custom command')
            elif not self.server_command:
                raise ValueError('--model-path for custom requires --server-command')
        if self.api_key_env:
            values['custom_api_key_env'] = self.api_key_env
        if backend == 'custom' and self.server_mode == 'start' and not self.server_command:
            raise ValueError('--start-server for custom requires --server-command JSON')
        if self.server_mode == 'start' and backend in ('custom', 'lmstudio', 'llamacpp'):
            host = values.get({'custom': 'custom_base_url', 'lmstudio': 'lm_host', 'llamacpp': 'llama_host'}[backend])
            host = host or getattr(settings, {'custom': 'custom_base_url', 'lmstudio': 'lm_host', 'llamacpp': 'llama_host'}[backend])
            if urlsplit(host).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('Starting a server requires a local loopback URL')
        return values


def add_arguments(parser):
    parser.add_argument('--base-url', '--host', dest='base_url', help='Explicit server URL; never fall back to another endpoint')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--server-mode', choices=['auto', 'connect', 'start'], default='auto', help='auto: existing backend behavior; connect: never spawn; start: explicitly start server')
    mode.add_argument('--start-server', dest='server_mode', action='store_const', const='start')
    parser.add_argument('--load-model', action='store_true', help='Explicitly load --model (llama.cpp/LM Studio) before submitting a prompt')
    parser.add_argument('--context-length', type=int, help='Load/start context tokens; custom: declared client budget (server command must set actual context)')
    parser.add_argument('--max-tokens', type=int, help='Completion token limit for local/custom backends')
    parser.add_argument('--server-executable', help='llama-server or ninfer-serve executable')
    parser.add_argument('--model-path', help='GGUF file, NInfer artifact, or custom command {model_path}')
    parser.add_argument('--server-command', help='Custom server argv as JSON array; no shell. Placeholders: {model}, {model_path}, {context_length}, {host}, {port}')
    parser.add_argument('--api-key-env', help='Custom server credential environment variable NAME (never the key)')
    parser.add_argument('--server-timeout', type=int, default=600, help='Startup/readiness timeout in seconds')


def from_args(args):
    if args.backend is None and any((args.base_url, args.load_model, args.context_length,
                                    args.server_executable, args.model_path, args.server_command,
                                    args.api_key_env, args.server_mode != 'auto')):
        raise ValueError('Server options require an explicit --backend')
    try:
        command = json.loads(args.server_command) if args.server_command else None
    except ValueError as exc:
        raise ValueError('--server-command must be a JSON array of arguments') from exc
    if command is not None and not isinstance(command, list):
        raise ValueError('--server-command must be a JSON array of arguments')
    return LaunchOptions(args.base_url, args.server_mode, args.load_model, args.context_length,
                         args.max_tokens, args.server_executable, args.model_path, command,
                         args.api_key_env, args.server_timeout)


def to_argv(options):
    """Use the same public arguments for supervised and external agents."""
    args = ['--server-mode', options.server_mode, '--server-timeout', str(options.timeout)]
    for name in ('base_url', 'context_length', 'max_tokens', 'server_executable', 'model_path', 'api_key_env'):
        value = getattr(options, name)
        if value is not None:
            args += ['--' + name.replace('_', '-'), str(value)]
    if options.load_model:
        args.append('--load-model')
    if options.server_command is not None:
        args += ['--server-command', json.dumps(options.server_command)]
    return args


async def prepare(app):
    app._explicit_launch_load = bool(getattr(app, '_launch_options', None) and (
        app._launch_options.load_model or app._launch_options.server_mode == 'start'))
    try:
        return await _prepare(app)
    finally:
        app._explicit_launch_load = False


async def _prepare(app):
    """Start only on explicit request, then load/verify before prompt dispatch."""
    options = getattr(app, '_launch_options', None)
    backend = app.backend
    if backend.name != getattr(app, '_launch_backend', backend.name):
        return await backend.ensure_running()
    if options is None:
        return await backend.ensure_running()
    if getattr(app, '_launch_prepared', False):
        return await backend.ensure_running()
    if options.server_mode == 'start':
        if backend.name == 'ninfer':
            app._system(await backend.start_engine())
        elif backend.name == 'custom':
            await start_custom(app, options)
        elif backend.name == 'lmstudio':
            executable = shutil.which('lms')
            if not executable:
                raise BackendError('LM Studio CLI (lms) is not installed/on PATH')
            port = urlsplit(backend.host()).port or 1234
            from litetui import ttyguard
            result = await asyncio.to_thread(ttyguard.run, [executable, 'server', 'start', '--port', str(port)], timeout=options.timeout)
            if result.returncode:
                raise BackendError('lms server start failed; open LM Studio and check its server settings')
    # Explicit llama endpoints and connect-only launches must not spawn or
    # discover another server when that precise endpoint is unavailable.
    if backend.name == 'llamacpp' and (options.server_mode == 'connect' or (options.base_url and options.server_mode != 'start')):
        from litetui.llm_backend import _healthy
        if not await asyncio.to_thread(_healthy, backend.host()):
            raise BackendError(f'No server at {backend.host()}; no fallback or server start was used')
    status = await backend.ensure_running()
    if options.load_model:
        await backend.load(app._cli_initial_model, ctx=options.context_length)
        await backend.ensure_chat_ready(app._cli_initial_model)
    app._launch_prepared = True
    return status


async def start_custom(app, options):
    from litetui import paths
    backend = app.backend
    try:
        await backend.list_models()
    except BackendError:
        pass
    else:
        raise BackendError('A server already answers at the custom URL; use --server-mode connect')
    parsed = urlsplit(backend.host())
    substitutions = {'model': app._cli_initial_model, 'model_path': options.model_path,
                     'context_length': options.context_length, 'host': parsed.hostname,
                     'port': parsed.port}
    argv = []
    for arg in options.server_command:
        for key, value in substitutions.items():
            token = '{' + key + '}'
            if token in arg and value is None:
                raise BackendError(f'Custom command requires {token}')
            arg = arg.replace(token, str(value))
        argv.append(arg)
    log = paths.data_root() / 'custom-server.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    from litetui import ttyguard
    with log.open('ab') as output:
        proc = ttyguard.popen(argv, stdout=output, stderr=output, kill_on_close=True)
    app._custom_server_process = proc
    app._system(f'Started custom server pid {proc.pid}; log: {log}')
    try:
        async with asyncio.timeout(options.timeout):
            while True:
                if proc.poll() is not None:
                    raise BackendError(f'Custom server exited ({proc.returncode}); see {log}')
                try:
                    await backend.list_models()
                    return
                except BackendError:
                    await asyncio.sleep(0.25)
    except BaseException:
        stop_custom(app)
        raise


def stop_custom(app):
    proc = getattr(app, '_custom_server_process', None)
    if proc is not None:
        from litetui import ttyguard
        if (proc.poll() is None or getattr(proc, '_litetui_job', None) is not None) and not ttyguard.kill_tree(proc.pid, proc):
            raise BackendError('Could not confirm custom server cleanup')
        proc.wait(timeout=15)
    app._custom_server_process = None
