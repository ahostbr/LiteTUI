"""Regressions demonstrated against the actual ab1d206 merge-check artifact.

Inherited/configured addopts ran unselected tests; a directory at the base path
was accepted as an existing test file. Both defects were fixed in 9c1591d.
These tests exercise the real CLI, inventory and collection plugin in scratch repos.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def git(root, *args):
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Gate Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    for name in ("tools/merge_gate.py", "tests/run_all.py", "tests/readiness_collection_gate.py"):
        shutil.copyfile(ROOT / name, root / name)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\nreview.json\n", encoding="utf-8", newline="\n")
    for name in ("test_autoscroll.py", "test_sidecar_jobs_t1082.py", "test_neighbor.py"):
        (root / "tests" / name).write_text("def test_ok():\n    assert True\n", encoding="utf-8", newline="\n")
    (root / "tests/test_unselected.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8", newline="\n")
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    base = commit(root)
    (root / "candidate.txt").write_text("candidate\n", encoding="utf-8", newline="\n")
    candidate = commit(root)
    return root, {
        "base": base, "candidate": candidate, "domain": "fixture production domain",
        "review": "fixture reviewer: selection reviewed", "neighbors": ["tests/test_neighbor.py"],
        "exceptions": ["unselected failing fixture intentionally not in this domain"],
    }


def run_gate(root, note):
    (root / "review.json").write_text(json.dumps(note), encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, "tools/merge_gate.py", "--base", note["base"],
         "--candidate", git(root, "rev-parse", "HEAD"), "--review-evidence", "review.json"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONUTF8": "1"},
    )


@pytest.mark.parametrize("source", ["environment", "configuration"])
def test_addopts_cannot_expand_selection(repository, monkeypatch, source):
    root, note = repository
    if source == "environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "tests")
    else:
        (root / "pytest.ini").write_text("[pytest]\naddopts = tests\n", encoding="utf-8", newline="\n")
        note["candidate"] = commit(root)
    result = run_gate(root, note)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert "test_unselected.py" not in result.stdout


def test_base_directory_does_not_count_as_existing_test_file(repository):
    root, note = repository
    path = root / "tests/test_old_dir.py"
    path.mkdir()
    (path / "placeholder").write_text("directory, not a test file", encoding="utf-8")
    note["base"] = commit(root)
    shutil.rmtree(path)
    path.write_text("def test_new():\n    assert True\n", encoding="utf-8", newline="\n")
    note["candidate"] = commit(root)
    note["neighbors"] = ["tests/test_old_dir.py"]
    result = run_gate(root, note)
    assert result.returncode == 1
    assert "existing regular test file at base" in result.stderr
    assert "PASS:" not in result.stdout
