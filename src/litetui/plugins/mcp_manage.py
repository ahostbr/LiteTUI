"""`/mcp` — the management surface for MCP servers.

WHY THIS EXISTS. LiteTUI has had a working MCP client for a long time and no
way to manage it: `mcp.json` was read ONCE at boot, and the only controls were
`mcp_enabled` and the `mcp_disabled_servers` denylist in Settings → Capabilities,
both boot-time filters that say so in their own confirmation ("Applies on next
/reconnect"). Adding a server meant quitting, hand-editing JSON and restarting.
Ryan, 2026-09-03: "there needs to be a full cmd and UI for adding connecting
disconnecting reconnecting and removing mcps etc."

🔴 EVERY VERB THAT CHANGES WHAT IS RUNNING CALLS `app.rebuild_mcp_dispatch()`.
The specs the model sees are already live — `mcp_plugin` registers a dynamic
provider whose specs callable is evaluated per turn — but the dispatch map is
cached at init. Advertise a tool without rebuilding that map and the model calls
a schema the loop cannot route, which surfaces as "unknown tool" and reads like
a model fault instead of a stale cache. The rebuild is cheap and idempotent, so
it is unconditional rather than guessing whether the set moved.

WHAT THIS DOES NOT DO. It never writes `mcp.json` — only `.mcp.json` (see
mcp_client.WRITE_CONFIG_NAME), and it refuses rather than producing a write that
the precedence rule would make invisible. And a `disconnect` is runtime-only: it
does not persist, because persistence already has an owner in
`mcp_disabled_servers`, and a transient stop that silently rewrote the user's
config would be a surprise on the next boot.
"""
from __future__ import annotations

import json

from litetui.plugins import PluginManifest

USAGE = (
    "/mcp                     list every server, its state and tool count\n"
    "/mcp connect <name>      start one that is declared but stopped\n"
    "/mcp disconnect <name>   stop one (runtime only — it stays declared)\n"
    "/mcp reconnect <name>    stop and start from a fresh connection\n"
    "/mcp reload              re-read mcp.json / .mcp.json, touching nothing\n"
    "/mcp add <name> <url|command [args...]>   declare in .mcp.json and start\n"
    "/mcp add <name> {json}   the same, for entries needing env or cwd\n"
    "/mcp remove <name>       stop it and delete it from .mcp.json"
)

#: Rendered per row. `orphan` is the one worth explaining in place: it is
#: running and NOT declared any more, so a restart will not bring it back.
_STATE_NOTE = {
    "connected": "",
    "stopped": "declared, not running",
    "disabled": "disabled: true in the config",
    "failed": "",
    "orphan": "running but no longer declared — a restart will not bring it back",
}


def _render(app) -> str:
    rows = app.mcp.describe()
    if not rows:
        return (
            "No MCP servers declared.\n"
            "  Add one:  /mcp add litesuite-tools http://localhost:7423/mcp\n"
            f"  Config:   {app.mcp.root / '.mcp.json'}"
        )
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        bits = [f"{r['name']:<{width}}  {r['state']:<9}  {r['transport']:<5}"]
        bits.append(f"{r['tools']:>2} tools" if r["state"] == "connected" else "        ")
        bits.append(r["target"][:60])
        line = "  ".join(b for b in bits if b.strip() or True)
        note = r["error"] or _STATE_NOTE.get(r["state"], "")
        if note:
            line += f"\n      {note}"
        lines.append(line)
    total = sum(r["tools"] for r in rows)
    lines.append(f"\n{len(rows)} server(s), {total} tool(s) offered to the model.")
    return "\n".join(lines)


def _entry_from_words(words: list[str]) -> tuple[dict | None, str | None]:
    """Turn the tail of a command line into a config entry.

    Two shapes, told apart the same way `_build` tells transports apart rather
    than by a flag the user has to remember: something that looks like a URL is
    an HTTP endpoint, anything else is a command plus its args. A `{...}` blob
    is passed through for the entries that need `env` or `cwd`, which no
    positional syntax should try to express.
    """
    if not words:
        return None, "nothing to add — see /mcp for the shapes"
    blob = " ".join(words).strip()
    if blob.startswith("{"):
        try:
            return json.loads(blob), None
        except ValueError as e:
            return None, f"that is not valid JSON: {e}"
    first = words[0]
    if first.startswith(("http://", "https://")):
        if len(words) > 1:
            return None, "a url takes no extra arguments — did you mean a command?"
        return {"type": "http", "url": first}, None
    return {"command": first, "args": words[1:]}, None


