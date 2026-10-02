"""Conversation lifecycle commands: new, system prompt, compact, list, resume.

Handler bodies moved verbatim from the chain (self -> app). These are the
command surfaces; the transcript format has one owner and it is not here.

READING AND LABELLING GO THROUGH THE PUBLIC REPOSITORY — `ConversationRepository`
for `read`/`label`/`fmt_size`/`list_all`, and `app.store` for writes and the
persist-error flag. The app still carries private aliases for those (T070 S2
removed this plugin as their last product caller), so do not reach back through
`app._*` for anything the repository already exposes.

Still app-owned and reached privately, pending the `ctx.conversation` facade:
new/resume/compact/edit/materialise and the picked-conversation callback.

⚠️ This paragraph used to list `_list_convos` among the private calls. It was
accurate when written and went stale at S2, and a `git grep app._` then counted
the PROSE as a live caller and reported this plugin as blocking that alias's
deletion. A private name written in a docstring is indistinguishable from a call
site to any text gate — which is why coupling here is counted by AST, and why
this paragraph is now kept true rather than merely informative.
"""
import time

from litetui import paths
from litetui.conversation import ConversationRepository
from litetui.picker import pick
from litetui.plugins import PluginManifest


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
    app.system_message(
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
        app.system_message(f"System prompt set: {preview}")
    else:
        app.system_message("Usage: /system <prompt>")


def _cmd_compact(app, name: str, arg: str) -> None:
    app._compact(arg)


def _open_convos_picker(app, agent_id=None) -> None:
    """Pick an authoritative agent first, then one of its conversations."""
    from litetui.agent_store import AgentStore, StoreError
    from litetui.storage_catalog import conversations
    session = getattr(app, '_agent_session', None)
    root = session.store.data_root if session is not None else paths.data_root()
    store = AgentStore(root)
    try:
        if agent_id is None:
            agents = store.list_agents()
            if not agents:
                app.system_message('No ready owned agents. Legacy conversations are read-only archives; use catalog/export.')
                return
            title = 'Choose an agent'
            if app.store.persist_error:
                title += '  [!] SAVING IS BROKEN'
            pick(app, title, [(a.agent_id, a.name) for a in agents],
                 lambda selected: _open_convos_picker(app, selected) if selected else None,
                 current=session.authority.agent_id if session is not None else None)
            return
        agent = store.find_agent(agent_id=agent_id)
        locations = conversations(root, agent_id=agent.agent_id)
        rows = [(row.transcript, *ConversationRepository.read(row.transcript)) for row in locations]
        rows.sort(key=lambda row: row[0].stat().st_mtime, reverse=True)
    except (StoreError, OSError) as exc:
        app.system_message(f'Agent catalog blocked: {exc}')
        return
    if session is None or session.authority.agent_id != agent.agent_id:
        # Selecting another agent is an explicit process boundary, never a hot
        # identity switch in this seat. The single action launches via harness.
        items = [(row[0].parent.name, ConversationRepository.label(row[1], row[2])) for row in rows]
        if not items:
            items = [('new', 'Start a new conversation')]
        def choose(selected):
            if not selected:
                return
            def opened(action):
                if action != 'open':
                    return
                async def launch():
                    from litetui.agent_seat_launch import open_seat
                    try:
                        result = await open_seat(app, agent.agent_id, None if selected == 'new' else selected)
                        app.system_message(result)
                    except (StoreError, OSError, TimeoutError) as exc:
                        app.system_message(f'Own-seat launch blocked: {exc}')
                app.run_worker(launch(), name='open-owned-agent', group='open-owned-agent', exclusive=True)
            pick(app, f'{agent.name}: separate agent seat',
                 [('open', f'Open {agent.name} in its own seat')], opened)
        pick(app, f'{agent.name}: choose a conversation', items, choose)
        return
    if not rows:
        app.system_message("Nothing to resume.")
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
                f"{ConversationRepository.fmt_size(path.stat().st_size):>7}  "
                f"{ConversationRepository.label(meta, msgs)}",
            )
        )
    title = "Resume a conversation"
    if app.store.persist_error:
        # _note_persist_error announces the FIRST save failure and is
        # never heard from again; this badge is the on-demand
        # re-statement, on the exact surface the user is looking at.
        title += "  [!] SAVING IS BROKEN"
    pick(
        app,
        title,
        items,
        app._on_convo_picked,
        current=str(app.convo_path) if app.convo_path else None,
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
                meta, _msgs = ConversationRepository.read(app.convo_path)
                current = str(meta.get("name") or "")
            except OSError:
                current = ""
        app.system_message(
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
        app.system_message("No conversation to name yet.")
        return
    app.store.write_record({"type": "rename", "name": wanted})
    app.system_message(f"Named this conversation {wanted!r}. It shows in /convos and /resume.")


def _cmd_resume(app, name: str, arg: str) -> None:
    if not arg:
        _open_convos_picker(app)
        return
    from litetui.storage_catalog import conversations
    session = getattr(app, '_agent_session', None)
    if session is None:
        app.system_message('Choose an owned agent first with /resume; archives are read-only.')
        return
    try:
        rows = [(row.transcript, *ConversationRepository.read(row.transcript))
                for row in conversations(session.store.data_root, agent_id=session.authority.agent_id)]
        rows.sort(key=lambda row: row[0].stat().st_mtime, reverse=True)
    except (OSError, ValueError) as exc:
        app.system_message(f'Agent conversations blocked: {exc}')
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
        app.system_message(
            f"No conversation matches {arg!r}. Run /resume with no argument to pick one."
        )
        return
    if target is None:
        _open_convos_picker(app)
        return
    app._resume(target[0])


def _cmd_claude(app, _name: str, arg: str) -> None:
    from litetui.claude_turn import command

    command(app, arg)


def _register(ctx) -> None:
    ctx.command(
        ("/claude",), _cmd_claude,
        palette="Claude session",
        help="Native Claude session: status | resolve (no replay) | continue | new.",
        group="convo",
        order=45,
    )
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
