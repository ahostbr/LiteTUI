"""Every shipped tool must actually reach the model's tool list.

WHY. `pccontrol` and `chrome` are gated on `SCRIPT.exists()`. Moving those
directories into tools/ left the constants pointing at the old paths, so
`exists()` went False and the tools were REMOVED FROM THE LIST — no error, no
warning, no log line. The model simply stopped having the capability, and the
only symptom would be it never using a tool it was never offered.

That is the same shape as a dead settings control, and it is worse here: a
capability with no pointer is indistinguishable from one that was never built.

The gate itself is right — offering a tool whose script is missing produces
confident calls that fail at the shell. What was missing is anything that
notices when the gate turns off for the wrong reason.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui import chrome_tool
from litetui import pccontrol_tool

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-tools-"))

#: Tools that must be offered on a healthy checkout, with the reason each exists.
EXPECTED = {
    "bash": "the baseline — if this is missing the harness itself is broken",
    "read": "file read",
    "write": "file write",
    "web_fetch": "network read",
    "chrome": "browser control (gated on SCRIPT.exists)",
    "ask_user_question": "asks the human — always offered, no precondition",
}

#: Tools deliberately WITHHELD from the immediate offer by 7b6640f — still
#: dispatchable, schemas sent only once the model loads them. `pccontrol`
#: moved here out of EXPECTED, where its absence read as "NOT offered to the
#: model" and pointed at a broken SCRIPT path instead of at the design.
DEFERRED = {
    "pccontrol": "desktop control — deferred, not missing",
}


def _app():
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    return a


@pytest.mark.asyncio
async def test_every_expected_tool_is_offered():
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        names = {t["function"]["name"] for t in a._all_tools()}
    missing = sorted(n for n in EXPECTED if n not in names)
    # The deferred set must be absent for the RIGHT reason: declared, not lost.
    from litetui.plugins import tool_search

    for n in DEFERRED:
        assert n in tool_search.DEFAULT_DEFERRED, (
            f"{n} is neither offered nor declared deferred — that is a lost tool"
        )
        assert n not in names, (
            f"{n} is in the immediate offer but still listed as deferred; "
            "DEFERRED here and DEFAULT_DEFERRED disagree"
        )
    assert missing == [], (
        "these tools are NOT offered to the model:\n  "
        + "\n  ".join(f"{n} — {EXPECTED[n]}" for n in missing)
        + "\n\nA SCRIPT path that no longer resolves removes a tool silently."
    )


@pytest.mark.asyncio
async def test_every_offered_tool_can_be_dispatched():
    """Offered but undispatchable is worse than absent: the model calls it and fails."""
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        names = [t["function"]["name"] for t in a._all_tools()]
        undispatchable = [n for n in names if a._dispatch_for(n) is None]
    assert undispatchable == [], f"offered with no dispatch: {undispatchable}"


@pytest.mark.parametrize(
    "mod,label", [(pccontrol_tool, "pccontrol"), (chrome_tool, "chrome")]
)
def test_the_gated_scripts_resolve(mod, label):
    """The gate's INPUT, checked directly, so a failure names the path."""
    assert mod.SCRIPT.exists(), (
        f"{label}: SCRIPT does not exist -> the tool is silently unregistered.\n"
        f"  points at: {mod.SCRIPT}\n"
        f"  moving that directory requires editing SCRIPT in {mod.__name__}.py"
    )


def test_the_gate_can_still_turn_off():
    """NEGATIVE CONTROL.

    If `SCRIPT.exists()` always returned True the tests above would pass on a
    checkout with the scripts deleted. This proves the gate discriminates.
    """
    assert not (pccontrol_tool.ROOT / "definitely-not-a-real-script.py").exists()


@pytest.mark.asyncio
async def test_ask_user_question_needs_no_precondition():
    """It renders inside this app, so unlike the browser tools it is always on.

    Asserted separately because it is the one whose absence would be easiest to
    misread as "the model chose not to ask".
    """
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        names = {t["function"]["name"] for t in a._all_tools()}
    assert "ask_user_question" in names
