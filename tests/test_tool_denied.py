"""prompts/tool-denied.md — the refusal text, and its floor.

The contract has two halves and they point in OPPOSITE directions on purpose:

  validate_tool_denied()  RAISES on a broken file.  Loud, at test time, where
                          a failure costs a red line and nothing else.
  tool_denied()           NEVER raises for the same faults.  The moment a
                          refusal is needed is the worst possible moment to
                          throw, so it falls back to a built-in constant.

A test that only checked the happy path would pass against a `tool_denied`
that simply called `load_prompt`, which is the implementation this replaced.
"""
from pathlib import Path

import pytest

from litetui import paths, textfmt
from litetui.textfmt import (
    TOOL_DENIED_FALLBACK,
    TOOL_DENIED_REQUIRED,
    tool_denied,
    validate_tool_denied,
)


def _render_all():
    return {
        k: tool_denied(k, name="shell", reason="a stated reason")
        for k in TOOL_DENIED_REQUIRED
    }


def test_the_shipped_file_is_well_formed():
    """The loud half. Fails the moment a heading or placeholder goes missing."""
    validate_tool_denied()


def test_every_refusal_names_the_tool_and_leaks_no_braces():
    for key, out in _render_all().items():
        assert "{" not in out and "}" not in out, f"{key} rendered a raw placeholder"
        if "name" in TOOL_DENIED_REQUIRED[key]:
            assert "shell" in out, f"{key} did not name the tool"
        assert out.strip(), f"{key} rendered empty"


def test_every_refusal_says_nothing_ran():
    """A refusal that does not say the tool did not run invites a retry."""
    for key, out in _render_all().items():
        if key == "unknown-tool":
            continue  # a typo'd name is an error, not a policy refusal
        assert "nothing ran" in out or "nothing changed" in out, key


def test_an_unknown_key_raises_because_that_is_a_programmer_error():
    with pytest.raises(KeyError):
        tool_denied("no-such-refusal")


def test_a_missing_file_falls_back_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PROMPTS_DIR", tmp_path)  # empty dir, no file
    out = tool_denied("by-user", name="shell")
    assert "shell" in out
    assert "{" not in out
    assert "denied by user" in out
    with pytest.raises(Exception):
        validate_tool_denied()  # ...while the loud half still complains


def test_a_section_that_lost_its_placeholder_is_not_used(tmp_path, monkeypatch):
    """THE DISCRIMINATING CASE.

    The file exists and the section exists, so a naive loader happily returns
    it — and the model receives a refusal that cannot say WHICH tool was
    refused. That is worse than the hardcoded string this replaced, so the
    section is rejected in favour of the fallback.
    """
    # EVERY section present — only `by-user` has lost its {name}. Without the
    # others the validator would trip on a missing SECTION first and this test
    # would pass for the wrong reason.
    body = []
    for k, req in TOOL_DENIED_REQUIRED.items():
        placeholders = "" if k == "by-user" else " ".join("{" + p + "}" for p in req)
        body.append(f"## {k}\n\nfine {placeholders}\n")
    (tmp_path / "tool-denied.md").write_text("\n".join(body), encoding="utf-8")
    monkeypatch.setattr(paths, "PROMPTS_DIR", tmp_path)

    out = tool_denied("by-user", name="shell")
    assert "shell" in out, "a section with no {name} was used anyway"
    assert out == TOOL_DENIED_FALLBACK["by-user"].replace("{name}", "shell")
    # ...and the sibling sections, which are fine, ARE still read from the file.
    assert tool_denied("unknown-tool", name="shell") == "fine shell"
    with pytest.raises(ValueError, match="placeholder"):
        validate_tool_denied()


def test_a_renamed_section_falls_back(tmp_path, monkeypatch):
    (tmp_path / "tool-denied.md").write_text(
        "## by_user\n\n[policy denied by user] {name}\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "PROMPTS_DIR", tmp_path)
    assert "shell" in tool_denied("by-user", name="shell")
    with pytest.raises(ValueError, match="missing section"):
        validate_tool_denied()


def test_an_edited_file_IS_actually_used(tmp_path, monkeypatch):
    """The negative control for every fallback test above.

    Without this, all of them would pass against a `tool_denied` that ignored
    the file entirely and always returned the constant.
    """
    (tmp_path / "tool-denied.md").write_text(
        "\n".join(
            f"## {k}\n\nEDITED {k} {' '.join('{' + p + '}' for p in req)}\n"
            for k, req in TOOL_DENIED_REQUIRED.items()
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "PROMPTS_DIR", tmp_path)
    out = tool_denied("by-user", name="shell")
    assert out == "EDITED by-user shell", out
    validate_tool_denied()  # an edited-but-valid file is not an error


def test_comments_in_the_file_do_not_reach_the_model():
    """The shipped file carries HTML comments telling the editor what not to
    break. None of that is instruction for the model."""
    for out in _render_all().values():
        assert "<!--" not in out and "-->" not in out
        assert "placeholders:" not in out
