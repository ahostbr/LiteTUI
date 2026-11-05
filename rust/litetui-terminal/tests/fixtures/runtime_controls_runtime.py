"""Controlled actual-source runtime policy controls; no external provider or OS execution."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ['RUST_RUNTIME_CONTROLS_SOURCE']).resolve()
root = Path(os.environ['LITETUI_DATA_ROOT'])
case = os.environ['RUST_RUNTIME_CONTROLS_CASE']
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE='runtime-controls')
spec = importlib.util.spec_from_file_location('_runtime_controls_boundaries', Path(__file__).with_name('history_runtime.py'))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
PREFIX = 'gui.runtime.native_'
streams: list[Any] = []
workers: list[Any] = []
helpers: list[Any] = []
release_tool = threading.Event()
release_publication = asyncio.Event()
report: dict[str, Any] = {'case': case, 'commands': [], 'wire': [], 'requests': [], 'summaries': [],
    'snapshots': [], 'reviews': [], 'results': [], 'readbacks': [], 'events': [], 'lease_release': []}
event_lock = threading.Lock()
real_popen = subprocess.Popen


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def record(kind, **data):
    with event_lock:
        row = {'sequence': len(report['events']) + 1, 'kind': kind, **copy.deepcopy(data)}
        report['events'].append(row)
    return row


def mark(stage, **data):
    row = record('stage', stage=stage, **data)
    path = root/'runtime-controls-stage.json'
    temporary = path.with_suffix('.pending.json')
    temporary.write_bytes(canonical(row))
    temporary.replace(path)
    (root/f'stage-{stage}.json').write_bytes(canonical(row))


async def until_file(name, timeout=30):
    async with asyncio.timeout(timeout):
        while not (root/name).exists():
            await asyncio.sleep(0.01)


def delta(content=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=tools), finish_reason=finish)], usage=None)


def tool_delta(index):
    return NS(index=index, id=f'runtime-tool-{index}', type='function',
              function=NS(name='runtime_fixture_tool', arguments=json.dumps({'ordinal': index})))


class Stream:
    def __init__(self, request, ordinal):
        self.request, self.ordinal, self.closed = copy.deepcopy(request), ordinal, False
        streams.append(self)
        history.boundary.streams.append(self)

    def __aiter__(self):
        async def chunks():
            if case.startswith('host-') and self.ordinal == 1:
                yield delta(tools=[tool_delta(0), tool_delta(1)])
                yield delta(finish='tool_calls')
            else:
                yield delta('CONTROLLED RUNTIME POLICY ANSWER')
                if (case in {'owner-finish-readback', 'stop-publication', 'quit-publication'} or case.startswith('midturn-')) and self.ordinal == 1:
                    mark('stream-active')
                    await until_file('release-stream')
                yield delta(finish='stop')
        return chunks()

    async def close(self):
        self.closed = True
        record('stream-closed', ordinal=self.ordinal)


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request['model'] == app.model_id
    if not request.get('stream'):
        report['summaries'].append(copy.deepcopy(request))
        return NS(choices=[NS(message=NS(content='Controlled runtime policy title'))])
    assert len(report['requests']) < 4, 'Unexpected provider replay in bounded control fixture'
    report['requests'].append(copy.deepcopy(request))
    record('provider-request', ordinal=len(report['requests']), profile=app._active_tool_profile,
           tools_enabled=app.tools_enabled, plan_mode=app._plan_mode)
    return Stream(request, len(report['requests']))


def controlled_tool(args):
    ordinal = args['ordinal']
    assert type(ordinal) is int and ordinal in (0, 1)
    record('tool-enter', ordinal=ordinal)
    if ordinal == 0 and case.startswith('host-'):
        mark('old-tool-running', ordinal=ordinal)
        deadline = time.monotonic() + 40
        while not release_tool.is_set() and not (root/'release-tool').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Controlled old tool release was not supplied')
            time.sleep(0.01)
    record('tool-exit', ordinal=ordinal)
    return f'CONTROLLED TOOL RESULT {ordinal}'


def identity(app):
    from litetui import background_input
    return background_input.identity(app)


def facts(app):
    from litetui import seat_authority
    return {'tools_enabled': app.tools_enabled, 'settings_profile': app.settings.tool_policy_profile,
        'chosen_profile': app.chosen_tool_profile, 'seat_profile': seat_authority.seat_profile(app),
        'effective_profile': app._active_tool_profile, 'requested_cli_profile': app._cli_tool_profile,
        'plan_mode': app._plan_mode}


def documents(app):
    paths = {'global': root/'settings.json', 'agent': app._agent_session.memory_root/'settings.json',
             'conversation': app.convo_dir/'settings.json', 'history': app.convo_path}
    return {key: {'path': str(path), 'sha256': digest(path.read_bytes()), 'text': path.read_text(encoding='utf-8')}
            if path.exists() else None for key, path in paths.items()}


def protected(app):
    return {'identity': identity(app), 'authority': asdict(app._agent_session.authority),
        'cwd': str(Path.cwd()), 'mcp_enabled': app.settings.mcp_enabled,
        'handles': [id(value) for value in (app._agent_session, app.store, app.backend, app.client, app.settings)]}


def seed(app):
    from litetui import store_identity
    app.conversation = [{'role': 'system', 'content': 'Runtime policy controls fixture'},
        {'role': 'user', 'content': 'Preserve earlier user input'},
        {'role': 'assistant', 'content': 'Preserve earlier answer'}]
    if case.startswith('playback-'):
        app.conversation.append({'role':'assistant','content':'Selected second answer with '+chr(96)+'inline code'+chr(96)+' and a final sentence.'})
        app.settings.tts_enabled = case != 'playback-disabled'
        app.settings.tts_engine = 'edge' if case == 'playback-edge' else 'pyttsx3'
        app.settings.tts_voice = 'Private fixture voice'
        app.settings.tts_edge_voice = 'Private edge voice'
        app.settings.tts_litesuite_first = case in ('playback-queued','playback-uncertain')
        app.settings.tts_timeout = 10
    app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
    app._materialise_convo()
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
    assert not app.store.persist_error


def seed_saved_target(app):
    from litetui import convo_settings, store_identity
    from litetui.conversation import ConversationRepository
    repository = ConversationRepository(agent_session=app._agent_session)
    repository.stage(str(uuid.uuid4()))
    repository.acquire()
    try:
        repository.pending = False
        authority = app._agent_session.authority
        repository.record_meta(app.model_id, authority.name, authority.agent_id)
        history_rows = [{'role': 'system', 'content': 'Controlled saved conversation'},
                        {'role': 'user', 'content': 'Saved target with strict authority'}]
        repository.record_snapshot(history_rows, store_identity=store_identity.capture(app), store_injected=True)
        assert not repository.persist_error
        saved = convo_settings.ConvoSettings(tool_policy_profile='strict')
        convo_settings.save(repository.convo_dir, saved, agent_session=app._agent_session)
        report['saved_target'] = {'session_id': repository.convo_id, 'profile': saved.tool_policy_profile}
    finally:
        repository.release()


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        gui_rpc,
        hook_host,
        native_runtime_controls,
        native_chat_pause,
        rpc,
        seat_authority,
        settings_runtime,
        tool_approval,
        tool_policy,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository
    modules = {'app': app_module, 'background_input': background_input, 'gui_rpc': gui_rpc, 'rpc': rpc,
        'native_runtime_controls': native_runtime_controls, 'native_chat_pause': native_chat_pause, 'hook_host': hook_host, 'seat_authority': seat_authority,
        'settings_runtime': settings_runtime, 'tool_approval': tool_approval, 'tool_policy': tool_policy}
    for name, module in modules.items():
        assert module.__file__ is not None and Path(module.__file__).resolve() == source/('src/litetui/'+name+'.py')
        report['source_'+name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and background_input.activation_pending(app)
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    seeded = False
    reader = None
    actual_gui, actual_dispatch, actual_emit = gui_rpc.async_dispatch, rpc._dispatch, rpc.rpc_emit
    actual_exit, actual_release, actual_worker = app.exit, ConversationRepository.release, LiteTUI.run_worker
    actual_authorize, actual_execute_tool, actual_hook_dispatch = app._authorize_action, app._execute_tool, hook_host.dispatch

    async def management(owner, command):
        operation = command['type']
        if operation == 'gui.voice.native_playback':
            result = await actual_gui(owner, command)
            report.setdefault('playback', []).append(copy.deepcopy(result))
            if command['action'] in ('start','close'):
                try:
                    await actual_gui(owner, {**command,'id':str(uuid.uuid4())})
                except ValueError:
                    report.setdefault('playback_replay_refused', []).append(command['action'])
                else:
                    raise AssertionError('Playback repeated a consumed operation')
            return result
        if operation == 'gui.voice.native_dictation':
            result = await actual_gui(owner, command)
            report.setdefault('dictation', []).append(copy.deepcopy(result))
            return result
        if operation.startswith('gui.prompt.native_pause_'):
            if (case == 'host-pause-readback-error' and operation.endswith('_readback')
                    and not report.get('pause_readback_error')):
                report['pause_readback_error'] = True
                raise ValueError('Controlled unavailable pause readback')
            result = await actual_gui(owner, command)
            record('chat-pause', result=result, requests=len(report['requests']))
            if result['stage'] == 'held':
                assert owner._chat_pause_waiter is not None
            return result
        if operation == 'gui.prompt.native_send_review' and case == 'midturn-interrupt-priority' and command['chord'] == 'alternate':
            try:
                await actual_gui(owner, {**command, 'type': 'gui.prompt.native_queue_review', 'id': str(uuid.uuid4())})
            except ValueError as exc:
                report['legacy_queue_refusal'] = str(exc)
            else:
                raise AssertionError('The older queue-only route allowed interruption')
            review = await actual_gui(owner, command)
            pending = owner._pending_input.pop(0)
            try:
                # Change only the disposable queue between review and apply. The captured
                # foreground is still running. A different ordered set must require a new review.
                try:
                    await actual_gui(owner, {**command, 'type': 'gui.prompt.native_send_apply',
                                            'id': str(uuid.uuid4()), 'review': review['review']})
                except ValueError as exc:
                    report['changed_order_refusal'] = str(exc)
                else:
                    raise AssertionError('An interrupt survived a changed pending queue')
                assert not owner._stop_requested
            finally:
                owner._pending_input.insert(0, pending)
        if operation in {'gui.prompt.native_queue_apply', 'gui.prompt.native_send_apply'} and case == 'midturn-normal':
            result = await actual_gui(owner, command)
            for label, updates in (
                ('replay', {}),
                ('wrong-policy', {'policy_epoch': command['policy_epoch'] + 1}),
                ('wrong-instance', {'work_instance': str(uuid.uuid4())}),
                ('wrong-operation', {'origin': {**command['origin'], 'operation_id': str(uuid.uuid4())}}),
                ('wrong-hook', {'origin': {**command['origin'], 'hook_id': str(uuid.uuid4())}}),
            ):
                try:
                    await actual_gui(owner, {**command, **updates, 'id': str(uuid.uuid4())})
                except ValueError as exc:
                    report.setdefault('queue_refusals', {})[label] = str(exc)
                else:
                    raise AssertionError(f'Accepted {label} queue Apply')
            return result
        if not operation.startswith(PREFIX):
            return await actual_gui(owner, command)
        before = documents(app)
        result = await actual_gui(owner, command)
        suffix = operation.removeprefix(PREFIX)
        report.setdefault({'tool_list': 'tool_lists', 'snapshot': 'snapshots', 'validate': 'reviews', 'apply': 'results', 'readback': 'readback_candidates'}[suffix], []).append(copy.deepcopy(result))
        record('control-result', operation=operation, result=result, runtime=facts(app))
        if suffix in {'tool_list', 'snapshot', 'validate'}:
            assert documents(app) == before, 'Runtime control inspection changed durable bytes'
        if suffix == 'apply':
            mark('setter-returned', action_id=result['action_id'], status=result['status'])
        if suffix == 'readback' and case in {'host-tool-selection-parked', 'host-next-tool-parked', 'host-stop-parked', 'host-quit-parked', 'host-stop-captured', 'host-quit-captured', 'owner-finish-readback'}:
            mark('readback-prepared', action_id=result['action_id'])
            async with asyncio.timeout(40):
                while not release_publication.is_set() and not (root/'release-publication').exists():
                    await asyncio.sleep(0.01)
        return result

    def dispatch(owner, command):
        report['commands'].append(copy.deepcopy(command))
        record('command', command=command)
        value = actual_dispatch(owner, command)
        if command['type'] in {'abort', 'shutdown'}:
            mark('command-'+command['type'], operation=command.get('id'))
            release_tool.set()
            (root/'release-stream').touch()
            (root/'release-authorization').touch()
        if command['type'] == 'shutdown':
            release_publication.set()
        return value

    def emit(row):
        report['wire'].append(copy.deepcopy(row))
        record('wire', frame=row)
        if row.get('type') == 'ready':
            assert background_input.activation_pending(app)
            report['startup_held'] = True
        if row.get('type') == 'response':
            if row.get('ok') and isinstance(row.get('result'), dict) and row['result'].get('kind') == 'runtime_controls_readback':
                report['readbacks'].append(copy.deepcopy(row['result']))
            command = next((c for c in report['commands'] if c.get('id') == row.get('id')), {})
            if command.get('type') == 'shutdown':
                report['shutdown'] = copy.deepcopy(row)
        return actual_emit(row)

    async def execute_tool(name, args, **kwargs):
        record('tool-attempt', name=name, args=args)
        if name == 'runtime_fixture_tool' and args.get('ordinal') == 1:
            mark('next-tool-attempt')
        return await actual_execute_tool(name, args, **kwargs)

    async def hooks(owner, event, payload, **kwargs):
        record('hook-dispatch', event=event, payload=payload, profile=kwargs.get('profile'), captured=kwargs.get('captured'))
        return await actual_hook_dispatch(owner, event, payload, **kwargs)

    async def authorize(name, args, policy, **kwargs):
        if case == 'host-old-authorize' and name == 'runtime_fixture_tool' and args.get('ordinal') == 0:
            mark('old-authorization-captured', profile=kwargs.get('profile'))
            await until_file('release-authorization')
        record('authorization-start', name=name, args=args, profile=kwargs.get('profile'),
               origin=background_input.current_origin(app).envelope_fields() if background_input.current_origin(app) else None)
        value = await actual_authorize(name, args, policy, **kwargs)
        record('authorization-result', name=name, args=args, profile=kwargs.get('profile'), result=value)
        return value

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if not seeded:
            return
        bridge = background_input.bridge(app)
        if case.startswith('playback-'):
            report['playback_retired'] = getattr(app, '_native_playback', None) is None
            report['playback_directories_removed'] = all(not Path(p).exists() for p in report.get('playback_directories', []))
        if case.startswith('dictation-'):
            report['dictation_retired'] = getattr(app, '_native_dictation', None) is None
            report['dictation_directories_removed'] = all(not Path(p).exists() for p in report.get('dictation_directories', []))
        report.update(after=protected(app), final_facts=facts(app), final_documents=documents(app),
            history=copy.deepcopy(app.conversation), durable=ConversationRepository.read(app.convo_path, max_bytes=8*1024*1024)[1] if app.convo_path.is_file() else [],
            questions=list(getattr(app, '_rpc_pending_asks', {})), approvals=list(getattr(app, '_approval_waiters', {})),
            authorities=list(getattr(app, '_approval_authority_records', {})))
        if bridge is not None:
            if case in {'midturn-quit', 'midturn-interrupt-quit'} and bridge.closing:
                reopened = background_input.Bridge(app)
                report['queue_recovered'] = reopened.recovered
                assert reopened.raw == bridge.raw and not reopened.active and not reopened.executions
            report['work'] = {'instance': bridge.instance, 'active': bridge.active, 'closing': bridge.closing,
                'producers': [{'ref': p.ref, 'phase': p.phase} for p in bridge.producers.values()],
                'inputs': [{'ref': i.ref, 'payload': i.payload, 'state': i.state, 'admission': i.admission, 'receipt': i.receipt}
                           for i in bridge.inputs.values()]}

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release(repository, *args, **kwargs):
        if seeded and repository is app.store:
            collect()
            active = [w.name for w in workers if not w.is_finished and (not case.startswith('identity-') or w.group == 'chat')]
            report['lease_release'].append({'active_workers': active, 'questions': report['questions'], 'approvals': report['approvals']})
            assert not active and not report['questions'] and not report['approvals']
        return actual_release(repository, *args, **kwargs)

    async def observe_pause_boundary():
        while not exited.is_set():
            if getattr(app, '_chat_pause_waiter', None) is not None:
                mark('chat-held', requests=len(report['requests']))
                return
            await asyncio.sleep(0.01)

    with ExitStack() as stack:
        if case.startswith('playback-'):
            from litetui import native_playback
            script = root / 'device_free_playback.py'
            remote = True if case == 'playback-queued' else False if case == 'playback-uncertain' else None
            delay = 8 if case in ('playback-stop','playback-quit') else 0.05
            child = f"import pathlib,time\ntime.sleep({delay})\npathlib.Path({str(root/'speech-completed')!r}).touch()\n"
            script.write_text(
                "import sys,json,pathlib\n"
                "from litetui import native_playback_worker as w\n"
                "v=w.voice_backend\n"
                "p=pathlib.Path(sys.argv[1])\n"
                f"(pathlib.Path({str(root/'speech-parameters.json')!r})).write_bytes((p/'parameters.json').read_bytes())\n"
                f"v.litesuite_tts.speak=lambda text:{remote!r}\n"
                "v.optional_python.resolve=lambda *modules:sys.executable\n"
                "v._speaker=lambda executable:(sys.executable,0)\n"
                f"v._SAPI_CHILD={child!r}\nv._EDGE_CHILD={child!r}\n"
                "w.run(p)\n", encoding='utf8')
            def command(directory):
                report.setdefault('playback_directories', []).append(str(directory))
                return [sys.executable, '-B', str(script), str(directory)]
            def popen(argv, **kwargs):
                assert argv[:3] == [sys.executable, '-B', str(script)]
                assert kwargs['cwd'] == Path(argv[3]) and kwargs['creationflags'] & 4
                return real_popen(argv, **kwargs)
            stack.enter_context(patch.object(native_playback, '_command', command))
            stack.enter_context(patch.object(subprocess, 'Popen', popen))
        if case.startswith('dictation-'):
            from litetui import native_dictation
            script = root / 'device_free_capture.py'
            script.write_text(
                "import pathlib,sys\n"
                "from litetui import native_dictation_worker as w, stt_backend\n"
                "p=pathlib.Path(sys.argv[1]);(p/'fixture-started').touch()\n"
                "class Recorder:\n"
                " def poll(self):return None\n"
                "stt_backend.missing=lambda:''\n"
                "def record(device,**kwargs):\n"
                " assert kwargs=={'directory':p,'duration_s':60}\n"
                " (p/'fixture-device').write_text(str(device),encoding='utf8');return Recorder()\n"
                "stt_backend.record_start=record\n"
                "stt_backend.record_stop=lambda proc:str(p/'fixture.wav')\n"
                "def transcribe(wav,model,*,local_only):\n"
                " assert local_only is True and model=='base.en' and wav==str(p/'fixture.wav')\n"
                + (" raise RuntimeError('Private unavailable model')\n" if case == 'dictation-failed'
                 else " return ''\n" if case == 'dictation-empty' else " return 'Fixture dictated words'\n") +

                "stt_backend.transcribe=transcribe\n"
                "w.run(p)\n",
                encoding='utf-8')
            def command(directory):
                report.setdefault('dictation_directories', []).append(str(directory))
                return [sys.executable, '-B', str(script), str(directory)]
            def popen(argv, **kwargs):
                assert argv[:3] == [sys.executable, '-B', str(script)]
                assert kwargs['cwd'] == Path(argv[3]) and kwargs['creationflags'] & 4
                return real_popen(argv, **kwargs)
            stack.enter_context(patch.object(native_dictation, '_command', command))
            stack.enter_context(patch.object(subprocess, 'Popen', popen))
        for owner, name, replacement in ((gui_rpc, 'async_dispatch', management), (rpc, '_dispatch', dispatch),
                (rpc, 'rpc_emit', emit), (app, '_authorize_action', authorize), (app, '_execute_tool', execute_tool),
                (hook_host, 'dispatch', hooks), (app, 'exit', exit_app),
                (ConversationRepository, 'release', release), (LiteTUI, 'run_worker', launched)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'), (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(145), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ('memory.md', 'soul.md', 'handoff.md'):
                    (app._agent_session.memory_root/name).write_text('', encoding='utf-8')
                seed(app)
                if case == 'identity-open':
                    seed_saved_target(app)
                await history.settle(app)
                seeded = True
                report.update(before=protected(app), initial_facts=facts(app), initial_documents=documents(app),
                              initial_history=copy.deepcopy(app.conversation), launch_arguments=sys.argv[1:])
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name='runtime-controls-rpc-reader', daemon=True)
                reader.start()
                if 'pause-' in case:
                    helpers.append(asyncio.create_task(observe_pause_boundary()))
                await asyncio.wait_for(exited.wait(), 125)
                collect()
        finally:
            release_tool.set()
            release_publication.set()
            (root/'release-stream').touch()
            if helpers:
                await asyncio.gather(*helpers, return_exceptions=True)
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{'name': w.name, 'group': w.group, 'finished': w.is_finished} for w in workers],
                streams=[{'closed': s.closed} for s in streams], denied=history.boundary.denied,
                reader_retired=reader is None or not reader.is_alive(), helper_retired=all(t.done() for t in helpers))
            (root/'runtime-controls-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            assert report['reader_retired'], 'Controlled runtime policy reader did not retire'


history.completions = completions
history.serve = serve

if __name__ == '__main__':
    from litetui import plugins, tool_policy
    original_register = plugins.register_plugins
    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        value = original_register(app, registry, order=('litetui.plugins.core_tools', 'litetui.plugins.convo', 'litetui.plugins.self_compact', 'litetui.plugins.misc'), disabled=disabled)
        registry.add_tool('runtime-controls-fixture', {'type': 'function', 'function': {'name': 'runtime_fixture_tool',
            'description': 'Controlled in-memory policy boundary fixture', 'parameters': {'type': 'object',
            'properties': {'ordinal': {'type': 'integer'}}, 'required': ['ordinal']}}}, controlled_tool,
            policy=tool_policy.ToolPolicy(frozenset({tool_policy.READ_ONLY}), 'Controlled fixture read-only callback'))
        return value
    with patch.object(plugins, 'register_plugins', controlled_register):
        history.main()
