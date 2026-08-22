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


def _convo_meta_bits(path, msgs) -> tuple[str, int, str]:
    """Timestamp, turn count, and memory badge — the three bits /convos and
    /resume both compute per row, from the same path+messages pair."""
    stamp = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    turns = sum(1 for m in msgs if m.get("role") in ("user", "assistant"))
    mem_dir = path.parent / paths.MEMORIES_DIR
    n_mem = len(list(mem_dir.glob("*.md"))) if mem_dir.exists() else 0
    badge = f" ✎{n_mem}" if n_mem else "   "
    return stamp, turns, badge


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


def _open_convos_picker(app) -> None:
    """The conversation picker modal, shared by /convos and /resume so
    the two commands show one UI instead of one modal and one wall of
    chat text. Selecting a row opens it; Esc closes it."""
    rows = app._list_convos()
    if not rows:
        app._system("Nothing to resume.")
        return
    # Same picker as /model, so the two interactions cannot drift.
    items = []
    for path, meta, msgs in rows[:40]:
        stamp, turns, badge = _convo_meta_bits(path, msgs)
        # The conversation's own uuid — on disk all along, never shown.
        cid = path.parent.name[:8]
        # The owning seat, only for conversations written since v3
        # meta. Older ones show blanks rather than a fabricated name.
        who = str(meta.get("agent_name") or "")[:10]
        aid = str(meta.get("agent_id") or "")[:8]
        owner = f"{who} {aid}".strip() or "—"
        items.append(
            (
                str(path),
                f"{stamp}  {cid}  {owner:<19}  {turns:>3} msg{badge}  "
                f"{app._fmt_size(path.stat().st_size):>7}  "
                f"{app._convo_label(meta, msgs)}",
            )
        )
    title = "Resume a conversation"
    if app._persist_error:
        # _note_persist_error announces the FIRST save failure and is
        # never heard from again; this badge is the on-demand
        # re-statement, on the exact surface the user is looking at.
        title += "  [!] SAVING IS BROKEN"
    app.push_screen(
        PickerScreen(
            title,
            items,
            current=str(app.convo_path) if app.convo_path else None,
        ),
        app._on_convo_picked,
    )


def _cmd_convos(app, name: str, arg: str) -> None:
    # The picker UI, same as /resume with no argument - the old behaviour
    # printed the rows into the chat, which is what the modal already is.
    _open_convos_picker(app)

#: Same cap the derived title uses, so a named row cannot blow the column
#: apart when every other row is bounded.
_NAME_MAX = 60


def _cmd_rename(app, name: str, arg: str) -> None:
    """Name the CURRENT conversation, so it can be found by what it was for."""
    wanted = " ".join(arg.split())          # collapse newlines and runs of spaces
    if not wanted:
        current = ""
        if app.convo_path is not None and app.convo_path.exists():
            try:
                meta, _msgs = app._read_convo(app.convo_path)
                current = str(meta.get("name") or "")
            except OSError:
                current = ""
        app._system(
            f"This conversation is named {current!r}." if current
            else "This conversation has no name. Give it one with /rename <name>."
        )
        return
    if len(wanted) > _NAME_MAX:
        wanted = wanted[:_NAME_MAX].rstrip() + "\u2026"
    # A conversation staged but not yet on disk has nowhere to put the record.
    # Materialising first means naming a fresh conversation works exactly like
    # naming an old one, instead of silently doing nothing.
    app._materialise_convo()
    if app.convo_path is None:
        app._system("No conversation to name yet.")
        return
    app._write_record({"type": "rename", "name": wanted})
    app._system(f"Named this conversation {wanted!r}. It shows in /convos and /resume.")


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
        _open_convos_picker(app)
        return
    app._resume(target[0])


def _register(ctx) -> None:
    ctx.command(
        ("/new", "/clear", "/reset"), _cmd_new,
        palette="New conversation",
        help="Start fresh. This chat is saved first.",
        group="convo",
        order=10,
    )
    ctx.command(("/system",), _cmd_system)
    ctx.command(
        ("/compact",), _cmd_compact,
        palette="Compact conversation",
        help="Makes room to keep going by summarising older messages. Nothing is deleted.",
        group="convo",
        order=30,
    )
    ctx.command(
        ("/convos", "/conversations", "/list"), _cmd_convos,
        palette="Conversations",
        help="Reopen an earlier chat.",
        group="convo",
        order=20,
    )
    ctx.command(("/resume",), _cmd_resume)
    ctx.command(
        ("/rename",), _cmd_rename,
        palette="Rename conversation",
        help="Give this chat a name so you can find it later.",
        group="convo",
        order=40,
    )


PLUGIN = PluginManifest(id="convo", register=_register)
