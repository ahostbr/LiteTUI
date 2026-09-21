"""Staged plugin-reload validation — build a candidate registry off to the
side, prove it is safe, and NEVER touch the working generation while doing so.

This is WS7 Slice 1's foundation (sub-plugin-reload.md → "Transactional registry
and module handling"). It answers exactly one question: *would this set of
plugins load cleanly and without silently changing authority?* It does not
import or reload modules, does not run ``activate()``, and does not swap
anything into the live app. Those are later, coordinated slices.

CONTRACTUAL PURITY, NOT A SANDBOX. ``reviewed_owners`` is an allowlist of plugin
ids whose ``register()`` has been reviewed and is trusted to only add rows to
the registry it is handed. Read-only Python cannot enforce that: a dishonest
``register()`` for an allowlisted owner can still mutate the app, the live
registry or module globals, and this module can neither prevent nor roll that
back. An owner NOT on the allowlist is never invoked at all — it is reported
``restart_required`` *before* its ``register()`` would run. Glassbox is
deliberately never reviewable here: its ``register()`` reassigns a module
global (glassbox_plugin.py), so a candidate build would recompute process-global
state. So this module claims *contractual* purity for reviewed owners, never
guaranteed rollback of arbitrary plugin side effects.

STATIC SPECS ONLY. Dynamic tool providers (MCP) resolve their tool set at call
time; validating them means CALLING an arbitrary ``specs_fn``, which this slice
refuses to do. A candidate that contains a dynamic provider stores the ref
unevaluated and reports ``restart_required`` for that owner, plus a limitation
note.

The caller supplies already-imported ``PluginManifest`` objects. How modules are
(re)imported safely is out of scope here — this module never imports one.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from litetui.plugins import PluginContext, PluginManifest, PluginRegistry
from litetui.tool_policy import MCP_UNKNOWN_POLICY

# ── issue kinds (constants so callers and tests reference them, not literals) ──
DUPLICATE_MANIFEST_ID = "duplicate_manifest_id"
RESTART_REQUIRED = "restart_required"
REGISTER_FAILED = "register_failed"
DUPLICATE_NAME = "duplicate_name"
PROMPT_SLOT_CONFLICT = "prompt_slot_conflict"
MALFORMED_SCHEMA = "malformed_schema"
POLICY_ENLARGED = "policy_enlarged"
CONFIRMATION_WEAKENED = "confirmation_weakened"
POLICY_OMITTED = "policy_omitted"
OWNERSHIP_CHANGED = "ownership_changed"
AUTHORITY_ADDED = "authority_added"


@dataclass(frozen=True)
class ReloadIssue:
    """One reason a candidate was rejected. ``name`` is the tool name, command
    token, or prompt-order slot involved, when there is one."""

    kind: str
    owner: str
    detail: str
    name: str = ""


@dataclass(frozen=True)
class CandidateResult:
    """The outcome of staging. ``registry`` is the built candidate ONLY when
    ``ok`` — on any issue it is ``None`` so a caller cannot accidentally swap in
    an unvalidated generation. ``manifests`` are those whose ``register()`` ran
    without raising; ``limitations`` names what this slice did not validate."""

    ok: bool
    registry: PluginRegistry | None
    issues: tuple[ReloadIssue, ...]
    manifests: tuple[PluginManifest, ...]
    limitations: tuple[str, ...] = ()


def stage_candidate(
    app: Any,
    manifests: Sequence[PluginManifest],
    *,
    live: PluginRegistry,
    reviewed_owners: frozenset[str],
    approved_new_tools: frozenset[str] = frozenset(),
) -> CandidateResult:
    """Build and validate a candidate registry without touching ``live``.

    Order matters: duplicate manifest ids are rejected before ANY register runs
    (a repeated id makes owner-stamping meaningless); unreviewed owners are
    reported and skipped before their register would run; only then is the
    candidate built and validated. ``live`` and ``app.plugins`` are read but
    never mutated by this function.
    """
    issues: list[ReloadIssue] = []
    limitations: list[str] = []

    # 1. Duplicate manifest ids — before any register().
    seen: set[str] = set()
    dup_ids: set[str] = set()
    for m in manifests:
        if m.id in seen:
            dup_ids.add(m.id)
        seen.add(m.id)
    for mid in sorted(dup_ids):
        issues.append(ReloadIssue(
            DUPLICATE_MANIFEST_ID, mid,
            "manifest id appears more than once in the candidate set"))
    if dup_ids:
        # Nothing was registered; the candidate cannot be trusted to be
        # owner-stamped correctly, so stop here.
        return CandidateResult(False, None, tuple(issues), (), tuple(limitations))

    # 2. Build the candidate. Only reviewed owners are invoked; the rest are
    #    reported restart_required and never called.
    candidate = PluginRegistry()
    registered: list[PluginManifest] = []
    for m in manifests:
        if m.id not in reviewed_owners:
            issues.append(ReloadIssue(
                RESTART_REQUIRED, m.id,
                "owner not in reviewed pure-register allowlist; register() not invoked"))
            continue
        if m.register is None:
            registered.append(m)   # activate-only plugin: nothing to register
            continue
        try:
            m.register(PluginContext(app, candidate, m.id))
        except ValueError as e:
            # The registry raises ValueError on owner collisions with a message
            # it wrote itself (__init__.py add_tool/add_command/add_prompt_section);
            # classify by that message rather than guessing.
            msg = str(e)
            if msg.startswith("prompt-section order"):
                issues.append(ReloadIssue(PROMPT_SLOT_CONFLICT, m.id, msg))
            elif msg.startswith("tool ") or msg.startswith("command "):
                issues.append(ReloadIssue(DUPLICATE_NAME, m.id, msg))
            else:
                issues.append(ReloadIssue(REGISTER_FAILED, m.id, f"{type(e).__name__}: {e}"))
        except Exception as e:  # noqa: BLE001 — an unreviewed failure mode; report, never swap
            issues.append(ReloadIssue(REGISTER_FAILED, m.id, f"{type(e).__name__}: {e}"))
        else:
            registered.append(m)

    # 3. Dynamic providers: store the ref, refuse to evaluate it in this slice.
    for owner in sorted({d.owner for d in candidate.dynamic}):
        issues.append(ReloadIssue(
            RESTART_REQUIRED, owner,
            "registers a dynamic tool provider; dynamic specs are not evaluated in this slice"))
    if candidate.dynamic:
        limitations.append(
            "dynamic tool providers were stored but not validated (static specs only)")

    # 4. Static schema validation.
    for e in candidate.tools:
        problem = _schema_problem(e.spec)
        if problem is not None:
            issues.append(ReloadIssue(MALFORMED_SCHEMA, e.owner, problem, name=_safe_name(e.spec)))

    # 5. Authority validation against the live generation (static tools only).
    issues.extend(_authority_issues(live, candidate, approved_new_tools))

    ok = not issues
    return CandidateResult(
        ok, candidate if ok else None, tuple(issues), tuple(registered), tuple(limitations))


def _safe_name(spec: Any) -> str:
    try:
        return str(spec["function"]["name"])
    except Exception:
        return ""


def _schema_problem(spec: Any) -> str | None:
    """None when the static tool spec is well formed, else why it is not.

    A tool spec is the model's only inventory of a tool (see tool_specs()'s
    docstring), so a malformed one is a tool the model cannot call correctly.
    """
    if not isinstance(spec, dict):
        return "spec is not an object"
    if spec.get("type") != "function":
        return f"spec.type must be 'function', got {spec.get('type')!r}"
    fn = spec.get("function")
    if not isinstance(fn, dict):
        return "spec.function must be an object"
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return "spec.function.name must be a non-empty string"
    params = fn.get("parameters")
    if not isinstance(params, dict):
        return "spec.function.parameters must be an object"
    props = params.get("properties")
    if not isinstance(props, dict):
        return "spec.function.parameters.properties must be a mapping"
    required = params.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        return "spec.function.parameters.required must be a list of strings"
    missing = [r for r in required if r not in props]
    if missing:
        return f"required names not in properties: {sorted(missing)}"
    return None


def _authority_issues(
    live: PluginRegistry, candidate: PluginRegistry, approved_new_tools: frozenset[str]
) -> list[ReloadIssue]:
    """No reload may silently change a tool's authority. New tools need explicit
    approval; existing tools may not gain capabilities, lose their explicit
    policy, weaken confirmation, or change owner without being caught here."""
    out: list[ReloadIssue] = []
    live_by_name = {e.name: e for e in live.tools}
    for e in candidate.tools:
        prior = live_by_name.get(e.name)
        if prior is None:
            if e.name not in approved_new_tools:
                out.append(ReloadIssue(
                    AUTHORITY_ADDED, e.owner,
                    "new tool not in approved_new_tools; no silent authority expansion",
                    name=e.name))
            continue
        if e.owner != prior.owner:
            out.append(ReloadIssue(
                OWNERSHIP_CHANGED, e.owner,
                f"tool now owned by {e.owner!r}, was {prior.owner!r}", name=e.name))
        # Policy omission: had an explicit policy, now the fail-safe fallback.
        # A capability comparison against the catch-all would be misleading, so
        # report the omission and skip it.
        if e.policy is MCP_UNKNOWN_POLICY and prior.policy is not MCP_UNKNOWN_POLICY:
            out.append(ReloadIssue(
                POLICY_OMITTED, e.owner,
                "tool lost its explicit policy (fell back to MCP_UNKNOWN_POLICY)", name=e.name))
            continue
        added = e.policy.capabilities - prior.policy.capabilities
        if added:
            out.append(ReloadIssue(
                POLICY_ENLARGED, e.owner,
                f"capabilities added: {sorted(added)}", name=e.name))
        if prior.policy.confirm_always and not e.policy.confirm_always:
            out.append(ReloadIssue(
                CONFIRMATION_WEAKENED, e.owner,
                "confirm_always relaxed from True to False", name=e.name))
    return out
