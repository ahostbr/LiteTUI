"""Reviewed, source-owned MCP lifecycle with exact result/readback publication."""

from __future__ import annotations

import asyncio
import copy
import secrets
from dataclasses import asdict, dataclass
from pathlib import PurePath
from urllib.parse import urlsplit

from litetui import (
    backend_catalog,
    backend_transition,
    native_context,
    native_mcp_config,
    native_settings,
    settings,
    settings_runtime,
)
from litetui import background_input as work
from litetui import native_runtime_controls as controls
from litetui.agent_preparation import await_preparation
from litetui.shutdown_wait import join_task

VERSION = 1
PREFIX = "gui.mcp.native_lifecycle_"
OPERATIONS = tuple(
    PREFIX + verb for verb in ("snapshot", "review", "apply", "readback")
)
ACTIONS = ("enable", "disable", "connect", "disconnect", "reconnect", "reload")
CLOSE_REQUIRED = "MCP publication is uncertain. Close this runtime without replay and inspect it again."


@dataclass(eq=False)
class State:
    app: object
    bridge: object
    manager: object
    references: tuple
    inventory: tuple
    review: object = None
    hold: bool = False
    error: str | None = None
    action: object = None


@dataclass(eq=False)
class Review:
    token: str
    snapshot: dict
    action: str
    target: str | None
    policy_before: dict
    context: bytes
    saved: object = None
    before_inventory: tuple | None = None
    applied: str | None = None
    after: str | None = None
    current: dict | None = None
    status: str = "reviewed"
    policy_after: dict | None = None
    inventory: tuple | None = None
    apply_task: object = None
    apply_command: bytes | None = None
    apply_result: object = None
    apply_raw: bytes | None = None
    apply_published: bool = False
    readback_task: object = None
    readback_command: bytes | None = None
    readback_result: object = None
    readback_raw: bytes | None = None
    publication: object = None


@dataclass(eq=False)
class Publication:
    app: object
    state: State
    review: Review
    task: object
    result: object
    raw: bytes
    phase: str
    used: bool = False


def _encoded(value):
    return backend_catalog._encoded(value, 1024 * 1024)


def _digest(value):
    return work.digest(_encoded(value))


def _state(app):
    state = getattr(app, "_native_mcp_lifecycle", None)
    return state if isinstance(state, State) else None


def enabled(app):
    value = work.bridge(app)
    return bool(
        value is not None
        and value.active
        and getattr(value, "mcp_lifecycle_version", None) == VERSION
    )


def blocking(app):
    state = _state(app)
    return bool(state is not None and state.hold)


def _manager(app):
    manager, _ = native_mcp_config._root(app)
    if manager is not getattr(app, "_owned_mcp_manager", None):
        raise ValueError("MCP manager differs from its constructor owner")
    return manager


def _references(app):
    manager = _manager(app)
    return (
        app._agent_session,
        app.store,
        app.backend,
        app.settings,
        app.plugins,
        manager.enabled,
        manager.cache,
        getattr(app.rebuild_mcp_dispatch, "__func__", None),
    )


def _dispatch_matches(cached, actual):
    if (
        not callable(cached)
        or not callable(actual)
        or getattr(cached, "__code__", None) is not getattr(actual, "__code__", None)
        or getattr(cached, "__defaults__", None)
        != getattr(actual, "__defaults__", None)
    ):
        return False
    left, right = (
        getattr(cached, "__closure__", None),
        getattr(actual, "__closure__", None),
    )
    return bool(
        left is not None
        and right is not None
        and len(left) == len(right)
        and all(
            a.cell_contents is b.cell_contents for a, b in zip(left, right, strict=True)
        )
    )


