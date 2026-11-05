"""Reviewed static tool-description reload for the current Work owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from dataclasses import dataclass

from litetui import gui_rpc, native_context, tool_schemas
from litetui.plugin_reload_commit import commit_metadata_candidate
from litetui.plugin_reload_provenance import provenance_ok
from litetui.plugin_reload_state import blocking_reasons
from litetui.plugin_schema_reload import stage_schema_refresh
from litetui.plugins import PluginRegistry
from litetui.plugins import plugin_reload_ui as source

PREFIX = "gui.plugins.native_"
MAX_REGISTRY = 512
MAX_SCHEMA = 65536
MAX_TEXT = 8192
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


@dataclass
class Reviewed:
    expected: PluginRegistry
    instance: str
    name: str
    token: str
    before_spec: str
    after_spec: str
    before: str
    after: str
    installed: PluginRegistry | None = None
    apply_id: str | None = None


def _eligible(registry):
    if not isinstance(registry, PluginRegistry) or len(registry.tools) > MAX_REGISTRY:
        raise ValueError("Static tool registry unavailable or too large")
    # Probe only the registered literal names in the same two canonical static
    # schema homes as tool_schemas.available(). Never enumerate an arbitrary home.
    package = tool_schemas._package_dir()
    legacy = tool_schemas._legacy_dir()
    names = set()
    for entry in registry.tools:
        name = entry.name
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or name == "skills"
            or name in source._TEMPLATED_EXCLUDE
        ):
            continue
        if (package is not None and package.joinpath(name + ".json").is_file()) or (
            legacy / (name + ".json")
        ).is_file():
            if name in names:
                raise ValueError("Ambiguous static tool registration")
            names.add(name)
    return sorted(names)


def _hash(spec):
    # Canonical registry schemas are JSON objects. Limit encoded input before
    # producing a retained review; no handler, gate or runtime spec producer runs.
    h = hashlib.sha256()
    size = 0
    for chunk in json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).iterencode(spec):
        raw = chunk.encode("utf-8")
        size += len(raw)
        if size > MAX_SCHEMA:
            raise ValueError("Static schema exceeds the metadata reload limit")
        h.update(raw)
    return h.hexdigest()


def _description(spec):
    description = spec["function"].get("description", "")
    if (
        type(description) is not str
        or len(description) > MAX_TEXT
        or len(description.encode("utf-8")) > MAX_TEXT
    ):
        raise ValueError("Static tool description is unavailable or exceeds 8192 bytes")
    return description


def _checked(app, registry, name):
    if app.plugins is not registry:
        raise ValueError("Registry generation changed; review again")
    if hasattr(app.backend, "app_server"):
        raise ValueError("Native Codex tool inventory requires a restart")
    if type(name) is not str or name not in _eligible(registry):
        raise ValueError("Choose a registered static tool schema")
    entry = next(e for e in registry.tools if e.name == name)
    fresh = tool_schemas.load_fresh_bounded(name, MAX_SCHEMA)
    if source._has_placeholder(fresh):
        raise ValueError("Templated tool schema requires a restart")
    before, after = _description(entry.spec), _description(fresh)
    candidate = stage_schema_refresh(registry, {name: fresh})
    if before == after:
        raise ValueError("Description already matches disk")
    ok, _reason = provenance_ok(app, name)
    if not ok:
        raise ValueError(
            "Handler or reload source changed or is unavailable; restart required"
        )
    return candidate, (_hash(entry.spec), _hash(fresh), before, after)


def _activity(app, cmd):
    from textual.worker import NoActiveWorker, get_current_worker

    # The RPC worker hosting *this exact command* is the caller,
    # equivalent to the owned Textual management dialog. Sibling workers in
    # gui-management, inherited task contexts and other app owners still block.
    try:
        caller = get_current_worker()
    except NoActiveWorker:
        caller = None
    own = (
        (caller,)
        if (
            caller is not None
            and caller.node is app
            and caller.group == "gui-management"
            and caller._task is asyncio.current_task()
            and gui_rpc.OPERATION_CONTEXT.get() == cmd["id"]
        )
        else ()
    )
    return source.current_activity(app, ignore_workers=own)


def dispatch(app, cmd):
    action = cmd["type"].removeprefix(PREFIX)
    fields = {
        "list": (),
        "review": ("name",),
        "apply": ("name", "token"),
        "readback": ("name", "token", "apply_id"),
    }
    bridge, binding = native_context.read_binding(
        app, cmd, PREFIX + action, *fields[action]
    )
    common = {"version": 1, **binding}
    from litetui import native_skills_index

    if action == "list":
        app._native_plugin_reload = None
        names = _eligible(app.plugins)
        if native_skills_index.registered(app):
            if len(names) >= MAX_REGISTRY:
                raise ValueError("Registered reload targets exceed the review limit")
            names = sorted([*names, "skills"])
        return {**common, "kind": "plugin_list", "names": names}
    name = cmd["name"]
    if name == "skills":
        return native_skills_index.dispatch(app, cmd, bridge, common, _activity)
    if action == "review":
        app._native_plugin_reload = None
        gui_rpc._idle(app)
        expected = app.plugins
        _, data = _checked(app, expected, name)
        blockers = blocking_reasons(_activity(app, cmd))
        if blockers:
            raise ValueError("Reload deferred: " + ", ".join(blockers))
        review = Reviewed(expected, bridge.instance, name, secrets.token_hex(32), *data)
        app._native_plugin_reload = review
        return {
            **common,
            "kind": "plugin_review",
            "name": name,
            "token": review.token,
            "before": review.before,
            "after": review.after,
            "before_spec": review.before_spec,
            "after_spec": review.after_spec,
        }
    review = getattr(app, "_native_plugin_reload", None)
    if (
        not isinstance(review, Reviewed)
        or review.instance != bridge.instance
        or review.name != name
        or cmd["token"] != review.token
    ):
        raise ValueError("Review this exact plugin metadata change again")
    if action == "apply":
        gui_rpc._idle(app)
        if review.apply_id is None:
            app._native_plugin_reload = None
            candidate, data = _checked(app, review.expected, name)
            if data != (
                review.before_spec,
                review.after_spec,
                review.before,
                review.after,
            ):
                raise ValueError("Static schema changed since review")

            def guarded_activity():
                activity = _activity(app, cmd)
                bridge.check()
                app._validate_owned_identity()
                gui_rpc._idle(app)
                return activity

            result = commit_metadata_candidate(
                app, review.expected, candidate, activity=guarded_activity
            )
            if result.status != "reloaded":
                raise ValueError(
                    "Metadata reload "
                    + result.status
                    + ": "
                    + ", ".join(result.reasons)
                )
            review.installed, review.apply_id = candidate, cmd["id"]
            app._native_plugin_reload = review
        elif review.apply_id != cmd["id"]:
            raise ValueError("This review was already applied")
        kind = "plugin_applied"
    else:
        if (
            review.apply_id is None
            or cmd["apply_id"] != review.apply_id
            or app.plugins is not review.installed
        ):
            raise ValueError(
                "Current registry no longer matches this metadata publication"
            )
        if len(app.plugins.tools) > MAX_REGISTRY:
            raise ValueError("Current registry exceeds the independent readback limit")
        matches = [entry for entry in app.plugins.tools if entry.name == name]
        if (
            len(matches) != 1
            or _hash(matches[0].spec) != review.after_spec
            or _description(matches[0].spec) != review.after
        ):
            raise ValueError(
                "Published plugin description could not be independently verified"
            )
        kind = "plugin_verified"
    return {
        **common,
        "kind": kind,
        "name": name,
        "token": review.token,
        "apply_id": review.apply_id,
        "after_spec": review.after_spec,
        **(
            {"description": _description(matches[0].spec)}
            if action == "readback"
            else {}
        ),
    }
