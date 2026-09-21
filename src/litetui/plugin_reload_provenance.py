"""Source provenance for description-only reload.

A baseline is fingerprinted at STARTUP so that editing source AFTER boot cannot
let /reload-plugins report a description refresh as a code reload: the running
process still holds the pre-edit code. Before any metadata commit, provenance is
re-checked; anything that cannot be PROVEN unchanged requires a restart.

Two source sets are covered:
  * the BOUNDED CORE SET — the machinery that performs a refresh (the reload
    command, candidate/commit/schema/state/activity/provenance modules, and the
    PluginRegistry implementation). Any core drift invalidates EVERY target.
  * the TARGET TOOL'S handler source — the code a specific refresh could
    misrepresent. The target -> source mapping is PINNED at startup and
    re-verified, so rebinding a handler to a different (even unchanged) source
    is caught as drift.

Bounded on purpose — what this is NOT:
  * NOT transitive-dependency or full-app source coverage. Only the direct
    defining files listed/mapped here are hashed.
  * NOT a proof that bytes on disk equal the code already imported into the
    process. The baseline is taken at ACTIVATE, which runs after import; a file
    edited BETWEEN import and activate would be baselined in its edited form
    (TOCTOU). This guarantee is "no change since the baseline", not "the loaded
    code equals this disk state". Do not widen that claim.

It never imports, reloads, or executes anything — only hashes bytes of already
resolved, existing files. Fail-closed: whatever cannot be proven unchanged
requires a restart. The baseline is captured ONCE per App instance (stored on
the app) and is never recaptured from the reload path, so a reload cannot bless
a post-startup edit.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

#: The machinery whose edit invalidates any refresh. Module NAMES; their actual
#: defining files are discovered via sys.modules at capture time (never guessed).
_CORE_MODULES: tuple[str, ...] = (
    "litetui.plugins",                     # PluginRegistry implementation (__init__.py)
    "litetui.plugins.plugin_reload_ui",    # the /reload-plugins command
    "litetui.plugin_reload",               # candidate validation
    "litetui.plugin_reload_commit",        # metadata commit boundary
    "litetui.plugin_schema_reload",        # schema staging
    "litetui.plugin_reload_state",         # session-state transfer + idle gate
    "litetui.plugin_reload_activity",      # activity producer
    "litetui.plugin_reload_provenance",    # this module
    "litetui.tool_schemas",                # load_fresh/available — the refresh reads through it
    "litetui.plugin_reload_children",      # children_pending — the commit's idle evidence
)
# All ten are top-level imports of plugins.plugin_reload_ui, so they are already
# in sys.modules by the time its activate() runs — capture never force-imports.

_KEY = "_plugin_source_baseline"


def _resolved(path_str: str | None) -> str | None:
    if not path_str:
        return None
    try:
        return str(Path(path_str).resolve())
    except OSError:
        return None


def _module_path(module_name: str) -> str | None:
    module = sys.modules.get(module_name)
    return _resolved(getattr(module, "__file__", None)) if module is not None else None


def _handler_path(entry: Any) -> str | None:
    """Resolved file backing a tool's handler, via its module. None when it
    cannot be located (a lambda/builtin/partial with no resolvable module)."""
    run = getattr(entry, "run", None)
    module_name = getattr(run, "__module__", None)
    return _module_path(module_name) if module_name else None


def _digest(path_str: str) -> str:
    return hashlib.sha256(Path(path_str).read_bytes()).hexdigest()


def capture_baseline(app: Any) -> dict:
    """Fingerprint the core set and every tool's handler source, ONCE, at
    startup. Returns the baseline record. If a baseline already exists it is
    returned unchanged — a reload must never recapture and thereby bless an
    edit made after startup.

    Record shape:
      {"files":   {resolved_path: digest | None},   # None = unreadable at capture
       "targets": {tool_name: resolved_path | None}, # pinned mapping
       "core":    (resolved_path, ...)}              # subset of files that gate all
    """
    existing = getattr(app, _KEY, None)
    if isinstance(existing, dict):
        return existing

    files: dict[str, str | None] = {}

    def _record(path: str | None) -> None:
        if path is None or path in files:
            return
        try:
            files[path] = _digest(path)
        except OSError:
            files[path] = None

    # Pin name->path for EVERY core module, recording None when a required core
    # module is unavailable at startup — omitting it would fail OPEN (its drift
    # would never be checked). The mapping is re-verified at check time, so a
    # module.__file__ rebound to another (even unchanged) file is caught.
    core: dict[str, str | None] = {}
    for name in _CORE_MODULES:
        path = _module_path(name)
        core[name] = path
        _record(path)                # _record no-ops on None

    targets: dict[str, str | None] = {}
    for entry in app.plugins.tools:
        path = _handler_path(entry)
        targets[entry.name] = path
        _record(path)

    baseline = {"files": files, "targets": targets, "core": core}
    setattr(app, _KEY, baseline)
    return baseline


def _drifted(files: dict, path: str) -> str | None:
    """Reason string when `path` cannot be proven unchanged against `files`
    (a {path: digest|None} map), else None."""
    if path not in files:
        return "was not in the startup baseline"
    before = files[path]
    if before is None:
        return "was unreadable at startup; cannot prove it is unchanged"
    try:
        now = _digest(path)
    except OSError as e:
        return f"is unreadable now: {type(e).__name__}"
    if now != before:
        return "changed after startup"
    return None


def provenance_ok(app: Any, tool_name: str) -> tuple[bool, str]:
    """(ok, reason). False whenever the core machinery or the target handler
    source cannot be proven unchanged since the startup baseline — the caller
    must then require a restart rather than commit a metadata refresh."""
    baseline = getattr(app, _KEY, None)
    if not isinstance(baseline, dict):
        return False, "no source provenance baseline was captured at startup"

    # Core drift invalidates every target. Check the pinned name->path mapping
    # first (a required module unavailable at startup or now, or remapped to a
    # different file, is drift even if that file's bytes are unchanged), then the
    # digest of the pinned path.
    for name, pinned in baseline["core"].items():
        if pinned is None:
            return False, f"reload machinery module {name!r} was unavailable at startup; restart required"
        current = _module_path(name)
        if current != pinned:
            return False, (f"reload machinery module {name!r} now resolves to a different file than "
                           "at startup; restart required")
        reason = _drifted(baseline["files"], pinned)
        if reason is not None:
            return False, f"reload machinery source {Path(pinned).name!r} {reason}; restart required"

    entry = next((e for e in app.plugins.tools if e.name == tool_name), None)
    if entry is None:
        return False, f"{tool_name!r} is not a registered tool"

    pinned = baseline["targets"].get(tool_name)
    if tool_name not in baseline["targets"]:
        return False, f"handler source for {tool_name!r} was not in the startup baseline"
    current = _handler_path(entry)
    if current != pinned:
        return False, (f"handler for {tool_name!r} is bound to a different source than at startup; "
                       "restart required")
    if pinned is None:
        return False, f"cannot locate the handler source for {tool_name!r}"
    reason = _drifted(baseline["files"], pinned)
    if reason is not None:
        return False, f"handler source for {tool_name!r} {reason}; restart required to load the new code"
    return True, ""


# ── skills-refresh source set ────────────────────────────────────────────────
# SEPARATE from the core/description set on purpose: it includes litetui.app
# (refresh_skills' defining source), whose frequent churn must gate a skills
# refresh but must NEVER block a tool-description refresh. Same fail-closed rules.
_SKILLS_MODULES: tuple[str, ...] = (
    "litetui.app",                    # refresh_skills defining source (churny — restart on any edit, by design)
    "litetui.plugins.skills_plugin",  # the /skills command surface
    "litetui.skills",                 # discover_all / write_cache — the actual disk work
    "litetui.plugin_reload_skills",   # the shared guarded helper both commands call
)
_SKILLS_KEY = "_skills_source_baseline"


def capture_skills_baseline(app: Any) -> dict:
    """Baseline the skills-refresh sources ONCE at startup, independent of the
    core baseline. Shape: {"files": {path: digest|None}, "modules": {name: path|None}}."""
    existing = getattr(app, _SKILLS_KEY, None)
    if isinstance(existing, dict):
        return existing
    files: dict[str, str | None] = {}
    modules: dict[str, str | None] = {}
    for name in _SKILLS_MODULES:
        path = _module_path(name)
        modules[name] = path
        if path is not None and path not in files:
            try:
                files[path] = _digest(path)
            except OSError:
                files[path] = None
    baseline = {"files": files, "modules": modules}
    setattr(app, _SKILLS_KEY, baseline)
    return baseline


def skills_provenance_ok(app: Any) -> tuple[bool, str]:
    """(ok, reason). False whenever any skills-refresh source cannot be proven
    unchanged since startup — the caller must require a restart before touching
    the skills cache. Fail-closed on a missing baseline."""
    baseline = getattr(app, _SKILLS_KEY, None)
    if not isinstance(baseline, dict):
        return False, "no skills-source provenance baseline was captured at startup"
    for name, pinned in baseline["modules"].items():
        if pinned is None:
            return False, f"skills-refresh module {name!r} was unavailable at startup; restart required"
        current = _module_path(name)
        if current != pinned:
            return False, (f"skills-refresh module {name!r} now resolves to a different file than "
                           "at startup; restart required")
        reason = _drifted(baseline["files"], pinned)
        if reason is not None:
            return False, f"skills-refresh source {Path(pinned).name!r} {reason}; restart required"
    return True, ""
