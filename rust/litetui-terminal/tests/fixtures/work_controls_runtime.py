"""Actual owned durable controls with provider, device and external OS boundaries denied."""
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
from pathlib import Path
from typing import Any
from unittest.mock import patch

source = Path(os.environ['RUST_WORK_SOURCE']).resolve()
root = Path(os.environ['LITETUI_DATA_ROOT'])
case = os.environ.get('RUST_WORK_CASE', 'success')
spec = importlib.util.spec_from_file_location('_controls_inspection_support', Path(__file__).with_name('work_inspection_runtime.py'))
assert spec and spec.loader
support: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
history = support.history
report: dict[str, Any] = {'commands': [], 'responses': [], 'checks': [], 'wire': [], 'lease_release': [], 'readbacks': []}
workers: list[Any] = []
PREFIX = 'gui.work.native_'


def unchanged(app):
    paths = [root/'settings.json', app.convo_path, app.convo_dir/'settings.json', app._agent_session.memory_root/'settings.json']
    return {'documents': {str(p): p.read_bytes().hex() if p.exists() else None for p in paths},
            'authority': asdict(app._agent_session.authority), 'settings': asdict(app.settings),
            'pending': copy.deepcopy(app._pending_input), 'history': copy.deepcopy(app.conversation),
            'handles': [id(x) for x in (app.backend, app.client, app.store, app._agent_session, app.settings)]}