def _inventory(app):
    """Independent manager specs plus exact cached callables, never a tool invocation."""
    manager = _manager(app)
    with manager._servers_lock:
        servers = tuple(sorted(manager.servers.items()))
        failed = tuple(sorted(manager._stop_failed))
        if (
            manager._lazy_pending
            or manager._lazy_tools
            or app.settings.mcp_enabled
            and manager.lazy
        ):
            raise ValueError(
                "Reviewed MCP lifecycle requires explicit connections, without lazy startup"
            )
        configs = _encoded(manager.configs)
        tools = _encoded({name: server.tools for name, server in servers})
        active_configs = _encoded({name: server.cfg for name, server in servers})
    specs = manager.tool_specs()
    names = [spec["function"]["name"] for spec in specs]
    cache = app.mcp_dispatch
    if (
        len(names) > 4096
        or len(names) != len(set(names))
        or set(names) != set(cache)
        or any(
            not _dispatch_matches(cache[name], manager.dispatch_for(name))
            for name in names
        )
    ):
        raise ValueError("MCP inventory and independently inspected dispatch disagree")
    providers = [entry for entry in app.plugins.dynamic if entry.owner == "mcp"]
    if names:
        if len(providers) != 1 or providers[0].specs_fn() != specs:
            raise ValueError("MCP plugin inventory differs from the owned manager")
        for name in names:
            resolved = next(
                (
                    (entry, entry.dispatch_fn(name))
                    for entry in app.plugins.dynamic
                    if entry.dispatch_fn(name) is not None
                ),
                None,
            )
            if (
                name in app.plugins._tool_by_name
                or resolved is None
                or resolved[0] is not providers[0]
                or resolved[1] is not cache[name]
            ):
                raise ValueError(
                    "Host registry dispatch differs from the reviewed MCP callable"
                )
    return (
        configs,
        tools,
        active_configs,
        servers,
        failed,
        _encoded(specs),
        tuple(sorted(cache.items())),
        bool(manager.lazy),
        _encoded(
            None if manager.seat_servers is None else sorted(manager.seat_servers)
        ),
        _encoded(app.settings.mcp_disabled_servers),
        tuple(app.plugins.dynamic),
        tuple(sorted(app.plugins._tool_by_name.items())),
    )


def capture(app, value):
    if (
        getattr(getattr(app, "_launch_options", None), "mcp_lifecycle_version", None)
        != VERSION
    ):
        raise ValueError(
            "MCP lifecycle requires its explicit startup-off launch capability"
        )
    manager = _manager(app)
    if (
        app.settings.mcp_enabled is not False
        or manager.servers
        or manager._stop_failed
        or manager._closing
        or manager._maint_active
    ):
        raise ValueError(
            "MCP lifecycle capture requires the explicit startup-off policy and no transports"
        )
    value.mcp_lifecycle_capture = (_references(app), _inventory(app))
    return {"mcp_lifecycle_version": VERSION}


def activation(app, value):
    prior = getattr(value, "mcp_lifecycle_capture", None)
    if (
        prior is None
        or app.settings.mcp_enabled is not False
        or any(a is not b for a, b in zip(prior[0], _references(app), strict=True))
        or prior[1] != _inventory(app)
    ):
        raise ValueError("MCP ownership or inventory changed after capture")
    return {"mcp_lifecycle_version": VERSION}


def activated(app, value):
    activation(app, value)
    app._native_mcp_lifecycle = State(
        app, value, _manager(app), _references(app), _inventory(app)
    )


def _same_owner(app, state):
    # After /backend the model is unchosen and /model is how one is chosen (Ryan 2026-11-12).
    work.validate_identity(app)
    if (
        not enabled(app)
        or state is not _state(app)
        or state.app is not app
        or state.bridge is not work.bridge(app)
        or state.manager is not _manager(app)
        or any(
            a is not b for a, b in zip(state.references, _references(app), strict=True)
        )
    ):
        raise ValueError("MCP lifecycle lost its captured runtime owner")


def _same(app, state):
    _same_owner(app, state)
    state.bridge.check()
    if state.error:
        raise ValueError(state.error)


def require_inventory(app):
    if not enabled(app):
        return
    state = _state(app)
    _same(app, state)
    if state.hold or _inventory(app) != state.inventory:
        raise ValueError("MCP inventory lacks its exact published lifecycle readback")


def _fatal(app, state):
    state.hold = True
    state.error = CLOSE_REQUIRED
    controls._state(app).error = CLOSE_REQUIRED
    controls.signal_stop(app)


