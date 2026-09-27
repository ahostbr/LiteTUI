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
NARROWING_SOURCES = frozenset({"harness", "child-result", "goal", "goal-ryan"})


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
    return locked_profile(app, launch_flag(app) or chosen or tool_policy.STRICT)


def turn_profile(app, source: str, requested: str | None = None) -> str:
    return locked_profile(app, _turn_profile(app, source, requested))


def _turn_profile(app, source: str, requested: str | None) -> str:
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


# ── T1049: the autonomy lock ────────────────────────────────────────────────
#
# Ryan (liteask a-29047520): "light TUI instances that are spawned by other agents
# cannot be set to auto mode. They can only go to interactive mode and must be
# handled by their leaders". The gate is `app._active_tool_profile` itself (a
# property over the raw value, app.py): it has 9 writers and ~12 readers, and only
# 3 writers go through the resolver, so a gate anywhere else leaves paths open.

def lock_refusal(app) -> str:
    """Why autonomous was refused, true to THIS seat (Dijkstra F2): the lock keys
    on "not Ryan's own", so a seat Ryan launched unmarked is locked too, and must
    not be told an agent spawned it."""
    why = ("was not launched by Ryan (an agent spawned it), so it runs interactive "
           "and its approvals belong to its leader" if is_spawned(app) else
           "is not Ryan's own (not owner-marked: launch it via run.bat or LiteGUI; "
           "or its terminal was written by an agent), so it runs interactive")
    return (f"autonomous refused: this LiteTUI {why} (T1049; Ryan: "
            "\"they can only go to interactive mode\").")


def locked(app) -> bool:
    """No autonomous authority here: NOT Ryan's own instance. The cached answer
    (recheck=False): this is read on every footer paint and tool decision, and the
    taint is re-asked once per turn by the floor check in accept_prompt."""
    return not is_ryans_own(app, recheck=False)


def locked_profile(app, profile: str | None) -> str | None:
    """THE gate: autonomous reads as interactive in a locked seat. Nothing else
    changes, so the lock can only ever narrow."""
    if profile == tool_policy.AUTONOMOUS and locked(app):
        return tool_policy.INTERACTIVE
    return profile


def confirm_route(app) -> str:
    """Where a CONFIRM goes (T1049-B, plan ab8969c2 §2, approved 5ef7612a).
      own      Ryan's own seat: UNCHANGED (modal / his GUI host; unattended refused).
      host     a locked rpc seat whose host relays (agent_supervisor sets
               LITETUI_APPROVAL_HOST): the host, for every source.
      spawner  a locked seat with a recorded spawner: the spawner by inbox, for EVERY
               source, typed and rpc included (Ryan 6e280dd4: "The launching agent").
      refuse   locked, no spawner, launched by an agent: refused + logged, never a
               modal and never the GUI human (Ryan 6e280dd4 (b)).
      hand     locked, no spawner, not agent-launched (Ryan's unmarked hand launch):
               as today, plus the log on its unattended refusal (Marquee Q1)."""
    if not locked(app):
        return "own"
    if getattr(app, "_rpc", False) and getattr(app, "_approval_host", False):
        return "host"
    if getattr(app, "_spawner_id", None):
        return "spawner"
    if getattr(app, "_agent_launched", True):  # unknown: the fail-safe answer
        return "refuse"
    return "hand"


def warn_if_capped(app) -> None:
    """Said once at connect, beside warn_if_below_floor: the seat asked for
    autonomous (a launch flag, the rpc default, a settings default, a resumed
    choice) and runs interactive instead."""
    if getattr(app, "_autonomy_cap_said", False) or not locked(app):
        return
    raw = getattr(app, "_active_tool_profile_raw", None)
    asked = tool_policy.AUTONOMOUS in (launch_flag(app), raw, getattr(
        getattr(app, "settings", None), "tool_policy_profile", None))
    say = getattr(app, "_system", None)
    if asked and say is not None:
        try:
            say("⚠ " + lock_refusal(app) + " Authority is interactive.")
        except Exception:  # noqa: BLE001 - called from _apply_cli_args' finally, possibly with no screen yet
            return  # not said: the connect site can still say it
        app._autonomy_cap_said = True


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
            return _flag_then_think(app)   # Dijkstra nit: the same as no builder
        return sent or getattr(app, "_thinking_level", None)
    return _flag_then_think(app)


