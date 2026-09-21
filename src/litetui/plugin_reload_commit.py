"""Synchronous owner-loop commit of metadata-only registries, never code reload.

Caller must invoke on the App event loop. No await between activity observation
and pointer replacement. This is not a lock against arbitrary foreign-thread
registry writes; those are outside the App ownership contract.
"""
from dataclasses import dataclass
from litetui.plugin_reload_state import ActivitySnapshot, blocking_reasons, transfer_session_state
from litetui.plugin_schema_reload import stage_schema_refresh


@dataclass(frozen=True)
class CommitResult:
    status: str
    reasons: tuple[str, ...] = ()


def commit_metadata_candidate(app, expected, candidate, *, activity):
    if app.plugins is not expected:
        return CommitResult('deferred', ('Registry generation changed; stage again.',))
    if hasattr(app.backend, 'app_server'):
        return CommitResult('restart-required', ('Native Codex thread inventory cannot be replaced in place.',))
    try:
        snapshot = activity()
        if not isinstance(snapshot, ActivitySnapshot):
            raise ValueError('Activity evidence unavailable')
        reasons = blocking_reasons(snapshot)
        if reasons:
            return CommitResult('deferred', reasons)
        if candidate is expected:
            raise ValueError('Candidate is the live registry')
        # Check every retained authority/resource table; this entrypoint must
        # never become an accidental general-purpose code activation bypass.
        for name in ('dynamic', 'commands', 'palette_rows', 'prompt_sections',
                     'observers', 'turn_finalizers', 'status'):
            if getattr(candidate, name) != getattr(expected, name):
                raise ValueError(f'{name} changed; metadata-only commit refused')
        if len(candidate.tools) != len(expected.tools):
            raise ValueError('Tool inventory changed')
        for old, new in zip(expected.tools, candidate.tools):
            if (old.name != new.name or old.owner != new.owner or old.run is not new.run
                    or old.gate is not new.gate or old.policy != new.policy):
                raise ValueError('Tool ownership, handler or authority changed')
        validated = stage_schema_refresh(expected, {entry.name: entry.spec for entry in candidate.tools})
        candidate.tools = validated.tools
        candidate._tool_by_name = validated._tool_by_name
        transfer_session_state(expected, candidate)
        # Activity provider itself must be read-only; also refuse if it changed
        # the generation while supplying evidence.
        if app.plugins is not expected:
            return CommitResult('deferred', ('Registry generation changed during activity check.',))
        app.plugins = candidate
        return CommitResult('reloaded')
    except Exception as exc:
        return CommitResult('failed', (f'{type(exc).__name__}: {exc}',))