def _quiet(app, state):
    controls.require_mcp_prior_settled(app)
    backend_transition._quiet(app, require_unowned_server=False)
    from textual.worker import NoActiveWorker, get_current_worker

    from litetui.plugins.mcp_manage import _mutation_blocked_reason

    try:
        worker = get_current_worker()
    except NoActiveWorker:
        worker = None
    own = (
        (worker,)
        if worker is not None
        and worker.group == "gui-management"
        and worker._task is asyncio.current_task()
        else ()
    )
    reason = _mutation_blocked_reason(app, ignore_workers=own)
    runtime = controls._state(app)
    if reason or runtime is None or runtime.tickets or runtime.closing or runtime.error:
        raise ValueError(
            reason or "MCP lifecycle requires an idle published runtime policy"
        )
    if (
        controls.mutation_hold(app)
        or state.manager._closing
        or state.manager._maint_active
    ):
        raise ValueError("Another owned operation must settle before MCP management")
    require_inventory(app)


def _declarations(app):
    files = native_mcp_config._files(_manager(app).root)
    merged = {}
    for raw in files:
        _, block, _ = native_mcp_config._doc(raw)
        for name, config in block.items():
            if (
                not native_mcp_config.SAFE_NAME.fullmatch(name)
                or not isinstance(config, dict)
                or native_mcp_config.mcp_client.validate_entry(config) is not None
                or type(config.get("disabled", False)) is not bool
            ):
                raise ValueError(
                    "MCP declarations require bounded names and valid transport entries"
                )
            merged.setdefault(name, config)
    if len(merged) > 64:
        raise ValueError("MCP lifecycle supports at most 64 reviewed declarations")
    return files, merged


def _saved(app):
    context = backend_catalog._context(app, app.convo_id)
    service = settings_runtime.service_for(app)
    before = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    paths, documents, revisions = native_settings.owned_documents(
        service, context.session_id, app._agent_session
    )
    if revisions != before.revisions:
        raise ValueError("MCP preference destination changed during inspection")
    return context, service, before, paths, documents


def _context(app):
    return _encoded(
        {
            "settings": asdict(app.settings),
            "history": app.conversation,
            "authority": asdict(app._agent_session.authority),
            "invocation": getattr(app, "_invocation_saved_values", {}),
            "launch": getattr(app, "_launch_overrides", {}),
        }
    )


def _redacted(config):
    if config.get("command"):
        target = PurePath(config["command"].replace("\\", "/")).name
        return "stdio", target[:120] if target and target.isprintable() and not any(
            char in target for char in "@?#"
        ) else "configured executable"
    try:
        url = urlsplit(config["url"])
        return "http", (
            f"{url.scheme}://{url.hostname or 'configured host'}"
            + (f":{url.port}" if url.port else "")
        )[:120]
    except ValueError:
        return "http", "configured HTTP endpoint"


def _choice(reason):
    return {"eligible": reason is None, "reason": reason}


