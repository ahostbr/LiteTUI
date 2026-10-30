"""Source routes open reviewed native controls without running plugin callbacks."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_rust_backend_catalog import catalog_app as _catalog_app
from test_rust_backend_catalog import forbidden, tree
from test_rust_command_surface import read, selection

from litetui import command_surface, plugins

command_app = _catalog_app


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,action,arguments", [
    ("/new", "conversation_new", ""),
    ("/CLEAR", "conversation_new", ""),
    ("/reset", "conversation_new", ""),
    ("/rename", "conversation_rename", ""),
    ("/rename   A  new\nname  ", "conversation_rename", "A  new\nname  "),
])
async def test_session_command_routes_preserve_raw_input_without_mutation(command_app, raw, action, arguments):
    app = command_app
    before = tree(app)
    catalog = read(app)
    request = selection(app, catalog, action)
    request.pop("row_id")
    result = command_surface.resolve(app, {**request, "command": raw})
    assert result["command"] == raw
    assert result["native_action"] == action
    assert result["arguments"] == arguments
    assert result["session_id"] == app.convo_id
    assert tree(app) == before


@pytest.mark.asyncio
async def test_rename_row_accepts_only_bounded_lossless_prefill(command_app):
    app = command_app
    before = tree(app)
    catalog = read(app)
    request = selection(app, catalog, "conversation_rename")
    value = "  \t" + "雪" * 1364 + " "
    assert len(value.encode("utf-8")) == 4096
    result = command_surface.resolve(app, {**request, "arguments": value})
    assert result["arguments"] == value
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["x" * 4097, "雪" * 1366, "bad\x00title"],
                         ids=["ascii-byte-limit", "unicode-byte-limit", "nul"])
async def test_rename_invalid_prefill_is_refused_before_opening_control(command_app, arguments):
    app = command_app
    before = tree(app)
    request = selection(app, read(app), "conversation_rename")
    with pytest.raises(ValueError, match="title|argument|text"):
        command_surface.resolve(app, {**request, "arguments": arguments})
    assert tree(app) == before


@pytest.mark.asyncio
async def test_canonical_theme_palette_row_routes_without_invoking_callback(command_app):
    app = command_app
    before = tree(app)
    catalog = read(app)
    request = selection(app, catalog, "themes")
    row = next(row for row in catalog["entries"] if row["id"] == request["row_id"])
    assert row["kind"] == "palette" and row["owner"] == "settings-ui"
    assert row["title"] == "Theme" and row["tokens"] == []
    assert not row["accepts_arguments"]
    result = command_surface.resolve(app, request)
    assert result["native_action"] == "themes" and result["arguments"] == ""
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["conversation_new", "themes"])
async def test_control_arguments_never_select_or_apply_implicitly(command_app, action):
    app = command_app
    before = tree(app)
    request = selection(app, read(app), action)
    with pytest.raises(ValueError, match="arguments"):
        command_surface.resolve(app, {**request, "arguments": "arbitrary target"})
    assert tree(app) == before


@pytest.mark.asyncio
async def test_theme_palette_owner_and_callback_revision_are_not_inferred(command_app):
    app = command_app
    registry = plugins.PluginRegistry()
    registry.add_palette_row("third-party", "Theme", "Not canonical", forbidden)
    registry.add_command("third-party", ("/new",), forbidden, palette="New")
    registry.add_command("third-party", ("/rename",), forbidden, palette="Rename")
    app.plugins = registry
    app.skills = []
    assert all(row["native_action"] is None for row in read(app)["entries"])
    registry.add_palette_row("settings-ui", "Theme", "Canonical adapter", forbidden)
    request = selection(app, read(app), "themes")
    registry.palette_rows[-1] = replace(registry.palette_rows[-1], run=lambda: forbidden())
    with pytest.raises(ValueError, match="stale"):
        command_surface.resolve(app, request)