def _flag_then_think(app) -> str | None:
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


#: Turns Ryan drives himself: what he types (held or interrupting), a /skill he
#: types, and the wake that follows HIS /compact ("compact-wake" on the host loop,
#: "compact" for Claude's, submitted through _submit_text), and his reply to a
#: Codex question ("codex-question", codex_async_questions' human UI). Everything else,
#: including an item with no label ("unlabelled"), is unattended.
#: Labels are produced ONLY by typed submits: "typed" (_submit_text's default
#: and the /mark paths), "queued" and "interrupted" (_submit_text, held or
#: interrupting, where an rpc submit becomes "rpc" instead).
#: "goal-ryan": every turn of a /goal loop RYAN started (RYAN_GOAL_ORIGINS). Ryan,
#: liteask a-04692a60 (answer 545c38e9), verbatim: "Exempt it: my /goal is mine".
#: That flipped the provisional "goal loops meet the floor" (Marquee's ruling (a)).
#: A goal issued any other way labels its turns "goal" and stays enforced.
ATTENDED_SOURCES = frozenset({"typed", "queued", "interrupted", "compact-wake", "compact",
                              "codex-question", "goal-ryan"})

#: Where a /goal was issued (GoalState.started_by) that makes the loop Ryan's.
#: "typed": his own TUI; "gui": LiteGUI's composer or Automation panel (an rpc
#: host that sent gui.hello). "rpc" from a host that did not identify is NOT his.
RYAN_GOAL_ORIGINS = frozenset({"typed", "gui"})


def command_origin(app) -> str:
    """Who issued the command being handled: _submit_text records its source in
    app._command_source for the length of _handle_command. An rpc host that sent
    gui.hello (LiteGUI) is "gui"."""
    origin = getattr(app, "_command_source", None) or "unknown"
    if origin == "rpc" and getattr(app, "_gui_rpc_enabled", False):
        return "gui"
    return origin


def goal_source(state) -> str:
    """The source every turn of this goal loop carries. The steering ledger keeps
    "source" (and drops goal_continuation), so the origin survives it."""
    return "goal-ryan" if getattr(state, "started_by", "") in RYAN_GOAL_ORIGINS else "goal"


#: Set in a shell an AGENT runs: Claude Code's Bash (CLAUDECODE), LiteTUI's own
#: tool shells (harness.AGENT_SHELL_MARKER), Codex's shell tool (it sets
#: CODEX_SANDBOX_NETWORK_DISABLED "whenever you use the shell tool", measured =1 in
#: a recorded session; CODEX_SANDBOX when sandboxed). Under any of them the owner
#: mark is VOID: a UI-made LiteSuite panel carries the mark to every process in
#: it, including a Claude or Codex Ryan starts there and THEIR shells.
AGENT_SHELL_MARKERS = ("CLAUDECODE", "LITETUI_AGENT_SHELL",
                       "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_SANDBOX")

#: LiteSuite's pty daemon puts this in every terminal's env (process-children.ts).
PTY_TERM_VAR = "LITESUITE_PTY_TERM"
BRIDGE = "http://127.0.0.1:7423"


def owner_mark_valid(environ) -> bool:
    """LITETUI_OWNER=1, and NOT inside an agent's shell. Read at startup."""
    return (environ.get("LITETUI_OWNER") == "1"
            and not any(environ.get(name) for name in AGENT_SHELL_MARKERS))


def pty_taint_clean(term: str, timeout: float = 1.0) -> bool:
    """Ask LiteSuite whether this terminal was ever written by the bridge
    (/pty/talk, /pty/write, MCP terminal writes; the taint is recorded BEFORE the
    write). Only a clear "untainted" keeps the mark: no answer, a timeout, an error
    or a tainted terminal all mean NOT the owner (fail-safe)."""
    import json
    import os
    import urllib.parse
    import urllib.request
    from pathlib import Path
    token = os.environ.get("LITESUITE_BRIDGE_TOKEN", "").strip()
    if not token:
        try:
            token = (Path.home() / ".litesuite" / "bridge-token").read_text(encoding="utf-8").strip()
        except OSError:
            return False
    url = f"{BRIDGE}/pty/owner-ok?term={urllib.parse.quote(term)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure to verify is "not the owner"
        return False
    return answer.get("tainted") is False