def _snapshot(app, state, *, inventory=None):
    _same(app, state)
    manager = state.manager
    files, declarations = _declarations(app)
    _, _, saved, _, _ = _saved(app)
    facts = controls._facts(app)
    inventory = _inventory(app) if inventory is None else inventory
    unsupported = (
        "Native Codex owns a fixed MCP inventory; runtime server changes require a restart."
        if hasattr(app.backend, "app_server")
        else None
    )
    environment = (
        "An environment override owns the MCP preference."
        if settings.source_of("mcp_enabled")
        else None
    )
    plugin_reason = (
        None
        if sum(entry.owner == "mcp" for entry in app.plugins.dynamic) == 1
        else "The MCP plugin is unavailable in this runtime."
    )
    base_reason = unsupported or environment or plugin_reason
    active = bool(app.settings.mcp_enabled)
    actions = {
        "enable": _choice(
            base_reason or ("MCP is already enabled." if active else None)
        ),
        "disable": _choice(
            base_reason
            or (None if active or manager.servers else "MCP is already disabled.")
        ),
        "reload": _choice(unsupported),
    }
    rows = []
    with manager._servers_lock:
        servers = dict(manager.servers)
        failed = set(manager._stop_failed)
        configs = copy.deepcopy(manager.configs)
    names = sorted(set(declarations) | set(configs) | set(servers))
    if len(names) > 64:
        raise ValueError("MCP lifecycle exceeds the complete 64-server review budget")
    for name in names:
        cfg = declarations.get(name) or configs.get(name) or servers[name].cfg
        transport, target = _redacted(cfg)
        server = servers.get(name)
        if (
            any(len(cfg.get(key) or []) > 4096 for key in ("args", "env", "headers"))
            or server is not None
            and len(server.tools) > 4096
        ):
            raise ValueError(
                "MCP declaration or inventory exceeds the complete review budget"
            )
        loaded = name in declarations and configs.get(name) == declarations[name]
        allowed = bool(manager.allowed(name))
        disabled = bool(cfg.get("disabled", False))
        reason = (
            unsupported
            or plugin_reason
            or ("Enable MCP first." if not active else None)
            or ("Reload the current declaration first." if not loaded else None)
            or (
                "This declaration or seat excludes the server."
                if disabled or not allowed
                else None
            )
        )
        stop_reason = unsupported or (
            None if server is not None else "The server is disconnected."
        )
        rows.append(
            {
                "name": name,
                "transport": transport,
                "target": target,
                "config_sha256": _digest(cfg),
                "loaded": loaded,
                "disabled": disabled,
                "allowed": allowed,
                "state": "failed"
                if name in failed
                or name in manager.failures
                or server is not None
                and bool(getattr(server, "error", None))
                else "connected"
                if server is not None
                else "disconnected",
                "tools": len(server.tools) if server is not None else 0,
                "active_config_sha256": _digest(server.cfg)
                if server is not None
                else None,
                "args": len(cfg.get("args") or []),
                "env": len(cfg.get("env") or {}),
                "headers": len(cfg.get("headers") or {}),
                "actions": {
                    "connect": _choice(
                        reason
                        or (
                            "Disconnect the retained failed server first."
                            if name in failed
                            else "The server is already connected."
                            if server is not None
                            else None
                        )
                    ),
                    "disconnect": _choice(stop_reason),
                    "reconnect": _choice(
                        reason
                        or (
                            "Disconnect the retained failed server first."
                            if name in failed
                            else None
                        )
                    ),
                },
            }
        )
    result = {
        "version": VERSION,
        "kind": "mcp_lifecycle",
        **state.bridge.identity,
        "work_instance": state.bridge.instance,
        "enabled": active,
        "saved_enabled": bool(saved.saved.mcp_enabled),
        "overridden": bool(
            settings.source_of("mcp_enabled")
            or "mcp_enabled" in getattr(app, "_invocation_saved_values", {})
        ),
        "provider": work.provider(app),
        "tools_enabled": facts["tools_enabled"],
        "profile": facts["effective_profile"],
        "approval_route": facts["approval_route"],
        "model_inventory": {
            "host": "host_dynamic",
            "claude": "claude_fixed",
            "codex": "codex_fixed",
        }[work.provider(app)],
        "config_revision": native_mcp_config._revision(files),
        "loaded_config_revision": _digest(configs),
        "dispatch": {
            "count": len(app.mcp_dispatch),
            "sha256": work.digest(inventory[5]),
        },
        "actions": actions,
        "rows": rows,
    }
    result["revision"] = _digest(
        {
            "state": result,
            "context": work.digest(_context(app)),
            "saved": saved.revisions,
            "facts": facts,
        }
    )
    return result


def _selected(snap, action, target):
    if action not in ACTIONS:
        raise ValueError("Unknown MCP lifecycle action")
    if action in ("enable", "disable", "reload"):
        if target is not None:
            raise ValueError("This MCP operation has no individual server target")
        row = snap["actions"][action]
    else:
        matches = [row for row in snap["rows"] if row["name"] == target]
        if len(matches) != 1:
            raise ValueError("Select an exact reviewed MCP server")
        row = matches[0]["actions"][action]
    if not row["eligible"]:
        raise ValueError(row["reason"])


def _policy(app):
    runtime = controls._state(app)
    if not controls.enabled(app) or runtime.generation >= controls.MAX_GENERATION:
        raise ValueError("MCP lifecycle needs an advancing published runtime policy")
    return {"generation": runtime.generation, "facts": controls._facts(app)}


