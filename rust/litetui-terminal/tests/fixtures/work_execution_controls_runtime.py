"""Controlled actual-source F1 execution; no provider/device/external process access."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import socket
import sys
import threading
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ['RUST_EXECUTION_CONTROLS_SOURCE']).resolve()
root = Path(os.environ['LITETUI_DATA_ROOT'])
case = os.environ['RUST_EXECUTION_CONTROLS_CASE']
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE='execution-controls')
spec = importlib.util.spec_from_file_location('_execution_controls_boundaries', Path(__file__).with_name('history_runtime.py'))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
PREFIX = 'gui.work.native_execution_'
RECEIPT = 'native-work-execution.json'
streams: list[Any] = []
workers: list[Any] = []
helpers: list[Any] = []
release_stream = asyncio.Event()
turn_started = asyncio.Event()
apply_reply_release = asyncio.Event()
launch_readback_release = asyncio.Event()
report: dict[str, Any] = {'case': case, 'controlled_wall_time': '2026-11-04T09:15:20', 'monotonic_deadlines': 'real',
                          'commands': [], 'wire': [], 'requests': [], 'evaluations': [],
                          'summaries': [], 'reviews': [], 'results': [], 'readbacks': [], 'lease_release': []}


class Clock(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 4, 9, 15, 20)
        return value if tz is None else value.replace(tzinfo=tz)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def delta(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=None), finish_reason=finish)], usage=None)


class Stream:
    closed = False

    def __aiter__(self):
        async def chunks():
            if case in {'stop-admitted', 'quit-admitted', 'stop-overlap-admitted'}:
                (root/'execution-gate.json').write_text(json.dumps({'stage': 'provider-stream'}), encoding='utf-8')
            yield delta('CONTROLLED EXPLICIT WORK EVIDENCE')
            if case in {'stop-admitted', 'quit-admitted', 'stop-overlap-admitted'}:
                await release_stream.wait()
            await asyncio.sleep(0)
            yield delta(finish='stop')
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request['model'] == app.model_id
    if not request.get('stream'):
        if any(str(row.get('content', '')).startswith('You are a stateless completion judge.') for row in request.get('messages', [])):
            report['evaluations'].append(copy.deepcopy(request))
            # A canonical evaluator verdict prevents an unbounded continuation;
            # the actual evaluator still validates and persists the state.
            content = json.dumps({'verdict': 'met', 'reason': 'Controlled transcript evidence is present',
                                  'evidence': ['CONTROLLED EXPLICIT WORK EVIDENCE'], 'missing_evidence': [],
                                  'next_instruction': ''})
        else:
            report['summaries'].append(copy.deepcopy(request))
            content = 'Controlled completed Work title'
        return NS(choices=[NS(message=NS(content=content))])
    assert len(report['requests']) < (2 if case.endswith('same-minute') else 1), 'Unexpected execution replay'
    receipt = app.convo_dir / RECEIPT
    if case == 'automatic-generation-race':
        assert not receipt.exists(), 'Automatic source job must not invent an explicit launch receipt'
        report['automatic_without_execution_receipt'] = True
    else:
        assert receipt.exists(), 'Provider admission preceded immutable receipt persistence'
        raw = receipt.read_bytes()
        launch = json.loads(raw)
        assert canonical(launch) == raw and launch['identity']['session_id'] == app.convo_id
        report.setdefault('receipt_at_admission', []).append({'document': raw.decode('utf-8'), 'sha256': digest(raw)})
    report['requests'].append(copy.deepcopy(request))
    stream = Stream()
    streams.append(stream)
    history.boundary.streams.append(stream)
    return stream


def stable(app):
    files = [root/'settings.json', app.convo_dir/'settings.json', app._agent_session.memory_root/'settings.json']
    return {'documents': {str(p): p.read_bytes().hex() if p.exists() else None for p in files},
            'authority': asdict(app._agent_session.authority), 'settings': asdict(app.settings),
            'handles': [id(x) for x in (app.backend, app.client, app.store, app._agent_session, app.settings)]}


def saved(app):
    from litetui import scheduler
    paths = {'jobs': scheduler.jobs_path(root), 'goal': app.convo_dir/'goal.json', 'receipt': app.convo_dir/RECEIPT}
    return {key: {'text': p.read_text(encoding='utf-8'), 'sha256': digest(p.read_bytes())} if p.exists() else None
            for key, p in paths.items()}


def seed(app):
    from litetui import goal_loop, scheduler, store_identity
    app.conversation = [{'role': 'system', 'content': 'Execution controls fixture system'},
                        {'role': 'user', 'content': 'Preserve original input'}, {'role': 'assistant', 'content': 'Preserve original answer'}]
    app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
    app._materialise_convo()
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
    loop_case = case.startswith('loop-')
    row = asdict(scheduler.Job.loop(prompt='CANONICAL EXPLICIT LOOP INPUT', interval_minutes=17, owner_convo_id=app.convo_id)
                 if loop_case else scheduler.Job(prompt='CANONICAL EXPLICIT CRON INPUT', schedule='0 0 1 1 *'))
    row.update(id='selected', tool_profile='strict', enabled='disabled' not in case, run_count=7,
               last_fired_slot='2026-11-04T09:15' if case.endswith('same-minute') else None)
    if case == 'automatic-generation-race':
        row['schedule'] = '* * * * *'
    if loop_case:
        row['next_run_at'] = '2027-01-01T00:00:00'
    row['future_extension'] = {'types': [False, 0, 0.0], 'opaque': ['preserve selected']}
    sibling = asdict(scheduler.Job(id='sibling', prompt='Never run sibling', schedule='0 0 1 1 *', enabled=False, tool_profile='strict'))
    sibling['future_extension'] = {'opaque': ['retain sibling']}
    scheduler.jobs_path(root).write_bytes(canonical([row, sibling]))
    scheduler.refresh(app.jobs, root)
    if case in {'goal-replacement', 'goal-resume'}:
        goal = asdict(goal_loop.GoalState('Preserve reviewed old goal', id='goal-owned', status='paused',
                     evaluated_turns=4, max_turns=9, last_verdict='not_met', last_reason='Prior retained evidence',
                     missing_evidence=['old missing evidence'], no_progress_count=1, tool_profile='strict', started_by='typed'))
        goal['future_extension'] = {'types': [False, 0, 0.0], 'evidence': 'retain on resume'}
        (app.convo_dir/'goal.json').write_bytes(canonical(goal))


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        goal_loop,
        gui_rpc,
        hook_host,
        native_work,
        native_work_execution,
        row_store,
        rpc,
        scheduler,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    modules = {'app': app_module, 'background_input': background_input, 'native_work': native_work,
               'native_work_execution': native_work_execution, 'goal_loop': goal_loop, 'scheduler': scheduler,
               'row_store': row_store, 'gui_rpc': gui_rpc, 'rpc': rpc, 'hook_host': hook_host}
    for name, module in modules.items():
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source/('src/litetui/'+name+'.py')
        report['source_'+name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app) and not background_input.enabled(app)
    report.update(launch_arguments=sys.argv[1:], controlled_wall_clock=Clock.now().isoformat())
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    seeded = False
    reader = None
    actual_gui, actual_dispatch, actual_emit = gui_rpc.async_dispatch, rpc._dispatch, rpc.rpc_emit
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker
    actual_persist, actual_replace = scheduler.save, os.replace
    actual_authorize = app._authorize_action
    actual_execute = native_work_execution.execute

    async def management(owner, command):
        operation = command['type']
        if case == 'quit-capture' and operation == 'gui.work.native_capture':
            result = await actual_gui(owner, command)
            report['held_capture'] = copy.deepcopy(result)
            (root/'execution-gate.json').write_text(json.dumps({'stage': 'initial-capture-response'}), encoding='utf-8')
            await apply_reply_release.wait()
            return result
        if not operation.startswith(PREFIX):
            return await actual_gui(owner, command)
        protected, documents = stable(app), saved(app)
        if operation == PREFIX+'apply':
            report['apply_started'] = True
            if case == 'automatic-generation-race':
                assert native_work_execution._attempt(app) is None
                job = next(j for j in app.jobs if j.id == 'selected')
                worker = app._fire_job(job)  # Actual automatic entry, no manual override.
                assert worker is not None
                await asyncio.wait_for(worker.wait(), 15)
                async with asyncio.timeout(15):
                    while True:
                        value = background_input.bridge(app)
                        entries = list(value.inputs.values())
                        if len(entries) == 1 and entries[0].receipt is not None and entries[0].producer.phase == 'retired':
                            break
                        await asyncio.sleep(0.01)
                report['automatic_before_explicit'] = {'producer': copy.deepcopy(entries[0].producer.ref),
                    'input': copy.deepcopy(entries[0].ref), 'admission': copy.deepcopy(entries[0].admission),
                    'settlement': copy.deepcopy(entries[0].receipt)}
                assert native_work_execution._attempt(app) is None
            if case == 'row-drift':
                rows = json.loads(documents['jobs']['text'])
                rows[0]['future_extension']['types'][0] = 0
                scheduler.jobs_path(root).write_bytes(canonical(rows))
                report['injected_drift'] = saved(app)
            if case == 'peer-drift':
                rows = json.loads(documents['jobs']['text'])
                rows[1]['future_extension'] = {'concurrent': 'changed after review'}
                scheduler.jobs_path(root).write_bytes(canonical(rows))
                report['peer_edit'] = rows[1]
        if operation == PREFIX+'readback' and case == 'receipt-drift':
            receipt = app.convo_dir/RECEIPT
            receipt.write_bytes(receipt.read_bytes()+b' ')
            report['injected_receipt_drift'] = True
        result = await actual_gui(owner, command)
        assert stable(app) == protected, 'Execution changed settings, authority or owned runtime handles'
        if operation == PREFIX+'validate':
            assert saved(app) == documents, 'Execution review performed durable effects'
            assert result['payload_sha256'] == digest(canonical(result['payload']))
            report['reviews'].append(copy.deepcopy(result))
        elif operation == PREFIX+'apply':
            report['results'].append(copy.deepcopy(result))
            if case in {'stop-hook', 'quit-hook', 'stop-admitted', 'quit-admitted'}:
                await apply_reply_release.wait()
            if case in {'early-work', 'goal-fast-evaluation'} and result['status'] == 'published':
                await asyncio.wait_for(turn_started.wait(), 15)
                if case == 'goal-fast-evaluation':
                    async with asyncio.timeout(15):
                        while True:
                            current_goal = goal_loop.load_goal(app.convo_dir)
                            if report['evaluations'] and current_goal is not None and current_goal.status != 'active':
                                break
                            await asyncio.sleep(0.01)
                report['early_wire_count_before_apply_reply'] = len(report['wire'])
        elif operation == PREFIX+'readback':
            report['readbacks'].append(copy.deepcopy(result))
            if case in {'stop-overlap-stop-first', 'stop-overlap-admitted'} and command['scope'] == 'launch':
                (root/'execution-gate.json').write_text(json.dumps({'stage': 'launch-readback-held'}), encoding='utf-8')
                await launch_readback_release.wait()
        return result

    def dispatch(owner, command):
        report['commands'].append(copy.deepcopy(command))
        if command['type'] in {'shutdown', PREFIX+'stop'}:
            apply_reply_release.set()
        if command['type'] == 'shutdown':
            release_stream.set()
        value = actual_dispatch(owner, command)
        if command['type'] == 'abort' and case in {'stop-admitted', 'stop-overlap-admitted'}:
            assert app._stop_requested is True
            report['controlled_stream_released_after_actual_abort'] = True
            release_stream.set()
        return value

    def emit(row):
        report['wire'].append(copy.deepcopy(row))
        if row.get('type') == 'ready':
            assert row.get('work_lifecycle_version') == 1 and background_input.activation_pending(app)
            report['startup_held'] = True
        if row.get('type') == 'turn_start':
            turn_started.set()
        if row.get('type') == 'response':
            command: dict[str, Any] = next((c for c in report['commands'] if c.get('id') == row.get('id')), {})
            if command.get('type') == 'shutdown':
                report['shutdown'] = copy.deepcopy(row)
            if command.get('type') == 'gui.work.native_activate' and row.get('ok'):
                assert row['result']['execution_version'] == 1
        value = actual_emit(row)
        if case == 'stop-overlap-admitted' and row.get('type') == 'work_settled':
            assert not launch_readback_release.is_set()
            assert any(c['type'] == 'abort' for c in report['commands'])
            report['launch_gate_released_after_settlement'] = True
            launch_readback_release.set()
        if (row.get('type') == 'response' and row.get('ok') and row.get('result', {}).get('kind') == 'work_execution_readback'
                and row['result']['scope'] == 'stop' and case == 'stop-overlap-stop-first'):
            launch_readback_release.set()
        return value

    def persist(*args, **kwargs):
        value = actual_persist(*args, **kwargs)
        if case == 'save-then-raise' and report.get('apply_started'):
            report['injected_save_failure'] = True
            raise OSError('Controlled save completed then raised; no rollback')
        return value

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == RECEIPT and report.get('apply_started'):
            if case == 'receipt-failure':
                report['injected_receipt_failure'] = True
                raise OSError('Controlled receipt persistence failure before replace')
            value = actual_replace(src, dst, *args, **kwargs)
            if case == 'crash-receipt':
                collect()
                report.update(expected_abnormal_exit=74, crash_stage='after-receipt-replace-before-readback', denied=history.boundary.denied)
                (root/'work-execution-controls-crash-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(74)
            if case == 'receipt-save-then-raise':
                report['injected_receipt_failure'] = True
                raise OSError('Controlled receipt persistence failure after replace')
            return value
        return actual_replace(src, dst, *args, **kwargs)

    async def authorize(name, args, policy, **kwargs):
        if name.startswith('hook:execution-pre-admission:'):
            assert kwargs.get('allow_prompt') is False, 'Pre-turn execution hook gained interactive authority'
            report.setdefault('hook_calls', []).append({'name': name, 'profile': kwargs.get('profile'), 'allow_prompt': False})
            if case in {'stop-hook', 'quit-hook', 'stop-hook-after-readback'}:
                value = background_input.bridge(app)
                assert value is not None and len(value.inputs) == 1
                entry = next(iter(value.inputs.values()))
                assert entry.admission is None and entry.claim_task is asyncio.current_task()
                (root/'execution-gate.json').write_text(json.dumps({'stage': 'prompt-before', 'input': entry.ref}), encoding='utf-8')
                try:
                    await asyncio.Event().wait()
                finally:
                    report['hook_gate_retired'] = True
        return await actual_authorize(name, args, policy, **kwargs)

    async def execute(owner, attempt):
        if case in {'stop-before-producer', 'quit-before-producer'}:
            attempt.task = background_input._task()
            assert native_work_execution._attempt(owner) is attempt
            assert attempt.worker is not None and attempt.entry is None and not attempt.effect_attempted
            review = background_input.strict_json(attempt.review.wire)
            (root/'execution-gate.json').write_text(json.dumps({'stage': 'before-producer', 'action_id': review['action_id'],
                                                             'apply_operation_id': attempt.operation}), encoding='utf-8')
            try:
                await asyncio.Event().wait()
            finally:
                report['launch_gate_retired'] = True
        return await actual_execute(owner, attempt)

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if not seeded:
            return
        value = background_input.bridge(app)
        report.update(after=stable(app), final_saved=saved(app), identity=background_input.identity(app),
                      history=copy.deepcopy(app.conversation), durable=ConversationRepository.read(app.convo_path, max_bytes=8*1024*1024)[1],
                      questions=list(getattr(app, '_rpc_pending_asks', {})), approvals=list(getattr(app, '_approval_waiters', {})),
                      authorities=list(getattr(app, '_approval_authority_records', {})))
        if value is not None:
            report['lifecycle'] = {'instance': value.instance, 'active': value.active, 'closing': value.closing,
                'producers': [{'ref': p.ref, 'phase': p.phase, 'worker_finished': p.worker is None or p.worker.is_finished,
                               'observer_done': p.observer is None or p.observer.done()} for p in value.producers.values()],
                'inputs': [{'ref': i.ref, 'payload': i.payload, 'admission': i.admission, 'state': i.state,
                            'worker_finished': i.worker is None or i.worker.is_finished,
                            'observer_done': i.observer is None or i.observer.done(), 'receipt': i.receipt} for i in value.inputs.values()]}
            report['ledger'] = json.loads(value.path.read_bytes()) if value.path.exists() else None

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            live = [w.name for w in workers if w.group in {'chat', 'work-jobs', 'work-goals', 'work-admission', 'work-execution'} and not w.is_finished]
            report['lease_release'].append({'active_execution': live, 'questions': report['questions'], 'approvals': report['approvals']})
            assert not live and not report['questions'] and not report['approvals']
        return actual_release()

    with ExitStack() as stack:
        for clock_module in (app_module, scheduler, goal_loop):
            stack.enter_context(patch.object(clock_module, 'datetime', Clock))
        if hasattr(native_work_execution, 'datetime'):
            stack.enter_context(patch.object(native_work_execution, 'datetime', Clock))
        for owner, name, replacement in ((gui_rpc, 'async_dispatch', management), (rpc, '_dispatch', dispatch),
                (rpc, 'rpc_emit', emit), (app, 'exit', exit_app), (app.store, 'release', release),
                (LiteTUI, 'run_worker', launched), (scheduler, 'save', persist), (os, 'replace', replace),
                (app, '_authorize_action', authorize), (native_work_execution, 'execute', execute)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'), (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(130), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ('memory.md', 'soul.md', 'handoff.md'):
                    (app._agent_session.memory_root/name).write_text('', encoding='utf-8')
                if case == 'historical-restart':
                    prior = json.loads((root/'work-execution-controls-crash-report.json').read_text(encoding='utf-8'))
                    assert background_input.identity(app) == prior['identity']
                    assert saved(app) == prior['final_saved']
                    report['recovered_from'] = prior['final_saved']['receipt']['sha256']
                else:
                    seed(app)
                await history.settle(app)
                seeded = True
                report.update(before=stable(app), initial_saved=saved(app), initial_history=copy.deepcopy(app.conversation))
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name='execution-controls-rpc-reader', daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 110)
                collect()
        finally:
            release_stream.set()
            apply_reply_release.set()
            launch_readback_release.set()
            if helpers:
                await asyncio.gather(*helpers, return_exceptions=True)
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{'name': w.name, 'group': w.group, 'finished': w.is_finished} for w in workers],
                          streams=[{'closed': s.closed} for s in streams], denied=history.boundary.denied,
                          reader_retired=reader is None or not reader.is_alive(), helper_retired=all(t.done() for t in helpers))
            (root/'work-execution-controls-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            assert report['reader_retired'], 'Controlled execution RPC reader did not retire'


history.completions = completions
history.serve = serve
def prepare_hooks():
    if case not in {'stop-hook', 'quit-hook', 'hook-refused', 'stop-hook-after-readback', 'stop-hook-refused'}:
        return
    from litetui.lifecycle_hooks import Hook, HookConfig
    os.environ['LITETUI_HOOKS'] = 'on'
    hook = Hook('execution-pre-admission', ('prompt_before',), 'controlled-never-start-hook', mode='gate')
    (root/'hooks.json').write_bytes(canonical({'version': 1, 'hooks': [hook.document()]}))
    assert HookConfig(root/'hooks.json', Path.cwd()/'.litetui/hooks.json').read('global') == (hook,)


history.prepare_hooks = prepare_hooks

if __name__ == '__main__':
    from litetui import plugins
    original_register = plugins.register_plugins
    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        selected: tuple[str, ...] = ('litetui.plugins.core_tools', 'litetui.plugins.convo', 'litetui.plugins.self_compact')
        if case == 'goal-fast-evaluation':
            selected += ('litetui.plugins.goal_loop_plugin',)
        return original_register(app, registry, order=selected, disabled=disabled)
    with patch.object(plugins, 'register_plugins', controlled_register):
        history.main()