def is_owner(app) -> bool:
    """Ryan's own launcher marked this process (LITETUI_OWNER=1, recorded at startup
    as app._owner_seat, void inside an agent's shell). Unknown counts as NOT the
    owner: the floor is the default.
    T1043 finding F (Dijkstra 90a4ba0c): "not spawned" read 102 of 105 convos on
    disk as Ryan's own, fleet seats typed into panes included.
    Inside a LiteSuite terminal (app._pty_term) the mark also needs that terminal
    to be untainted by the bridge, re-asked each time, because /pty/talk can type
    into a shell Ryan opened. A taint is permanent: once seen, the mark is gone."""
    if not getattr(app, "_owner_seat", False):
        return False
    term = getattr(app, "_pty_term", None)
    if term and not pty_taint_clean(term):
        app._owner_seat = False
        return False
    return True


def is_ryans_own(app, recheck: bool = True) -> bool:
    """THE one test for "Ryan's own instance": owner-marked and not spawned.
    recheck=False skips re-asking LiteSuite about a bridge taint and uses the
    answer as of the last check (T1049's hot-path getter). The floor check in
    accept_prompt re-asks before every turn, so a taint (a bridge write, which
    is itself how a new turn is typed in) is seen before the turn it starts."""
    if is_spawned(app):
        return False
    return is_owner(app) if recheck else bool(getattr(app, "_owner_seat", False))


def is_spawned(app) -> bool:
    """A launcher spawned this seat (LITETUI_SPAWN_IDENTITY, recorded at startup
    as app._spawned_seat). Unknown counts as spawned: the floor is the default."""
    return getattr(app, "_spawned_seat", True)


def floor_applies(app, source: str) -> bool:
    """Which turns the fleet floor governs.

    RYAN, form 4 (verbatim, via Marquee 1e92852f): "no leave that unchanged no
    warning nothing". So in HIS OWN instance (owner-marked, LITETUI_OWNER, and not
    spawned; finding F: unmarked is NOT his) the turns he drives
    are exempt, with no refusal and no warning. Every source in a spawned seat,
    and every UNATTENDED source even in his instance (inbox, cron/loop, goal,
    child-result, rpc from another process), stays enforced.

    "rpc" is attended ONLY in a LiteGUI-hosted instance (Marquee f2af2bcc): LiteGUI's
    composer sends "rpc" (its idle Send is the rpc `prompt`, rpc.py:141), and so
    does agent_supervisor for LiteTUI subagent children, whose spawn marker it
    strips. The host is told apart by `gui.hello`, which sets _gui_rpc_enabled.
    ⚠️ gui.hello is a HOST CLAIM, NOT AUTHENTICATION: any process that launches
    `litetui --rpc` could send it. Today only LiteGUI does (LiteGUI
    src/host/supervisor.ts:28). The exemption covers rpc only; inbox, cron, goal
    and child-result turns in the same instance stay enforced."""
    if not is_ryans_own(app):
        return True   # spawned wins; and only Ryan's own launcher is exempt
    if source == "rpc":
        # ponytail: host claim via gui.hello; a real per-launch host token if anything but LiteGUI speaks it
        return not getattr(app, "_gui_rpc_enabled", False)
    return source not in ATTENDED_SOURCES


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
    if is_ryans_own(app):
        return  # Ryan, form 4: "no warning nothing" in his own instance
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
    if source in ("goal", "goal-ryan"):
        why += " Goal loops run unattended and meet the fleet floor."
    elif source == "rpc" and not getattr(app, "_gui_rpc_enabled", False):
        why += " (rpc host did not identify)"
    return (f"TURN REFUSED: {why} Policy: {where} (keys floors.{name}.models / "
            f"min_model / min_thinking_level). Nothing was substituted and nothing "
            f"was sent to the model.")