def _receipt(state, review, kind):
    return {
        "version": VERSION,
        "kind": kind,
        **state.bridge.identity,
        "work_instance": state.bridge.instance,
        "token": review.token,
        "action": review.action,
        "target": review.target,
        "before": review.snapshot["revision"],
        "after": review.after,
        "apply_id": review.applied,
        "status": review.status,
        "policy_before": copy.deepcopy(review.policy_before),
        "policy_after": copy.deepcopy(review.policy_after),
    }


def _save_enabled(app, cmd, state, review, want):
    def reader(owner, _cmd):
        _same(owner, state)
        context, service, before, paths, _ = _saved(owner)
        if (
            before.revisions != review.saved[2].revisions
            or _saved(owner)[4] != review.saved[4]
            or _context(owner) != review.context
        ):
            raise ValueError("MCP preference authority changed after review")
        return (
            {
                "field": "mcp_enabled",
                "value": want,
                "scope": "conversation",
                "destination": str(paths["conversation"]),
                "effective_before": owner.settings.mcp_enabled,
            },
            context,
            service,
            before,
        )

    def result(_owner, _cmd, status, **details):
        if (
            status in {"saved", "partial"}
            and details.get("saved") is want
            and not details.get("close_required")
        ):
            return None
        return "MCP preference publication did not match canonical durable readback"

    error = native_settings.publish_reviewed(
        app, cmd, reader, result, lambda _key, value: value, strict=True
    )
    if error:
        raise ValueError(error)
    # This explicitly reviewed lifecycle operation applies the reconnect-timed
    # preference after SettingsService's durable readback. It starts no server.
    app.settings.mcp_enabled = want
    getattr(app, "_invocation_saved_values", {}).pop("mcp_enabled", None)
    from litetui import convo_settings

    app._convo_settings = convo_settings.load(app.convo_dir)
    for attribute in ("_saved_values", "_baseline"):
        values = copy.deepcopy(getattr(app.settings, attribute, {}))
        values["mcp_enabled"] = want
        object.__setattr__(app.settings, attribute, values)
    if (
        _saved(app)[2].saved.mcp_enabled is not want
        or app.settings.mcp_enabled is not want
    ):
        raise ValueError("MCP preference failed independent final readback")