def saved(app):
    from litetui import scheduler
    job_path, goal_path = scheduler.jobs_path(root), app.convo_dir/'goal.json'
    return {'jobs': json.loads(job_path.read_text(encoding='utf-8')) if job_path.exists() else [],
            'goal': json.loads(goal_path.read_text(encoding='utf-8')) if goal_path.exists() else None}


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        goal_loop,
        gui_rpc,
        native_work,
        native_work_controls,
        row_store,
        rpc,
        scheduler,
        store_identity,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    modules = {'app': app_module, 'native_work': native_work, 'native_work_controls': native_work_controls,
               'scheduler': scheduler, 'row_store': row_store, 'goal_loop': goal_loop, 'gui_rpc': gui_rpc}
    for name, module in modules.items():
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source / ('src/litetui/'+name+'.py')
        report['source_'+name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app) and not background_input.enabled(app)
    report['launch_arguments'] = sys.argv[1:]
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    reader = None
    seeded = False
    actual_gui, actual_dispatch, actual_emit = gui_rpc.async_dispatch, rpc._dispatch, rpc.rpc_emit
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker

    async def management(owner, command):
        operation = command['type']
        if not operation.startswith(PREFIX):
            return await actual_gui(owner, command)
        before = unchanged(app)
        documents = saved(app)
        if operation in {PREFIX+'control_validate', PREFIX+'control_apply', PREFIX+'control_readback'}:
            assert background_input.enabled(app) and not app._chat_running()
        if operation == PREFIX+'control_apply' and command['action'] == 'job_update' and not report.get('peer_edit'):
            rows = documents['jobs']
            sibling = next(row for row in rows if row['id'] == 'sibling')
            sibling['future_extension'] = {'peer': ['changed after review', 7]}
            sibling['label'] = 'Concurrent sibling edit retained'
            scheduler.jobs_path(root).write_text(json.dumps(rows), encoding='utf-8')
            report['peer_edit'] = copy.deepcopy(sibling)
            documents = saved(app)
        if case == 'readback-drift' and operation == PREFIX+'control_readback':
            rows = documents['jobs']
            selected = next(row for row in rows if row['id'] == 'selected')
            selected['label'] = 'External edit before independent readback'
            scheduler.jobs_path(root).write_text(json.dumps(rows), encoding='utf-8')
            report['injected_readback_drift'] = True
        try:
            result = await actual_gui(owner, command)
            after = saved(app)
            assert unchanged(app) == before, 'Control changed transcript/settings/authority/queue/runtime handles'
            if operation in {PREFIX+'control_validate', PREFIX+'control_readback'}:
                assert after == documents, 'Review or readback changed durable configuration'
            if operation == PREFIX+'control_apply':
                assert result['status'] == 'applied' and result['mutated'] and not result['close_required']
                action, target = command['action'], command['target_id']
                if action.startswith('goal_'):
                    assert after['jobs'] == documents['jobs']
                    expected = None if action == 'goal_clear' else {**documents['goal'], 'status': 'paused'}
                    assert after['goal'] == expected
                else:
                    assert after['goal'] == documents['goal']
                    prior = {row['id']: row for row in documents['jobs']}
                    current = {row['id']: row for row in after['jobs']}
                    assert {k:v for k,v in current.items() if k != target} == {k:v for k,v in prior.items() if k != target}
                    if action == 'job_delete':
                        assert target not in current
                    elif action == 'job_create':
                        assert target not in prior and current[target]['run_count'] == 0
                        assert current[target]['last_fired_slot'] is None and not current[target]['enabled']
                    else:
                        row = current[target]
                        for field in ('id', 'run_count', 'last_fired_slot', 'tool_profile', 'future_extension'):
                            assert row.get(field) == prior[target].get(field)
                report['last_saved'] = copy.deepcopy(after)
            if operation == PREFIX+'control_readback':
                assert result['verified'] is True
                report['readbacks'].append(copy.deepcopy(result))
            report['checks'].append({'operation': operation, 'command_id': command['id'], 'unchanged': True,
                                     'before': documents, 'after': after})
            return result
        except Exception as exc:
            report.setdefault('refusals', []).append({'operation': operation, 'error': str(exc)})
            raise

    def dispatch(owner, command):
        report['commands'].append(copy.deepcopy(command))
        return actual_dispatch(owner, command)

    def emit(row):
        report['wire'].append(copy.deepcopy(row))
        if row.get('type') == 'ready':
            assert row.get('work_lifecycle_version') == 1 and background_input.activation_pending(app)
            report['startup_held'] = True
        if row.get('type') == 'response':
            report['responses'].append(copy.deepcopy(row))
            assert len(json.dumps(row).encode()) <= 1024*1024
        return actual_emit(row)

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if seeded:
            value = background_input.bridge(app)
            report.update(after=unchanged(app), final_saved=saved(app),
                          durable=ConversationRepository.read(app.convo_path, max_bytes=8*1024*1024)[1],
                          questions=list(getattr(app, '_rpc_pending_asks', {})), approvals=list(getattr(app, '_approval_waiters', {})),
                          authorities=list(getattr(app, '_approval_authority_records', {})),
                          producers=[] if value is None else list(value.producers), inputs=[] if value is None else list(value.inputs))

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            live = [w.name for w in workers if w.group in {'chat', 'work-jobs', 'work-admission'} and not w.is_finished]
            report['lease_release'].append({'active_execution': live, 'questions': report['questions'], 'approvals': report['approvals']})
            assert not live and not report['questions'] and not report['approvals']
        return actual_release()

    with ExitStack() as stack:
        for owner, name, replacement in ((gui_rpc, 'async_dispatch', management), (rpc, '_dispatch', dispatch),
                (rpc, 'rpc_emit', emit), (app, 'exit', exit_app), (app.store, 'release', release), (LiteTUI, 'run_worker', launched),
                (app, '_fire_job', history.boundary.forbidden)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'), (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ('memory.md', 'soul.md', 'handoff.md'):
                    (app._agent_session.memory_root/name).write_text('', encoding='utf-8')
                app.conversation = [{'role':'system','content':'Work controls fixture system'},
                                    {'role':'user','content':'Preserved prior history'}, {'role':'assistant','content':'Preserved answer'}]
                app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                selected = asdict(scheduler.Job(id='selected', prompt='Selected original prompt', schedule='0 9 * * 1-5',
                    label='Selected source row', enabled=False, run_count=7, last_fired_slot='2026-10-01T09:00'))
                selected['future_extension'] = {'opaque': ['keep', 1, True]}
                sibling = asdict(scheduler.Job(id='sibling', prompt='Sibling untouched', schedule='0 12 * * *', enabled=False))
                sibling['future_extension'] = {'peer': ['original']}
                loop = asdict(scheduler.Job.loop(prompt='Selected existing loop', interval_minutes=15, owner_convo_id=app.convo_id))
                loop.update(id='loop-owned', enabled=False)
                loop['future_extension'] = {'loop': 'retain'}
                scheduler.jobs_path(root).parent.mkdir(parents=True, exist_ok=True)
                scheduler.jobs_path(root).write_text(json.dumps([selected,sibling,loop]), encoding='utf-8')
                scheduler.refresh(app.jobs, root)
                goal_loop.save_goal(app.convo_dir, goal_loop.GoalState('Preserve goal evidence', id='goal-owned',
                    evaluated_turns=4, last_reason='Retained reason', missing_evidence=['still missing']))
                goal_path = app.convo_dir/'goal.json'
                goal = json.loads(goal_path.read_text(encoding='utf-8'))
                goal['future_extension'] = {'goal': ['retain']}
                goal_path.write_text(json.dumps(goal), encoding='utf-8')
                await history.settle(app)
                seeded = True
                report.update(before=unchanged(app), initial_saved=saved(app))
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name='work-controls-rpc-reader', daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{'name': w.name, 'group': w.group, 'finished': w.is_finished} for w in workers],
                          denied=history.boundary.denied, calls=history.boundary.calls,
                          provider_requests=history.requests, reader_retired=reader is None or not reader.is_alive())
            (root/'work-controls-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            assert report['reader_retired'], 'Controlled RPC reader did not retire'


history.completions = history.boundary.forbidden_async
history.serve = serve
history.prepare_hooks = lambda: None

if __name__ == '__main__':
    from litetui import plugins
    original_register = plugins.register_plugins
    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=('litetui.plugins.core_tools', 'litetui.plugins.convo', 'litetui.plugins.self_compact'), disabled=disabled)
    with patch.object(plugins, 'register_plugins', controlled_register):
        history.main()
