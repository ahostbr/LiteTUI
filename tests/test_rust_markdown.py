"""gui.markdown.native_render: the native frontend draws the lines AnswerBody draws."""
from __future__ import annotations

import pytest

from litetui import gui_rpc, native_markdown
from litetui.textfmt import _markdown_to_text

SOURCE = (
    "PERFCHUNK0000 deterministic reply with Unicode café 猫.\n"
    "PERFCHUNK0001 deterministic reply.\n\n"
    "- **bold** item\n- `code` item\n\n"
    "```python\nx = 1\n```\n\n# Heading\n\n> quoted\n\n1. first\n2. second\n"
)


def plain(result):
    return ["".join(text for text, _style in line) for line in result["lines"]]


@pytest.mark.parametrize("width", [121, 122, 40, 1])
def test_lines_are_the_answer_bodys_own_text(width):
    result = native_markdown.render(None, {"type": native_markdown.OPERATION, "id": "x",
                                           "version": 1, "text": SOURCE, "width": width})
    assert result["width"] == width
    assert plain(result) == _markdown_to_text(SOURCE, width).plain.split("\n")


def test_runs_carry_rich_styles():
    result = native_markdown.render(None, {"version": 1, "text": "- **bold**\n\n```\nx\n```\n",
                                           "width": 30})
    styles = [style for line in result["lines"] for _text, style in line]
    assert {"bold": True} in styles
    assert any("bg" in style and style["bg"].startswith("#") for style in styles)
    assert all(set(style) <= {"fg", "bg", "bold", "dim", "italic", "underline", "strike", "reverse"}
               for style in styles)


@pytest.mark.parametrize("cmd, error", [
    ({"version": 2, "text": "x", "width": 10}, "version 1"),
    ({"version": 1, "text": "x", "width": 0}, "width"),
    ({"version": 1, "text": "x" * (256 * 1024 + 1), "width": 10}, "256 KiB"),
    ({"version": 1, "text": "x", "width": 10, "extra": 1}, "exactly"),
])
def test_refuses_malformed_requests(cmd, error):
    with pytest.raises(ValueError, match=error):
        native_markdown.render(None, cmd)


def test_the_operation_is_advertised_and_answered_before_every_gate():
    import inspect
    assert any("markdown.native_render" in str(entry) for entry in gui_rpc.OPERATIONS)
    source = inspect.getsource(gui_rpc.async_dispatch)
    assert source.index("native_markdown.OPERATION") < source.index("native_claude_reset.OPERATIONS")