async def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    fields = {
        "snapshot": (),
        "review": ("revision", "action", "target"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    if op not in fields or not enabled(app):
        raise ValueError(
            "Negotiate MCP lifecycle version 1 before managing its runtime"
        )
    native_context.read_binding(app, cmd, PREFIX + op, *fields[op])
    state = _state(app)
    _same(app, state)
    review = state.review
    if state.hold and op != "readback":
        raise ValueError("Await exact MCP result and readback publication")
    if op in ("apply", "readback") and (
        not isinstance(review, Review) or review.token != cmd["token"]
    ):
        raise ValueError("Review the current MCP operation again")
    if op == "readback":
        try:
            if (
                not review.apply_published
                or review.readback_result is not None
                or review.applied != cmd["apply_id"]
                or review.inventory != _inventory(app)
                or review.current != _snapshot(app, state)
                or review.policy_after["facts"] != controls._facts(app)
            ):
                raise ValueError(
                    "MCP readback lost the exact applied inventory or policy"
                )
            result = {
                **_receipt(state, review, "mcp_lifecycle_settled"),
                "state": copy.deepcopy(review.current),
            }
            review.readback_task, review.readback_command = (
                asyncio.current_task(),
                _encoded(cmd),
            )
            review.readback_result, review.readback_raw = result, _encoded(result)
            return result
        except BaseException:
            _fatal(app, state)
            raise
    require_inventory(app)
    snap = _snapshot(app, state)
    if op == "snapshot":
        return snap
    _quiet(app, state)
    if op == "review":
        if snap["revision"] != cmd["revision"]:
            raise ValueError("Refresh MCP state before reviewing this operation")
        _selected(snap, cmd["action"], cmd["target"])
        review = Review(
            secrets.token_hex(32),
            snap,
            cmd["action"],
            cmd["target"],
            _policy(app),
            _context(app),
        )
        review.saved, review.before_inventory = _saved(app), state.inventory
        if _snapshot(app, state) != snap:
            raise ValueError("MCP review changed while binding durable documents")
        state.review = review
        return _receipt(state, review, "mcp_lifecycle_review")
    if (
        review.applied is not None
        or review.snapshot != snap
        or review.context != _context(app)
        or review.policy_before != _policy(app)
    ):
        raise ValueError("MCP operation or runtime policy changed after review")
    _selected(snap, review.action, review.target)
    state.hold, state.action = True, asyncio.current_task()
    manager = state.manager
    try:
        failed = False
        if review.action == "enable":
            _save_enabled(app, cmd, state, review, True)
            manager.lazy = False
            await await_preparation(manager.reload_configs)
        elif review.action == "disable":
            targets = tuple(manager.servers)
            for name in targets:
                await await_preparation(lambda name=name: manager.disconnect(name))
            failed = bool(manager.servers or manager._stop_failed)
            if not failed:
                _save_enabled(app, cmd, state, review, False)
        elif review.action == "reload":
            await await_preparation(manager.reload_configs)
        else:
            outcome = await await_preparation(
                lambda: getattr(manager, review.action)(review.target)
            )
            failed = (
                bool(outcome)
                if review.action in ("connect", "reconnect")
                else review.target in manager.servers
            )
        _same(app, state)
        if (
            review.action in ("enable", "reload")
            and manager.configs != _declarations(app)[1]
        ):
            raise ValueError("Canonical MCP reload differs from reviewed declarations")
        if (
            native_mcp_config._revision(_declarations(app)[0])
            != review.snapshot["config_revision"]
        ):
            raise ValueError("MCP declarations changed during the reviewed operation")
        before_context, after_context = (
            work.strict_json(review.context),
            work.strict_json(_context(app)),
        )
        for context in (before_context, after_context):
            context["settings"].pop("mcp_enabled", None)
            context["invocation"].pop("mcp_enabled", None)
        if before_context != after_context:
            raise ValueError(
                "MCP operation changed unrelated runtime settings or conversation state"
            )
        expected_documents = copy.deepcopy(review.saved[4])
        if review.action == "enable" or review.action == "disable" and not failed:
            from litetui.settings_service import SettingChange, conversation_document

            expected_documents["conversation"] = conversation_document(
                expected_documents["conversation"],
                review.saved[2].saved,
                [
                    SettingChange(
                        "mcp_enabled", review.action == "enable", "conversation"
                    )
                ],
            )
        if _saved(app)[4] != expected_documents:
            raise ValueError("MCP operation changed unrelated durable settings")
        before_servers = dict(review.before_inventory[3])
        after_servers = dict(manager.servers)
        if review.action in ("connect", "disconnect", "reconnect"):
            before_servers.pop(review.target, None)
            after_servers.pop(review.target, None)
        elif review.action == "disable":
            if any(
                before_servers.get(name) is not server
                for name, server in after_servers.items()
            ):
                raise ValueError("Disable encountered an unreviewed server owner")
            before_servers = {name: before_servers[name] for name in after_servers}
        if before_servers != after_servers:
            raise ValueError("MCP operation changed an unrelated server owner")
        if review.action != "disable":
            old_rows = {row["name"]: row for row in review.snapshot["rows"]}
            old_tools = work.strict_json(review.before_inventory[1])
            for name, server in before_servers.items():
                row = old_rows[name]
                failed_now = (
                    name in manager._stop_failed
                    or name in manager.failures
                    or bool(getattr(server, "error", None))
                )
                if (
                    server.tools != old_tools[name]
                    or _digest(server.cfg) != row["active_config_sha256"]
                    or ("failed" if failed_now else "connected") != row["state"]
                ):
                    raise ValueError(
                        "MCP operation changed an unrelated active server inventory"
                    )
        app.rebuild_mcp_dispatch()
        review.inventory = _inventory(app)
        current = _snapshot(app, state, inventory=review.inventory)
        policy = {
            "generation": review.policy_before["generation"] + 1,
            "facts": controls._facts(app),
        }
        controls.validate_mcp_policy(app, review.policy_before, policy)
        if (
            review.action in ("connect", "reconnect")
            and not failed
            and (
                review.target not in manager.servers
                or review.target in manager._stop_failed
            )
        ):
            raise ValueError("MCP connection lacks its exact retained server")
        review.status = "failed" if failed else "completed"
        review.applied, review.after, review.current = (
            cmd["id"],
            current["revision"],
            current,
        )
        review.policy_after = policy
        review.apply_task, review.apply_command = asyncio.current_task(), _encoded(cmd)
        result = _receipt(state, review, "mcp_lifecycle_applied")
        review.apply_result, review.apply_raw = result, _encoded(result)
        return result
    except BaseException as exc:
        # A cancelled thread is joined by await_preparation. Keep the exact
        # manager and hold for shutdown; never guess rollback or retry effects.
        if getattr(app, "mcp", None) is manager:
            app.rebuild_mcp_dispatch()
        _fatal(app, state)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ValueError(CLOSE_REQUIRED) from exc
    finally:
        state.action = None


def readback_allowed(app, cmd):
    state = _state(app)
    review = state.review if state is not None else None
    return bool(
        isinstance(review, Review)
        and cmd.get("type") == PREFIX + "readback"
        and review.apply_published
        and review.readback_result is None
        and cmd.get("token") == review.token
        and cmd.get("apply_id") == review.applied
    )


def prepare_publication(app, cmd, result):
    op = cmd.get("type")
    if op not in {PREFIX + "apply", PREFIX + "readback"}:
        return None
    state = _state(app)
    review = state.review if state is not None else None
    phase = "apply" if op.endswith("_apply") else "readback"
    try:
        if (
            not isinstance(review, Review)
            or review.publication is not None
            or asyncio.current_task() is not getattr(review, phase + "_task")
            or _encoded(cmd) != getattr(review, phase + "_command")
            or result is not getattr(review, phase + "_result")
            or _encoded(result) != getattr(review, phase + "_raw")
            or phase == "apply"
            and review.apply_published
        ):
            raise ValueError("MCP publication is foreign, changed or consumed")
        _same(app, state)
        if review.inventory != _inventory(app) or review.current != _snapshot(
            app, state
        ):
            raise ValueError("MCP inventory changed before publication")
        ticket = Publication(
            app, state, review, asyncio.current_task(), result, _encoded(result), phase
        )
        review.publication = ticket
        return ticket
    except BaseException:
        if state is not None:
            _fatal(app, state)
        raise


def finish_publication(app, ticket):
    state, review = ticket.state, ticket.review
    try:
        if (
            ticket.app is not app
            or ticket.used
            or ticket.task is not asyncio.current_task()
            or review.publication is not ticket
            or _encoded(ticket.result) != ticket.raw
            or state.review is not review
            or review.inventory != _inventory(app)
            or review.current != _snapshot(app, state)
        ):
            raise ValueError("MCP publication lost its exact one-use owner")
        ticket.used = True
        review.publication = None
        if ticket.phase == "apply":
            review.apply_published = True
            return
        controls.install_mcp_policy(app, ticket)
        state.inventory = review.inventory
        state.review, state.hold = None, False
        controls.signal_stop(app)
        if getattr(app, "_pending_input", ()) and not getattr(
            app, "_gui_quitting", False
        ):
            app.call_later(app._flush_pending_input)
    except BaseException:
        _fatal(app, state)
        raise


async def settle_before_shutdown(app):
    state = _state(app)
    if state is None:
        return
    state.hold = True
    action = state.action
    if action is not None:
        if action is asyncio.current_task():
            raise RuntimeError("MCP lifecycle cannot join its own action")
        if not action.done():
            action.cancel()
        try:
            await join_task(action)
        except Exception as exc:  # noqa: BLE001 - shutdown retains the exact manager for cleanup
            from litetui import runtime_log

            runtime_log.record_error(
                "mcp_lifecycle_failed_during_shutdown", detail=type(exc).__name__
            )



def _retired_transition_inventory(app, state):
    """Inspect only a completed quiet retirement; never reopen MCP admission."""
    _same_owner(app, state)
    value = state.bridge
    task = value.shutdown_task
    proof = getattr(value, "transition_proof", None)
    cutoff = value.close_event
    if (
        not value.closing
        or not isinstance(task, asyncio.Task)
        or not task.done()
        or task.cancelled()
        or task.exception() is not None
        or getattr(task.get_coro(), "cr_code", None) is not work._settle_shutdown.__code__
        or not isinstance(proof, dict)
        or not isinstance(cutoff, dict)
        or cutoff.get("type") != "work_closing"
        or cutoff.get("instance") != value.instance
        or cutoff.get("identity") != value.identity
        or cutoff.get("cutoff_sequence") != cutoff.get("sequence")
        or getattr(app, "_gui_quitting", False)
        or getattr(app, "_conversation_transition_shutdown_requested", False)
        or app._chat_running()
        or app._pending_input
        or value.reservations
        or any(entry.claim_task is not None for entry in value.inputs.values())
    ):
        raise ValueError("MCP transition inspection requires exact completed quiet Work retirement")
    # Bind the successful retained task result to the exact cutoff and current
    # ledger. A copied dictionary or merely closing bridge is not retirement.
    ids = set(cutoff["input_ids"])
    ids.update(entry.ref["input_id"] for entry in value.inputs.values()
               if entry.ref["producer_id"] in cutoff["producer_ids"])
    expected = {
        "version": work.VERSION, "instance": value.instance,
        "closing_sequence": cutoff["sequence"], "final_sequence": value.sequence,
        "admission_closed": True, "worker_settled": True,
        "callback_settled": True, "process_settled": True,
        "producer_ids": cutoff["producer_ids"],
        "inputs": [{"input_id": key, "disposition": value.inputs[key].state}
                   for key in sorted(ids)],
        "admission_ids": cutoff["admission_ids"],
        "ledger_sha256": work.digest(value.raw if value.raw is not None else b""),
        "effects_verified": False,
    }
    if (work.encoded(task.result()) != work.encoded(proof)
            or work.encoded(proof) != work.encoded(expected)
            or work._read(value.path) != value.raw):
        raise ValueError("MCP transition inspection lost its exact Work retirement proof")
    runtime = controls._state(app)
    retirement = getattr(value, "runtime_controls_retirement", None)
    if (runtime is None or runtime.bridge is not value or not runtime.closing
            or runtime.shutdown is None or runtime.error or runtime.tickets or runtime.waiters
            or not isinstance(retirement, dict)
            or retirement.get("runtime_controls_shutdown") != runtime.shutdown):
        raise ValueError("MCP transition inspection requires settled runtime authorization")
    value.check(allow_closing=True)
    manager = state.manager
    if (state.hold or state.error or state.action is not None and not state.action.done()
            or app.settings.mcp_enabled is not False or manager.servers or manager._stop_failed
            or manager._closing or manager._maint_active or _inventory(app) != state.inventory):
        raise ValueError("MCP transition inspection requires exact disabled published inventory")


def transition_refusal(app):
    if not enabled(app):
        return None
    state = _state(app)
    if state.hold or state.error:
        return "Settle the exact MCP operation before changing runtime identity."
    if state.bridge.closing:
        _retired_transition_inventory(app, state)
    else:
        require_inventory(app)
    if app.settings.mcp_enabled or state.manager.servers or state.manager._stop_failed:
        return "Review Disable in MCP Runtime before changing conversation, backend or model."
    return None


def require_transition(app):
    if reason := transition_refusal(app):
        raise ValueError(reason)


def apply_startup_overlay(app):
    """A new captured identity starts off even when its saved preference is on."""
    state = _state(app)
    if state is None or not state.bridge.closing:
        return
    manager = _manager(app)
    if manager.servers or manager._stop_failed:
        raise ValueError("Target MCP overlay requires settled outgoing transports")
    if not (
        "mcp_enabled" in app._invocation_saved_values
        and app._launch_overrides.get("mcp_enabled") is False
    ):
        app._invocation_saved_values["mcp_enabled"] = app.settings.mcp_enabled
    app._launch_overrides["mcp_enabled"] = False
    app.settings.mcp_enabled = False


def retired(app, value):
    state = _state(app)
    if state is None:
        return
    if (
        state.bridge is not value
        or not value.closing
        or state.hold
        or state.manager.servers
        or state.manager._stop_failed
    ):
        raise ValueError("MCP identity changed without an exact disabled retirement")
    apply_startup_overlay(app)
    app._native_mcp_lifecycle = None
