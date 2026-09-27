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

from litetui import fleet_policy, tool_policy

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


def effective_thinking(app) -> str | None:
    """The effort THIS request will send — chat_request's own expression
    (turn_engine: the merged overrides' reasoning_effort, else thinking_level).

    Dijkstra E1 (T1043 review): the precedence is the launch flag
    (_cli_effective_thinking) > the per-model /modelcfg reasoning_effort >
    thinking_level. Reading only the first and last passed gpt-6-sol with an
    override of "medium" under /think high, and SENT medium. One function, used by
    resolve() and by the presence report (app._sync_seat_resolution), so the floor,
    the fleet and the wire judge one value. A host without the request builder
    (a partial test double) falls back to the flag, then thinking_level."""
    overrides = getattr(app, "_effective_request_overrides", None)
    if overrides is not None:
        try:
            sent = overrides().get("reasoning_effort")
        except Exception:  # noqa: BLE001 - an unbuildable request is refused by its own send
            sent = None
        return sent or getattr(app, "_thinking_level", None)
    return getattr(app, "_cli_effective_thinking", None) or getattr(app, "_thinking_level", None)


def resolve(app, source: str = "typed", requested: str | None = None) -> Effective:
    """The turn about to run, as the seat will actually run it."""
    return Effective(
        backend=getattr(getattr(app, "backend", None), "name", None),
        model=getattr(app, "model_id", None) or None,
        thinking_level=effective_thinking(app),
        profile=turn_profile(app, source, requested),
        source=source,
    )


# ── T1043: the fleet floor, enforced by the SEAT on every turn ──────────────
#
# Ryan (2026-09-26 17:5x): "this MUST NEVER happen again" (a codex seat ran
# gpt-5.6-sol at medium). Every spawn-time check is a snapshot: a reconnect onto
# the pin, a LITETUI_NO_HARNESS seat, a /model or /think after launch. Only the
# seat sees every turn. The rule set is liteharness's own (fleet_policy.py, a
# byte-identical copy — scripts/sync_deny_floor.py).


def floor_applies(app, source: str) -> bool:
    """Which turns the fleet floor governs: EVERY source, Ryan's own attended
    typed turns included — the safe default for "must never happen again"
    (Marquee 7d1e2cd3, Sentinel a9aa4df0). Ryan rules the scope; if he exempts
    his own instance's typed turns, this is the one line, keyed on the absence of
    harness.SPAWN_IDENTITY_MARKER."""
    return True


def seat_policy(app) -> tuple[dict, str]:
    """(policy, where). ⚠️ ASYMMETRIC WITH THE SPAWN PATH ON PURPOSE (Marquee
    7d1e2cd3): a malformed policy file refuses every SPAWN (fleet_policy.check),
    but here it falls back to the module's own DEFAULT_POLICY — Ryan's ruled
    floor — with one loud warning. Refusing every turn would lock Ryan out of his
    own LiteTUI, local models included, over a typo in a JSON file."""
    try:
        return fleet_policy.load()
    except fleet_policy.PolicyError as exc:
        if getattr(app, "_fleet_policy_warned", None) != str(exc):
            app._fleet_policy_warned = str(exc)
            say = getattr(app, "_system", None)
            if say is not None:
                say(f"⚠ {exc} This seat enforces the BUILT-IN default floor until it is fixed.")
        return fleet_policy.DEFAULT_POLICY, f"the built-in default ({fleet_policy.policy_path()} is malformed)"


def warn_if_below_floor(app) -> None:
    """Said at connect and after launch flags apply, before anyone types.
    Enforcement is per turn (hook_host.accept_prompt); this only makes a refusal
    unsurprising. A function over `app`, like app._sync_seat_resolution, so
    the many partial test hosts need no stub."""
    why = floor_refusal(app)
    say = getattr(app, "_system", None)
    if why is not None and say is not None:
        say("⚠ This seat is below the fleet floor, so every turn will be refused "
            "until /model or /think meets it. " + why)


def floor_refusal(app, source: str = "typed") -> str | None:
    """None when this turn may run; otherwise the refusal text. Nothing is ever
    substituted: a seat below the floor stops, it does not pick another model."""
    if not floor_applies(app, source):
        return None
    policy, where = seat_policy(app)
    seat = resolve(app, source)
    governed = fleet_policy.floor_for(policy, seat.backend, seat.model)
    if governed is None:
        return None
    name, floor = governed
    why = fleet_policy.below_floor(name, floor, seat.model, seat.thinking_level)
    if why is None:
        return None
    return (f"TURN REFUSED: {why} Policy: {where} (keys floors.{name}.models / "
            f"min_model / min_thinking_level). Nothing was substituted and nothing "
            f"was sent to the model.")
