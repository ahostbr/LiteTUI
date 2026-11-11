"""Reviewed monitor operations use private files and a controlled probe only."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import native_monitor as monitor
from litetui import native_monitor_files as files
from litetui import native_monitor_worker as worker
from litetui import native_runtime_controls as controls
from litetui.monitor.probe import FakeProbe

app, armed, switch_app = _app, _armed, _switch_app
pytestmark = pytest.mark.asyncio
_OWNED_WRITER = monitor.ttyguard.run_owned_writer
_REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def private_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(worker, 'ChromeProbe', lambda **_: FakeProbe())
    def controlled(command, *, cwd, **kwargs):
        worker.run(cwd)
        return 0
    monkeypatch.setattr(monitor.ttyguard, 'run_owned_writer', controlled)
    return workspace


def cmd(app, action, **fields):
    bridge = work.bridge(app)
    return {'version': 1, **bridge.identity, 'work_instance': bridge.instance,
            'id': 'monitor-' + action, 'type': monitor.PREFIX + action, **fields}


async def review(app, monkeypatch, arguments):
    snapshot = await published_rpc(app, monkeypatch, cmd(app, 'snapshot', arguments=arguments))
    assert not monitor.blocking(app)
    result = await published_rpc(app, monkeypatch, cmd(app, 'review', arguments=arguments,
        revision=snapshot['snapshot']['revision']))
    assert not monitor.blocking(app)
    return result


async def applied(app, monkeypatch, arguments):
    reviewed = await review(app, monkeypatch, arguments)
    result = await published_rpc(app, monkeypatch, cmd(app, 'apply', token=reviewed['token']))
    attempt = app._native_monitor
    await asyncio.wait_for(attempt.task, 4)
    return result, attempt


async def close(app, monkeypatch, result):
    return await published_rpc(app, monkeypatch, cmd(app, 'close', token=result['token'], apply_id=result['apply_id']))


async def test_targets_snapshot_and_default_route_never_start(armed, monkeypatch, private_workspace):
    app, _ = armed
    path = private_workspace / 'monitor-targets.json'
    path.write_text(json.dumps({'targets': [{'name': 'one', 'url': 'https://name:password@example.test/a?token=secret#frag', 'screenshot': False}]}))
    for arguments in ('targets', ''):
        result = await published_rpc(app, monkeypatch, cmd(app, 'snapshot', arguments=arguments))
        assert result['snapshot']['targets'][0]['url'] == 'https://example.test/a?[redacted]'
        assert not monitor.blocking(app)
        assert not (private_workspace / 'monitor-log.jsonl').exists()
        assert 'password' not in json.dumps(result) and 'secret' not in json.dumps(result)


async def test_init_exact_seed_and_no_overwrite(armed, monkeypatch, private_workspace):
    app, _ = armed
    result, attempt = await applied(app, monkeypatch, 'init --config "nested/config with spaces.json"')
    assert attempt.status == 'completed'
    target = private_workspace / 'nested/config with spaces.json'
    assert target.read_bytes() == attempt.snapshot['seed'].encode()
    assert controls.mutation_hold(app)
    read = await published_rpc(app, monkeypatch, cmd(app, 'readback', token=result['token'], apply_id=result['apply_id']))
    assert read['cleanup_verified'] and read['artifacts'][0]['sha256'] == files.digest(target.read_bytes())
    folder = attempt.directory
    await close(app, monkeypatch, result)
    assert not monitor.blocking(app) and not folder.exists()
    snapshot = await published_rpc(app, monkeypatch, cmd(app, 'snapshot', arguments='init --config "nested/config with spaces.json"'))
    assert not snapshot['snapshot']['allowed']


async def test_demo_preserves_log_prefix_ignores_unrelated_config(armed, monkeypatch, private_workspace):
    app, _ = armed
    (private_workspace / 'monitor-targets.json').write_text('not a config')
    log = private_workspace / 'monitor-log.jsonl'
    log.write_bytes(b'{"old":"preserved"}\n')
    result, attempt = await applied(app, monkeypatch, 'demo')
    assert attempt.status == 'completed'
    assert log.read_bytes().startswith(b'{"old":"preserved"}\n')
    assert [row['ok'] for row in attempt.rows] == [True, False]
    await close(app, monkeypatch, result)


async def test_sweep_redacts_credentials_in_persisted_metrics(armed, monkeypatch, private_workspace):
    app, _ = armed
    (private_workspace / 'monitor-targets.json').write_text(json.dumps({'targets': [
        {'name': 'one', 'url': 'https://name:password@example.test/a?token=secret', 'screenshot': False}]}))
    result, attempt = await applied(app, monkeypatch, '')
    assert attempt.status == 'completed'
    data = (private_workspace / 'monitor-log.jsonl').read_bytes()
    assert b'password' not in data and b'secret' not in data and b'[redacted]' in data
    await close(app, monkeypatch, result)


@pytest.mark.parametrize('when', ['before-apply', 'during-helper'])
async def test_changed_output_is_never_overwritten(armed, monkeypatch, private_workspace, when):
    app, _ = armed
    result = await review(app, monkeypatch, 'demo')
    log = private_workspace / 'monitor-log.jsonl'
    if when == 'before-apply':
        log.write_bytes(b'external change')
        with pytest.raises(AssertionError, match='changed'):
            await published_rpc(app, monkeypatch, cmd(app, 'apply', token=result['token']))
        assert not monitor.blocking(app)
    else:
        def changed(command, *, cwd, **kwargs):
            worker.run(cwd)
            log.write_bytes(b'external change')
            return 0
        monkeypatch.setattr(monitor.ttyguard, 'run_owned_writer', changed)
        result = await published_rpc(app, monkeypatch, cmd(app, 'apply', token=result['token']))
        await app._native_monitor.task
        assert app._native_monitor.status == 'failed'
        await close(app, monkeypatch, result)
    assert log.read_bytes() == b'external change'


async def test_substituted_seed_is_refused(armed, monkeypatch, private_workspace):
    app, _ = armed
    def forged(command, *, cwd, **kwargs):
        worker.run(cwd)
        (cwd / 'output-0').write_bytes(b'forged')
        return 0
    monkeypatch.setattr(monitor.ttyguard, 'run_owned_writer', forged)
    result, attempt = await applied(app, monkeypatch, 'init')
    assert attempt.status == 'failed' and not (private_workspace / 'monitor-targets.json').exists()
    await close(app, monkeypatch, result)


@pytest.mark.parametrize('closing', [False, True])
async def test_cancel_and_quit_join_contained_worker(armed, monkeypatch, private_workspace, closing):
    app, _ = armed
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    joined = []
    def waiting(command, *, cancel, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert cancel.wait(3)
        joined.append(True)
        raise InterruptedError('controlled cancellation')
    monkeypatch.setattr(monitor.ttyguard, 'run_owned_writer', waiting)
    reviewed = await review(app, monkeypatch, 'demo')
    result = await published_rpc(app, monkeypatch, cmd(app, 'apply', token=reviewed['token']))
    await asyncio.wait_for(entered.wait(), 2)
    attempt = app._native_monitor
    if closing:
        await work.settle_before_shutdown(app)
    else:
        await close(app, monkeypatch, result)
    assert joined == [True] and attempt.task.done()
    assert not monitor.blocking(app) and not attempt.directory.exists()
    assert not (private_workspace / 'monitor-log.jsonl').exists()


@pytest.mark.parametrize('name', ['../escape', 'CON', 'con.png', 'NUL', 'ends.'])
async def test_unsafe_target_paths_refused(armed, monkeypatch, private_workspace, name):
    app, _ = armed
    (private_workspace / 'monitor-targets.json').write_text(json.dumps({'targets': [{'name': name, 'url': 'https://example.test'}]}))
    with pytest.raises(ValueError, match='safe artifact'):
        files.snapshot(app, '')

async def test_close_publication_is_exact_and_foreign_copy_cannot_release(armed, monkeypatch):
    app, _ = armed
    result, _attempt = await applied(app, monkeypatch, 'demo')
    request = cmd(app, 'close', token=result['token'], apply_id=result['apply_id'])
    closed = await monitor.dispatch(app, request)
    with pytest.raises(ValueError, match='exact'):
        monitor.prepare_publication(app, request, dict(closed))
    async def foreign():
        return monitor.prepare_publication(app, request, closed)
    with pytest.raises(ValueError, match='exact'):
        await asyncio.create_task(foreign())
    assert monitor.blocking(app)
    ticket = monitor.prepare_publication(app, request, closed)
    monitor.finish_publication(app, ticket)
    assert not monitor.blocking(app)
    with pytest.raises(ValueError, match='consumed'):
        monitor.finish_publication(app, ticket)


async def test_lost_close_publication_retains_hold_until_joined_quit(armed, monkeypatch):
    app, _ = armed
    result, attempt = await applied(app, monkeypatch, 'demo')
    await monitor.dispatch(app, cmd(app, 'close', token=result['token'], apply_id=result['apply_id']))
    assert monitor.blocking(app) and controls.mutation_hold(app)
    assert not attempt.directory.exists()
    await work.settle_before_shutdown(app)
    assert not monitor.blocking(app)


async def test_independent_readback_refuses_changed_completed_output(armed, monkeypatch, private_workspace):
    app, _ = armed
    result, _attempt = await applied(app, monkeypatch, 'init')
    (private_workspace / 'monitor-targets.json').write_bytes(b'external replacement')
    with pytest.raises(ValueError, match='changed after publication'):
        await monitor.dispatch(app, cmd(app, 'readback', token=result['token'], apply_id=result['apply_id']))
    assert monitor.blocking(app)
    await work.settle_before_shutdown(app)
    assert not monitor.blocking(app)


@pytest.mark.parametrize('kind', ['sweep', 'cancel', 'quit'])
async def test_real_contained_fake_child_sweep_or_join(armed, monkeypatch, private_workspace, kind):
    import sys

    import psutil

    from litetui import ttyguard
    app, _ = armed
    # Run only our fixed device-free fixture under the real Windows job envelope.
    monkeypatch.setattr(ttyguard, "run_owned_writer", _OWNED_WRITER)
    fixture = Path(__file__).parent / 'fixtures/rust_monitor_worker.py'
    def command(directory):
        if kind != 'sweep':
            (directory / 'wait').touch()
        return [sys.executable, '-B', str(fixture), str(directory)]
    def contained(command, **kwargs):
        assert command[:3] == [sys.executable, "-B", str(fixture)]
        assert Path(command[3]).parent == app._agent_session.store.data_root / "monitor-runs"
        assert kwargs["creationflags"] & 0x4
        return _REAL_POPEN(command, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", contained)
    monkeypatch.setattr(monitor, '_command', command)
    (private_workspace / 'monitor-targets.json').write_text(json.dumps({'targets': [{'name': 'one', 'url': 'https://example.test'}]}))
    reviewed = await review(app, monkeypatch, '')
    result = await published_rpc(app, monkeypatch, cmd(app, 'apply', token=reviewed['token']))
    attempt = app._native_monitor
    for _ in range(200):
        if (attempt.directory / 'child.pid').exists():
            break
        if attempt.task.done():
            await attempt.task
            pytest.fail('Contained fixture ended before its readiness marker')
        await asyncio.sleep(.01)
    pid = int((attempt.directory / 'child.pid').read_text())
    if kind == 'sweep':
        await asyncio.wait_for(attempt.task, 5)
        assert attempt.status == 'completed'
        assert (private_workspace / 'monitor-shots/one.png').read_bytes() == b'controlled screenshot bytes'
        assert json.loads((private_workspace / 'monitor-baselines.json').read_text())['one'] == files.digest(b'controlled screenshot bytes')
        await close(app, monkeypatch, result)
    elif kind == 'cancel':
        await close(app, monkeypatch, result)
    else:
        await work.settle_before_shutdown(app)
    assert not psutil.pid_exists(pid) and attempt.task.done()
    assert not monitor.blocking(app) and not attempt.directory.exists()

@pytest.mark.parametrize('action', ['init', 'demo', 'sweep'])
async def test_export_exact_monitor_source_contract(armed, monkeypatch, private_workspace, action):
    import os
    app, _ = armed
    if action == 'sweep':
        (private_workspace / 'monitor-targets.json').write_text(json.dumps({'targets': [{'name': 'one', 'url': 'https://user:pass@example.test/path?secret=value', 'screenshot': False}]}))
    snapshot = await published_rpc(app, monkeypatch, cmd(app, 'snapshot', arguments=action))
    reviewed = await published_rpc(app, monkeypatch, cmd(app, 'review', arguments=action, revision=snapshot['snapshot']['revision']))
    result = await published_rpc(app, monkeypatch, cmd(app, 'apply', token=reviewed['token']))
    await app._native_monitor.task
    readback = await published_rpc(app, monkeypatch, cmd(app, 'readback', token=result['token'], apply_id=result['apply_id']))
    closed = await close(app, monkeypatch, result)
    assert readback['status'] == 'completed' and closed['cleanup_verified']
    if directory := os.environ.get('LITETUI_MONITOR_FIXTURES'):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / (action + '.json')).write_bytes(work.encoded({'snapshot': snapshot, 'review': reviewed,
            'apply': result, 'readback': readback, 'closed': closed}))

async def test_missing_screenshot_keeps_prior_image_and_baseline(armed, monkeypatch, private_workspace):
    app, _ = armed
    (private_workspace / 'monitor-targets.json').write_text(json.dumps({'targets': [{'name': 'one', 'url': 'https://example.test', 'screenshot': True}]}), encoding='utf-8')
    shots = private_workspace / 'monitor-shots'
    shots.mkdir()
    (shots / 'one.png').write_bytes(b'previous image')
    baseline = private_workspace / 'monitor-baselines.json'
    baseline.write_bytes(b'{"one":"previous baseline"}')
    result, attempt = await applied(app, monkeypatch, '')
    assert attempt.status == 'completed' and attempt.rows[0]['regression'] == 'none'
    assert (shots / 'one.png').read_bytes() == b'previous image'
    assert baseline.read_bytes() == b'{"one":"previous baseline"}'
    await close(app, monkeypatch, result)


@pytest.mark.parametrize('arguments', ['other', 'demo --config x', 'init --config', 'run extra', 'targets --config a --config b'])
async def test_invalid_route_arguments_are_refused(armed, arguments):
    app, _ = armed
    from test_rust_command_surface import raw_request

    from litetui import command_surface
    with pytest.raises(ValueError):
        command_surface.resolve(app, raw_request(app, command='/monitor ' + arguments))
    assert not monitor.blocking(app)


async def test_review_rejects_changed_registered_handler_and_policy(armed, monkeypatch):
    app, _ = armed
    reviewed = await review(app, monkeypatch, 'demo')
    app.tools_enabled = not app.tools_enabled
    with pytest.raises(ValueError, match='different registered runtime'):
        await monitor.dispatch(app, cmd(app, 'apply', token=reviewed['token']))
    assert not monitor.blocking(app)


async def test_registered_monitor_route_never_calls_source_sweep(armed, monkeypatch):
    app, _ = armed
    from test_rust_command_surface import raw_request

    from litetui import command_surface
    for arguments in ('', 'targets', 'init', 'demo', 'run', 'sweep --config "has spaces.json"'):
        route = command_surface.resolve(app, raw_request(app, command=('/monitor ' + arguments).rstrip()))
        assert route['native_action'] == 'monitor'
        assert route['arguments'] == arguments
        assert not monitor.blocking(app)
