"""Tool schemas are FILES in tools/, and the set of files must equal the set of tools.

Ryan, 2026-08-22: "extract all the tool schemas out of the app and get them into
separated schema files per tool ... the tools folder is where it should live."

A tool schema is the contract the model reads on every single request. It was
buried across seven source files as nested dict literals, which made it the
hardest text in the project to edit and the easiest to describe wrongly
elsewhere — prompts/tools.md claimed FOUR tools for months while eleven were
offered, because prose was a second copy of a fact the schemas already carried.

🔴 THE GATE THAT MATTERS IS BIDIRECTIONAL, and a one-way version would be
worthless:

  a tool with no file  -> someone put a schema back into the source, and the
                          folder silently stopped being the source of truth
  a file with no tool  -> a schema nothing loads, which is a fact no code reads
                          and therefore a fact that rots

Checking only the first direction passes happily while tools/ fills with dead
files; checking only the second passes while the extraction quietly unwinds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import tool_schemas


def _registered() -> dict[str, dict]:
    """Every statically-defined tool spec the app can offer, by name.

    Deliberately NOT the app's live registry: gates (skills off, no PowerShell
    on PATH) mean the OFFERED set is a subset of the DEFINED set, and a gated
    tool still needs its schema on disk."""
    from litetui import app  # noqa: F401 — imports every plugin module as a side effect
    from litetui.ask_user_question import ASK_USER_QUESTION_TOOL_SPEC
    from litetui.chrome_tool import CHROME_TOOL_SPEC
    from litetui import file_tools as ft
    from litetui.harness import HARNESS_TOOL_SPEC
    from litetui.listen_tool import LISTEN_TOOL_SPEC
    from litetui.pccontrol_tool import PCCONTROL_TOOL_SPEC
    from litetui.plugins import core_tools as ct
    from litetui.plugins import subagent_plugin
    from litetui.plugins.view_image import VIEW_IMAGE_TOOL_SPEC
    from litetui.skills import SKILL_TOOL_SPEC
    from litetui.studio_tool import STUDIO_TOOL_SPEC

    specs = [
        ASK_USER_QUESTION_TOOL_SPEC, CHROME_TOOL_SPEC, HARNESS_TOOL_SPEC,
        LISTEN_TOOL_SPEC, PCCONTROL_TOOL_SPEC, VIEW_IMAGE_TOOL_SPEC,
        SKILL_TOOL_SPEC,
        STUDIO_TOOL_SPEC, ct.BASH_SPEC, ct.READ_SPEC, ct.WRITE_SPEC,
        ct.WEB_FETCH_SPEC, ct.powershell_spec(),
        ft.GREP_TOOL_SPEC, ft.EDIT_TOOL_SPEC,
        subagent_plugin.SPEC,
    ]
    return {s["function"]["name"]: s for s in specs}


# ── the bidirectional gate ──────────────────────────────────────────────────
def test_every_tool_has_a_schema_file() -> None:
    missing = set(_registered()) - tool_schemas.available()
    assert not missing, (
        f"these tools have no file in tools/ — a schema went back into the "
        f"source and the folder stopped being the source of truth: {sorted(missing)}"
    )


def test_every_schema_file_belongs_to_a_tool() -> None:
    orphans = tool_schemas.available() - set(_registered())
    assert not orphans, (
        f"these files in tools/ are loaded by nothing — a fact no code reads "
        f"is a fact that rots: {sorted(orphans)}"
    )


def test_no_tool_schema_is_still_an_inline_dict_literal() -> None:
    """The extraction, gated. A future edit that re-inlines one would keep both
    set tests green, because the constant would still exist and still match its
    file — right up until the two drift apart."""
    src = Path(__file__).resolve().parent.parent / "src" / "litetui"
    offenders = []
    for py in list(src.glob("*.py")) + list((src / "plugins").glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for const in ("_TOOL_SPEC = {", "_SPEC = {"):
            if const in text:
                offenders.append(f"{py.name} ({const.strip(' ={')})")
    assert not offenders, (
        f"a tool schema is inline again instead of in tools/: {offenders}"
    )


# ── the loader's contract ───────────────────────────────────────────────────
def test_a_schema_loads_as_a_valid_openai_function_spec() -> None:
    for name in sorted(tool_schemas.available()):
        spec = tool_schemas.load(name)
        assert spec.get("type") == "function", f"{name}: not a function spec"
        fn = spec.get("function", {})
        assert fn.get("name") == name, (
            f"{name}.json declares the name {fn.get('name')!r} — the file name "
            f"and the tool name must agree or lookups silently miss"
        )
        assert fn.get("description"), f"{name}: no description for the model to read"
        assert "parameters" in fn, f"{name}: no parameters block"


def test_a_missing_schema_raises_rather_than_returning_a_stub() -> None:
    """A tool whose schema cannot be read must not be offered to the model with
    a degraded description. A hard failure at registration is visible; a quietly
    wrong contract is the thing this module exists to prevent."""
    with pytest.raises(Exception):
        tool_schemas.load("definitely-not-a-tool")


# ── templating, which exists for exactly one field ──────────────────────────
def test_the_powershell_schema_is_templated_on_disk() -> None:
    """The file must stay machine-independent. If someone regenerates it on a
    box with pwsh and commits the resolved name, it is wrong everywhere else."""
    raw = tool_schemas.raw("powershell")
    assert "{exe}" in raw, "the placeholder was baked out — the file is now box-specific"


def test_the_loaded_powershell_spec_names_the_real_interpreter() -> None:
    from litetui.plugins import core_tools as ct
    exe = ct.powershell_exe()
    if exe is None:
        pytest.skip("no PowerShell on PATH")
    desc = ct.powershell_spec()["function"]["description"]
    assert exe in desc, f"the spec does not name {exe!r}"
    assert "{exe}" not in desc, "the placeholder reached the model unfilled"


def test_substitution_leaves_unknown_braces_alone() -> None:
    """str.format would raise KeyError on the first brace it did not recognise,
    and a JSON Schema may legitimately contain them."""
    node = {"a": "keep {this} and fill {exe}"}
    tool_schemas._fill(node, {"exe": "pwsh"})
    assert node["a"] == "keep {this} and fill pwsh"
