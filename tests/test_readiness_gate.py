import importlib.util
from pathlib import Path
import pytest


def gate():
    path = Path(__file__).parents[1] / 'scripts' / 'readiness_gate.py'
    spec = importlib.util.spec_from_file_location('readiness_gate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_manifest_never_passes(tmp_path):
    issues = gate().validate({}, tmp_path)
    assert issues
    assert any('candidate' in issue for issue in issues)


def test_self_declared_green_without_evidence_never_passes(tmp_path):
    module = gate()
    manifest = {'candidate': {'commit': 'a' * 40, 'version': '1.0', 'wheel': 'missing.whl', 'sha256': 'b' * 64},
                'results': [{'id': key, 'status': 'PASS'} for key in module.REQUIRED],
                'approval': {'observer': 'Ryan', 'approved': True, 'version': '1.0'}}
    issues = module.validate(manifest, tmp_path)
    assert any('wheel' in issue for issue in issues)
    assert any('evidence' in issue for issue in issues)


def test_duplicate_results_are_rejected(tmp_path):
    module = gate()
    issues = module.validate({'results': [{'id': 'settings', 'status': 'PASS'}] * 2}, tmp_path)
    assert any('duplicate' in issue.lower() for issue in issues)


def test_path_escape_is_not_accepted_as_evidence(tmp_path):
    module = gate()
    outside = tmp_path.parent / 'outside-evidence.txt'
    outside.write_text('green')
    issues = module.validate({'results': [{'id': 'settings', 'status': 'PASS',
                            'evidence': '../outside-evidence.txt'}]}, tmp_path)
    assert any('outside' in issue.lower() for issue in issues)


def complete_manifest(tmp_path, module):
    import hashlib
    wheel = tmp_path / 'candidate.whl'
    wheel.write_bytes(b'fixture wheel, not an installable package')
    candidate = {'commit': 'a' * 40, 'version': '1.0', 'wheel': wheel.name,
                 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
    identity = {key: candidate[key] for key in ('commit', 'version', 'sha256')}
    def record(name):
        path = tmp_path / name
        path.write_text('fixture evidence')
        return name
    rows = []
    for key in sorted(module.REQUIRED):
        row = dict(identity, id=key, status='PASS', timestamp='2026-09-20T00:00:00Z',
                   conditions='fixture', command='fixture command', exit_code=0,
                   evidence=record(key + '.txt'))
        if key in module.CRITICAL:
            row['passes'] = [{'status': 'PASS', 'timestamp': f'2026-09-20T00:00:0{i}Z',
                              'evidence': record(f'{key}-{i}.txt')} for i in range(3)]
            row['negative_evidence'] = record(key + '-negative.txt')
        rows.append(row)
    approval = dict(identity, observer='Ryan', approved=True, scenarios=['fixture only'],
                    timestamp='2026-09-20T00:01:00Z', evidence=record('approval.txt'))
    return {'candidate': candidate, 'results': rows, 'approval': approval}


def test_complete_structural_fixture_passes_then_tampering_blocks(tmp_path):
    module = gate()
    manifest = complete_manifest(tmp_path, module)
    assert module.validate(manifest, tmp_path) == []
    (tmp_path / 'candidate.whl').write_bytes(b'tampered')
    assert any('hash' in issue for issue in module.validate(manifest, tmp_path))


def test_reused_critical_evidence_and_missing_approval_block(tmp_path):
    module = gate()
    manifest = complete_manifest(tmp_path, module)
    row = next(row for row in manifest['results'] if row['id'] in module.CRITICAL)
    row['passes'] = [row['passes'][0]] * 3
    manifest['approval']['approved'] = False
    issues = module.validate(manifest, tmp_path)
    assert any('duplicated pass' in issue for issue in issues)
    assert any('Ryan approval' in issue for issue in issues)
