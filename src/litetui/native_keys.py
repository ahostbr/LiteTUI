"""/keys for the native frontend: Textual's key panel rows, as data.

Python's /keys (plugins/misc._cmd_keys) toggles Textual's KeyPanel, whose BindingsTable
lists ``app.screen.active_bindings`` grouped by namespace. ``rows`` builds the same rows
the same way (textual/widgets/_key_panel.py, BindingsTable.render_bindings_table), so a
native frontend draws the table Python draws: one dict per Rich table row, with the
section breaks Rich would draw after it.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import groupby
from operator import itemgetter

MAX_ROWS = 512
MAX_TEXT = 1024


def _cut(value) -> str:
    return str(value)[:MAX_TEXT]


def rows(app) -> list[dict] | None:
    """The key panel's table rows for the current screen and focus; None before a screen."""
    from rich.text import Text

    if not getattr(app, "_screen_stack", None):
        return None

    out: list[dict] = []

    def add(key, text, tooltip="", title=False):
        out.append({"key": _cut(key), "text": _cut(text), "tooltip": _cut(tooltip),
                    "title": title, "end_section": False})

    previous = None
    for namespace, grouped in groupby(app.screen.active_bindings.values(), key=itemgetter(0)):
        table_bindings = list(grouped)
        if not table_bindings:
            continue
        group_title = getattr(namespace, "BINDING_GROUP_TITLE", None)
        if group_title:
            add("", group_title, title=True)
        by_action = defaultdict(list)
        for _, binding, _enabled, _tooltip in table_bindings:
            if not binding.system:
                by_action[binding.action].append(binding)
        for bindings in by_action.values():
            binding = bindings[0]
            keys = " ".join(dict.fromkeys(app.get_key_display(b) for b in bindings))
            add(keys, Text.from_markup(binding.description).plain, binding.tooltip or "")
        if namespace != previous and out:
            # Rich's add_section marks the last row so far, whichever group it came from.
            out[-1]["end_section"] = True
        previous = namespace
        if len(out) > MAX_ROWS:
            return out[:MAX_ROWS]
    return out
