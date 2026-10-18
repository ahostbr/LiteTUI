"""Bounded prompt commands through existing private OS-user RPC capabilities.

A catalog read is not a write capability. Only the live target's owning app
writes. The apply actor is an assertion from the trusted actor route, not a
cryptographically authenticated identity against other same-OS-user processes.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
from pathlib import Path
import secrets
import re
from uuid import uuid4

from litetui import harness, paths, prompt_overrides as prompts
from litetui.agent_store import AgentStore, valid_id

REQUEST = "gui.prompts.request"
APPLY = "gui.prompts.apply"
LOCAL_OPERATIONS = frozenset({REQUEST, APPLY})


def target(root: Path, seat: str, convo: str):
    store = AgentStore(root)
    try:
        identity = valid_id(seat)
    except ValueError:
        agent = store.find_agent(name=seat)
    else:
        agent = store.find_agent(agent_id=identity)
    if convo is not None:
        store.conversation_directory(agent, valid_id(convo))
    return agent, agent.directory


def registry(identity: str) -> dict:
    identity = valid_id(identity)
    file = prompts.checked(harness.AGENTS_DIR / f"{identity}.json")
    row = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(row, dict) or row.get("agent_id") != identity:
        raise prompts.PromptError("Registered identity mismatch")
    return row


async def attach(identity: str, command: dict) -> dict:
    """Same existing local attachment authentication, bounded and no retries."""
    from litetui.local_rpc import MAX_FRAME
    row = registry(identity)
    endpoint = row.get("litetui_rpc")
    if (not isinstance(endpoint, dict) or endpoint.get("agent_id") != identity
            or endpoint.get("pid") != row.get("session_pid")
            or endpoint.get("host") != "127.0.0.1"
            or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535
            or not isinstance(endpoint.get("nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", endpoint["nonce"])):
        raise prompts.PromptError("Live private LiteTUI endpoint unavailable")
    credential = prompts.checked(Path(endpoint["token_path"]))
    expected = prompts.checked(paths.data_root() / "rpc" / identity / f"{endpoint['nonce']}.json")
    if credential != expected:
        raise prompts.PromptError("Private endpoint credential is outside its owner directory")
    auth = json.loads(prompts.read_text(credential))
    if (not isinstance(auth, dict) or any(auth.get(k) != endpoint[k]
                                        for k in ("agent_id", "pid", "nonce"))
            or not isinstance(auth.get("token"), str)):
        raise prompts.PromptError("Private endpoint credential mismatch")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", endpoint["port"], limit=MAX_FRAME + 1), 10)
    async def send(frame):
        raw = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise prompts.PromptError("Prompt request exceeds frame limit")
        writer.write(raw)
        await asyncio.wait_for(writer.drain(), 10)
    async def receive():
        raw = await asyncio.wait_for(reader.readline(), 30)
        if not raw or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            raise prompts.PromptError("Private endpoint response missing or oversized; outcome unknown")
        return json.loads(raw)
    try:
        await send({k: auth[k] for k in ("agent_id", "nonce", "token")})
        accepted = await receive()
        if (accepted.get("type") != "authenticated" or accepted.get("agent_id") != identity
                or accepted.get("nonce") != endpoint["nonce"]):
            raise prompts.PromptError("Private endpoint authentication refused")
        await send(command)
        response = await receive()
        if response.get("id") != command["id"] or not response.get("ok"):
            raise prompts.PromptError(response.get("error", "Private prompt operation failed"))
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def validate(command: dict, *, applying: bool) -> dict:
    fields = {"type", "id", "action", "seat", "convo", "name", "reason", "text", "global_sha"}
    if applying:
        fields.update({"actor", "notification_sent"})
    if set(command) != fields:
        raise prompts.PromptError("Unexpected prompt request fields")
    valid_id(command["id"])
    valid_id(command["seat"])
    if command["convo"] is not None:
        valid_id(command["convo"])
    prompts.mutable(command["name"])
    if command["action"] == "reset" and command["name"] in {"role.md", "method.md"}:
        raise prompts.PromptError("Role/method reset has no global default; explicitly set the desired body instead")
    if command["action"] not in {"set", "reset"}:
        raise prompts.PromptError("Unknown prompt mutation")
    reason = command["reason"]
    text = command["text"]
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 2048:
        raise prompts.PromptError("A bounded nonempty reason is required")
    if not isinstance(text, str) or len(text.encode("utf-8")) > prompts.MAX_BYTES or "\0" in text:
        raise prompts.PromptError("Prompt must be bounded UTF-8 text without NUL")
    if command["action"] == "reset" and text:
        raise prompts.PromptError("Reset must not carry replacement text")
    if not isinstance(command["global_sha"], str) or not re.fullmatch(r"[0-9a-f]{64}", command["global_sha"]):
        raise prompts.PromptError("Global SHA required")
    if applying:
        valid_id(command["actor"])
        if command["notification_sent"] is not None and type(command["notification_sent"]) is not bool:
            raise prompts.PromptError("Notification result must be boolean or pending")
    return command


def owner(app):
    session = getattr(app, "_agent_session", None)
    seat = getattr(app, "seat", None)
    if session is None or seat is None or not seat.registered:
        raise prompts.PromptError("A registered, owned named-agent session is required")
    authority = session.authority
    if (authority.agent_id, authority.name) != (seat.agent_id, seat.name):
        raise prompts.PromptError("Live seat disagrees with owned agent authority")
    return authority


async def dispatch(app, command: dict) -> dict:
    if command["type"] == REQUEST:
        validate(command, applying=False)
        actor = owner(app)
        agent, _ = target(paths.data_root(), command["seat"], command["convo"])
        if agent.agent_id == actor.agent_id or harness.registered_spawner(agent.agent_id) != actor.agent_id:
            raise prompts.PromptError("Only the target's current registered leader may change prompts")
        forwarded = {**command, "type": APPLY, "actor": actor.agent_id, "notification_sent": None}
        result = await attach(agent.agent_id, forwarded)
        if result.get("duplicate"):
            return {**result, "actor": actor.agent_id}
        notice = result["notice"]
        try:
            sent = await asyncio.to_thread(app.seat.send, agent.agent_id, notice)
        except Exception:
            sent = False
        try:
            receipt = await attach(agent.agent_id, {**forwarded, "notification_sent": sent})
        except (OSError, ValueError, TimeoutError):
            return {**result, "actor": actor.agent_id, "notification_sent": sent,
                    "status": "APPLIED_NOTIFICATION_ACK_UNKNOWN"}
        return {**receipt, "actor": actor.agent_id}
    if command["type"] != APPLY:
        raise prompts.PromptError("Unknown private prompt operation")
    validate(command, applying=True)
    authority = owner(app)
    if authority.agent_id != command["seat"]:
        raise prompts.PromptError("Prompt request addressed to another owner")
    if (harness.registered_spawner(authority.agent_id) != command["actor"]
            or command["actor"] == authority.agent_id):
        raise prompts.PromptError("Prompt actor is not the current registered leader")
    registry(command["actor"])
    agent, directory = target(paths.data_root(), command["seat"], command["convo"])
    store = app.store
    if (store.agent_session is not app._agent_session
            or not store.owned or store.loading or store.convo_dir != app.convo_dir
            or app._agent_session.memory_root != directory
            or (command["convo"] is not None and app.convo_id != command["convo"])):
        raise prompts.PromptError("Target must own its agent home and requested live conversation")
    # Role text can vary; role authority cannot. Validate before publishing.
    if command["action"] == "set":
        from litetui.seat_prompts import role_body, role_name, validate_method
        if command["name"] == "role.md":
            role_body(command["text"], role_name(app.seat.tier))
        elif command["name"] == "method.md":
            validate_method(command["text"])
    # No await from ownership validation through publication: the target app loop
    # owns selection and turn snapshots; neither can interleave with this write.
    result, notice = apply(directory, command)
    if not result.get("duplicate"):
        try:
            app._system(notice)
        except Exception:
            result = {**result, "local_notice_failed": True}
    return {**result, "notice": notice}


def sync_directory(path: Path) -> None:
    # POSIX needs parent fsync for rename/creation durability. Windows does not
    # support directory fsync through os.open; publication there has filesystem
    # atomicity + file flush, not an additional power-loss durability guarantee.
    if os.name == "nt":
        return
    fd = os.open(prompts.checked(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_audit(directory: Path, row: dict) -> None:
    from datetime import datetime, timezone
    path = prompts.checked(directory / "prompts" / "audit.jsonl")
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(), **row}, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())
    sync_directory(path.parent)


def publish(path: Path, text: str) -> None:
    path = prompts.checked(path)
    temporary = prompts.checked(path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp"))
    with temporary.open("x", encoding="utf-8", newline="") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)  # failed temporaries are retained, never deleted
    sync_directory(path.parent)


def apply(directory: Path, command: dict, *, actor_provenance="trusted-private-RPC-route assertion; OS-user capability") -> tuple[dict, str]:
    if command["action"] == "reset" and command["name"] in {"role.md", "method.md"}:
        raise prompts.PromptError("Role/method reset has no global default; explicitly set the desired body instead")
    from litetui.shared_state import coordinated_write
    folder = prompts.checked(directory / "prompts")
    folder.mkdir(exist_ok=True)
    sync_directory(directory)
    audit = prompts.checked(folder / "audit.jsonl")
    prompts.checked(Path(str(audit) + ".lock"))
    with coordinated_write(audit):
        prompts.checked(audit)
        rows = []
        if audit.exists():
            raw = audit.read_text(encoding="utf-8")
            if raw and not raw.endswith("\n"):
                raise prompts.PromptError("Prompt audit has an incomplete tail; preserve and inspect before recovery")
            try:
                rows = [json.loads(line) for line in raw.splitlines() if line]
            except ValueError as exc:
                raise prompts.PromptError("Prompt audit is malformed; preserve and inspect before recovery") from exc
            if any(not isinstance(row, dict) for row in rows):
                raise prompts.PromptError("Prompt audit is malformed; preserve and inspect before recovery")
        matches = [r for r in rows if r.get("request_id") == command["id"] and r.get("phase") == "applied"]
        original = {k: v for k, v in command.items() if k != "notification_sent"}
        fingerprint = prompts.sha(json.dumps(original, sort_keys=True, ensure_ascii=False))
        if matches:
            row = matches[-1]
            if row["fingerprint"] != fingerprint:
                raise prompts.PromptError("Request id reused for different prompt mutation")
            published = prompts.metadata(directory, command["name"])
            if published is None:
                raise prompts.PromptError("Applied revision metadata is missing; publication outcome uncertain")
            if published.get("request_id") == command["id"] and (
                    published["active"] != (command["action"] == "set") or
                    (published["active"] and prompts.sha(prompts.read_text(directory / "prompts" / command["name"]))
                     != prompts.sha(command["text"].replace("\r\n", "\n").replace("\r", "\n")))):
                raise prompts.PromptError("Applied revision disagrees with disk; publication outcome uncertain")
            notification = [r for r in rows if r.get("request_id") == command["id"] and r.get("phase") == "notification"]
            sent = notification[-1]["notification_sent"] if notification else None
            acknowledgement = command["notification_sent"]
            if acknowledgement is not None:
                if notification and sent != acknowledgement:
                    raise prompts.PromptError("Conflicting notification acknowledgement")
                if not notification:
                    append_audit(directory, {"version": 1, "request_id": command["id"],
                                             "phase": "notification", "who": command["actor"],
                                             "fingerprint": fingerprint, "notification_sent": acknowledgement})
                sent = acknowledgement
            status = ("APPLIED" if sent is True else "APPLIED_NOTIFICATION_FAILED" if sent is False
                      else "APPLIED_NOTIFICATION_UNKNOWN")
            return ({"status": status, "request_id": command["id"], "duplicate": True,
                     "current_revision": published.get("request_id") == command["id"],
                     "notification_sent": sent}, "Prompt request historically applied; not replayed")
        if command["notification_sent"] is not None:
            raise prompts.PromptError("Cannot acknowledge a prompt that was not applied")
        # Publication uncertainty belongs to the whole conversation, not just
        # the requested id/name. Preserve all evidence before a fresh mutation.
        applied_ids = {r.get("request_id") for r in rows if r.get("phase") == "applied"}
        intent_ids = {r.get("request_id") for r in rows if r.get("phase") == "intent"}
        if intent_ids - applied_ids:
            raise prompts.PromptError("Prior prompt request has uncertain publication; inspect audit, do not retry blindly")
        retained_folder = prompts.checked(folder / "revisions")
        if retained_folder.exists():
            for retained_revision in retained_folder.iterdir():
                prompts.checked(retained_revision)
                if (not retained_revision.is_file() or retained_revision.suffix != ".json"
                        or retained_revision.stem not in intent_ids):
                    raise prompts.PromptError("Retained revision exists without audit intent; pre-publication outcome uncertain, inspect before new request")
        if any(r.get("request_id") == command["id"] for r in rows):
            raise prompts.PromptError("Prior prompt request has uncertain publication; inspect audit, do not retry blindly")
        name = command["name"]
        global_file = paths.prompt_file(name)
        global_text = prompts.read_text(global_file) if global_file.is_file() else ""
        if prompts.sha(global_text) != command["global_sha"]:
            raise prompts.PromptError("Global prompt drifted before publication; inspect diff and submit a new request")
        previous_file = paths.prompt_file(name, convo_dir=directory)
        before = prompts.read_text(previous_file) if previous_file.is_file() else ""
        after = command["text"] if command["action"] == "set" else global_text
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                          fromfile="previous", tofile="requested"))
        row = {"version": 1, "request_id": command["id"], "phase": "intent",
               "who": command["actor"], "actor_provenance": actor_provenance,
               "seat": command["seat"], "convo": command["convo"], "name": name,
               "action": command["action"], "reason": command["reason"], "diff": diff,
               "global_sha": prompts.sha(global_text), "fingerprint": fingerprint}
        revisions = prompts.checked(folder / "revisions")
        revisions.mkdir(exist_ok=True)
        retained = prompts.checked(revisions / f"{command['id']}.json")
        if retained.exists():
            raise prompts.PromptError("Retained revision exists without audit intent; pre-publication outcome uncertain, inspect before new request")
        with retained.open("x", encoding="utf-8") as out:
            json.dump({"previous_text": before, "previous_metadata": prompts.metadata(directory, name)}, out)
            out.flush()
            os.fsync(out.fileno())
        sync_directory(revisions)
        sync_directory(folder)
        append_audit(directory, row)
        # Publish the manifest first: a crash before body publication fails closed
        # rather than exposing untracked text (including a first managed override).
        normalized = after.replace("\r\n", "\n").replace("\r", "\n")
        publish(prompts.metadata_path(directory, name), json.dumps({
            "version": 1, "active": command["action"] == "set", "global_text": global_text,
            "global_sha": prompts.sha(global_text), "body_sha": prompts.sha(normalized),
            "request_id": command["id"]}, ensure_ascii=False))
        if command["action"] == "set":
            publish(folder / name, after)
        append_audit(directory, {**row, "phase": "applied"})
        notice = f"Prompt {command['action']} applied: {name}; leader {command['actor']}; reason: {command['reason']}; request {command['id']}. Effective next turn."
        return {"status": "APPLIED_NOTIFICATION_UNKNOWN", "notification_sent": None,
                "request_id": command["id"], "global_sha": row["global_sha"]}, notice


def inspect(directory: Path, name: str, *, diff: bool) -> str:
    prompts.mutable(name)
    effective = prompts.read_text(paths.prompt_file(name, convo_dir=directory))
    if not diff:
        return effective
    global_file = paths.prompt_file(name)
    global_text = prompts.read_text(global_file) if global_file.is_file() else ""
    metadata = prompts.metadata(directory, name)
    drift = metadata is not None and metadata["global_sha"] != prompts.sha(global_text)
    output = f"Global drift: {str(drift).lower()}\nCurrent global SHA: {prompts.sha(global_text)}\n"
    if metadata:
        output += f"Branched global SHA: {metadata['global_sha']}\nActive: {metadata['active']}\n"
        output += "".join(difflib.unified_diff(metadata["global_text"].splitlines(True), global_text.splitlines(True), fromfile="branched-global", tofile="current-global"))
    return output + "".join(difflib.unified_diff(global_text.splitlines(True), effective.splitlines(True), fromfile="current-global", tofile="seat-effective"))


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="litetui prompt")
    parser.add_argument("action", choices=("set", "show", "diff", "reset"))
    parser.add_argument("seat")
    parser.add_argument("name")
    parser.add_argument("--convo", help="optional current-conversation assertion; prompt storage is agent-home scoped")
    parser.add_argument("--via", help="actual live leader's private OS-user capability")
    parser.add_argument("--file")
    parser.add_argument("--reason")
    parser.add_argument("--request-id", help="canonical UUID for outcome lookup/dedup, never auto-retried")
    args = parser.parse_args(argv)
    try:
        name = prompts.mutable(args.name if args.name.endswith(".md") else args.name + ".md")
        agent, directory = target(paths.data_root(), args.seat, args.convo)
        if args.action in {"show", "diff"}:
            print(inspect(directory, name, diff=args.action == "diff"))
            return
        if not args.via or not args.reason or (args.action == "set") != bool(args.file):
            raise prompts.PromptError("set/reset requires --via and --reason; only set requires --file")
        leader, _ = target_agent(args.via)
        command = {"type": REQUEST, "id": valid_id(args.request_id) if args.request_id else str(uuid4()),
                   "action": args.action, "seat": agent.agent_id, "convo": valid_id(args.convo) if args.convo else None,
                   "name": name, "reason": args.reason,
                   "text": prompts.read_text(Path(args.file).absolute()) if args.file else "",
                   "global_sha": prompts.sha(prompts.read_text(paths.prompt_file(name)) if paths.prompt_file(name).is_file() else "")}
        validate(command, applying=False)
        print(f"REQUESTED {command['id']} (no automatic retry; a timeout leaves outcome unknown)", flush=True)
        print(json.dumps(asyncio.run(attach(leader.agent_id, command)), ensure_ascii=False))
    except (OSError, ValueError, TimeoutError) as exc:
        parser.error(str(exc))


def target_agent(seat: str):
    store = AgentStore(paths.data_root())
    try:
        identity = valid_id(seat)
    except ValueError:
        return store.find_agent(name=seat), store
    return store.find_agent(agent_id=identity), store
