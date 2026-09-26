"""System-prompt placeholders are compiled before the model sees them."""
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.prompt_compiler import (
    PromptPlaceholderError,
    compile_prompt,
)


def test_both_supported_placeholder_forms_resolve(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    skills = tmp_path / "claude-cache"
    compiled = compile_prompt(
        "repo=<root> skills=${CLAUDE_SKILL_DIR}",
        root=root,
        skill_dir=skills,
    )
    assert compiled == f"repo={root} skills={skills}"


def test_text_without_placeholders_is_unchanged(tmp_path: Path) -> None:
    authored = "You are a helpful local assistant."
    assert compile_prompt(authored, root=tmp_path) == authored


def test_blank_user_name_omits_the_name_clause(tmp_path: Path) -> None:
    compiled = compile_prompt(
        "You are helpful.${USER_NAME_CLAUSE}", root=tmp_path, user_name=""
    )
    assert compiled == "You are helpful."


def test_user_name_adds_the_name_clause(tmp_path: Path) -> None:
    compiled = compile_prompt(
        "You are helpful.${USER_NAME_CLAUSE}", root=tmp_path, user_name="Ada"
    )
    assert compiled == "You are helpful. Your user's name is Ada."


def test_unknown_placeholder_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(PromptPlaceholderError, match="MISSING_PATH"):
        compile_prompt("bad=${MISSING_PATH}", root=tmp_path)


def test_composed_prompt_contains_no_unresolved_root_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    authored = tmp_path / "systemprompt.md"
    authored.write_text("source: <root>/src", encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_PROMPT_FILE", authored)
    app = app_mod.LiteTUI()
    composed = app._system_prompt_text()
    assert "<root>" not in composed
    assert f"{paths.ROOT}" in composed
