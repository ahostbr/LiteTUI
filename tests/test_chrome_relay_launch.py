"""T693 — the persistent Chrome relay must be genuinely headless on Windows."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAYCTL = ROOT / "tools" / "chrome-bridge" / "relayctl.py"


def test_windows_relay_launch_is_headless_and_keeps_diagnostic_logs() -> None:
    """Guard the two halves of a background daemon launch.

    DETACHED_PROCESS is insufficient for a console-subsystem executable: Windows
    may give the child a new visible console. CREATE_NO_WINDOW prevents that.
    A windowless daemon must also retain stdout/stderr logs or startup failures
    disappear with the console.
    """
    tree = ast.parse(RELAYCTL.read_text(encoding="utf-8"), filename=str(RELAYCTL))
    start = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "start"
    )
    popen = next(
        node
        for node in ast.walk(start)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    )
    keywords = {keyword.arg: keyword.value for keyword in popen.keywords if keyword.arg}

    flags = keywords["creationflags"]
    assert isinstance(flags, ast.Name) and flags.id == "flags"
    assert "0x08000000" in ast.get_source_segment(RELAYCTL.read_text(encoding="utf-8"), start)
    assert "0x00000008" not in ast.get_source_segment(
        RELAYCTL.read_text(encoding="utf-8"), start
    )

    stdout = keywords["stdout"]
    stderr = keywords["stderr"]
    assert isinstance(stdout, ast.Name) and stdout.id == "out"
    assert isinstance(stderr, ast.Name) and stderr.id == "err"

    opened_logs = {
        node.targets[0].id: node.value.args[0].id
        for node in start.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "open"
        and node.value.args
        and isinstance(node.value.args[0], ast.Name)
    }
    assert opened_logs["out"] == "OUT_LOG"
    assert opened_logs["err"] == "ERR_LOG"
