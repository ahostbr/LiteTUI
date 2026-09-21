"""`/reload-plugins` — the operator command for the ONE supported reload op:
refresh a single static tool's DESCRIPTION from disk (metadata only).

This is deliberately NOT a full plugin reload. Handlers, argument contracts,
new tools, and native Codex threads all require a restart; this command says so
rather than pretending otherwise. All safety lives in the core helpers
(stage_schema_refresh / commit_metadata_candidate / produce_activity): this file
only parses the command, validates the name, and relays the CommitResult. It
never imports-reloads, never activates, never retries automatically, and never
touches app.plugins except through commit_metadata_candidate.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.plugin_reload_activity import produce_activity
from litetui.plugin_reload_children import children_pending
from litetui.plugin_reload_commit import commit_metadata_candidate
from litetui.plugin_reload_provenance import capture_baseline, provenance_ok
from litetui.plugin_schema_reload import stage_schema_refresh

#: Schemas whose DESCRIPTION carries an unresolved runtime placeholder that the
#: live spec has already filled — e.g. powershell.json's "{exe}", filled with the
#: interpreter found on this box (core_tools.powershell_spec). A raw description
#: refresh would install the literal "{exe}". Excluded until a template binding is
#: reused; no guessing the fmt here.
_TEMPLATED_EXCLUDE = frozenset({"powershell"})

#: General backstop for any other templated schema not in the set above.
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

_AGENTS_ROOT = ".litetui-agents"


def _eligible(app: Any) -> list[str]:
    registered = {e.name for e in app.plugins.tools}
    return sorted((registered & tool_schemas.available()) - _TEMPLATED_EXCLUDE)


def _usage(app: Any) -> str:
    names = _eligible(app)
    return (
        "/reload-plugins <name> — refresh ONE static tool's DESCRIPTION from disk (metadata only).\n"
        "This is NOT a full plugin reload: handlers, argument contracts, new tools, and native "
        "Codex threads all require a restart.\n"
        f"Refreshable now: {', '.join(names) if names else '(none)'}"
    )


def _has_placeholder(spec: Any) -> bool:
    if isinstance(spec, dict):
        return any(_has_placeholder(v) for v in spec.values())
    if isinstance(spec, list):
        return any(_has_placeholder(v) for v in spec)
    if isinstance(spec, str):
        return bool(_PLACEHOLDER.search(spec))
    return False


def _render(name: str, result: Any) -> str:
    if result.status == "reloaded":
        return f"[reload-plugins] reloaded: refreshed the description of {name!r}; the next turn sees it."
    reasons = "; ".join(result.reasons) if result.reasons else "no reason given"
    if result.status == "deferred":
        return (f"[reload-plugins] deferred: {reasons}. Not retried automatically — run "
                f"/reload-plugins {name} again after the active work finishes.")
    if result.status == "restart-required":
        return f"[reload-plugins] restart required: {reasons}. Live tools unchanged."
    return f"[reload-plugins] failed: {reasons}. Live tools unchanged."


def _handle(app: Any, name: str, arg: str) -> None:
    # Provenance: bind the generation we operate on BEFORE any lookup or load,
    # so every check below and the commit refer to the same registry object.
    expected = app.plugins
    target = (arg or "").strip()
    if not target:
        app.system_message(_usage(app))
        return

    registered = {e.name for e in expected.tools}
    if (target not in registered or target not in tool_schemas.available()
            or target in _TEMPLATED_EXCLUDE):
        app.system_message(
            f"[reload-plugins] unknown or ineligible tool schema: {target!r}. "
            "Run /reload-plugins with no argument to list eligible names.")
        return

    try:
        fresh = tool_schemas.load_fresh(target)
    except (FileNotFoundError, OSError, ValueError, TypeError) as e:
        app.system_message(
            f"[reload-plugins] failed to load {target!r} from disk: "
            f"{type(e).__name__}: {e}. Live tools unchanged.")
        return

    if _has_placeholder(fresh):
        app.system_message(
            f"[reload-plugins] {target!r} is a templated schema needing its runtime binding; "
            "restart required. Live tools unchanged.")
        return

    live_spec = next(e.spec for e in expected.tools if e.name == target)
    if fresh == live_spec:
        app.system_message(f"[reload-plugins] unchanged: {target!r} already matches disk.")
        return

    try:
        candidate = stage_schema_refresh(expected, {target: fresh})
    except ValueError as e:
        msg = str(e)
        if "restart" in msg or "contract" in msg:
            app.system_message(f"[reload-plugins] restart required: {msg}. Live tools unchanged.")
        else:
            app.system_message(f"[reload-plugins] failed: {msg}. Live tools unchanged.")
        return

    # Code-currency gate, immediately before the metadata commit: if the reload
    # machinery or this tool's handler source changed since the startup baseline,
    # a description refresh would misrepresent stale code as reloaded. Require a
    # restart instead of committing.
    ok, reason = provenance_ok(app, target)
    if not ok:
        app.system_message(f"[reload-plugins] restart required: {reason}. Live tools unchanged.")
        return

    def _activity():
        return produce_activity(
            app,
            children_pending=lambda: children_pending(Path.home() / _AGENTS_ROOT, app.convo_id),
        ).snapshot

    result = commit_metadata_candidate(app, expected, candidate, activity=_activity)
    app.system_message(_render(target, result))


def _register(ctx) -> None:
    ctx.command(
        ("/reload-plugins",), _handle,
        palette="Reload plugins",
        help="Refresh a static tool description from disk (metadata only).",
        group="tools",
    )


def _activate(app: Any) -> None:
    # Capture the source-provenance baseline at STARTUP. activate runs after all
    # plugins register (app.py register_plugins -> activate_plugins), so every
    # tool is present. NOT lazy/first-reload: an edit made before the first
    # /reload-plugins must still be detectable against the pristine baseline.
    capture_baseline(app)


PLUGIN = PluginManifest(id="reload-plugins", register=_register, activate=_activate)
