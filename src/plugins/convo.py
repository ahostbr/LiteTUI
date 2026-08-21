"""Conversation lifecycle commands: new, system prompt, compact, list, resume.

Handler bodies moved verbatim from the chain (self -> app). The persistence
machinery they call (_new_convo, _list_convos, _resume, _compact, the store
layout) stays app-owned — one owner for the transcript format; these are
the command surfaces over it.
"""
import time

import paths
from picker import PickerScreen
from plugins import PluginManifest


def _cmd_new(app, name: str, arg: str) -> None:
    app.conversation.clear()
    app._new_convo()  # a fresh file — never reuse the old one
    app._load_system_prompt()
    app.query_one("#chat-log").remove_children()
    app._system(
        f"New conversation — {app.convo_id}\n"
        f"  store: {app.convo_dir}\n"
        f"  memory.md · soul.md · handoff.md · {paths.MEMORIES_DIR}/"
    )


def _cmd_system(app, name: str, arg: str) -> None:
    if arg:
        if app.conversation and app.conversation[0]["role"] == "system":
            app.conversation[0]["content"] = arg
        else:
            app.conversation.insert(
                0, {"role": "system", "content": arg}
            )
        app._edit(0, "system prompt changed")
        preview = arg[:80] + ("..." if len(arg) > 80 else "")
        app._system(f"System prompt set: {preview}")
    else:
        app._system("Usage: /system <prompt>")


def _cmd_compact(app, name: str, arg: str) -> None:
    app._compact(arg)


def _cmd_convos(app, name: str, arg: str) -> None:
    rows = app._list_convos()
    if not rows:
        app._system(f"No saved conversations yet.\nThey land in {paths.CONVO_DIR}")
        return
    lines = []
    total = 0
    for i, (p, meta, msgs) in enumerate(rows[:30], 1):
        mark = ">" if p == app.convo_path else " "
        stamp = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
        turns = sum(1 for m in msgs if m.get("role") in ("user", "assistant"))
        mem = p.parent / paths.MEMORIES_DIR
        n_mem = len(list(mem.glob("*.md"))) if mem.exists() else 0
        badge = f" ✎{n_mem}" if n_mem else "   "
        total += p.stat().st_size
        lines.append(
            f" {mark} {i:>2}. {p.parent.name[:8]}  {stamp}  {turns:>3} msg{badge}  "
            f"{app._fmt_size(p.stat().st_size):>7}  {app._convo_title(msgs)}"
        )
    extra = f"\n(+{len(rows) - 30} older)" if len(rows) > 30 else ""
    warn = (
        f"\n\n[!] saving is BROKEN this session: {app._persist_error}"
        if app._persist_error
        else ""
    )
    app._system(
        "Saved conversations (newest first):\n"
        + "\n".join(lines)
        + extra
        + f"\n{app._fmt_size(total)} of transcript across {len(rows)} conversation(s)"
        + "\nUse /resume <number> to load one."
        + warn
    )


def _cmd_resume(app, name: str, arg: str) -> None:
    rows = app._list_convos()
    if not rows:
        app._system("Nothing to resume.")
        return
    target = None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(rows):
            target = rows[idx]
    elif arg:
        # Match on the uuid FOLDER, not the transcript stem — every
        # transcript is named convo.jsonl, so stems no longer identify.
        for row in rows:
            uid = row[0].parent.name
            if uid == arg or uid.startswith(arg):
                target = row
                break
    if target is None and arg:
        app._system(
            f"No conversation matches {arg!r}. Run /resume with no argument to pick one."
        )
        return
    if target is None:
        # Same picker as /model, so the two interactions cannot drift.
        items = []
        for path_, meta_, msgs_ in rows[:40]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(path_.stat().st_mtime))
            turns = sum(1 for x in msgs_ if x.get("role") in ("user", "assistant"))
            memdir = path_.parent / paths.MEMORIES_DIR
            nmem = len(list(memdir.glob("*.md"))) if memdir.exists() else 0
            badge = f" ✎{nmem}" if nmem else "   "
            # The conversation's own uuid — on disk all along, never shown.
            cid = path_.parent.name[:8]
            # The owning seat, only for conversations written since v3
            # meta. Older ones show blanks rather than a fabricated name.
            who = str(meta_.get("agent_name") or "")[:10]
            aid = str(meta_.get("agent_id") or "")[:8]
            owner = f"{who} {aid}".strip() or "—"
            items.append(
                (
                    str(path_),
                    f"{stamp}  {cid}  {owner:<19}  {turns:>3} msg{badge}  "
                    f"{app._fmt_size(path_.stat().st_size):>7}  "
                    f"{app._convo_title(msgs_)}",
                )
            )
        app.push_screen(
            PickerScreen(
                "Resume a conversation",
                items,
                current=str(app.convo_path) if app.convo_path else None,
            ),
            app._on_convo_picked,
        )
        return
    app._resume(target[0])


def _register(ctx) -> None:
    ctx.command(
        ("/new", "/clear", "/reset"), _cmd_new,
        palette="New conversation",
        help="Fresh transcript on disk (/new)",
    )
    ctx.command(("/system",), _cmd_system)
    ctx.command(
        ("/compact",), _cmd_compact,
        palette="Compact conversation",
        help="Summarise older turns, keep the recent (/compact)",
    )
    ctx.command(
        ("/convos", "/conversations", "/list"), _cmd_convos,
        palette="Conversations",
        help="List saved conversations (/convos)",
    )
    ctx.command(("/resume",), _cmd_resume)


PLUGIN = PluginManifest(id="convo", register=_register)
