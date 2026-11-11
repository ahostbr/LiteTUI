"""Registered monitor source under real RPC, with only the fixed fake child allowed."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('_monitor_boundaries', os.environ['RUST_CONTEXT_COMMON_FIXTURE'])
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
root = Path(os.environ['LITETUI_DATA_ROOT'])
source = Path(os.environ['RUST_RUNTIME_CONTROLS_SOURCE'])
real_popen = subprocess.Popen
original_seed = common.seed
original_serve = common.serve
attempts = []


def seed(app):
    from litetui.plugins import PluginContext, monitor_plugin
    original_seed(app)
    monitor_plugin.PLUGIN.register(PluginContext(app, app.plugins, 'monitor'))
    assert Path(app._hook_workspace).samefile(root.parent / 'workspace')


async def serve(app):
    from litetui import native_monitor
    assert Path(native_monitor.__file__).resolve() == source / 'src/litetui/native_monitor.py'
    script = source / 'tests/fixtures/rust_monitor_worker.py'
    def command(directory):
        attempts.append(str(directory))
        (root / 'monitor-current.json').write_text(json.dumps({'directory': str(directory), 'count': len(attempts)}), encoding='utf-8')
        if (root / 'monitor-wait').exists():
            (directory / 'wait').touch()
        return [sys.executable, '-B', str(script), str(directory)]
    def popen(argv, **kwargs):
        assert argv[:3] == [sys.executable, '-B', str(script)]
        assert Path(argv[3]).parent.samefile(root / 'monitor-runs')
        assert kwargs['cwd'] == Path(argv[3]) and kwargs['creationflags'] & 4
        return real_popen(argv, **kwargs)
    try:
        with patch.object(native_monitor, '_command', command), patch.object(subprocess, 'Popen', popen):
            await original_serve(app)
    finally:
        (root / 'monitor-after.json').write_text(json.dumps({'released': not native_monitor.blocking(app),
            'attempts': attempts, 'remaining': [p for p in attempts if Path(p).exists()]}), encoding='utf-8')


common.seed = seed
common.history.serve = serve
if __name__ == '__main__':
    common.history.main()