def _settle_maintenance(app) -> str:
    """Rebuild the dispatch map, clear the in-flight flag, and WAKE waiters —
    the shared finally of every off-loop MCP mutation (reconcile AND the single
    verbs). Returns a block-warning suffix ('' unless the rebuild failed).

    A FAILED rebuild must not resume turns over a stale map, so its outcome is
    explicit: success clears `_mcp_dispatch_blocked` (current map installed),
    failure SETS it (bounded reason — type only, never raw error text). Waiters
    are woken either way; the _stream gate / tool dispatch then defer on the
    flag until a later reconcile rebuilds successfully."""
    try:
        app.rebuild_mcp_dispatch()
    except Exception as e:  # noqa: BLE001 — a stale map BLOCKS, never resumes
        app._mcp_dispatch_blocked = f"dispatch rebuild failed ({type(e).__name__})"
    else:
        app._mcp_dispatch_blocked = None
    finally:
        app._mcp_maintenance = False
        ev = getattr(app, "_mcp_maintenance_done", None)
        if ev is not None:
            ev.set()
    if getattr(app, "_mcp_dispatch_blocked", None):
        return ("\n\n[mcp] ⚠ tool dispatch map FAILED to rebuild — tool routing is BLOCKED until a "
                "successful /mcp reconcile or restart.")
    return ""


async def _reconcile_worker(app) -> None:
    """Off-loop MCP reconcile with a cancellation-safe join. await_preparation
    joins the worker thread even under cancellation; _settle_maintenance clears
    the in-flight flag ONLY after it settles and wakes any awaiting turn."""
    from litetui.agent_preparation import await_preparation
    note = ""
    try:
        outcomes = await await_preparation(app.mcp.reconcile)
    except Exception as e:  # noqa: BLE001 — report, never leave the flag stuck
        # Bounded: exception TYPE only. A raw reconcile error can embed a server
        # URL or a secret from mcp.json; per-server outcomes are already
        # sanitized by the coordinator.
        outcomes = {"": f"failed ({type(e).__name__})"}
    finally:
        note = _settle_maintenance(app)
    app.system_message(_format_reconcile(outcomes) + note)


async def _mutation_worker(app, op, describe) -> None:
    """Run ONE MCP mutation (connect/disconnect/reconnect/remove/add) off-loop —
    off the UI thread, through the coordinator's own claim — then settle the
    dispatch map. `describe(result)` formats the op's return into a user line.
    MCPBusy and any other error become a bounded message; the map is settled
    (and blocked on rebuild failure) in every case."""
    from litetui.agent_preparation import await_preparation
    from litetui.mcp_client import MCPBusy
    msg, note = "", ""
    try:
        msg = describe(await await_preparation(op))
    except MCPBusy:
        msg = "MCP maintenance is in progress — try again in a moment."
    except Exception as e:  # noqa: BLE001 — bounded, never echo raw error text
        msg = f"MCP operation failed ({type(e).__name__})."
    finally:
        note = _settle_maintenance(app)
    app.system_message(msg + note)


def _claim_and_run(app, coro) -> bool:
    """Claim maintenance synchronously (so a second op is rejected by the gate
    rather than racing this one), then run `coro` off-loop via run_guarded.

    A worker cancelled BEFORE its coroutine's first step never runs the finally
    that settles maintenance; run_guarded's cleanup is the release that un-sticks
    it in that case (and on a schedule/attach failure). The release no-ops once
    the coro has already settled (its Event is set). Lifecycle co-designed with
    RigidStem (worker._task done-callback + a startup gate; see run_guarded)."""
    import asyncio
    from litetui.agent_preparation import run_guarded
    app._mcp_maintenance = True
    ev = asyncio.Event()
    app._mcp_maintenance_done = ev

    def _release():
        if not ev.is_set():
            app._mcp_maintenance = False
            ev.set()

    def _report(e):
        app.system_message(
            f"Could not start the MCP operation ({type(e).__name__ if e else 'not tracked'}).")

    return run_guarded(app, coro, group="mcp", cleanup=_release, report=_report) is not None


