"""T1055 — a mouse WHEEL must not be able to act in LiteTUI.

LiteSuite's AgentBridge lets an agent scroll a terminal pane (`pane/scroll`), and that
route deliberately does NOT taint the terminal (Sentinel cc022be1). Ryan: "no leave that
unchanged no warning nothing" — an agent scrolling to READ his pane must not cost him his
fleet-floor exemption. That decision rests on one fact about THIS app, proved in source
rather than assumed:

  * LiteTUI runs Textual with mouse reporting ON (the `App.run()` default, cli.py), so
    xterm.js delivers a wheel as an SGR mouse report (xterm 6.0.0
    CoreBrowserTerminal.ts:710-713), never as arrow keys.
  * Textual 8.1.0 maps that report to MouseScrollUp/Down/Left/Right ONLY
    (_xterm_parser.py:101-107: button bit 64, button=0, so no MouseDown, no Click),
    and its handlers only move the view (widget.py:4734-4762, _footer.py:337-350).
    No built-in widget selects, accepts or submits on a scroll.
  * LiteTUI itself defines no mouse-scroll handler.

So a scroll can move what Ryan SEES, never what the app DOES. This file turns red the
moment that stops being true of LiteTUI's own code: a widget that handles a scroll event,
or a run that turns mouse reporting OFF. With reporting off, xterm turns the wheel into
arrow keys in the alt screen, and an arrow is a keystroke. If this goes red, do not
silence it: make LiteSuite's pane/scroll taint the pane (agent-bridge.ts, the pane/scroll
comment) and then update this file.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "litetui"

_HANDLER = re.compile(r"^_?on_mouse_scroll(_\w+)?$")
_EVENT = re.compile(r"^MouseScroll(Up|Down|Left|Right)?$")


def scroll_hazards(source: str, name: str = "<src>") -> list[str]:
    """Every place in `source` that could let a wheel act: handler, event use, mouse=False."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, name)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _HANDLER.match(node.name):
            found.append(f"{name}:{node.lineno} handler {node.name}")
        elif isinstance(node, ast.Attribute) and _EVENT.match(node.attr):
            found.append(f"{name}:{node.lineno} uses {node.attr}")
        elif isinstance(node, ast.Name) and _EVENT.match(node.id):
            found.append(f"{name}:{node.lineno} uses {node.id}")
        elif isinstance(node, ast.alias) and _EVENT.match(node.name.rsplit(".", 1)[-1]):
            found.append(f"{name}:{getattr(node, 'lineno', 0)} imports {node.name}")
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg == "mouse"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    found.append(f"{name}:{node.lineno} mouse=False")
    return found


def test_no_litetui_code_can_act_on_a_scroll() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 20, f"scanned only {len(files)} files under {SRC}: wrong root?"
    hazards = [h for f in files for h in scroll_hazards(f.read_text(encoding="utf-8"), f.name)]
    assert hazards == [], (
        "LiteTUI can now act on a mouse wheel. LiteSuite's pane/scroll does NOT taint on that "
        "assumption (T1055); taint it there before relaxing this:\n  " + "\n  ".join(hazards)
    )


def test_control_the_scan_can_go_red() -> None:
    """A scan that finds nothing must be able to find something."""
    bad = (
        "from textual import events\n"
        "from textual.events import MouseScrollDown\n"
        "class W:\n"
        "    def on_mouse_scroll_down(self, e): self.post_message(Submitted())\n"
        "    async def _on_mouse_scroll_up(self, e): ...\n"
        "    def on_event(self, e):\n"
        "        if isinstance(e, events.MouseScrollUp): pass\n"
        "app.run(mouse=False)\n"
    )
    hazards = scroll_hazards(bad)
    assert any("handler on_mouse_scroll_down" in h for h in hazards)
    assert any("handler _on_mouse_scroll_up" in h for h in hazards)
    assert any("uses MouseScrollUp" in h for h in hazards)
    assert any("imports textual.events.MouseScrollDown" in h or "imports MouseScrollDown" in h
               for h in hazards)
    assert any("mouse=False" in h for h in hazards)
    # And the ordinary shapes it must NOT flag.
    assert scroll_hazards("app.run()\napp.run(mouse=True)\ndef on_click(self, e): ...\n") == []
