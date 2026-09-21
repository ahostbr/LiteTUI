"""Shared, gated skills refresh — used by BOTH /skills refresh and
/reload-plugins skills, so neither path can bypass the guards.

This delegates the actual disk work to the existing app.refresh_skills()
(discover -> write cache -> swap self.skills as its LAST line, so a failure
leaves the live skills mapping untouched — atomic for our purposes). It never
regenerates the registry, imports/reloads modules, or retries automatically.

Every refusal happens BEFORE any discovery or cache write:
  1. skills disabled in settings            -> "disabled"
  2. native app-server session              -> "restart-required"
  3. skills-source drift since startup      -> "restart-required"
  4. app busy, or unknown durable children  -> "deferred" (via the activity gate)
Only when all four pass is app.refresh_skills() called.

Lives in its own module so both plugin surfaces can import it without a cycle
(it imports only plugin_reload_state + plugin_reload_provenance).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from litetui.plugin_reload_state import ActivitySnapshot, blocking_reasons
from litetui.plugin_reload_provenance import skills_provenance_ok


@dataclass(frozen=True)
class SkillsRefreshResult:
    status: str                       # disabled | restart-required | deferred | failed | refreshed
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reason: str = ""


def refresh_skills_guarded(app: Any, *, activity: Callable[[], ActivitySnapshot]) -> SkillsRefreshResult:
    """Run the gated skills refresh. `activity` is supplied by the app owner and
    must return an ActivitySnapshot (built with the real durable-child probe), so
    the idle gate and unknown-children both defer here. No auto-retry."""
    if not getattr(app.settings, "skills_enabled", False):
        return SkillsRefreshResult("disabled", reason="Skills are OFF in /settings — nothing to refresh.")

    if hasattr(app.backend, "app_server"):
        return SkillsRefreshResult(
            "restart-required",
            reason="native Codex session — a skills refresh is not verified in-thread; restart required")

    ok, why = skills_provenance_ok(app)
    if not ok:
        return SkillsRefreshResult("restart-required", reason=why)

    snapshot = activity()
    if not isinstance(snapshot, ActivitySnapshot):
        return SkillsRefreshResult("deferred", reason="activity evidence unavailable")
    reasons = blocking_reasons(snapshot)
    if reasons:
        return SkillsRefreshResult("deferred", reason="; ".join(reasons))

    try:
        added, removed = app.refresh_skills()
    except Exception as e:  # noqa: BLE001 — discover/cache error: live skills untouched (swap is last)
        return SkillsRefreshResult("failed", reason=f"{type(e).__name__}: {e}")

    return SkillsRefreshResult("refreshed", added=tuple(added), removed=tuple(removed))


def render_result(result: SkillsRefreshResult) -> str:
    """Compact one-line-ish rendering for a caller that has no richer report."""
    if result.status == "refreshed":
        delta = []
        if result.added:
            delta.append("+ " + ", ".join(result.added))
        if result.removed:
            delta.append("- " + ", ".join(result.removed))
        return "[reload-plugins] skills refreshed" + (": " + "; ".join(delta) if delta else " — no change")
    if result.status == "disabled":
        return f"[reload-plugins] {result.reason}"
    if result.status == "deferred":
        return (f"[reload-plugins] deferred: {result.reason}. Not retried automatically — "
                "run it again after the active work finishes.")
    if result.status == "restart-required":
        return f"[reload-plugins] restart required: {result.reason}. Skills unchanged."
    return f"[reload-plugins] failed: {result.reason}. Skills unchanged."
