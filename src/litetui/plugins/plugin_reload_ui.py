"""`/reload-plugins` — the operator command for the supported reload ops.

Two modes, both fail-closed and restart-honest:

  1. METADATA-ONLY (the original, verified-green path): refresh a single static
     tool's DESCRIPTION from disk. Handlers, argument contracts, new tools, and
     native Codex threads all require a restart; the command says so rather than
     pretending otherwise. All safety lives in the core helpers (stage_schema_
     refresh / commit_metadata_candidate / produce_activity); this file only
     parses the command, validates the name, and relays the CommitResult.

  2. HANDLER-GENERATION RELOAD (WS7 full scope): for a plugin that has declared
     reload-compatibility metadata (module-level ``RELOAD_COMPATIBLE``) AND is on
     the host reviewed allowlist, swap the plugin's handler generation at an idle
     boundary through the no-rollback stack (plugin_reload_command.handler_reload
     -> reload_handler_generation -> commit_handler_candidate). For the current
     plugin set -- none declare RELOAD_COMPATIBLE -- this branch always falls
     through to mode 1, so the verified-green metadata path is preserved exactly.

Neither mode imports-reloads a live module, retries automatically, or touches
app.plugins except through its own no-rollback commit helper.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.plugin_reload_activity import produce_activity
from litetui.plugin_reload_children import children_pending
from litetui.plugin_reload_command import ReloadCompatibility, ReloadTarget, handler_reload
from litetui.plugin_reload_commit import commit_metadata_candidate
from litetui.plugin_reload_generation import read_live_module_source
from litetui.plugin_reload_handler import render_handler_reload
from litetui.plugin_reload_provenance import capture_baseline, capture_skills_baseline, provenance_ok
from litetui.plugin_reload_skills import refresh_skills_guarded, render_result as _render_skills
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

#: Host allowlist of plugin ids reviewed as pure-register for HANDLER reload.
#: A plugin that declares RELOAD_COMPATIBLE is still restart-required until it
#: is listed here (a plugin cannot review itself). Empty: no plugin is
#: handler-reload-reviewed yet, so the handler-reload branch is the "thin" state
#: (it falls through to the metadata-only path for every current plugin).
_HANDLER_RELOAD_REVIEWED: frozenset[str] = frozenset()


def _eligible(app: Any) -> list[str]:
    registered = {e.name for e in app.plugins.tools}
    return sorted((registered & tool_schemas.available()) - _TEMPLATED_EXCLUDE)


def _usage(app: Any) -> str:
    names = _eligible(app)
    return (
        "/reload-plugins <name> — refresh ONE static tool's DESCRIPTION from disk (metadata only).\n"
        "/reload-plugins skills — re-scan the skills index from disk (same guarded path as /skills refresh).\n"
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


def _module_name_for(owner: str) -> str | None:
    """Map a registered plugin id to its module name by scanning sys.modules for
    the litetui plugin module whose ``.PLUGIN.id`` matches. Defensive: an absent
    or ambiguous match returns None so the handler-reload branch stays a clean
    fall-through (never a guess). Read-only: it touches no app or module state."""
    hits: list[str] = []
    for name, mod in sys.modules.items():
        if not name.startswith("litetui.plugins."):
            continue
        plug = getattr(mod, "PLUGIN", None)
        if plug is not None and getattr(plug, "id", None) == owner:
            hits.append(name)
    return hits[0] if len(hits) == 1 else None


def _resolve_reload(target: str) -> ReloadTarget | None:
    """The production App-state resolution: the plugin's on-disk source + its
    declared ``RELOAD_COMPATIBLE``. Returns None when the target is not a known
    litetui plugin module or declares no handler-reload compatibility -- the
    default, so the branch falls through to the metadata-only path. A source-read
    failure (e.g. a vanished file) is the same: None, live generation untouched."""
    mod_name = _module_name_for(target)
    if mod_name is None:
        return None
    module = sys.modules.get(mod_name)
    if module is None:
        return None
    compat = getattr(module, "RELOAD_COMPATIBLE", None)
    if not isinstance(compat, ReloadCompatibility):
        return None
    try:
        source = read_live_module_source(mod_name)
    except Exception:
        return None
    return ReloadTarget(owner=target, module_name=mod_name, source=source, compat=compat)


def _handler_reload_branch(
    app: Any, target: str, *, activity: Callable[[], Any] | None = None
) -> str | None:
    """The ``/reload-plugins`` handler-reload branch. Returns the operator line
    when ``target`` is a plugin that declared handler-reload compatibility; else
    None (fall through to the metadata-only path, unchanged). Never mutates
    app.plugins except through the no-rollback swap. ``activity`` defaults to the
    live idle-boundary snapshot; a test may inject one. For the current plugin
    set (none declare RELOAD_COMPATIBLE) this is always None, so the verified-
    green metadata-only path is preserved exactly."""
    resolved = _resolve_reload(target)
    if resolved is None:
        return None
    if activity is None:
        # noqa: E731 -- a throwaway bound at call site, not a stored lambda.
        activity = lambda: produce_activity(
            app,
            children_pending=lambda: children_pending(Path.home() / _AGENTS_ROOT, app.convo_id),
        ).snapshot
    result = handler_reload(
        app, target,
        resolve=lambda _t: resolved,
        live_manifests=list(getattr(app, "_plugin_manifests", []) or []),
        reviewed_owners=_HANDLER_RELOAD_REVIEWED,
        activity=activity)
    return render_handler_reload(target, result)


def _handle(app: Any, name: str, arg: str) -> None:
    # Provenance: bind the generation we operate on BEFORE any lookup or load,
    # so every check below and the commit refer to the same registry object.
    expected = app.plugins
    target = (arg or "").strip()
    if not target:
        app.system_message(_usage(app))
        return

    # Reserved data-refresh verb: `/reload-plugins skills` refreshes the skills
    # index (disk data), NOT a tool schema. Routed through the shared guarded
    # helper so it and /skills refresh cannot diverge on the gates.
    if target == "skills":
        _handle_skills(app)
        return

    # Handler-generation reload branch (WS7 full scope): fires ONLY for a plugin
    # that declared RELOAD_COMPATIBLE and is host-reviewed. For the current
    # plugin set (none declare it) this is always None and the metadata-only path
    # below runs unchanged.
    handler_line = _handler_reload_branch(app, target)
    if handler_line is not None:
        app.system_message(handler_line)
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


def _handle_skills(app: Any) -> None:
    """`/reload-plugins skills` — refresh the skills index (disk data) through the
    shared guarded helper. Native/busy/source-drift/unknown-children all refuse
    before any discovery or cache write."""
    def _activity():
        return produce_activity(
            app,
            children_pending=lambda: children_pending(Path.home() / _AGENTS_ROOT, app.convo_id),
        ).snapshot
    app.system_message(_render_skills(refresh_skills_guarded(app, activity=_activity)))


def _register(ctx) -> None:
    ctx.command(
        ("/reload-plugins",), _handle,
        palette="Reload plugins",
        help="Refresh a static tool description from disk (metadata only).",
        group="tools",
    )


def _activate(app: Any) -> None:
    # Capture BOTH provenance baselines at STARTUP. activate runs after all
    # plugins register (app.py register_plugins -> activate_plugins), so every
    # tool and required module is present. NOT lazy/first-reload: an edit made
    # before the first reload must still be detectable against the pristine
    # baselines. The skills set is separate so app.py churn never blocks a
    # description refresh.
    capture_baseline(app)
    capture_skills_baseline(app)


PLUGIN = PluginManifest(id="reload-plugins", register=_register, activate=_activate)
