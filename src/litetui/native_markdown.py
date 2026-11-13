"""Python's reply Markdown for a native frontend: the lines AnswerBody draws.

Python's assistant card shows its answer through ``textfmt._markdown_to_text``: Rich's
Markdown rendered at the card's content width, kept as one styled Text (while it streams
and when it settles). A native frontend draws its own transcript, so it asks for those
lines here with the text and the width its card has, and draws them as they come back:
the same wrap, bullets, rules and code blocks, with each run's style.

A pure read: it changes nothing, so it is answered whatever else is running (a turn
included), before every management gate.
"""
from __future__ import annotations

from rich.console import Console

from litetui.textfmt import _markdown_to_text

OPERATION = "gui.markdown.native_render"
VERSION = 1
MAX_TEXT = 256 * 1024
MAX_WIDTH = 1000
MAX_LINES = 20000


def _color(color):
    if color is None or color.is_default:
        return None
    r, g, b = color.get_truecolor()
    return f"#{r:02x}{g:02x}{b:02x}"


def _style(style) -> dict:
    if style is None:
        return {}
    out = {}
    if (fg := _color(style.color)) is not None:
        out["fg"] = fg
    if (bg := _color(style.bgcolor)) is not None:
        out["bg"] = bg
    for name in ("bold", "dim", "italic", "underline", "strike", "reverse"):
        if getattr(style, name):
            out[name] = True
    return out


def render(app, cmd) -> dict:
    """gui.markdown.native_render: {text, width} -> the card's lines, each a list of runs."""
    if set(cmd) - {"type", "id"} != {"version", "text", "width"}:
        raise ValueError("Markdown render takes exactly version, text and width")
    if type(cmd["version"]) is not int or cmd["version"] != VERSION:
        raise ValueError("Markdown render requires version 1")
    text, width = cmd["text"], cmd["width"]
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT:
        raise ValueError("Markdown text must be at most 256 KiB")
    if type(width) is not int or not 1 <= width <= MAX_WIDTH:
        raise ValueError("Markdown width must be 1 to 1000 cells")
    console = Console(width=width, highlight=False)
    lines = []
    for line in _markdown_to_text(text, width).split("\n", allow_blank=True):
        runs = []
        for segment in line.render(console, end=""):
            if segment.text:
                runs.append([segment.text, _style(segment.style)])
        lines.append(runs)
        if len(lines) > MAX_LINES:
            raise ValueError("Markdown render is too long")
    return {"version": VERSION, "width": width, "lines": lines}
