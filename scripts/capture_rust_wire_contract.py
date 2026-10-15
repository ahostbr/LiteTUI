"""Capture synthetic in-process RPC evidence; never launch LiteTUI or a provider.

Runs only the dedicated pytest module. A unique directory is provisioned BEFORE
pytest's checkout-root guard. Only a zero-exit pytest run with verified bundle
hashes receives completed.json; interrupted/failed runs remain diagnostic data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def complete_run(directory: Path, returncode: int) -> None:
    """Validate all captured case bytes before publishing the green run index."""
    if returncode != 0:
        raise ValueError(f"pytest exited {returncode}; evidence is incomplete")
    manifests = sorted(directory.glob("*.manifest.json"))
    if not manifests:
        raise ValueError("No wire cases captured; evidence is incomplete")
    hashes = {}
    for path in manifests:
        case = json.loads(path.read_text(encoding="utf-8"))
        name = path.name.removesuffix(".manifest.json")
        if case["run_id"] != directory.name or case["case"] != name:
            raise ValueError(f"Mismatched case/run identity: {path.name}")
        for suffix, key in (("input.json", "input_sha256"), ("output.jsonl", "output_sha256")):
            content = directory / f"{name}.{suffix}"
            digest = hashlib.sha256(content.read_bytes()).hexdigest()
            if digest != case[key]:
                raise ValueError(f"Capture hash mismatch: {content.name}")
            hashes[content.name] = digest
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    hashes["pytest.log"] = hashlib.sha256((directory / "pytest.log").read_bytes()).hexdigest()
    index = {"format_version": 1, "run_id": directory.name, "pytest_exit_code": returncode,
             "scope": "invoked pytest process exited zero; listed capture bytes validated",
             "coverage_limit": "consult pytest.log for execution/skip counts; this index does not independently prove collection completeness",
             "case_count": len(manifests), "file_sha256": hashes,
             "evidence_kind": "synthetic in-process; not a launched LiteTUI runtime"}
    pending = directory / "completed.json.tmp"
    pending.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8", newline="\n")
    pending.replace(directory / "completed.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/rust-tui-port/wire-captures")
    args = parser.parse_args()
    directory = args.output_root.resolve() / f"run-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1",
           "RUST_WIRE_EVIDENCE_DIR": str(directory)}
    # User/global pytest selection must not silently turn a full capture into a subset.
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "tests/test_rust_wire_contract.py", "-q"]
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=180, check=False)
    except subprocess.TimeoutExpired:
        (directory / "incomplete.txt").write_text("pytest timed out; no completed evidence index\n", encoding="utf-8")
        print(f"INCOMPLETE: {directory}", file=sys.stderr)
        return 1
    log = result.stdout + result.stderr
    (directory / "pytest.log").write_text(log, encoding="utf-8", newline="\n")
    encoding = sys.stdout.encoding or "utf-8"
    print(log.encode(encoding, errors="backslashreplace").decode(encoding), end="")
    try:
        complete_run(directory, result.returncode)
    except (ValueError, OSError, KeyError) as exc:
        print(f"INCOMPLETE: {directory}: {exc}", file=sys.stderr)
        return 1
    print(f"COMPLETED in-process capture: {directory / 'completed.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
