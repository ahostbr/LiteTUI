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
    assert all(row["accepts_arguments"] == (row["kind"] == "skill" or row["native_action"] == "models")
               for row in rows)
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


@pytest.mark.asyncio
async def test_registered_glassbox_routes_only_inspect_or_prefill_review(command_app, monkeypatch):
    from litetui.plugins import glassbox_plugin as gb

    app = command_app
    catalog = read(app)
    row = next(r for r in catalog["entries"] if r["native_action"] == "glassbox")
    assert row["tokens"] == ["glassbox", "/glassbox"]
    assert row["palette"] and row["title"] == "Glass box feed" and row["accepts_arguments"]
    assert app.plugins.commands["glassbox"] is app.plugins.commands["/glassbox"]
    assert app.plugins.commands["glassbox"].handler is gb._cmd_glassbox
    request = selection(app, catalog, "glassbox")
    before = tree(app)
    enabled, server, recorder = gb._ENABLED, gb.SERVER, gb._RECORDER
    monkeypatch.setattr(gb.Server, "start", forbidden)
    monkeypatch.setattr(gb.Server, "stop", forbidden)
    for arguments in ("", "status", "START", "on", "stop", "off", "  on  "):
        assert command_surface.resolve(app, {**request, "arguments": arguments})["arguments"] == arguments
        raw = {k: v for k, v in request.items() if k != "row_id"}
        route = command_surface.resolve(app, {**raw, "command": "/glassbox " + arguments})
        assert route["native_action"] == "glassbox"
    for arguments in ("reload", "start now", "status stop", "--port 9999", "on\x85off"):
        with pytest.raises(ValueError, match="Glassbox accepts"):
            command_surface.resolve(app, {**request, "arguments": arguments})
    assert gb._ENABLED is enabled and gb.SERVER is server and gb._RECORDER is recorder
    assert tree(app) == before
    old = app.plugins.commands["glassbox"]
    replacement = replace(old, handler=forbidden)
    for token in old.tokens:
        app.plugins.commands[token] = replacement
    with pytest.raises(ValueError, match="stale"):
        command_surface.resolve(app, request)
    foreign = replace(old, owner="foreign-plugin")
    for token in old.tokens:
        app.plugins.commands[token] = foreign
    changed = read(app)
    assert next(r for r in changed["entries"] if "/glassbox" in r["tokens"])["native_action"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("command_app", [("custom", False)], indirect=True)
async def test_registered_control_commands_preserve_intent_without_callbacks(command_app, monkeypatch):
    app = command_app
    for token, entry in list(app.plugins.commands.items()):
        replacement = replace(entry, handler=forbidden)
        for alias in entry.tokens:
            app.plugins.commands[alias] = replacement
    catalog = read(app)
    base = {k: v for k, v in selection(app, catalog).items() if k != "row_id"}
    before = tree(app)
    cases = [
        ("/compact", "conversation_compact", ""),
        ("/compact Keep  exact guidance 雪\n  and trailing  ", "conversation_compact", "Keep  exact guidance 雪\n  and trailing  "),
        ("/load", "model_load", ""), ("/load fixture-model", "model_load", "fixture-model"),
        ("/unload fixture-model", "model_unload", "fixture-model"),
        ("/reconnect", "model_reconnect", ""),
        ("/engine STOP", "engine", "STOP"), ("/engine start", "engine", "start"),
        ("/engine status", "engine", "status"), ("/hooks", "hooks", ""),
        ("/reload-plugins", "plugin_reload", ""),
        ("/reload-plugins skills", "plugin_reload", "skills"),
        ("/reload-plugins exact_tool", "plugin_reload", "exact_tool"),
        ("/job", "work_new_job", ""), ("/plan", "plan", ""),
        ("/plan ON", "plan", "ON"), ("/plan off", "plan", "off"),
    ]
    for command, action, arguments in cases:
        route = command_surface.resolve(app, {**base, "command": command})
        assert (route["command"], route["native_action"], route["arguments"]) == (command, action, arguments)
    for command, reason in [
        ("/load different-model", "current model"), ("/unload different-model", "current model"),
        ("/engine start 4", "lane arguments"), ("/engine lanes 4", "configuration"),
        ("/plan maybe", "on, off"), ("/reload-plugins two targets", "one exact"),
        ("/compact bad\0hint", "NUL"), ("/hooks extra", "arguments"),
        ("/job every minute", "arguments"), ("/reconnect extra", "arguments"),
    ]:
        with pytest.raises(ValueError, match=reason):
            command_surface.resolve(app, {**base, "command": command})
    assert tree(app) == before


@pytest.mark.asyncio
async def test_codex_native_compact_guidance_is_refused_instead_of_discarded(command_app):
    app = command_app
    request = {k: v for k, v in selection(app, read(app)).items() if k != "row_id"}
    with pytest.raises(ValueError, match="does not accept guidance"):
        command_surface.resolve(app, {**request, "command": "/compact keep this exact guidance"})
    assert command_surface.resolve(app, {**request, "command": "/compact"})["arguments"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["/compact", "/reconnect", "/load", "/unload", "/engine", "/hooks", "/reload-plugins", "/job", "/plan"])
async def test_control_adapters_require_exact_source_registry_owner(command_app, token):
    app = command_app
    registry = plugins.PluginRegistry()
    registry.add_command("third-party", (token,), forbidden)
    app.plugins = registry
    catalog = read(app)
    row = catalog["entries"][0]
    assert row["native_action"] is None
    with pytest.raises(ValueError, match="no native action adapter"):
        command_surface.resolve(app, {"session_id": app.convo_id, "version": 1,
            "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"], "command": token})


