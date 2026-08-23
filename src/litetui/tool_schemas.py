"""Tool schemas live in `tools/`, one JSON file per tool.

Ryan, 2026-08-22: "extract all the tool schemas out of the app and get them into
separated schema files per tool ... the tools folder is where it should live."

WHY THIS IS NOT JUST TIDYING. A tool schema is the contract the model reads
every single request — it is the most-read text in the app and it was buried in
seven different source files as nested dict literals. That made it the hardest
thing in the project to edit and the easiest to describe wrongly somewhere else:
prompts/tools.md spent months claiming FOUR tools while eleven were offered,
because the prose was a SECOND COPY of a fact the schemas already carried. One
file per tool, read at registration, is the shape where that cannot recur.

TEMPLATING, and why it exists for exactly one field. `powershell`'s description
names the interpreter that was actually found — pwsh or powershell, whichever is
on PATH. Freezing that string into a file would make it a lie on any box with
the other one, which is the same drift this move exists to kill. So a schema may
contain `{placeholder}` and the caller fills it at registration. Everything not
templated is literal, and a `{` that is not a known placeholder is left alone
rather than raising, because JSON Schema legitimately contains braces.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from litetui import paths

SCHEMA_DIR_NAME = "tools"


def schema_dir() -> Path:
    return Path(paths.ROOT) / SCHEMA_DIR_NAME


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (schema_dir() / f"{name}.json").read_text(encoding="utf-8")


def load(name: str, **fmt: str) -> dict:
    """The schema for one tool, with any {placeholders} filled from `fmt`.

    Raises on a missing file rather than returning a stub: a tool whose schema
    cannot be read must not be silently offered to the model with a degraded
    description. A hard failure at registration is visible; a quietly wrong
    contract is what this module exists to prevent.
    """
    spec = json.loads(_read(name))
    if fmt:
        _fill(spec, fmt)
    return spec


def _fill(node, fmt: dict[str, str]) -> None:
    """Substitute {placeholders} in every string, in place.

    str.format is deliberately NOT used: a JSON Schema may legitimately contain
    braces, and format() would raise KeyError on the first one it did not
    recognise. Replacing only the placeholders we were given leaves everything
    else untouched.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                node[k] = _sub(v, fmt)
            else:
                _fill(v, fmt)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _sub(v, fmt)
            else:
                _fill(v, fmt)


def _sub(text: str, fmt: dict[str, str]) -> str:
    for key, value in fmt.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def available() -> set[str]:
    """Every tool that has a schema file. Used by the drift gate, which
    asserts this set and the set of REGISTERED tools are the same in both
    directions — a file with no tool rots, and a tool with no file is a
    schema that went back into the source."""
    d = schema_dir()
    return {p.stem for p in d.glob("*.json")} if d.is_dir() else set()
