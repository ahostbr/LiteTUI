import importlib.util
from pathlib import Path
import pytest


def probe_module():
    path = Path(__file__).parents[1] / 'scripts' / 'resource_admission_probe.py'
    spec = importlib.util.spec_from_file_location('resource_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_requires_explicit_identifiers():
    probe = probe_module()
    with pytest.raises(SystemExit): probe.parser().parse_args([])
    args = probe.parser().parse_args(['--backend','lmstudio','--model','fixture','--context','8192','--endpoint','http://localhost:1234','--evidence-dir','output'])
    assert not args.live


def test_live_probe_cannot_bypass_missing_calibration(tmp_path):
    probe = probe_module()
    args = probe.parser().parse_args(['--backend','lmstudio','--model','fixture','--context','8192','--endpoint','http://localhost:1234','--evidence-dir',str(tmp_path),'--live'])
    assert probe.run(args)['status'] == 'BLOCKED'
    assert (tmp_path / 'resource-probe.json').exists()