def raw_request(app, **extra):
    catalog = read(app)
    base = {k: v for k, v in selection(app, catalog).items() if k != "row_id"}
    return {**base, **extra}


@pytest.mark.asyncio
async def test_argument_routes_only_preselect_and_refuse_what_the_control_cannot_honor(command_app):
    from litetui.thinking_capabilities import thinking_capabilities

    app = command_app
    app.available_models = ["fixture-model", "second-model"]
    levels = thinking_capabilities(app)["levels"]
    backends = [name for name, _label in command_surface.llm_backend.visible_backends()]
    prefix = app.convo_id[:8]
    before = tree(app)
    cases = [
        ("/model 2", "models", "2"), ("/models second-model", "models", "second-model"),
        ("/model", "models", ""), ("/think unset", "thinking", "unset"),
        ("/thinking DEFAULT", "thinking", "DEFAULT"), (f"/backend {backends[-1].upper()}", "backend", backends[-1].upper()),
        ("/backend", "backend", ""), (f"/resume {prefix}", "conversation_resume", prefix),
        (f"/resume {app.convo_id}", "conversation_resume", app.convo_id), ("/resume", "conversation_resume", ""),
        ("/convos", "conversations", ""),
    ] + [(f"/think {level}", "thinking", level) for level in levels if level != "default"]
    for command, action, arguments in cases:
        route = command_surface.resolve(app, raw_request(app, command=command))
        assert (route["native_action"], route["arguments"]) == (action, arguments), command
    for command, reason in [
        ("/model 0", "Use 1-2"), ("/model 3", "Use 1-2"), ("/model missing-model", "not found"),
        ("/think extreme", "not offered"), ("/think very high", "not offered"),
        ("/backend --default codex", "--default"), ("/backend nonsense", "must be one of"),
        ("/backend codex claude", "must be one of"),
        ("/resume 1", "numeric"), ("/resume NOT-HEX", "lowercase"), ("/resume ffffffff", "No conversation matches"),
        ("/convos 1", "arguments"),
        ("/tools list", "/tools takes no arguments"), ("/reconnect now", "/reconnect takes no arguments"),
        ("/settings theme", "/settings takes no arguments"),
    ]:
        with pytest.raises(ValueError, match=reason):
            command_surface.resolve(app, raw_request(app, command=command))
    rows = {row["tokens"][0]: row for row in read(app)["entries"] if row["kind"] == "command"}
    assert all(rows[token]["accepts_arguments"] for token in ("/model", "/think", "/backend", "/resume"))
    assert not any(rows[token]["accepts_arguments"] for token in ("/tools", "/reconnect", "/settings", "/convos"))
    assert tree(app) == before


