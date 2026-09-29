"""T0113 — parity guards: lists that are copies of each other, pinned so drift fails a test.

Both were "kept in sync" by a comment and nothing else.

(1) AGENT_SHELL_MARKERS here vs LiteSuite's AGENT_SHELL_ENV
    (packages/shared/src/agentShellEnv.ts, T1043/T1049). LiteSuite deletes those names from an owner
    terminal and from the Frontier chat's LiteTUI child; this side reads them as "an agent started
    me". A name added only here survives into Ryan's own seats and voids their owner mark; one added
    only there is stripped for nothing.
(2) spawn_agent.json's `tool_profile` enum vs tool_policy.PROFILE_NAMES (T1086 made them equal). A
    profile that exists but is not in the schema cannot be chosen by a spawning agent.

Style follows tests/test_deny_floor.py::test_the_copy_is_byte_identical_to_liteharness: the sibling
checkout is $LITESUITE_SRC, else the `LiteSuite` beside this repo or any parent of it, and only a
machine with no LiteSuite checkout at all skips. A checkout that HAS no such file FAILS.
"""
from __future__ import annotations

import json
import os
import re
import warnings
from pathlib import Path

import pytest

from litetui import seat_authority, tool_policy

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "src" / "litetui" / "schemas" / "spawn_agent.json"
SUITE_FILE = Path("packages") / "shared" / "src" / "agentShellEnv.ts"


def suite_checkout() -> Path | None:
    named = os.environ.get("LITESUITE_SRC")
    roots = ([Path(named)] if named else []) + [p / "LiteSuite" for p in ROOT.parents]
    for root in roots:
        if (root / "packages").is_dir():
            return root
    return None


def suite_shell_env(ts_source: str) -> list[str]:
    """The names in `export const AGENT_SHELL_ENV = [ ... ] as const`. Fails rather than returns nothing."""
    block = re.search(r"export const AGENT_SHELL_ENV\s*=\s*\[(.*?)\]\s*as const", ts_source, re.S)
    assert block, "no `export const AGENT_SHELL_ENV = [...] as const` in the LiteSuite file: the guard cannot read it"
    names = re.findall(r'"([^"]+)"', re.sub(r"//[^\n]*", "", block.group(1)))
    assert names, "AGENT_SHELL_ENV parsed as empty: a set compared to nothing proves nothing"
    return names


def marker_drift(suite: list[str], tui: tuple[str, ...]) -> str:
    """"" when the two lists name the same variables, else who has what the other lacks."""
    only_suite, only_tui = sorted(set(suite) - set(tui)), sorted(set(tui) - set(suite))
    if len(set(suite)) != len(suite):
        return f"LiteSuite lists a name twice: {suite}"
    return (f"only in LiteSuite AGENT_SHELL_ENV: {only_suite}; only in LiteTUI AGENT_SHELL_MARKERS: {only_tui}"
            if only_suite or only_tui else "")


def schema_profiles(schema: dict) -> list[str]:
    node = schema["function"]["parameters"]["properties"]["tool_profile"]
    assert node.get("enum"), "spawn_agent.json tool_profile has no enum: the guard has nothing to compare"
    return list(node["enum"])


# ── the detectors can fail ──────────────────────────────────────────────────

def test_the_marker_detector_reads_the_file_and_names_each_side():
    ts = 'export const AGENT_SHELL_ENV = [\n  "A", // why\n  "B",\n] as const;'
    assert suite_shell_env(ts) == ["A", "B"]
    assert marker_drift(["A", "B"], ("B", "A")) == ""
    assert marker_drift(["A", "B", "C"], ("A", "B")) == (
        "only in LiteSuite AGENT_SHELL_ENV: ['C']; only in LiteTUI AGENT_SHELL_MARKERS: []")
    assert "['D']" in marker_drift(["A"], ("A", "D"))
    assert "twice" in marker_drift(["A", "A"], ("A",))
    for unreadable in ("", "export const OTHER = [] as const", "export const AGENT_SHELL_ENV = [] as const"):
        with pytest.raises(AssertionError):
            suite_shell_env(unreadable)


def test_the_schema_reader_fails_on_a_missing_or_empty_enum():
    good = {"function": {"parameters": {"properties": {"tool_profile": {"enum": ["a", "b"]}}}}}
    assert schema_profiles(good) == ["a", "b"]
    with pytest.raises(AssertionError):
        schema_profiles({"function": {"parameters": {"properties": {"tool_profile": {"type": "string"}}}}})


# ── the guards ──────────────────────────────────────────────────────────────

def test_agent_shell_markers_match_litesuite():
    root = suite_checkout()
    if root is None:
        pytest.skip("no LiteSuite checkout found and LITESUITE_SRC unset")
    source = root / SUITE_FILE
    assert source.is_file(), (
        f"{source} does not exist: point LITESUITE_SRC at a checkout that has agentShellEnv.ts (T1043)")
    warnings.warn(f"AGENT_SHELL_MARKERS compared against {source}", stacklevel=1)
    drift = marker_drift(suite_shell_env(source.read_text(encoding="utf-8")), seat_authority.AGENT_SHELL_MARKERS)
    assert drift == "", f"{drift}. Change both together (seat_authority.py and {SUITE_FILE.as_posix()})."


def test_spawn_agent_schema_profiles_match_tool_policy():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema_profiles(schema) == list(tool_policy.PROFILE_NAMES), (
        "spawn_agent.json tool_profile enum and tool_policy.PROFILE_NAMES differ: a profile missing from the "
        "schema exists but cannot be chosen by a spawning agent")
