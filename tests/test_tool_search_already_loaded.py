"""T1371: tool_search searches only what is DEFERRED, and it must not read as "no such tool".

Ryan's Basalt seat, 2026-11-20: `view_image` was in its tool list. It asked tool_search for "image"
(answer: four unrelated deferred tools) and then for "select:view_image" (answer: "no deferred tool
matches"). Both answers were true and both said, to a model, that the tool did not exist; it went on
to sample pixels through the shell for about 90 calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui.plugins import PluginRegistry, tool_search


def _spec(name: str, description: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": {}}}}


def _registry() -> PluginRegistry:
    reg = PluginRegistry()
    for name, text in (("read", "Read a text file"), ("view_image", "Show the model an image file"),
                       ("tool_search", "Load deferred tools"), ("studio", "Generate an image or a video"),
                       ("listen", "Transcribe audio")):
        reg.add_tool("test", _spec(name, text), lambda args: "ran")
    reg.deferred_static = frozenset({"studio", "listen"})
    return reg


def test_asking_for_a_loaded_tool_by_name_says_it_is_loaded() -> None:
    reg = _registry()
    out = tool_search._run(reg, {"query": "select:view_image"})
    assert out.startswith("Already loaded, call directly (no search needed): view_image. ")
    assert "no deferred tool matches 'select:view_image'" in out          # the old sentence stays true, second
    assert reg.activated == set(), "nothing deferred was loaded by that question"


def test_a_keyword_search_names_the_loaded_tool_before_the_deferred_ones() -> None:
    reg = _registry()
    out = tool_search._run(reg, {"query": "image"})
    assert out.startswith("Already loaded, call directly (no search needed): view_image. Loaded 1 tool(s): studio.")
    assert out.index("view_image") < out.index("Schemas:"), "said before the schema dump, where it is read"
    again = tool_search._run(reg, {"query": "image"})                     # studio is active now: it is named as loaded
    assert again.startswith("Already loaded, call directly (no search needed): view_image. no deferred tool matches")


def test_a_search_with_nothing_loaded_to_name_reads_as_before() -> None:
    reg = _registry()
    assert tool_search._run(reg, {"query": "audio"}).startswith("Loaded 1 tool(s): listen. Callable from your next turn on.")
    assert tool_search._run(reg, {"query": "select:nothing"}).startswith("no deferred tool matches 'select:nothing'. Deferred names: studio")
    assert tool_search.already_loaded(reg, "search") == [], "tool_search does not name itself"
    assert tool_search.already_loaded(reg, "select:read, VIEW_IMAGE") == ["read", "view_image"]
