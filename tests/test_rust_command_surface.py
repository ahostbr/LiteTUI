"""Native discovery uses real registrations; resolution cannot run plugin code."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from test_rust_backend_catalog import catalog_app as _catalog_app
from test_rust_backend_catalog import forbidden, tree

from litetui import command_surface, gui_rpc, plugins
from litetui.skills import Skill
from litetui.widgets import LiteTUICommands

command_app = _catalog_app


def read(app):
    return command_surface.catalog(app, {"session_id": app.convo_id})


def selection(app, catalog, action="settings"):
    row = next(row for row in catalog["entries"] if row["native_action"] == action)
    return {"session_id": app.convo_id, "version": 1, "agent_id": catalog["agent_id"],
            "snapshot": catalog["snapshot"], "row_id": row["id"]}


def custom_registry(app):
    registry = plugins.PluginRegistry()
    registry.add_command("settings-ui", ("/settings", "/prefs"), forbidden,
                         palette="Settings", help="Inspect effective settings", group="app")
    registry.add_command("model-switch", ("/model",), forbidden,
                         palette="Models", group="backend", order=10)
    registry.add_command("themes", ("/theme",), forbidden, palette="Theme", group="screen")
    registry.add_command("custom", ("/z",), forbidden, palette="Alpha", group="convo")
    registry.add_command("custom", ("/hidden",), forbidden, group="tools")
    registry.add_palette_row("scheduler", "Jobs", "Inspect jobs", forbidden, tag="/cron list")
    app.plugins = registry
    app.skills = [Skill("prefs", "shadowed alias", Path("alias/SKILL.md")),
                  Skill("SETTINGS", "shadowed primary", Path("primary/SKILL.md")),
                  Skill("zzz", "Last skill", Path("zzz/SKILL.md")),
                  Skill("aaa", "First skill", Path("aaa/SKILL.md"))]
    return registry


@pytest.mark.asyncio
async def test_live_catalog_tracks_canonical_palette_and_routes_without_execution(command_app):
    app = command_app
    before = tree(app)
    result = read(app)
    rows = result["entries"]
    assert result["session_id"] == app.convo_id
    assert result["agent_id"] == app._agent_session.authority.agent_id
    assert result["backend"] == app.backend.name
    assert result["model"] == app.model_id
    assert result == read(app)
    # The actual Textual provider is the parity oracle; constructing callbacks is safe.
    provider = cast(LiteTUICommands, SimpleNamespace(app=app, _describe=LiteTUICommands._describe))
    textual = LiteTUICommands._commands(provider)
    native = sorted((row for row in rows if row["palette"]), key=lambda row: row["palette_order"])
    assert [row["title"] for row in native] == [row[0].split(" · ")[0] for row in textual]
    for action in ("models", "backend", "thinking", "conversations", "settings", "quit",
                   "work_tasks", "work_jobs", "work_calendar", "work_loops", "work_goal", "plugins"):
        request = selection(app, result, action)
        route = command_surface.resolve(app, request)
        assert route == {"version": 1, "kind": "command_route", "session_id": app.convo_id,
                         "agent_id": result["agent_id"], "snapshot": result["snapshot"],
                         "row_id": request["row_id"], "native_action": action,
                         "backend": result["backend"], "model": result["model"], "arguments": ""}
    assert tree(app) == before


@pytest.mark.asyncio
async def test_work_routes_only_accept_inspection_and_preserve_callback_ownership(command_app):
    app = command_app
    catalog = read(app)
    request = {k: v for k, v in selection(app, catalog).items() if k != "row_id"}
    before = tree(app)
    for command, action in (("/tasks list", "work_tasks"), ("/cron LS", "work_jobs"),
                            ("/loop status", "work_loops"), ("/goal show", "work_goal"), ("/cal", "work_calendar")):
        route = command_surface.resolve(app, {**request, "command": command})
        assert (route["command"], route["native_action"]) == (command, action)
    jobs = next(e for e in catalog["entries"] if e["kind"] == "palette" and e["title"] == "Scheduled jobs")
    assert command_surface.resolve(app, {**request, "row_id": jobs["id"]})["native_action"] == "work_jobs"
    for command in ("/cron run saved", "/tasks kill id", "/loop clear", "/goal start something", "/cal run"):
        with pytest.raises(ValueError, match="inspection|arguments"):
            command_surface.resolve(app, {**request, "command": command})
    assert tree(app) == before


@pytest.mark.asyncio
async def test_alias_collapse_shadowing_distinct_orders_and_callback_only_rows(command_app):
    app = command_app
    custom_registry(app)
    result = read(app)
    rows = result["entries"]
    assert len(rows) == 10
    assert [row["title"] for row in sorted((row for row in rows if row["kind"] == "skill"),
                                           key=lambda row: row["skill_order"])] == ["aaa", "prefs", "SETTINGS", "zzz"]
    assert all(row["skill_order"] is None for row in rows if row["kind"] != "skill")
    assert [row["title"] for row in sorted((r for r in rows if r["palette"]),
                                           key=lambda r: r["palette_order"])] == [
        "Settings", "Theme", "Alpha", "Jobs", "Models"]
    completion = sorted((r for r in rows if r["completion_order"] is not None),
                        key=lambda r: r["completion_order"])
    assert [row["tokens"][0] for row in completion] == [
        "/z", "/model", "/hidden", "/theme", "/settings", "/aaa", "/zzz"]
    settings = next(row for row in rows if row["native_action"] == "settings")
    assert settings["tokens"] == ["/settings", "/prefs"]
    assert {row["title"] for row in rows if row["kind"] == "skill"
            and row["completion_order"] is None} == {"prefs", "SETTINGS"}
    assert all(row["accepts_arguments"] == (row["kind"] == "skill") for row in rows)
    for row in rows:
        if row["native_action"] is None:
            with pytest.raises(ValueError, match="adapter"):
                command_surface.resolve(app, {**selection(app, result), "row_id": row["id"]})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["registry", "handler", "help", "skill", "policy"])
async def test_registry_reload_metadata_and_policy_changes_invalidate_selection(command_app, monkeypatch, mutation):
    app = command_app
    registry = custom_registry(app)
    request = selection(app, read(app))
    if mutation == "registry":
        replacement = plugins.PluginRegistry()
        replacement.commands = registry.commands.copy()
        replacement.palette_rows = registry.palette_rows.copy()
        app.plugins = replacement
    elif mutation in ("handler", "help"):
        old = registry.commands["/settings"]
        new = replace(old, **({"handler": lambda *_: forbidden()} if mutation == "handler" else {"help": "Changed"}))
        for token in new.tokens:
            registry.commands[token] = new
    elif mutation == "skill":
        app.skills[0].source = "changed"
        # Even a completion-shadowed skill remains an exact selectable row.
    else:
        original = gui_rpc._tool_policy(app)
        monkeypatch.setattr(gui_rpc, "_tool_policy", lambda _app: {**original, "revision": "changed"})
    with pytest.raises(ValueError, match="stale"):
        command_surface.resolve(app, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, message", [
    ({"version": True}, "version"), ({"version": 2}, "version"),
    ({"agent_id": "foreign-agent"}, "another agent"), ({"snapshot": "old"}, "stale"),
    ({"row_id": "absent"}, "missing"), ({"arguments": "extra"}, "arguments"),
    ({"arguments": None}, "arguments"), ({"session_id": "11111111-1111-4111-8111-111111111111"}, "owned session"),
])
async def test_resolution_refuses_invalid_or_foreign_requests(command_app, extra, message):
    app = command_app
    with pytest.raises(ValueError, match=message):
        command_surface.resolve(app, {**selection(app, read(app)), **extra})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation, message", [
    ("alias", "aliases"), ("duplicate", "aliases"), ("token", "token"),
    ("help", "text"), ("order", "order"), ("rows", "entry budget"),
    ("bytes", "byte|response budget"),
])
async def test_malformed_or_oversized_registrations_fail_closed(command_app, mutation, message):
    app = command_app
    registry = custom_registry(app)
    old = registry.commands["/settings"]
    if mutation == "alias":
        del registry.commands["/prefs"]
    elif mutation == "duplicate":
        new = replace(old, tokens=("/settings", "/settings"))
        registry.commands = {"/settings": new}
    elif mutation == "token":
        new = replace(old, tokens=("/bad token",))
        registry.commands = {"/bad token": new}
    elif mutation in ("help", "order"):
        new = replace(old, **({"help": "x" * 4097} if mutation == "help" else {"order": True}))
        registry.commands = {token: new for token in new.tokens}
    elif mutation == "rows":
        registry.palette_rows *= 1025
    else:
        for index in range(150):
            registry.add_command("extra", (f"/extra{index}",), forbidden, help="x" * 4096)
    with pytest.raises(ValueError, match=message):
        read(app)


@pytest.mark.asyncio
async def test_no_adapter_inferred_from_name_without_reviewed_owner(command_app):
    app = command_app
    registry = plugins.PluginRegistry()
    registry.add_command("third-party", ("/settings",), forbidden, palette="Settings")
    app.plugins, app.skills = registry, []
    assert read(app)["entries"][0]["native_action"] is None


@pytest.mark.asyncio
async def test_mid_read_registry_change_cannot_publish_snapshot(command_app, monkeypatch):
    app = command_app
    registry = custom_registry(app)
    original = gui_rpc._tool_policy
    calls = 0
    def mutate(value):
        nonlocal calls
        calls += 1
        if calls == 1:
            registry.add_command("late", ("/late",), forbidden)
        return original(value)
    monkeypatch.setattr(gui_rpc, "_tool_policy", mutate)
    with pytest.raises(ValueError, match="changed while reading"):
        read(app)


@pytest.mark.asyncio
async def test_public_management_operations_preserve_owned_catalog_and_route(command_app):
    app = command_app
    result = await gui_rpc.async_dispatch(app, {"type": "gui.commands.list", "session_id": app.convo_id})
    assert result == read(app)
    request = selection(app, result)
    route = await gui_rpc.async_dispatch(app, {"type": "gui.commands.resolve", **request})
    assert route == command_surface.resolve(app, request)
    assert gui_rpc.current_operation(app) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("original, action, arguments", [
    (" /PrEfS\t ", "settings", ""),
    ("/MODEL", "models", ""),
    ("/aaa  exact  arguments \r\n", "skill_prepare", "exact  arguments \r\n"),
    ("/AAA\n\tfirst\nsecond  ", "skill_prepare", "first\nsecond  "),
])
async def test_raw_command_uses_source_alias_case_and_exact_tail(command_app, original, action, arguments):
    app = command_app
    custom_registry(app)
    catalog = read(app)
    before = tree(app)
    request = {key: value for key, value in selection(app, catalog).items() if key != "row_id"}
    result = command_surface.resolve(app, {**request, "command": original})
    assert result["command"] == original and result["native_action"] == action
    assert result["arguments"] == arguments
    assert result["backend"] == catalog["backend"] and result["model"] == catalog["model"]
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, message", [
    ({"command": "   "}, "slash"), ({"command": "model"}, "slash"),
    ({"command": "/unknown"}, "unknown"), ({"command": "/prefs value"}, "arguments"),
    ({"command": "/theme"}, "adapter"), ({"command": "/prefs", "row_id": "extra"}, "combined"),
    ({"command": "/prefs", "arguments": ""}, "combined"),
    ({"command": "/" + "a" * 65536}, "oversized"),
])
async def test_raw_command_refuses_unknown_unsupported_mixed_or_argument_controls(command_app, extra, message):
    app = command_app
    custom_registry(app)
    request = {key: value for key, value in selection(app, read(app)).items() if key != "row_id"}
    with pytest.raises(ValueError, match=message):
        command_surface.resolve(app, {**request, **extra})


@pytest.mark.asyncio
async def test_skills_command_routes_view_and_exact_rows_disambiguate_names(command_app):
    app = command_app
    custom_registry(app)
    app.plugins.add_command("skills", ("/skills", "/skill"), forbidden, palette="Skills")
    app.skills.append(Skill("AAA", "Other source", Path("elsewhere/SKILL.md"), "other"))
    catalog = read(app)
    request = {key: value for key, value in selection(app, catalog).items() if key != "row_id"}
    assert command_surface.resolve(app, {**request, "command": "/SKILL"})["native_action"] == "skills"
    with pytest.raises(ValueError, match="arguments"):
        command_surface.resolve(app, {**request, "command": "/skills refresh"})
    with pytest.raises(ValueError, match="ambiguous"):
        command_surface.resolve(app, {**request, "command": "/aaa task"})
    for row in catalog["entries"]:
        if row["kind"] == "skill" and row["title"].lower() == "aaa":
            result = command_surface.resolve(app, {**request, "row_id": row["id"], "arguments": " exact \n"})
            assert result["native_action"] == "skill_prepare" and result["arguments"] == " exact \n"

@pytest.mark.asyncio
async def test_shadowed_skill_is_exact_selectable_without_stealing_command_alias(command_app):
    app = command_app
    custom_registry(app)
    app.plugins.add_command("skills", ("/skills", "/skill"), forbidden, palette="Skills")
    catalog = read(app)
    request = {key: value for key, value in selection(app, catalog).items() if key != "row_id"}
    assert command_surface.resolve(app, {**request, "command": "/prefs"})["native_action"] == "settings"
    for value in ("/skills prefs  exact \r\n", "/skill /PREFS  exact \r\n"):
        result = command_surface.resolve(app, {**request, "command": value})
        assert result["command"] == value and result["arguments"] == "exact \r\n"
        assert result["native_action"] == "skill_prepare"
        assert next(row for row in catalog["entries"] if row["id"] == result["row_id"])["title"] == "prefs"

@pytest.mark.asyncio
async def test_unicode_alias_shadowing_matches_canonical_lowercase_dispatch(command_app):
    app = command_app
    registry = custom_registry(app)
    original = registry.commands["/settings"]
    entry = replace(original, tokens=(*original.tokens, "/ss"))
    for token in entry.tokens:
        registry.commands[token] = entry
    app.skills.extend([
        Skill("\u00df", "Distinct Unicode name", Path("sharp-s/SKILL.md")),
        Skill("SS", "Actual uppercase alias collision", Path("ascii/SKILL.md")),
    ])
    catalog = read(app)
    rows = {row["title"]: row for row in catalog["entries"] if row["kind"] == "skill"}
    assert rows["\u00df"]["completion_order"] is not None
    assert rows["SS"]["completion_order"] is None
    assert rows["prefs"]["completion_order"] is None
    assert rows["SETTINGS"]["completion_order"] is None
    request = {key: value for key, value in selection(app, catalog).items() if key != "row_id"}
    before = tree(app)
    for token in ("/ss", "/SS", "/prefs", "/SETTINGS"):
        assert command_surface.resolve(app, {**request, "command": token})["native_action"] == "settings"
    for token in ("/\u00df", "/\u1e9e"):
        result = command_surface.resolve(app, {**request, "command": token})
        assert result["native_action"] == "skill_prepare" and result["row_id"] == rows["\u00df"]["id"]
        assert result["command"] == token
    assert tree(app) == before
