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
from litetui.friendly_errors import present

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
        note = (present(r["error"], app.settings.error_message_style, surface=f"mcp:{r['name']}")
                if r["error"] else _STATE_NOTE.get(r["state"], ""))
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


def _cmd_mcp(app, name: str, arg: str) -> None:
    words = (arg or "").split()
    if not words:
        # Bare `/mcp` opens the dialog; `/mcp list` prints the same content as
        # text. Both exist because the two callers are different: a person wants
        # buttons, and the agent — which can run this command through the shell
        # of its own harness — wants something it can read back.
        from litetui.mcp_list import MCPListBody
        from litetui.side_panel import open_dialog

        app.mcp.reload_configs()
        # `open_dialog`, not `show_dialog`: this handler is SYNC and show_dialog
        # is a coroutine (the T078 mismatch). No callback — every action in the
        # dialog is applied when it is made, so there is no answer to collect.
        open_dialog(app, MCPListBody)
        return

    verb = words[0].lower()
    rest = words[1:]

    if verb in ("help", "?"):
        app.system_message(USAGE)
        return

    if verb in ("list", "ls", "status"):
        app.mcp.reload_configs()
        app.system_message(_render(app))
        return

    if verb == "reload":
        app.mcp.reload_configs()
        app.system_message("Re-read mcp.json / .mcp.json — nothing started or stopped.\n\n"
                           + _render(app))
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
