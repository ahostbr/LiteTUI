"""Make the repo root importable from tests/.

The tests moved out of the repo root into tests/, and `import app` resolved only
by accident: `python -m pytest` from the root puts the CWD on sys.path. Run
pytest from anywhere else, or as a bare `pytest`, and every module here fails to
import.

This makes it explicit rather than dependent on how you happened to invoke it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"          # the modules moved; data stays at ROOT
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 🔴 THE SUITE MUST NOT TOUCH THE LIVE FLEET REGISTRY.
#
# Constructing LiteTUI registers a harness seat, and registration passes
# --takeover, which evicts the running instance's row to
# ~/.liteharness/.ghost_evicted_*. Running the tests therefore stole the
# name "LiteTUI" from Ryan's live app and left the roster naming a test
# process that had already exited.
#
# Set BEFORE any test imports app/harness, and never overwritten if the
# caller already chose a value.
os.environ.setdefault("LITETUI_NO_HARNESS", "1")

# The suite must not meet the first-boot ENGINE PICKER either: this box has
# both engines installed, so a fresh Settings() would push a modal over every
# pilot test. An env-pinned backend is product logic for "nothing to ask"
# (model_switch._activate checks source_of), and lmstudio is the historical
# default every existing test was written against. Tests OF the picker
# monkeypatch.delenv this.
os.environ.setdefault("LITETUI_BACKEND", "lmstudio")

# 🔴 THE SUITE MUST NOT WRITE THE LIVE settings.json EITHER.
#
# Measured 2026-08-20, with a backup and a restore: ONE test run reset Ryan's
# tool_iterations from 100 back to 48.
#
#   tests/test_no_implicit_model_load.py:124,144   a._on_settings_saved(Settings(...))
#   app.py:3452                                    settings_mod.save(new)   <- no root=
#   settings.settings_path()                       <repo>/settings.json     <- HIS file
#
# The tests build a fresh Settings(...) naming two fields; every OTHER field
# sits at its dataclass default, and settings.py:90 is `tool_iterations = 48`.
# That whole object lands on the live file.
#
# ⚠️ IT LOOKS INTERMITTENT, WHICH IS WHY IT SURVIVED. The running app holds the
# real values in memory and writes them back on its next save, healing the file
# — so the damage is only visible when the app RESTARTS between a test run and
# that next save. Exactly the sequence Ryan hit: set 100, test run wrote 48,
# restart loaded 48.
#
# 🔴 THE CALL IS INVISIBLE AT THE TEST SITE. Those tests never mention save();
# they call an app method that reaches it three frames down. Grepping the tests
# for `save(` finds nothing, which is why this is a choke-point guard and not a
# fix applied to two call sites — the next test to call an app method that
# persists would reintroduce it, and nobody would see it in review.
#
# Third instance of this class in this repo, all found the same day: the suite
# evicting the live fleet row (a1e8686), the suite polluting .convos (55001fa),
# and this. One missing rule, not three bugs: A TEST MUST NEVER WRITE A PATH THE
# RUNNING APP OWNS.
@pytest.fixture(autouse=True)
def _never_write_the_live_settings(tmp_path, monkeypatch):
    """Redirect the DEFAULT settings path to a per-test temp dir.

    An explicit `root=` is honoured untouched, so tests that already pass
    tmp_path (test_settings.py does, correctly) behave exactly as before. Only
    the root=None case — the live file — is redirected.
    """
    from litetui import settings as settings_mod

    real_settings_path = settings_mod.settings_path

    def redirected(root=None):
        if root is not None:
            return real_settings_path(root)
        return tmp_path / settings_mod.SETTINGS_FILENAME

    monkeypatch.setattr(settings_mod, "settings_path", redirected)


# 🔴 THE SUITE MUST NOT DIAL OUT. THIS WAS 4.0 SECONDS PER CONSTRUCTED APP.
#
# Measured 2026-09-03, tests/_probe_floor.py, 12 reps per arm:
#
#   noop (pytest overhead only)               ~0.0 ms
#   LiteTUI() constructed, never started    4316.7 ms
#   full `async with app.run_test()`        4364.2 ms
#
# So `run_test()` — the compositor boot and teardown everyone assumed was the
# cost — is 47.5 ms, ONE PERCENT. The 4.3 s is the constructor, and cProfile puts
# 4.038 s of it in one place:
#
#   app.py __init__ -> mcp_client.load -> connect -> start -> _post -> urlopen
#
# `.mcp.json` at the repo root declares `litesuite-tools` at
# http://localhost:7423/mcp. LiteSuite is usually DOWN while the suite runs, so
# every constructed app POSTs there and waits to be refused. Timed directly three
# times: 4.033 s / 4.051 s / 4.105 s, "[WinError 10061] the target machine
# actively refused it" — the Windows dual-stack localhost path (::1 then
# 127.0.0.1, with retries), not a timeout anyone chose.
#
# ⚠️ AND SLOWNESS IS THE SECOND PROBLEM. With 7423 down the manager connects
# nothing and `tool_specs()` carries no MCP tools; with LiteSuite UP the same
# constructor reaches a real server and the tool set is DIFFERENT — and
# test_tools_registered.py and test_tool_schemas.py assert on that set. The
# suite's answer would depend on whether another app happens to be running.
# That is the same family as the three incidents above, one axis over: a test
# must not depend on a service being up, and must not touch the network at all.
#
# 📌 CONFIGS ARE STILL READ. The replacement calls `reload_configs()` and stops
# short of `connect()`, so the manager still knows what is DECLARED — `describe()`
# and the /mcp dialog still have rows to show. A blanket no-op would have been
# easier and would have quietly changed what those surfaces see.
#
# Tests that are ABOUT mcp opt out with `pytestmark = pytest.mark.real_mcp_load`
# and drive `load()` against their own tmp configs.
@pytest.fixture(autouse=True)
def _never_dial_out_from_a_constructor(request, monkeypatch):
    if "real_mcp_load" in request.keywords:
        return
    from litetui import mcp_client

    def _no_servers(self) -> dict:
        # 🔴 THE TEST WORLD DECLARES NO MCP SERVERS, and that is a CORRECTION to
        # the reasoning in 561e1d8. That commit kept `reload_configs()` so the
        # /mcp dialog would still have rows — but every test that reads
        # `mcp.configs` or `describe()` is one of the four MARKED files, and each
        # builds its own configs under tmp_path. Nothing unmarked needs the
        # repo's real .mcp.json, and depending on it means the suite's inputs
        # change the day somebody adds a server to that file.
        #
        # It also removes a mount-time side effect that reached two unrelated
        # tests: with configs present, T239's boot worker runs in every mounted
        # app, the stub below reports the server as unreachable, and the app
        # posts a system line into the chat — correct behaviour, in a world that
        # should have had nothing to report. test_skill_autocomplete and
        # test_slash_autocomplete_commands both failed on that line; measured by
        # running them at 561e1d8 (17 passed, three times) and against T239 with
        # the announce disabled (17 passed, twice).
        self.configs = {}
        return self.configs

    def _refuse(self, name: str) -> str:
        return "not connected: the test suite does not dial out"

    monkeypatch.setattr(mcp_client.MCPManager, "load", lambda self: None)
    monkeypatch.setattr(mcp_client.MCPManager, "reload_configs", _no_servers)
    # 🔴 BOTH DOORS, AND THE SECOND ONE WAS ADDED AFTER IT BIT.
    #
    # Stubbing `load` alone was correct for exactly one commit. T239 then moved
    # the boot connect OFF the constructor into an `on_mount` worker that calls
    # `connect()` DIRECTLY — so every mounted app started dialling again, at ~4s
    # per test, and the guard test stayed green because it only ever measured
    # CONSTRUCTION. The suite went from 5:32 back past 10 minutes and the first
    # symptom was a tool timeout, not a failure.
    #
    # A chokepoint that names one CALLER is a chokepoint for that caller. This
    # one names the thing that actually touches the network, so any future path
    # to it — boot, /mcp connect, a reconnect on resume — is covered by having
    # been written, not by being remembered.
    monkeypatch.setattr(mcp_client.MCPManager, "connect", _refuse)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_mcp_load: this test drives MCPManager.load itself; do not stub it",
    )


# =============================================================================
# SCRIPT-STYLE FILES ARE NOT PYTEST MODULES, AND IMPORTING ONE KILLS THE RUN.
#
# Measured 2026-08-21: `python -m pytest tests/` reported
#
#     INTERNALERROR> ... test_ask_user_question.py, line 232, in main
#     INTERNALERROR>     sys.exit(0 if all(ok) else 1)
#     no tests collected
#
# ZERO tests, for the whole repository, from an INTERNALERROR. Some of the files
# in here are standalone scripts: the module body IS the test, it prints
# "N/M passed", and the last line exits the process. pytest imports every file
# it collects, so importing one of those runs it and calls sys.exit() *inside
# the collector*. Ignoring one only hands the crash to the next.
#
# The scripts are not broken -- they run and they assert when invoked directly,
# which is how the suite has always been run. What was broken is that the
# STANDARD entry point could not reach the files that ARE pytest tests.
#
# 🔴 NO COUNTS IN THIS COMMENT, DELIBERATELY. It used to say "18 of the 47
# files", "13 files do this" and "the 29 files that ARE pytest tests". On
# 2026-09-03 the real numbers were 12 ignored of 147, and nobody had noticed --
# the DERIVATION below was correct the whole time and only the prose rotted.
# Fresh counts here would just restart that clock, so the numbers live in a
# command instead:
#
#   python -c "import pathlib; ps=list(pathlib.Path('tests').glob('test_*.py')); \
#     s=[p for p in ps if 'def test_' not in p.read_text(encoding='utf-8',errors='ignore')]; \
#     print(len(s),'ignored of',len(ps))"
#
# ⚠️ AND THIS RULE IS NOT THE ONLY PARTITION ON THIS BOX. tests/run_all.py's
# `classify()` asks a STRICTER question -- an AST module-level exit OR no
# module-level `def test_*` -- and gets a different set. They disagree on
# test_ttyguard.py, which has test functions AND an exit; that exit sits under
# `if __name__ == "__main__"`, so pytest importing it is harmless, and the file
# is covered by whichever entry point ran. Two rules, no gap. run_all's is the
# authority for "which runner", and tools/changed_tests.py imports it rather
# than carrying a third copy.
#
# ⚠️ WRAPPING THE LAST LINE IN `if __name__ == "__main__"` DOES NOT FIX THIS.
# The assertions in a script-style file execute at module level ABOVE that
# line, so a guard stops the exit and lets the body run on import anyway --
# slower, noisier, and touching whatever the script touches. It would look like
# a fix and change nothing that matters. Don't collect them at all.
#
# 🔴 DERIVED, NOT LISTED. A hardcoded filename list drifts the day someone adds
# a script, and drifts silently, because the symptom is an INTERNALERROR that
# blames the new file rather than the stale list. The rule below is the actual
# distinction: a pytest module defines `def test_`; a script does not.
def _is_script_style(path: Path) -> bool:
    """True when the file has no `def test_` -- i.e. nothing pytest can call."""
    try:
        return "def test_" not in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


collect_ignore = sorted(
    p.name for p in Path(__file__).resolve().parent.glob("test_*.py")
    if _is_script_style(p)
)
