"""One startup contract for interactive and agent-driven CLI launches."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from litetui.llm_backend import BackendError

#: The engine backends by the name a refusal uses: only ever the one that was asked for.
_ENGINE_LABELS = {'ninfer': 'NInfer', 'strata': 'Strata'}


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
    codex_engine: str | None = None
    engine_management: bool = False
    history_projection_version: int | None = None
    work_lifecycle_version: int | None = None
    mcp_lifecycle_version: int | None = None
    thinking_explicit: bool | None = None

    def validate_mcp_lifecycle(self) -> None:
        version = self.mcp_lifecycle_version
        if version is not None and (type(version) is not int or version != 1 or self.work_lifecycle_version != 1):
            raise ValueError('--mcp-lifecycle-version requires integer 1 and --work-lifecycle-version 1')

    def validate_work_lifecycle(self) -> None:
        version = self.work_lifecycle_version
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError('--work-lifecycle-version must be integer 1 when selected')

    def validate_history_projection(self) -> None:
        version = self.history_projection_version
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError('--history-projection-version must be integer 1 when selected')

    def validate_engine_management(self, backend, model):
        # Validate before endpoint discovery, settings overlays or hardware checks.
        # This flag grants management access, never automatic load/start consent.
        if type(self.engine_management) is not bool:
            raise ValueError('engine_management must be a boolean')
        if not self.engine_management:
            return
        if backend not in ('ninfer', 'strata'):
            raise ValueError('--engine-management requires an explicit --backend ninfer or strata')
        if (not isinstance(model, str) or not model.strip() or '\x00' in model
                or len(model.encode('utf-8')) > 1024):
            raise ValueError('--engine-management requires an explicit --model of 1..1024 UTF-8 bytes')
        if self.server_mode != 'connect' or self.load_model is not False:
            raise ValueError('--engine-management requires --server-mode connect without automatic start/load')

    def overrides(self, settings, backend, model):
        self.validate_history_projection()
        self.validate_work_lifecycle()
        self.validate_mcp_lifecycle()
        self.validate_engine_management(backend, model)
        from litetui.custom_backend import api_base
        values: dict[str, Any] = {}
        if self.mcp_lifecycle_version is not None:
            values["mcp_enabled"] = False
        if self.codex_engine is not None:
            if self.codex_engine not in ('native', 'http'):
                raise ValueError('codex_engine must be native or http')
            values['codex_native_engine'] = self.codex_engine == 'native'
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
        if backend == 'strata' and self.server_executable:
            raise ValueError('Strata has no server executable to name; --model-path takes its strata-<model>.json')
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
                     'lmstudio': 'lm_host', 'ninfer': 'ninfer_host', 'strata': 'strata_host'}[backend]
            values[field] = base if backend == 'custom' else base.removesuffix('/v1')
            if backend == 'llamacpp':
                values['llama_attach_hosts'] = []
        if self.context_length:
            if backend in ('llamacpp', 'lmstudio') and not self.load_model:
                raise ValueError('--context-length requires --load-model for llama.cpp/LM Studio; changing context reloads the model')
            if backend == 'llamacpp' and (self.server_mode == 'connect' or (self.base_url and self.server_mode != 'start')):
                raise ValueError('llama.cpp context changes require --start-server to permit a preset rebuild')
            if backend in ('ninfer', 'strata') and self.server_mode != 'start':
                # Names the backend that was asked for: a Strata refusal never says NInfer (T893).
                raise ValueError(f'{_ENGINE_LABELS[backend]} context is fixed at startup; use --start-server --context-length')
            field = {'custom': 'custom_context_length', 'ninfer': 'ninfer_max_context',
                     'strata': 'strata_max_context',
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
            if backend in ('ninfer', 'strata'):
                if self.server_mode != 'start':
                    raise ValueError(f'--model-path for {_ENGINE_LABELS[backend]} requires --start-server')
                values['ninfer_artifact' if backend == 'ninfer' else 'strata_config'] = self.model_path
            elif backend == 'llamacpp':
                if not self.load_model:
                    raise ValueError('--model-path requires --load-model for llama.cpp')
                path = Path(self.model_path).resolve()
                if not path.is_file():
                    raise ValueError(f'Model file does not exist: {path}')
                values['llama_models_dirs'] = [*settings.llama_models_dirs, str(path.parent)]
            elif backend != 'custom':
                raise ValueError('--model-path applies to llama.cpp, NInfer, Strata or a custom command')
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
    parser.add_argument("--mcp-lifecycle-version", type=int, choices=(1,),
                        help="Reviewed MCP RPC lifecycle; starts MCP off and requires captured Work version 1")
    parser.add_argument('--work-lifecycle-version', type=int, choices=(1,),
                        help='Captured background-input RPC lifecycle; requires --rpc and --hold-restored-input')
    parser.add_argument('--history-projection-version', type=int, choices=(1,),
                        help='Bounded saved-history RPC projection; requires --rpc and --hold-restored-input')
    parser.add_argument('--engine-management', action='store_true',
                        help='Owned RPC engine controls only; requires --hold-restored-input, --backend ninfer|strata, --model and --server-mode connect; never submits a launch prompt')
    parser.add_argument('--codex-engine', choices=('native', 'http'),
                        help='Invocation-only Codex loop: native app-server or LiteTUI HTTP; overrides LITETUI_CODEX_ENGINE, then saved setting (default: http)')
    parser.add_argument('--base-url', '--host', dest='base_url', help='Explicit server URL; never fall back to another endpoint')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--server-mode', choices=['auto', 'connect', 'start'], default='auto', help='auto: existing backend behavior; connect: never spawn; start: explicitly start server')
    mode.add_argument('--start-server', dest='server_mode', action='store_const', const='start')
    parser.add_argument('--load-model', action='store_true', help='Explicitly load --model (llama.cpp/LM Studio) before submitting a prompt')
    parser.add_argument('--context-length', type=int, help='Load/start context tokens; custom: declared client budget (server command must set actual context)')
    parser.add_argument('--max-tokens', type=int, help='Completion token limit for local/custom backends')
    parser.add_argument('--server-executable', help='llama-server or ninfer-serve executable')
    parser.add_argument('--model-path', help='GGUF file, NInfer artifact, Strata strata-<model>.json, or custom command {model_path}')
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
    # Resolve once at the CLI boundary: flag > environment > saved settings.
    # Both sources use the existing invocation overlay, never a persisted edit.
    codex_engine = args.codex_engine
    if codex_engine is None:
        codex_engine = os.environ.get('LITETUI_CODEX_ENGINE') or None
        if codex_engine is not None and codex_engine not in ('native', 'http'):
            raise ValueError('LITETUI_CODEX_ENGINE must be native or http')
    options = LaunchOptions(args.base_url, args.server_mode, args.load_model, args.context_length,
                            args.max_tokens, args.server_executable, args.model_path, command,
                            args.api_key_env, args.server_timeout, codex_engine,
                            getattr(args, 'engine_management', False),
                            getattr(args, 'history_projection_version', None),
                            getattr(args, 'work_lifecycle_version', None),
                            getattr(args, 'mcp_lifecycle_version', None),
                            getattr(args, 'reasoning_effort', None) is not None
                            or getattr(args, 'thinking_level', None) is not None)
    options.validate_history_projection()
    options.validate_work_lifecycle()
    options.validate_mcp_lifecycle()
    if options.work_lifecycle_version is not None:
        if not getattr(args, 'rpc', False) or not getattr(args, 'hold_restored_input', False):
            raise ValueError('--work-lifecycle-version requires --rpc and --hold-restored-input')
        if getattr(args, 'prompt', None) is not None:
            raise ValueError('--work-lifecycle-version cannot carry --prompt; submit after capture and activation')
    options.validate_engine_management(args.backend, getattr(args, 'model', None))
    if options.engine_management and getattr(args, 'prompt', None) is not None:
        raise ValueError('--engine-management cannot carry --prompt; submit explicitly after a verified connection')
    return options


def to_argv(options):
    """Use the same public arguments for supervised and external agents."""
    options.validate_history_projection()
    options.validate_work_lifecycle()
    options.validate_mcp_lifecycle()
    args = ['--server-mode', options.server_mode, '--server-timeout', str(options.timeout)]
    for name in ('base_url', 'context_length', 'max_tokens', 'server_executable', 'model_path', 'api_key_env', 'codex_engine', 'history_projection_version', 'work_lifecycle_version', 'mcp_lifecycle_version'):
        value = getattr(options, name)
        if value is not None:
            args += ['--' + name.replace('_', '-'), str(value)]
    if options.load_model:
        args.append('--load-model')
    if options.engine_management:
        args.append('--engine-management')
    if options.server_command is not None:
        args += ['--server-command', json.dumps(options.server_command)]
    return args


async def prepare(app):
    options = getattr(app, '_launch_options', None)
    if options is not None:
        options.validate_history_projection()
        options.validate_work_lifecycle()
        options.validate_mcp_lifecycle()
    if options is not None and getattr(options, 'engine_management', False):
        options.validate_engine_management(
            app.backend.name, getattr(app, '_cli_initial_model', None) or getattr(app, 'model_id', None))
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
    # Bind no-start authority to the backend itself so reconnect/recovery and
    # load-triggered preset rebuilds cannot bypass a launch-only health check.
    if backend.name == 'llamacpp' and (options.server_mode == 'connect' or (options.base_url and options.server_mode != 'start')):
        backend.require_existing_server()
    if getattr(app, '_launch_prepared', False):
        return await backend.ensure_running()
    if options.server_mode == 'start':
        if backend.name in ('ninfer', 'strata'):     # llm_backend.ENGINE_BACKENDS
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
