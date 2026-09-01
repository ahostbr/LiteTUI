"""Tool schemas live in ``litetui/schemas/``, one JSON file per tool.

Ryan, 2026-08-22: "extract all the tool schemas out of the app and get them into
separated schema files per tool ... the tools folder is where it should live."

WHY THIS IS NOT JUST TIDYING. A tool schema is the contract the model reads
every single request — it is the most-read text in the app and it was buried in
seven different source files as nested dict literals. That made it the hardest
thing in the project to edit and the easiest to describe wrongly somewhere else:
prompts/tools.md spent months claiming FOUR tools while eleven were offered,
because the prose was a SECOND COPY of a fact the schemas already carried. One
file per tool, read at registration, is the shape where that cannot recur.

🔴 T135 (2026-08-30): THE FILES MOVED INTO THE PACKAGE, and the reader stopped
counting directories. The old ``schema_dir()`` was ``paths.ROOT / "tools"`` —
the repo root, derived as two ``Path(__file__).parent`` hops above this module.
In a dev checkout that is true; in an installed wheel the package sits in
site-packages, so the same arithmetic lands OUTSIDE the install and first
launch from PyPI died with::

    FileNotFoundError ...\\Lib\\tools\\harness.json

Every pre-publish proof missed it because ``litetui --version`` takes a fast
path that skips exactly this import (cli.py defers app past the probe) — a
probe engineered around the heavy path cannot certify the heavy path. The gate
that now catches this class is tools/wheel_import_gate.py: build the wheel,
install it into a scratch venv, and run the HEAVY import there.

Resolution order, deliberately package-first: the shipped copy is what an
installed user gets, so it wins; the repo-root ``tools/`` layout remains as a
fallback for consumers that still write schemas there (and for a dev checkout
mid-move). Both locations are searched by ``available()`` too — a file with no
tool rots in EITHER home.

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
from importlib.resources import files as _pkg_files
from pathlib import Path

from litetui import paths

#: The package subfolder that ships the schemas (see schemas/__init__.py).
SCHEMA_PACKAGE = "litetui.schemas"
#: The legacy repo-root layout, kept as a fallback location only.
LEGACY_DIR_NAME = "tools"


def _package_dir():
    """The in-package schema folder as a Traversable, or None if absent."""
    try:
        return _pkg_files(SCHEMA_PACKAGE)
    except ModuleNotFoundError:
        return None


def _legacy_dir() -> Path:
    return Path(paths.ROOT) / LEGACY_DIR_NAME


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    """The stored text of one schema file, package-first, legacy second."""
    pkg = _package_dir()
    if pkg is not None:
        ref = pkg.joinpath(f"{name}.json")
        try:
            if ref.is_file():
                return ref.read_text(encoding="utf-8")
        except OSError:
            pass  # unreadable in-package copy — fall through to the legacy home
    legacy = _legacy_dir() / f"{name}.json"
    if legacy.is_file():
        return legacy.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"tool schema {name!r} not found in {SCHEMA_PACKAGE}/ or "
        f"{_legacy_dir()} — the wheel is missing its package data, or the "
        f"file was deleted from both homes"
    )


def raw(name: str) -> str:
    """The schema file's text AS STORED, placeholders unfilled.

    The drift gate uses this to prove powershell.json still carries ``{exe}``
    on disk — a check that must read the file, not the templated spec.
    """
    return _read(name)


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
    """Every tool that has a schema file, in EITHER home. Used by the drift
    gate, which asserts this set and the set of REGISTERED tools are the same
    in both directions — a file with no tool rots (in whichever folder it
    hides), and a tool with no file is a schema that went back into the source."""
    names: set[str] = set()
    pkg = _package_dir()
    if pkg is not None:
        try:
            for entry in pkg.iterdir():
                if entry.name.endswith(".json"):
                    names.add(entry.name[: -len(".json")])
        except OSError:
            pass
    legacy = _legacy_dir()
    if legacy.is_dir():
        names.update(p.stem for p in legacy.glob("*.json"))
    return names
