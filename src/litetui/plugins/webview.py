"""`/browser` — open the in-TUI browser in the sidebar.

Ryan, 2026-09-19: "we already have the sidebar in litetui. add that webview
theres with a url bar and browser controls at the top. i want a in tui
browser." He confirmed the browser should ALWAYS dock to the sidebar (not
follow the global dialog_style), with the Swap button popping it to a modal.

The browser itself lives in ``litetui.webview`` (``BrowserBody``). This plugin
only gives it a door: a slash command and a palette row. It opens through
``open_dialog`` — the sync-handler shape, so the fetch and rendering that run
as a worker inside the body do not block the command frame — with
``style="sidebar"`` forced, so it docks to the side panel regardless of the
global setting. ``side`` is left to the global ``dialog_side``; the SwapButton
moves it the other way.
"""
from __future__ import annotations

from functools import partial

from litetui.plugins import PluginManifest


def _cmd_browser(app, name: str, arg: str) -> None:
    from litetui.side_panel import open_dialog
    from litetui.webview import BrowserBody

    url = (arg or "").strip()
    # `open_dialog`, not `show_dialog`: this handler is SYNC and show_dialog is
    # a coroutine — the same mismatch the /tools and /mcp openers note. No
    # callback: the browser has no single answer to collect; it is closed with
    # Close or Esc.
    #
    # style="sidebar" is FORCED, not read from the setting: Ryan wants the
    # browser to always dock to the side panel, even when his dialogs otherwise
    # default to modal. side stays None so it respects the global dialog_side.
    open_dialog(app, partial(BrowserBody, url), style="sidebar")


def _register(ctx) -> None:
    ctx.command(
        ("/browser", "/web", "/browse"), _cmd_browser,
        palette="Browser",
        help="In-TUI browser: a URL bar and Back/Forward/Reload/Home over the page's text and links. /browser <url>.",
        group="automation",
        order=10,
    )


PLUGIN = PluginManifest(id="webview", register=_register)