def _schedule_mutation(app, op, describe) -> None:
    """Run one MCP mutation off-loop through the coordinator. A fresh completion
    Event is what a concurrent turn's _stream gate awaits."""
    _claim_and_run(app, _mutation_worker(app, op, describe))


def _format_reconcile(outcomes: dict) -> str:
    if list(outcomes) == [""]:
        return f"[mcp reconcile] {outcomes['']}"
    if not outcomes:
        return "[mcp reconcile] no changes."
    rows = "\n".join(f"  {n}: {o}" for n, o in sorted(outcomes.items()))
    return "[mcp reconcile]\n" + rows


def _safe_reload(app) -> str | None:
    """Re-read configs, returning a user message if a maintenance op holds the
    coordinator claim. reload_configs raises MCPBusy while a reconcile is in
    flight; the read verbs (bare/list/reload) are not in the server-changing
    gate above, so without this the exception would escape to the command
    dispatcher as an error instead of a one-line "try again"."""
    from litetui.mcp_client import MCPBusy
    try:
        app.mcp.reload_configs()
        return None
    except MCPBusy:
        # Bounded message: MCPBusy carries lock state, but never echo exception
        # text to the user — config errors can embed URLs/tokens.
        return "MCP maintenance is in progress; try again in a moment."


def _mutation_blocked_reason(app, *, ignore_workers=(), ignore_screen=None) -> str | None:
    """Why an MCP server-changing action must be refused right now, or None to
    proceed. Shared by the /mcp command AND the dialog so neither bypasses the
    native / maintenance / idle gates.

    The idle gate (a mutation must not race an active turn/tool/child) is real
    for both callers. The dialog is itself hosted in a worker behind a modal, so
    it would otherwise see ITSELF as busy — it passes its own host-worker id and
    modal screen as ignore_* so produce_activity excludes exactly that identity
    while STILL blocking on a real turn/tool/child/busy-store or a second modal.
    The command passes neither (full gate). Defaults empty = fail-closed."""
    if getattr(app, "_mcp_maintenance", False):
        return "MCP maintenance is in progress — try again in a moment."
    if hasattr(app.backend, "app_server"):
        return ("Native Codex session: an MCP server change needs a restart to reach the model "
                "(the thread's tool inventory is fixed for its lifetime).")
    from pathlib import Path
    from litetui.plugin_reload_activity import produce_activity
    from litetui.plugin_reload_children import children_pending
    from litetui.plugin_reload_state import blocking_reasons
    try:
        snap = produce_activity(
            app,
            children_pending=lambda: children_pending(Path.home() / ".litetui-agents", app.convo_id),
            ignore_workers=ignore_workers,
            ignore_screen=ignore_screen,
        ).snapshot
        reasons = blocking_reasons(snap)
    except Exception:  # noqa: BLE001 — an activity-probe failure DEFERS, never proceeds
        return "Could not confirm it is safe to change MCP servers right now — try again shortly."
    if reasons:
        return f"Deferred (busy): {'; '.join(reasons)}. Try the action again shortly."
    return None


