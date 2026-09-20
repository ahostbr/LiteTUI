"""Read-only host resource evidence. Live calibration is not yet implemented."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time
import urllib.request


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', required=True, choices=['lmstudio', 'llamacpp', 'ninfer', 'codex'])
    p.add_argument('--model', required=True)
    p.add_argument('--context', required=True, type=int)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--evidence-dir', required=True, type=Path)
    p.add_argument('--live', action='store_true')
    return p


def run(args):
    report = {'backend': args.backend, 'model': args.model, 'context': args.context,
              'endpoint': args.endpoint, 'timestamp': time.time(), 'status': 'BLOCKED',
              'live_requested': args.live, 'loads_attempted': 0, 'errors': []}
    if args.context <= 0:
        report['errors'].append('Context must be positive')
    elif args.live:
        report['errors'].append('Live loader and calibrated peak bounds not implemented; no load attempted')
    else:
        from litetui.resource_telemetry import host_snapshot
        report['capacity'] = asdict(host_snapshot())
        if args.backend == 'lmstudio':
            from litetui.resource_inventory import lmstudio_residency
            try:
                started = time.time()
                with urllib.request.urlopen(args.endpoint.rstrip('/') + '/api/v0/models', timeout=5) as response:
                    body = json.load(response)
                rows = body if isinstance(body, list) else body.get('data', body.get('models'))
                report['inventory'] = rows
                report['inventory_started'] = started
                report['resident'] = lmstudio_residency(rows, args.model)
                if time.time() - started > 5:
                    report['resident'] = None
                    report['errors'].append('Inventory stale')
            except Exception as exc:
                report['errors'].append(f'Inventory unavailable: {exc}')
        else:
            report['errors'].append('Backend residency adapter not implemented')
        report['errors'].append('Read-only capacity is not calibrated load admission')
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / 'resource-probe.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    result = run(parser().parse_args())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'PASS' else 2)
