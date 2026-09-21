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


async def _reconcile_worker(app) -> None:
    """Off-loop MCP reconcile with a cancellation-safe join. await_preparation
    joins the worker thread even under cancellation; the in-flight flag is
    cleared ONLY after it settles.

    A FAILED dispatch rebuild must not resume turns over a stale map. So the
    rebuild's outcome is explicit: on success `_mcp_dispatch_blocked` is cleared
    (the current map is installed); on failure it is SET, the waiters are still
    woken (no infinite wait), and the _stream gate / tool dispatch then defer on
    that flag until a later reconcile rebuilds successfully. The exception is
    recorded and reported, never swallowed as success."""
    from litetui.agent_preparation import await_preparation
    try:
        outcomes = await await_preparation(app.mcp.reconcile)
    except Exception as e:  # noqa: BLE001 — report, never leave the flag stuck
        # Bounded: exception TYPE only. A raw reconcile error can embed a
        # server URL or a secret from mcp.json; per-server outcomes are already
        # sanitized by the coordinator.
        outcomes = {"": f"failed ({type(e).__name__})"}
    finally:
        try:
            app.rebuild_mcp_dispatch()
        except Exception as e:  # noqa: BLE001 — a stale map BLOCKS, never resumes
            # Bounded reason (type + operation), never the raw message.
            app._mcp_dispatch_blocked = f"dispatch rebuild failed ({type(e).__name__})"
        else:
            app._mcp_dispatch_blocked = None   # verified: current map installed
        finally:
            # The reconcile op itself is done either way: clear the in-flight
            # flag and WAKE waiters. They re-check _mcp_dispatch_blocked after
            # the wait and defer (typed) rather than run over a stale map.
            app._mcp_maintenance = False
            ev = getattr(app, "_mcp_maintenance_done", None)
            if ev is not None:
                ev.set()
    if getattr(app, "_mcp_dispatch_blocked", None):
        app.system_message(
            _format_reconcile(outcomes)
            + "\n\n[mcp] ⚠ tool dispatch map FAILED to rebuild — tool routing is BLOCKED until a "
              "successful /mcp reconcile or restart.")
    else:
        app.system_message(_format_reconcile(outcomes))


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
        # A fresh (cleared) completion Event is what _stream awaits in-worker.
        import asyncio
        app._mcp_maintenance = True
        app._mcp_maintenance_done = asyncio.Event()
        app.run_worker(_reconcile_worker(app), group="mcp", exclusive=False)
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
        err = app.mcp.add(rest[0], cfg)
        app.rebuild_mcp_dispatch()
        # A start failure still leaves the server DECLARED, so the message says
        # both halves rather than a bare "failed" that hides the write.
        if err:
            app.system_message(f"Declared {rest[0]!r} in .mcp.json, but it did not start: {err}")
        else:
            app.system_message(f"Added and connected {rest[0]!r}.\n\n" + _render(app))
        return

    if not rest:
        app.system_message(f"Usage: /mcp {verb} <name>")
        return
    target = rest[0]

    if verb == "connect":
        err = app.mcp.connect(target)
        app.rebuild_mcp_dispatch()
        app.system_message(f"Could not connect {target!r}: {err}" if err
                           else f"Connected {target!r}.\n\n" + _render(app))
        return

    if verb in ("disconnect", "stop"):
        was = app.mcp.disconnect(target)
        app.rebuild_mcp_dispatch()
        app.system_message(
            f"Disconnected {target!r}. It stays declared — /mcp connect {target} brings it back."
            if was else f"{target!r} was not running."
        )
        return

    if verb == "reconnect":
        # Re-read first: the usual reason to reconnect is that the config was
        # just edited, and reconnecting to the OLD entry would look like the
        # edit did nothing.
        app.mcp.reload_configs()
        err = app.mcp.reconnect(target)
        app.rebuild_mcp_dispatch()
        app.system_message(f"Could not reconnect {target!r}: {err}" if err
                           else f"Reconnected {target!r}.\n\n" + _render(app))
        return

    if verb in ("remove", "rm", "delete"):
        err = app.mcp.remove(target)
        app.rebuild_mcp_dispatch()
        app.system_message(f"Could not remove {target!r}: {err}" if err
                           else f"Removed {target!r} from .mcp.json.\n\n" + _render(app))
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