def _cmd_mcp(app, name: str, arg: str) -> None:
    words = (arg or "").split()
    if not words:
        # Bare `/mcp` opens the dialog; `/mcp list` prints the same content as
        # text. Both exist because the two callers are different: a person wants
        # buttons, and the agent — which can run this command through the shell
        # of its own harness — wants something it can read back.
        from litetui.mcp_list import MCPListBody
        from litetui.side_panel import open_dialog

        busy = _safe_reload(app)
        if busy:
            app.system_message(busy)
            return
        # `open_dialog`, not `show_dialog`: this handler is SYNC and show_dialog
        # is a coroutine (the T078 mismatch). No callback — every action in the
        # dialog is applied when it is made, so there is no answer to collect.
        open_dialog(app, MCPListBody)
        return

    verb = words[0].lower()
    rest = words[1:]

    # Server-changing verbs share the reload exclusion + native/idle gates. On
    # native the live Codex thread's dynamicTools are frozen, so a tool-set
    # change needs a restart to reach the model; and a mutation must not race an
    # active turn or an in-flight maintenance pass.
    if verb in ("add", "connect", "disconnect", "stop", "reconnect", "remove",
                "rm", "delete", "reconcile"):
        if getattr(app, "_mcp_maintenance", False):
            app.system_message("MCP maintenance is in progress — try again in a moment.")
            return
        if hasattr(app.backend, "app_server"):
            app.system_message(
                "Native Codex session: an MCP server change needs a restart to reach the model "
                "(the thread's tool inventory is fixed for its lifetime).")
            return
        from pathlib import Path
        from litetui.plugin_reload_activity import produce_activity
        from litetui.plugin_reload_children import children_pending
        from litetui.plugin_reload_state import blocking_reasons
        snap = produce_activity(
            app,
            children_pending=lambda: children_pending(Path.home() / ".litetui-agents", app.convo_id),
        ).snapshot
        reasons = blocking_reasons(snap)
        if reasons:
            app.system_message(f"Deferred (busy): {'; '.join(reasons)}. Run /mcp {verb} again shortly.")
            return

    if verb == "reconcile":
        # Claim maintenance SYNCHRONOUSLY (before scheduling) so a second
        # reconcile is rejected by the gate above rather than cancelling this
        # one; the worker joins off-loop and clears the flag only when settled.
        # _claim_and_run un-sticks maintenance if scheduling itself fails.
        if _claim_and_run(app, _reconcile_worker(app)):
            app.system_message("MCP reconcile started — re-reading config and reconnecting owned servers.")
        return

    if verb in ("help", "?"):
        app.system_message(USAGE)
        return

    if verb in ("list", "ls", "status"):
        busy = _safe_reload(app)
        app.system_message(busy or _render(app))
        return

    if verb == "reload":
        busy = _safe_reload(app)
        app.system_message(busy or ("Re-read mcp.json / .mcp.json — nothing started or stopped.\n\n"
                                    + _render(app)))
        return

    if verb == "add":
        if not rest:
            app.system_message("Usage: /mcp add <name> <url|command [args...]>")
            return
        cfg, why = _entry_from_words(rest[1:])
        if cfg is None:
            app.system_message(f"Cannot add {rest[0]!r}: {why}")
            return
        srv = rest[0]
        # A start failure still leaves the server DECLARED, so the message says
        # both halves rather than a bare "failed" that hides the write.
        _schedule_mutation(
            app, lambda: app.mcp.add(srv, cfg),
            lambda err: (f"Declared {srv!r} in .mcp.json, but it did not start: {err}" if err
                         else f"Added and connected {srv!r}.\n\n" + _render(app)))
        return

    if not rest:
        app.system_message(f"Usage: /mcp {verb} <name>")
        return
    target = rest[0]

    if verb == "connect":
        _schedule_mutation(
            app, lambda: app.mcp.connect(target),
            lambda err: (f"Could not connect {target!r}: {err}" if err
                         else f"Connected {target!r}.\n\n" + _render(app)))
        return

    if verb in ("disconnect", "stop"):
        _schedule_mutation(
            app, lambda: app.mcp.disconnect(target),
            lambda was: (f"Disconnected {target!r}. It stays declared — /mcp connect {target} brings it back."
                         if was else f"{target!r} was not running."))
        return

    if verb == "reconnect":
        # Re-read first: the usual reason to reconnect is that the config was
        # just edited, and reconnecting to the OLD entry would look like the
        # edit did nothing. Both run off-loop in the mutation worker.
        def _reconnect_op():
            app.mcp.reload_configs()
            return app.mcp.reconnect(target)
        _schedule_mutation(
            app, _reconnect_op,
            lambda err: (f"Could not reconnect {target!r}: {err}" if err
                         else f"Reconnected {target!r}.\n\n" + _render(app)))
        return

    if verb in ("remove", "rm", "delete"):
        _schedule_mutation(
            app, lambda: app.mcp.remove(target),
            lambda err: (f"Could not remove {target!r}: {err}" if err
                         else f"Removed {target!r} from .mcp.json.\n\n" + _render(app)))
        return

    app.system_message(f"Unknown /mcp verb {verb!r}.\n\n{USAGE}")


def _register(ctx) -> None:
    ctx.command(
        ("/mcp",), _cmd_mcp,
        palette="MCP servers",
        help="Add, connect, disconnect or remove MCP tool servers.",
        group="backend",
        order=70,
    )


PLUGIN = PluginManifest(id="mcp_manage", register=_register)
