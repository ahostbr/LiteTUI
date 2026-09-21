"""Fail-closed release evidence gate. Validates records, not observer authenticity.

Paths are relative to the manifest directory. A PASS is not a signature: human
approval provenance must still be reviewed by the release operator.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

REQUIRED = frozenset({'codex', 'lmstudio', 'llamacpp', 'ninfer', 'dual_instance',
    'settings', 'resource_admission', 'child_headless', 'child_headed', 'plugin_reload',
    'package', 'sandbox', 'startup_context'})
CRITICAL = frozenset({'dual_instance', 'resource_admission', 'plugin_reload'})


def validate(manifest, root):
    issues = []
    root = Path(root).resolve()
    if not isinstance(manifest, dict):
        return ['manifest must be an object']

    def evidence(value, label):
        if not isinstance(value, str) or not value:
            issues.append(f'{label}: missing evidence path')
            return None
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            issues.append(f'{label}: evidence outside manifest directory')
            return None
        if not path.is_file() or path.stat().st_size == 0:
            issues.append(f'{label}: evidence missing or empty: {value}')
            return None
        return path

    candidate = manifest.get('candidate')
    if not isinstance(candidate, dict):
        candidate = {}
        issues.append('candidate missing')
    commit, version, digest = (candidate.get(k) for k in ('commit', 'version', 'sha256'))
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        issues.append('candidate commit invalid')
    if not isinstance(version, str) or not version.strip():
        issues.append('candidate version missing')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        issues.append('candidate wheel sha256 invalid')
    wheel = evidence(candidate.get('wheel'), 'candidate wheel')
    if wheel and (wheel.suffix != '.whl' or hashlib.sha256(wheel.read_bytes()).hexdigest() != digest):
        issues.append('candidate wheel hash/type mismatch')
    rows = manifest.get('results', [])
    if not isinstance(rows, list):
        rows = []
        issues.append('results must be a list')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            issues.append('malformed criterion')
            continue
        key = row['id']
        if key in seen:
            issues.append(f'duplicate criterion: {key}')
        seen.add(key)
        if row.get('status') != 'PASS':
            issues.append(f'{key}: failed, blocked or missing outcome')
        if row.get('commit') != commit or row.get('version') != version or row.get('sha256') != digest:
            issues.append(f'{key}: candidate identity mismatch')
        evidence(row.get('evidence'), key)
        if not row.get('timestamp') or not row.get('conditions') or not row.get('command'):
            issues.append(f'{key}: incomplete execution provenance')
        if type(row.get('exit_code')) is not int or row['exit_code'] != 0:
            issues.append(f'{key}: successful exit code missing')
        if key in CRITICAL:
            passes = row.get('passes')
            if not isinstance(passes, list) or len(passes) < 3:
                issues.append(f'{key}: three consecutive pass records required')
            else:
                paths = set()
                for run in passes[-3:]:
                    if not isinstance(run, dict) or run.get('status') != 'PASS' or not run.get('timestamp'):
                        issues.append(f'{key}: invalid critical pass')
                        continue
                    path = evidence(run.get('evidence'), f'{key} pass')
                    if path in paths:
                        issues.append(f'{key}: duplicated pass evidence')
                    paths.add(path)
            evidence(row.get('negative_evidence'), f'{key} negative proof')
    for key in sorted(REQUIRED - seen):
        issues.append(f'{key}: required criterion missing')
    approval = manifest.get('approval')
    if not isinstance(approval, dict):
        approval = {}
    if (approval.get('observer') != 'Ryan' or approval.get('approved') is not True
            or approval.get('version') != version or approval.get('commit') != commit
            or approval.get('sha256') != digest or not approval.get('scenarios')
            or not approval.get('timestamp')):
        issues.append('candidate-specific Ryan approval missing or invalid')
    evidence(approval.get('evidence'), 'Ryan approval')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        issues = validate(manifest, args.manifest.parent)
    except (OSError, ValueError, TypeError) as exc:
        issues = [f'Manifest unreadable or malformed: {exc}']
    print(json.dumps({'status': 'BLOCKED' if issues else 'PASS', 'issues': issues}, indent=2))
    return 2 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