@pytest.mark.asyncio
async def test_resume_argument_refuses_ambiguous_and_blocked_catalogs_without_choosing_one(command_app, monkeypatch):
    from litetui import storage_catalog

    app = command_app
    first = SimpleNamespace(conversation_id="abcdef00-0000-4000-8000-000000000001")
    second = SimpleNamespace(conversation_id="abcdef00-0000-4000-8000-000000000002")
    monkeypatch.setattr(storage_catalog, "conversations", lambda *_a, **_k: [first, second])
    with pytest.raises(ValueError, match="matches 2 conversations"):
        command_surface.resolve(app, raw_request(app, command="/resume abcdef"))
    assert command_surface.resolve(app, raw_request(app, command=f"/resume {first.conversation_id}"))[
        "arguments"] == first.conversation_id
    monkeypatch.setattr(storage_catalog, "conversations", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(ValueError, match="Agent conversations blocked: denied"):
        command_surface.resolve(app, raw_request(app, command="/resume abcdef"))


@pytest.mark.asyncio
async def test_claude_routes_require_the_claude_backend(command_app):
    app = command_app
    for command in ("/claude", "/claude resolve"):
        with pytest.raises(ValueError, match="requires /backend claude"):
            command_surface.resolve(app, raw_request(app, command=command))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_app", [("claude", False)], indirect=True)
async def test_claude_routes_accept_only_known_verbs_on_the_claude_backend(command_app):
    app = command_app
    for command, arguments in (("/claude", ""), ("/claude STATUS", "STATUS"), ("/claude resolve", "resolve"),
                               ("/claude continue", "continue"), ("/claude new", "new")):
        route = command_surface.resolve(app, raw_request(app, command=command))
        assert (route["native_action"], route["arguments"]) == ("claude", arguments)
    with pytest.raises(ValueError, match="status | resolve | continue | new"):
        command_surface.resolve(app, raw_request(app, command="/claude replay"))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_app", [("custom", False)], indirect=True)
async def test_help_keys_mcp_and_clear_screen_route_to_native_views_with_inspection_arguments_only(command_app):
    app = command_app
    before = tree(app)
    for command, action, arguments in (
            ("/help", "help", ""), ("/?", "help", ""), ("/keys", "keys", ""),
            ("/mcp", "mcp", ""), ("/MCP List", "mcp", "List"), ("/mcp ls", "mcp", "ls"), ("/mcp status", "mcp", "status"),
            ("/clear-screen", "clear_screen", ""), ("/cls", "clear_screen", ""), ("/clearscreen", "clear_screen", "")):
        route = command_surface.resolve(app, raw_request(app, command=command))
        assert (route["native_action"], route["arguments"]) == (action, arguments), command
    for command, reason in (
            ("/help me", "/help takes no arguments"), ("/keys all", "/keys takes no arguments"),
            ("/cls now", "/clear-screen takes no arguments"),
            ("/mcp add x y", "list only"), ("/mcp remove x", "list only"), ("/mcp reconnect x", "list only"),
            ("/mcp reload", "list only")):
        with pytest.raises(ValueError, match=reason):
            command_surface.resolve(app, raw_request(app, command=command))
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("command_app", [("custom", False)], indirect=True)
async def test_every_unrouted_registered_command_states_its_own_reason(command_app):
    app = command_app
    rows = [row for row in read(app)["entries"] if row["native_action"] is None]
    reasons = {row["tokens"][0]: row["unavailable_reason"] for row in rows}
    assert set(reasons) == {"/screenshot", "/maximize", "/test-sidebar", "/monitor", "/sidecar", "/mark"}
    assert len(set(reasons.values())) == len(reasons)
    for token in ("/screenshot", "/maximize", "/test-sidebar"):
        assert "Textual" in reasons[token]
    for token in ("/monitor", "/sidecar", "/mark"):
        assert "desktop overlay" in reasons[token]
    assert "no native action adapter" not in "".join(reasons.values())
    for row in rows:
        with pytest.raises(ValueError) as refusal:
            command_surface.resolve(app, raw_request(app, command=row["tokens"][0]))
        assert str(refusal.value) == row["unavailable_reason"]
    # Only the exact reviewed owner inherits a reason; a lookalike gets the generic one.
    registry = plugins.PluginRegistry()
    registry.add_command("third-party", ("/screenshot",), forbidden)
    app.plugins, app.skills = registry, []
    assert "no native action adapter" in read(app)["entries"][0]["unavailable_reason"]
