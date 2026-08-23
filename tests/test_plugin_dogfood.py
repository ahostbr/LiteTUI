"""The dogfood gate — 'a plugin system that ships none of its own plugins'
is a recorded failure mode here, so it is a BUILD FAILURE, permanently.

Two claims, both load-bearing:
1. Real capabilities ship AS plugins (counted by owner, not by API existing).
2. app.py never re-accretes: the host may import the substrate, never a
   plugin module — the road back to monolith is gated, not eyeballed.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui.plugins import PLUGIN_LOAD_ORDER  # noqa: E402

APP_SRC = Path(m.__file__).read_text(encoding="utf-8")


def _app():
    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def test_real_capabilities_ship_as_plugins():
    a = _app()
    tool_owners = {e.owner for e in a.plugins.tools}
    cmd_owners = {e.owner for e in a.plugins.commands.values()} - {"host"}
    section_owners = {s.owner for s in a.plugins.prompt_sections} - {"host"}
    row_owners = {r.owner for r in a.plugins.palette_rows}
    assert len(a.plugins.tools) >= 8, "the tool floor shrank"
    assert len(tool_owners) >= 5, f"tools come from only {tool_owners}"
    assert len(cmd_owners) >= 6, f"commands come from only {cmd_owners}"
    assert section_owners, "no plugin owns a prompt section"
    # palette_row is the ESCAPE HATCH for a capability with no command of its
    # own, so a shrinking count is an improvement, not a regression: on
    # 2026-08-22 "New scheduled job" and "Toggle agent tools" both graduated to
    # real commands (/job, /tools) and stopped needing it. What this line is
    # actually guarding is that a PLUGIN still owns the hatch rather than the
    # host -- the floor was never the point.
    assert len(row_owners) >= 1 and len(a.plugins.palette_rows) >= 1
    assert a.plugins.dynamic, "the MCP dynamic provider vanished"
    assert len(PLUGIN_LOAD_ORDER) >= 15


def test_app_never_imports_a_plugin_module():
    """The re-accretion guard. app.py owns the substrate import and nothing
    below it; the day a submodule import appears, the monolith is growing
    back and this fails before it merges."""
    # Covers BOTH namespaces. After the package move the submodule form is
    # `from litetui.plugins.x import ...`, and a guard that still only knew the
    # bare `plugins.` spelling would have gone quietly permissive -- passing
    # forever while the thing it forbids became sayable again. A guard that
    # cannot see the current spelling of the violation is not a guard.
    assert re.search(r"^\s*(?:from|import)\s+(?:litetui\.)?plugins\.", APP_SRC, re.M) is None, (
        "app.py imports a plugin submodule"
    )
    assert "from litetui.plugins import" not in APP_SRC, (
        "app.py from-imports the substrate — module import only, one binding"
    )
    # positive control: the substrate import itself IS present.
    assert re.search(r"^from litetui import plugins as plugins_mod$", APP_SRC, re.M), (
        "the substrate import must still be PRESENT -- without this positive control "
        "the two bans above pass trivially on a file that imports nothing at all"
    )


def test_the_reaccretion_guard_can_fail():
    # The guard is a regex; prove it bites on the exact shapes it forbids.
    for bad in ("from plugins.core_tools import tool_bash",
                "import plugins.scheduler_ui",
                "    from plugins.misc import _cmd_think"):
        assert re.search(r"^\s*(?:from|import)\s+plugins\.", bad, re.M), bad
