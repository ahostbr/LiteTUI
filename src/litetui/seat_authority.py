"""T1027: ONE answer to "under what authority, on what engine, does this turn run?"

🔴 WHY THIS EXISTS. Every turn passes `hook_host.accept_prompt`, which stamps
`_active_tool_profile` from the profile the PRODUCER carried — and no producer
saw `--tool-profile`: typed carried `chosen_tool_profile`, inbox carried
`settings.tool_policy_profile`, a child wake and a goal read settings too. So the
flag governed nothing after the first accepted prompt of ANY source, while the
footer (reading `_active_tool_profile` before that prompt) said it did.

The rule, per source (Marquee's ruling on the T1027 design):
  - seat profile = an unretired launch flag, else the conversation's choice,
    else the global default (`chosen_tool_profile`).
  - attended turns (typed, rpc, queued, interrupted) run the profile their
    submit carried — the human's choice at that moment — unless a flag outranks it.
  - inbox mail, a child's result and a goal continuation NARROW: the narrower of
    what they carried and the seat profile. Mail never widens a seat.
  - a cron/loop fire is AUTONOMOUS (T085, Ryan: a schedule has nobody to ask) —
    but an explicit flag is a CEILING on every source, so a seat launched
    `--tool-profile interactive` does not escalate for its schedules either.

📌 T1043 (the fleet floor on every turn) is one call beside the stamp in
`accept_prompt`: `floor.check(resolve(app, source))`. Not built here.
"""
from __future__ import annotations

from dataclasses import dataclass

from litetui import tool_policy

#: Turn sources that carry a profile to be narrowed against the seat.
NARROWING_SOURCES = frozenset({"harness", "child-result", "goal"})


@dataclass(frozen=True)
class Effective:
    backend: str | None
    model: str | None
    thinking_level: str | None
    profile: str
    source: str


def narrower(a: str | None, b: str | None) -> str:
    """The less authority of two profiles. Unknown counts as the floor."""
    order = tool_policy.PROFILE_NAMES
    rank = lambda p: order.index(p) if p in order else -1  # noqa: E731
    lo = min((p for p in (a, b) if p is not None), key=rank, default=None)
    return lo if lo in order else order[0]


def launch_flag(app) -> str | None:
    flag = getattr(app, "_cli_tool_profile", None)
    return flag if flag in tool_policy.PROFILES else None


def seat_profile(app) -> str:
    """The flag, else this conversation's choice, else the global default."""
    chosen = getattr(app, "chosen_tool_profile", None) or getattr(
        getattr(app, "settings", None), "tool_policy_profile", None)
    return launch_flag(app) or chosen or tool_policy.STRICT


def turn_profile(app, source: str, requested: str | None = None) -> str:
    flag = launch_flag(app)
    if source == "scheduled":
        wanted = requested or tool_policy.AUTONOMOUS
        return narrower(wanted, flag) if flag else wanted
    if source in NARROWING_SOURCES:
        seat = seat_profile(app)
        return narrower(requested or seat, seat)
    # Attended: what the human's submit carried (the choice at that moment),
    # unless an explicit launch flag outranks it.
    return flag or requested or seat_profile(app)


def resolve(app, source: str = "typed", requested: str | None = None) -> Effective:
    """The turn about to run, as the seat will actually run it."""
    return Effective(
        backend=getattr(getattr(app, "backend", None), "name", None),
        model=getattr(app, "model_id", None) or None,
        thinking_level=getattr(app, "thinking_level", None),
        profile=turn_profile(app, source, requested),
        source=source,
    )
