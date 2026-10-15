"""Evidence packaging cannot turn a partial run or changed bytes into green proof."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capture_rust_wire_contract import complete_run


def sample(directory):
    directory.mkdir()
    content = {"input.json": "[]\n", "output.jsonl": '{"type":"fixture_event"}\n'}
    manifest = {"run_id": directory.name, "case": "sample"}
    for suffix, text in content.items():
        (directory / f"sample.{suffix}").write_text(text, encoding="utf-8", newline="\n")
        key = "input_sha256" if suffix == "input.json" else "output_sha256"
        manifest[key] = hashlib.sha256(text.encode()).hexdigest()
    (directory / "sample.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "pytest.log").write_text("fixture runner log", encoding="utf-8")


def test_successful_run_index_binds_every_case_file_and_log(tmp_path):
    directory = tmp_path / "run-fixture"
    sample(directory)
    complete_run(directory, 0)
    index = json.loads((directory / "completed.json").read_text())
    assert index["case_count"] == 1 and index["pytest_exit_code"] == 0
    assert set(index["file_sha256"]) == {"sample.input.json", "sample.output.jsonl", "sample.manifest.json", "pytest.log"}
    for name, digest in index["file_sha256"].items():
        assert digest == hashlib.sha256((directory / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["pytest", "tampered", "wrong-run", "empty"])
def test_incomplete_or_mismatched_run_has_no_completion_index(tmp_path, failure):
    directory = tmp_path / "run-fixture"
    if failure == "empty":
        directory.mkdir()
    else:
        sample(directory)
    if failure == "tampered":
        (directory / "sample.output.jsonl").write_text("changed bytes", encoding="utf-8")
    if failure == "wrong-run":
        path = directory / "sample.manifest.json"
        manifest = json.loads(path.read_text())
        manifest["run_id"] = "another-run"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        complete_run(directory, 1 if failure == "pytest" else 0)
    assert not (directory / "completed.json").exists()
