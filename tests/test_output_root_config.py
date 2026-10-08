"""Portable card scratch grants require explicit machine-local configuration."""
import json
from pathlib import Path

import pytest

from litetui import worktree_scope


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("LITETUI_OUTPUT_SCRATCH_ROOTS", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    root = tmp_path / "scratch" / "T0335" / "Seat"
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /fixture/.git/worktrees/Seat\n", encoding="utf-8")
    config = Path.home() / ".litetui" / "output-roots.json"
    config.parent.mkdir(parents=True)
    return root, config


def test_unconfigured_grants_only_owned_tree(context):
    root, _ = context
    assert worktree_scope.output_context(root, None)[0] == [root]


def test_home_file_applies_to_plain_seat_and_not_unlisted_scratch(context):
    root, config = context
    config.write_text(json.dumps({"scratch_roots": [str(root.parent.parent)]}), encoding="utf-8")
    roots, _ = worktree_scope.output_context(root, None)
    assert roots == [root, root.parent]
    other = root.parents[2] / "unlisted" / ".scratch" / "T0335" / "Seat"
    other.mkdir(parents=True)
    (other / ".git").write_text("gitdir: /fixture/.git/worktrees/Other\n", encoding="utf-8")
    assert worktree_scope.output_context(other, None)[0] == [other]


def test_explicit_empty_env_disables_home_grant(context, monkeypatch):
    root, config = context
    config.write_text(json.dumps({"scratch_roots": [str(root.parent.parent)]}), encoding="utf-8")
    monkeypatch.setenv("LITETUI_OUTPUT_SCRATCH_ROOTS", "")
    assert worktree_scope.output_context(root, None)[0] == [root]


@pytest.mark.parametrize("body", ['{', '{"scratch_roots":["relative"]}', '{"scratch_roots":"bad"}', '[]'])
def test_invalid_file_fails_closed_visibly(context, body):
    root, config = context
    config.write_text(body, encoding="utf-8")
    with pytest.warns(UserWarning, match="Output scratch roots disabled"):
        assert worktree_scope.output_context(root, None)[0] == [root]
