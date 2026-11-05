"""Actual owned scheduler/New/Work handoff with controlled provider and clock triggers."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import socket
import sys
import threading
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ['RUST_SCHEDULE_SOURCE']).resolve()
root = Path(os.environ['LITETUI_DATA_ROOT'])
case = os.environ['RUST_SCHEDULE_CASE']
recovery = case == 'recover-staged'
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE='scheduled-transition')
spec = importlib.util.spec_from_file_location('_scheduled_support', Path(__file__).with_name('history_runtime.py'))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
report: dict[str, Any] = {'commands': [], 'wire': [], 'requests': [], 'lease_release': [], 'epochs': [],
                          'bytecode_disabled': sys.dont_write_bytecode}
workers: list[Any] = []
streams: list[Any] = []
helpers: list[Any] = []
release_gate = asyncio.Event()


def delta(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=None), finish_reason=finish)], usage=None)


class Stream:
    closed = False

    def __aiter__(self):
        async def chunks():
            yield delta('CONTROLLED SCHEDULED ANSWER')
            await asyncio.sleep(0)
            yield delta(finish='stop')
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request['model'] == app.model_id
    if not request.get('stream'):
        return NS(choices=[NS(message=NS(content='Controlled scheduled conversation'))])
    assert not report['requests'], 'Scheduled invocation repeated its provider request'
    report['requests'].append(copy.deepcopy(request))
    stream = Stream()
    streams.append(stream)
    history.boundary.streams.append(stream)
    return stream


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        conversation_transition,
        gui_rpc,
        native_deferred,
        native_schedule,
        native_work,
        rpc,
        scheduler,
        store_identity,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    for name, module in (('app', app_module), ('background_input', background_input), ('native_schedule', native_schedule),
                         ('conversation_transition', conversation_transition), ('gui_rpc', gui_rpc), ('scheduler', scheduler),
                         ('native_deferred', native_deferred), ('native_work', native_work), ('rpc', rpc)):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source / ('src/litetui/' + name + '.py')
        report['source_' + name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app)
    report['launch_arguments'] = sys.argv[1:]
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    reader = None
    seeded = False
    original_path: Path | None = None
    epochs: list[Any] = []
    unrelated_origin = None
    human_done, scheduled_readback_done = asyncio.Event(), asyncio.Event()
    actual_dispatch, actual_emit, actual_gui = rpc._dispatch, rpc.rpc_emit, gui_rpc.async_dispatch
    actual_exit, actual_worker, actual_release = app.exit, LiteTUI.run_worker, ConversationRepository.release
    scheduler_now = datetime.now().replace(second=15, microsecond=0)  # noqa: DTZ005 - canonical scheduler uses local wall time
    report['controlled_scheduler_time'] = scheduler_now.isoformat(timespec='seconds')

    class SchedulerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return scheduler_now if tz is None else scheduler_now.astimezone(tz)

    def fire_controlled():
        # offer imports datetime locally; execution uses app.datetime. Freeze only
        # scheduler wall time, leaving real monotonic native timeout/cleanup clocks.
        with patch('datetime.datetime', SchedulerClock):
            return app._fire_job(app.jobs[0], manual=False)

    def remember_epoch():
        value = background_input.bridge(app)
        if value is not None and all(value is not old for old in epochs):
            epochs.append(value)

    def collect():
        if not seeded:
            return
        assert original_path is not None
        remember_epoch()
        report['identity'] = background_input.identity(app)
        report['history'] = copy.deepcopy(app.conversation)
        report['durable'] = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1]
        report['original_path'] = str(original_path)
        report['original_after'] = original_path.read_text(encoding='utf-8')
        report['jobs_after'] = json.loads(scheduler.jobs_path(root).read_text(encoding='utf-8'))
        path = app.convo_dir / background_input.DEFERRED_FILENAME
        report['deferred'] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        report['questions'] = list(getattr(app, '_rpc_pending_asks', {}))
        report['approvals'] = list(getattr(app, '_approval_waiters', {}))
        report['authorities'] = list(getattr(app, '_approval_authority_records', {}))
        report['epochs'] = [{'instance': value.instance, 'identity': value.identity, 'active': value.active, 'closing': value.closing,
            'ledger': json.loads(value.path.read_text(encoding='utf-8')) if value.path.exists() else None,
            'producers': [{'phase': p.phase, 'worker_finished': p.worker is None or p.worker.is_finished,
                           'observer_done': p.observer is None or p.observer.done()} for p in value.producers.values()],
            'inputs': [{'ref': i.ref, 'state': i.state, 'admission': i.admission, 'receipt': i.receipt,
                        'worker_finished': i.worker is None or i.worker.is_finished,
                        'observer_done': i.observer is None or i.observer.done()} for i in value.inputs.values()]}
            for value in epochs]

    async def management(owner, command):
        op = command['type']
        ordinal = sum(c['type'] == op for c in report['commands'])
        human_race = case.startswith('human-new-')
        if human_race and op == 'gui.conversations.native_apply':
            assert command['action'] == 'new'
            assert native_schedule.enabled(app)
            fire_controlled()
            assert getattr(app, '_native_schedule_pending', None) is not None
            report['human_apply_before'] = background_input.identity(app)
            if case == 'human-new-preclose':
                app._agent_session.conversation_directory(command['target_session_id']).mkdir()
            try:
                if case == 'human-new-postclose':
                    def fail_stage(*args, **kwargs):
                        report['postclose_bridge_closing'] = background_input.bridge(app).closing
                        raise RuntimeError('Controlled human New failure after old Work closed')
                    with patch.object(app, '_stage_new_convo', fail_stage):
                        result = await actual_gui(owner, command)
                else:
                    result = await actual_gui(owner, command)
                report['human_apply_result'] = copy.deepcopy(result)
                report['human_apply_after'] = background_input.identity(app)
            finally:
                human_done.set()
            # Keep the human Apply response pending until the independently owned
            # old proposal has been declined and read back. This exposes whether
            # ScheduledHeld accidentally clears the pending human shutdown proof.
            await asyncio.wait_for(scheduled_readback_done.wait(), 30)
            remember_epoch()
            return result
        if human_race and op == 'gui.work.native_schedule_decline':
            await asyncio.wait_for(human_done.wait(), 30)
        phase = {'gui.work.native_schedule_accept': 'accept', 'gui.work.native_schedule_readback': 'readback',
                 'gui.work.native_capture': 'capture', 'gui.work.native_activate': 'activate'}.get(op)
        if case in {'mutate', 'manual-stale'} and phase == 'accept':
            app.jobs[0].label = 'Changed after native proposal'
            scheduler.save(app.jobs, root)
            report['expected_refusal_jobs'] = json.loads(scheduler.jobs_path(root).read_text(encoding='utf-8'))
        if case == 'timeout-readback' and phase == 'readback':
            (root / 'gate.json').write_text(json.dumps({'phase': phase, 'operation_id': command['id']}), encoding='utf-8')
            async def unrelated_traffic():
                while not release_gate.is_set():
                    # Invoke the actual source observation emitter. This fixture's
                    # controlled usage sample has no admission/receipt authority.
                    assert unrelated_origin is not None
                    app._rpc_emit({"type": "usage", "usage": {"context_tokens": 7}}, origin=unrelated_origin)
                    report['unrelated_usage_events'] = report.get('unrelated_usage_events', 0) + 1
                    await asyncio.sleep(0.05)
            helpers.append(asyncio.create_task(unrelated_traffic()))
            await release_gate.wait()
        if case == 'quit-' + str(phase) and (phase not in ('capture', 'activate') or ordinal == 2):
            (root / 'gate.json').write_text(json.dumps({'phase': phase, 'operation_id': command['id']}), encoding='utf-8')
            await release_gate.wait()
        result = await actual_gui(owner, command)
        if human_race and op == 'gui.work.native_schedule_readback':
            scheduled_readback_done.set()
        remember_epoch()
        return result

    def dispatch(owner, command):
        report['commands'].append(copy.deepcopy(command))
        with (root / 'scheduled-transition-commands.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(command) + '\n')
        if command['type'] == 'shutdown':
            release_gate.set()
        return actual_dispatch(owner, command)

    def emit(row):
        report['wire'].append(copy.deepcopy(row))
        with (root / 'scheduled-transition-wire.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row) + '\n')
        if row.get('type') == 'ready':
            assert row.get('work_lifecycle_version') == 1
            assert background_input.activation_pending(app)
        result = actual_emit(row)
        if row.get('type') == 'response' and any(c.get('id') == row.get('id') and c['type'] == 'shutdown' for c in report['commands']):
            report['shutdown'] = copy.deepcopy(row)
        if case == 'crash-staged' and row.get('type') == 'response' and row.get('result', {}).get('kind') == 'work_schedule_result' and row['result']['status'] == 'staged':
            # Exact source response is published, but readback/recapture cannot run.
            # This child deliberately has no normal cleanup claim; the OS reaps it.
            collect()
            report.update(expected_abnormal_exit=73, crash_stage='after-staged-response', denied=history.boundary.denied)
            (root / 'scheduled-transition-crash-report.json').write_text(json.dumps(report), encoding='utf-8')
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(73)
        return result

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release(owner, *args, **kwargs):
        if seeded:
            report['lease_release'].append({'path': str(owner.convo_path), 'chat_workers': [w.name for w in workers if w.group == 'chat' and not w.is_finished]})
        return actual_release(owner, *args, **kwargs)

    async def tick():
        while not (root / 'tick').exists() and not exited.is_set():
            await asyncio.sleep(0.01)
        if exited.is_set():
            return
        assert native_schedule.enabled(app)
        report['tick_return_type'] = type(fire_controlled()).__name__

    with ExitStack() as stack:
        stack.enter_context(patch.object(app_module, 'datetime', SchedulerClock))
        stack.enter_context(patch.object(scheduler, 'datetime', SchedulerClock))
        for owner, name, replacement in ((gui_rpc, 'async_dispatch', management), (rpc, '_dispatch', dispatch), (rpc, 'rpc_emit', emit),
                                        (app, 'exit', exit_app), (LiteTUI, 'run_worker', launched), (ConversationRepository, 'release', release)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'), (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ('memory.md', 'soul.md', 'handoff.md'):
                    (app._agent_session.memory_root / name).write_text('', encoding='utf-8')
                if recovery:
                    prior = json.loads((root / 'scheduled-transition-crash-report.json').read_text(encoding='utf-8'))
                    original_path = Path(prior['original_path'])
                    report.update(original_identity=prior['original_identity'], original_before=prior['original_before'],
                                  jobs_before=prior['jobs_after'], resumed_identity=background_input.identity(app))
                    assert app.convo_id == prior['identity']['session_id']
                    assert getattr(app, '_native_work_deferred', None) is None, 'Restart must not reconstruct a replay owner'
                    assert report['requests'] == []
                else:
                    app.conversation = [{'role': 'system', 'content': 'Scheduled fixture original system'},
                                        {'role': 'user', 'content': 'Keep original input'}, {'role': 'assistant', 'content': 'Keep original answer'}]
                    app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                    await history.settle(app)
                    original_path = app.convo_path
                    report['original_identity'] = background_input.identity(app)
                    from litetui.event_origin import EventOrigin
                    unrelated_origin = EventOrigin(session_id=app.convo_id)
                    report['original_before'] = original_path.read_text(encoding='utf-8')
                    job = scheduler.Job(prompt='CANONICAL SCHEDULED INPUT Δ', schedule='* * * * *', id='native-scheduled',
                                        new_conversation=True, tool_profile='strict')
                    if case.startswith('manual-'):
                        job.enabled = False
                    if case == 'loop':
                        job.kind, job.owner_convo_id, job.interval_minutes = 'loop', app.convo_id, 1
                        job.next_run_at = (datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds')  # noqa: DTZ005 - canonical scheduler uses local wall time
                    app.jobs[:] = [job]
                    scheduler.save(app.jobs, root)
                    report['job_before'] = asdict(job)
                    report['jobs_before'] = json.loads(scheduler.jobs_path(root).read_text(encoding='utf-8'))
                seeded = True
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name='scheduled-rpc-reader', daemon=True)
                reader.start()
                if not recovery and not case.startswith('manual-'):
                    helpers.append(asyncio.create_task(tick()))
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            release_gate.set()
            exited.set()
            if helpers:
                await asyncio.gather(*helpers, return_exceptions=True)
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{'name': w.name, 'group': w.group, 'finished': w.is_finished} for w in workers],
                          streams=[{'closed': s.closed} for s in streams], denied=history.boundary.denied,
                          reader_retired=reader is None or not reader.is_alive(), helper_retired=all(t.done() for t in helpers))
            (root / 'scheduled-transition-report.json').write_text(json.dumps(report), encoding='utf-8')
            assert report['reader_retired'], 'Scheduled fixture RPC reader did not retire'


history.completions = completions
history.serve = serve
history.prepare_hooks = lambda: None

if __name__ == '__main__':
    from litetui import plugins
    original_register = plugins.register_plugins

    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=('litetui.plugins.core_tools', 'litetui.plugins.self_compact', 'litetui.plugins.convo'), disabled=disabled)

    with patch.object(plugins, 'register_plugins', controlled_register):
        history.main()