"""Background tool tasks: a tool call the turn does not wait for.

Input -> do work -> output, with the OUTPUT arriving later. A background task
is a tool call whose result is delivered as INPUT NOBODY TYPED — the same class
as inbox mail and a cron job, and it rides the same funnel (`_deliver_inbox`:
held mid-turn, flushed after, wakes the agent unattended). Nothing here starts
a turn, and nothing here runs a tool: the door stays `_execute_tool`, this
module only names, records and formats.

WHY THE RESULT IS A USER-ROLE WAKE AND NOT A LATE ``role: "tool"`` MESSAGE
    On the OpenAI-compatible backends this app talks to (llama.cpp, LM Studio)
    a `tool` message must follow the assistant `tool_calls` message it answers,
    inside the same exchange — the chat templates reject a stray one. A result
    that lands minutes later cannot take that seat, so it takes the seat mail
    already takes: a user turn carrying the pointer, which the model can `read`.

THE RAW NEVER ENTERS THE CONVERSATION WHOLE. It is written to
`<data root>/output/tasks/<id>.log` and the wake carries an excerpt (head +
tail) plus the ABSOLUTE path (T643: a relative one resolved against the reader's
cwd, which is not the data root when LITETUI_DATA_ROOT is set) — the same discipline `tool_context` applies to large results, for the
same reason: a model that cannot tell "the output did not contain X" from
"X was cut" reports absence as fact.

This module is pure — no Textual, no app, no worker threads — so the store and the
wake text are testable without a terminal.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from litetui import paths, router_record, row_store

#: Set by the runner around the tool call; `core_tools._run_shell` reads it to
#: park the child on the TASK instead of the one foreground cancel slot, so a
#: background shell never hijacks the cancel button of a foreground one.
CURRENT: contextvars.ContextVar = contextvars.ContextVar("litetui_task", default=None)
PROCESS_SLOT: contextvars.ContextVar = contextvars.ContextVar("litetui_process_slot", default=None)

# Only the handle handoff is locked, never spawn/kill or other blocking work.
# Stop and a late process attachment must agree which side owns the kill.
_PROCESS_LOCK = threading.Lock()

#: The task store. Since T0132 it lives at `<CONVO_DIR>/<convo id>/STORE`, one
#: per conversation. `<data root>/STORE` is the LEGACY shared file: read for
#: migration, never written by this code (seats on older code still write it).
STORE = "background-tasks.json"
#: Written LAST by `topup`: which state of the legacy file this conversation has
#: already been topped up from.
MIGRATED = ".tasks-migrated.json"
LOG_DIR = ("output", "tasks")

RUNNING = "running"
DONE = "done"
FAILED = "failed"
KILLED = "killed"
#: A row still `running` when the app came back up: the Job Object took the
#: tree down with the app, so the work is gone and must be reported as gone —
#: never left looking in flight.
LOST = "lost"

HEAD_CHARS = 1500
TAIL_CHARS = 2500
#: The store is rewritten on every task transition, so an unbounded prompt is
#: paid for repeatedly. A panel shows a dozen lines; this is generous of that.
PROMPT_CAP = 4000


@dataclass
class Task:
    id: str
    tool: str
    label: str
    convo_id: str
    started: float
    state: str = RUNNING
    ended: float | None = None
    ok: bool | None = None
    log: str = ""  # relative to the root; empty until finished
    #: Completion tokens used (subagent calls); None for non-LLM tasks.
    tokens: int | None = None
    #: What was actually asked, bounded. `label` is the first line cut to 60
    #: chars — enough for a list ROW, and not enough to answer "what did I send
    #: it?", which is the first thing T570's subagent panel has to show. Kept on
    #: the row rather than re-derived because `args` is gone by then: the runner
    #: hands the awaitable to the task and never stores the call.
    prompt: str = ""
    #: The pid of the instance that STARTED this task (T689).
    #:
    #: 🔴 LOST IS A CLAIM ABOUT A PROCESS, AND IT WAS BEING MADE BY THE WRONG
    #: ONE. `load` marked every `running` row LOST at boot, on the true premise
    #: that a task cannot outlive its app — the child sits in that app's
    #: kill-on-close Job Object. True of the app that started it; false of a
    #: SIBLING's task, and with two windows supported (T690) a second instance
    #: booting reported the first one's live work as killed.
    #:
    #: ⚠️ None MEANS "no claim", not "no owner": every row written before this
    #: field existed keeps the old answer and is marked LOST at boot.
    owner_pid: int | None = None
    owner_instance: str | None = None
    owner_created: str | None = None
    #: The live child, for `/tasks kill`. Not persisted, not compared.
    proc: object = field(default=None, repr=False, compare=False)

    def to_row(self) -> dict:
        # Never `asdict` here: it deep-copies every field first, and the live
        # child (a Popen holding a _thread.lock) cannot be copied — that was the
        # crash in logs/crash-9-8-2026.txt. Every other field is a scalar.
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "proc"}

    @property
    def seconds(self) -> float:
        end = self.ended if self.ended is not None else time.time()
        return max(0.0, end - self.started)


@dataclass
class ProcessSlot:
    """One call's handle and mutable owner, shared across promotion and spawn."""
    proc: object | None = None
    task: Task | None = None


async def run_in_process_slot(aw, slot: ProcessSlot):
    token = PROCESS_SLOT.set(slot)
    try:
        return await aw
    finally:
        PROCESS_SLOT.reset(token)


def promote_process(slot: ProcessSlot, task: Task, foreground: dict) -> None:
    with _PROCESS_LOCK:
        slot.task = task
        task.proc = slot.proc
        if foreground.get("proc") is slot.proc:
            foreground["proc"] = None


def publish_process(slot: ProcessSlot, proc: object, foreground: dict) -> tuple[Task | None, bool]:
    with _PROCESS_LOCK:
        slot.proc = proc
        if slot.task is not None:
            slot.task.proc = proc
            return slot.task, slot.task.state == KILLED
        foreground.update(proc=proc, cancelled=False, kill_confirmed=True)
        return None, False


def finish_process(slot: ProcessSlot | None, task: Task | None, proc: object,
                   foreground: dict, *, consume_cancelled: bool = True) -> tuple[bool, bool]:
    """Clear and take cancellation metadata only while this call still owns it."""
    with _PROCESS_LOCK:
        if task is not None or (slot is not None and slot.task is not None):
            return False, True
        current = foreground.get("proc")
        if current is not None and current is not proc:
            return False, True
        if current is proc:
            foreground["proc"] = None
        cancelled = bool(foreground.get("cancelled", False)) if consume_cancelled else False
        confirmed = bool(foreground.get("kill_confirmed", True))
        if consume_cancelled:
            foreground["cancelled"] = False
        return cancelled, confirmed


def label_of(tool: str, args: dict) -> str:
    """What the task is, in one short line: the command's first line, trimmed."""
    raw = (args or {}).get("command") or (args or {}).get("prompt") or ""
    cmd = str(raw).strip().splitlines()
    head = cmd[0] if cmd else ""
    return (head[:60] + "…") if len(head) > 60 else head


def prompt_of(args: dict) -> str:
    """The whole request, capped — same two keys as `label_of`, in the same
    order, so the row and the panel can never disagree about which field of a
    tool call is "what was asked"."""
    raw = (args or {}).get("command") or (args or {}).get("prompt") or ""
    text = str(raw).strip()
    return (text[:PROMPT_CAP] + "…") if len(text) > PROMPT_CAP else text


_INSTANCE_ID = uuid.uuid4().hex

def new_task(tool: str, args: dict, convo_id: str) -> Task:
    from litetui.task_supervisor import process_creation_identity
    return Task(
        id="t-" + uuid.uuid4().hex,
        tool=tool,
        label=label_of(tool, args),
        convo_id=convo_id or "",
        started=time.time(),
        prompt=prompt_of(args),
        owner_pid=os.getpid(),
        owner_instance=_INSTANCE_ID,
        owner_created=process_creation_identity(os.getpid()),
    )


def pending_cancellable(task: Task) -> bool:
    """Only shell providers participate in the late-process handoff below."""
    return (task.state == RUNNING and getattr(task, "owner_pid", None) == os.getpid()
            and task.tool in {"bash", "powershell"}
            and getattr(task, "_pending_process_handoff", False))


def request_kill(task: Task) -> tuple[bool, object | None]:
    """Claim a local running task, including the interval before spawn."""
    with _PROCESS_LOCK:
        owner = getattr(task, "owner_pid", None)
        if task.owner_instance is not None and task.owner_instance != _INSTANCE_ID:
            return False, None
        if task.owner_created is not None:
            from litetui.task_supervisor import process_creation_identity
            if process_creation_identity(os.getpid()) != task.owner_created:
                return False, None
        if task.state != RUNNING or owner not in (None, os.getpid()):
            return False, None
        if task.proc is None and not pending_cancellable(task):
            return False, None  # other providers cannot promise a late-child kill
        task.state = KILLED
        return True, task.proc


def attach_process(task: Task, proc: object) -> bool:
    """Publish the handle; True transfers a pending Stop to this caller."""
    with _PROCESS_LOCK:
        task.proc = proc
        return task.state == KILLED


def log_path(task: Task, root: Path | str) -> Path:
    return Path(root).joinpath(*LOG_DIR) / f"{task.id}.log"


@functools.lru_cache(maxsize=None)
def backgroundable(tool: str) -> bool:
    """May this tool run in the background at all?

    the user (2026-09-08 13:3x): "not everything should be backgroundable ... only
    what makes sense" — the rule is the SCHEMA: a tool qualifies only if its own
    JSON declares a `background` property (bash, powershell). read/edit/grep are
    instant, ask_user_question waits on the human by design, chrome and pccontrol
    are single steps, studio image generation SUSPENDS the agent's own model while
    it runs (a backgrounded generate would leave the next turn without a model),
    studio sound is already a job. The same gate covers the explicit flag and the
    auto-promotion, so nothing can be backgrounded that was never declared.
    """
    try:
        from litetui import tool_schemas
        spec = tool_schemas.load(tool)
    except Exception:
        return False
    props = ((spec.get("function") or {}).get("parameters") or {}).get("properties") or {}
    return "background" in props


async def wait_or_promote(aw, seconds: float):
    """Await `aw` for up to `seconds`.

    (True, future) when it finished in time; (False, future) when it is still
    running — the caller hands the future to a background task (T517: the user,
    "the calls are still blocked is it off by default or something ?"). The
    future is the SAME awaitable either way: nothing is started twice.
    """
    fut = asyncio.ensure_future(aw)
    if seconds <= 0:
        await asyncio.wait({fut})
        return True, fut
    done, _ = await asyncio.wait({fut}, timeout=seconds)
    return bool(done), fut


def start_text(task: Task, root: Path | str, promoted_after: float | None = None) -> str:
    """What the MODEL gets back immediately — the tool result of a background call.

    🔴 THE PATH IS ABSOLUTE, BECAUSE THE READER RESOLVES AGAINST ITS OWN CWD.
    This used to advertise `output/tasks/<id>.log`, relative to `root` (T643).
    Under LITETUI_DATA_ROOT the log is written beneath the DATA ROOT while the
    read tool resolves a relative path against the process's working directory —
    the user's checkout — so the model was handed a path to nothing and reported
    "file not found" for a file that had been written correctly elsewhere. With
    the two roots equal it resolved by coincidence, which is why it survived.
    """
    where = log_path(task, root)
    head = (
        f"[task {task.id} started · {task.tool} · {task.label}]\n" if not promoted_after else
        f"[task {task.id} · {task.tool} · {task.label} — still running after {promoted_after:g}s, "
        f"moved to the background; its own timeout still applies]\n"
    )
    return (
        head +
        f"Running in the background. Its output arrives later as a message tagged "
        f"[inbox from task]; the full output will be at {where.as_posix()} "
        f"(read it with the read tool). Continue with other work, or end the turn "
        f"and wait."
    )


def excerpt(text: str) -> str:
    """Head + tail of a long result, with the cut named. Short text passes whole."""
    text = text or ""
    if len(text) <= HEAD_CHARS + TAIL_CHARS + 40:
        return text
    cut = len(text) - HEAD_CHARS - TAIL_CHARS
    return f"{text[:HEAD_CHARS]}\n[... {cut} chars cut — the log has all of it ...]\n{text[-TAIL_CHARS:]}"


def finish(task: Task, result: str, ok: bool, root: Path | str) -> str:
    """Record the end, write the raw to its log, return the wake text.

    A task already marked KILLED keeps that state: the kill happened, and the
    subprocess returning afterwards is the kill working, not a completion.
    """
    task.ended = time.time()
    task.ok = ok
    if task.state != KILLED:
        task.state = DONE if ok else FAILED
    p = log_path(task, root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(result or "", encoding="utf-8", errors="replace")
        # T643: the absolute path, for the same reason `start_text` gives one —
        # this string is handed to a model that will pass it to the read tool.
        task.log = p.as_posix()
    except OSError:
        task.log = ""
    where = f"full output: {task.log}" if task.log else "the log could not be written"
    body = excerpt(result)
    return (
        f"[task {task.id} {task.state} · {task.tool} · {task.label} · "
        f"{task.seconds:.0f} s]\n{where}\n{body}".rstrip()
    )


def tail_text(task: Task | None, root: Path | str, lines: int = 40) -> str:
    if task is None:
        return "no such task"
    if task.state == RUNNING:
        return f"{task.id} is still running ({task.seconds:.0f} s) — output arrives when it ends"
    p = log_path(task, root)
    try:
        rows = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"{task.id}: no log on disk"
    return "\n".join(rows[-lines:]) or "(empty)"


#: The tool whose Task rows ARE subagents. Everything else backgroundable is a
#: "background process" — the two footer chips and the two modals split on this
#: one name, so it lives here rather than being spelled in four places.
SUBAGENT_TOOL = "subagent"


def live(tasks) -> list:
    """Running tasks, newest first. Never the finished or LOST ones."""
    return sorted(
        (t for t in tasks if t.state == RUNNING),
        key=lambda t: t.started,
        reverse=True,
    )


def host_tasks_for_app(app) -> list[Task]:
    """Host tasks belonging to this conversation, including finished history."""
    convo_id = getattr(app, "convo_id", None)
    return [row for row in getattr(app, "bg_tasks", {}).values()
            if row.convo_id == convo_id]


def live_for_app(app):
    """Shared visible rows; provider rows never enter the host process store."""
    convo_id = getattr(app, "convo_id", None)
    host = host_tasks_for_app(app)
    native = [row for row in getattr(app, "codex_native_activity", {}).values()
              if row.convo_id == convo_id
              and getattr(getattr(app, "backend", None), "name", None) == "codex"]
    return split_live([*host, *native])


def split_live(tasks) -> tuple[list, list]:
    """(subagents, background processes), both running, both newest first.

    🔴 ONE PREDICATE, TWO CHIPS. A task is a subagent or it is a background
    process; deriving that twice is how a row eventually shows up in both
    counts or in neither, and neither mistake is visible in a number.
    """
    rows = live(tasks)
    subs = [t for t in rows if t.tool == SUBAGENT_TOOL]
    bg = [t for t in rows if t.tool != SUBAGENT_TOOL]
    return subs, bg


def render_list(tasks) -> str:
    rows = sorted(tasks, key=lambda t: t.started, reverse=True)
    if not rows:
        return "no background tasks"
    out = []
    for t in rows[:20]:
        tok = f"  {t.tokens} tok" if t.tokens is not None else ""
        out.append(f"{t.id}  {t.state:<7} {t.seconds:6.0f} s{tok}  {t.tool}  {t.label}")
    return "\n".join(out)


# ── store ───────────────────────────────────────────────────────────────


def save(tasks, root: Path | str) -> None:
    """Persist through `row_store`: re-read, apply OUR delta, replace atomically.

    🔴 A WHOLE-FILE WRITE IS A LOST UPDATE AS SOON AS THERE ARE TWO WINDOWS.
    Each instance loaded this store once at boot and rewrote it in full on
    every transition, so the second to save erased the first's rows: A adds a
    task and saves, B — whose memory predates that row and therefore cannot
    contain it — saves, and the file holds only B's.

    ⬜ WHY THE HELPER TAKES A BASELINE INSTEAD OF JUST MERGING BY ID: a merge
    would also let B's stale copy of a row A had just advanced overwrite A's
    newer one. Nothing here ever DELETES a row — measured across the whole
    package including `plugins/` and `rpc.py`, rows are added and mutated in
    place and there is no prune, cap, `/tasks clear` or rpc delete — so this
    file is the degenerate case whose delta never contains a removal.
    `jobs.json`, which has three deletion paths, needs the same helper for the
    half this file does not exercise.
    """
    row_store.write(Path(root) / STORE, [t.to_row() for t in tasks], prefix=".tasks-")


class StoreNotBorn(Exception):
    """A row belongs to a conversation with no directory to keep it in.

    Not an OSError, so a caller can tell "this row has no home" from "the disk
    failed". `save_by_convo` RAISES it; `LiteTUI._save_background` REPORTS it
    (runtime log plus one system line, once per distinct failure, never silent)
    rather than raising, because that method also runs from the completion
    worker and a raise there would skip the wake. It is the tripwire behind the
    materialise in `_execute_tool` / `_start_background`: `row_store.write` would
    mkdir a missing conversation directory, and a half-born `.convos/<id>/`
    litters /resume. Refusing is loud; skipping would lose a row.
    """


def convo_store_dir(convo_id: str) -> Path:
    """The directory holding one conversation's store. It must already exist."""
    if not convo_id or convo_id in (".", "..") or Path(convo_id).name != convo_id:
        raise StoreNotBorn(f"no task store for conversation id {convo_id!r}")
    d = Path(paths.CONVO_DIR) / convo_id
    if not d.is_dir():
        raise StoreNotBorn(f"conversation {convo_id} has no directory yet; its task rows have nowhere to go")
    return d


def save_by_convo(tasks) -> None:
    """Persist each row into the store of the conversation that STARTED it.

    A row's `convo_id` never changes, and a task can finish long after the user
    resumed another conversation, so the destination follows the row and not
    whatever is current. One conversation failing does not stop the others being
    saved; the first failure is raised afterwards, `StoreNotBorn` before OSError.
    """
    groups: dict[str, list[Task]] = {}
    for t in tasks:
        groups.setdefault(t.convo_id, []).append(t)
    failures: list[Exception] = []
    for convo_id, rows in groups.items():
        try:
            save(rows, convo_store_dir(convo_id))
        except (StoreNotBorn, OSError) as e:
            failures.append(e)
    if failures:
        raise next((f for f in failures if isinstance(f, StoreNotBorn)), failures[0])


def _write_marker(marker: Path, sig: dict, copied: int) -> None:
    payload = json.dumps({"source": STORE, **sig, "copied": copied, "ts": time.time()})
    fd, tmp = tempfile.mkstemp(dir=str(marker.parent), prefix=".tasks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, marker)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _marker_sig(marker: Path) -> dict | None:
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {"size": raw.get("size"), "mtime_ns": raw.get("mtime_ns")}


def topup(convo_dir: Path | str, legacy_root: Path | str) -> int:
    """Copy this conversation's rows out of the legacy shared file. Returns how many.

    🔴 COPY-ONLY, AND ON EVERY BIND. The legacy file is opened for READING and
    nothing else: no write, no rename, no delete, and not its `.lock` - seats on
    older code keep writing it until they are relaunched, and one that resumes
    this conversation AFTER it was migrated appends its rows there. A one-shot
    copy would strand them, so the marker records the file's (size, mtime_ns)
    and a changed file is simply copied from again.

    The copy is id-keyed under one lock (`row_store.add_missing`): idempotent,
    concurrent-writer safe, and a row already in the conversation's store wins.
    The marker is written LAST, so a crash re-runs the copy rather than skipping
    it. A directory that does not exist is left alone (a top-up never creates
    one); a legacy file that cannot be read leaves no marker and is retried.
    """
    convo_dir = Path(convo_dir)
    legacy = Path(legacy_root) / STORE
    if not convo_dir.is_dir():
        return 0
    try:
        st = legacy.stat()
        raw = json.loads(legacy.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    sig = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    marker = convo_dir / MIGRATED
    if _marker_sig(marker) == sig:
        return 0
    mine = ([r for r in raw if isinstance(r, dict) and r.get("convo_id") == convo_dir.name]
            if isinstance(raw, list) else [])
    copied = row_store.add_missing(convo_dir / STORE, mine, prefix=".tasks-") if mine else 0
    _write_marker(marker, sig, copied)
    return copied


def bind(held: dict[str, Task], convo_dir: Path | str, legacy_root: Path | str) -> None:
    """Bring one conversation's rows into `held`: top up, load, merge.

    Rows of other conversations already in `held` are untouched (a task started
    before a /resume is still ours to kill and to finish). A row THIS instance
    started keeps its in-memory object: it carries the live child handle, which
    no disk copy can, and it is at least as new as the disk. Everything else the
    store says replaces what `held` had.

    ⚠️ BIND ONLY ADDS. Nothing is evicted when the user resumes another
    conversation, so `held` (the app's `bg_tasks`) accumulates every row of every
    conversation this process has bound or started, finished history included.
    Consumers that take `bg_tasks.values()` unfiltered (the GUI state snapshot and
    `tasks.list`) therefore show that whole set - NOT rows of conversations never
    resumed here, which the pre-T0132 boot-time load did show. `/tasks` and the
    rpc `tasks.list` filter to the current conversation (`host_tasks_for_app`).
    """
    topup(convo_dir, legacy_root)
    for task_id, task in load(convo_dir).items():
        ours = held.get(task_id)
        if ours is not None and ours.owner_instance == _INSTANCE_ID:
            continue
        held[task_id] = task


def load(root: Path | str) -> dict[str, Task]:
    """One store directory's rows, a running one marked LOST only if its owner is gone.

    Called through `bind`, for the conversation being bound. The premise is
    unchanged and still true: a task cannot survive the app that started it,
    because the shell runner puts the child in a kill-on-close Job Object. What
    changed is WHOSE bind may say so - see `Task.owner_pid` and `_owner_alive`.
    A row with no pid predates the field and keeps the old answer.
    """
    p = Path(root) / STORE
    out: dict[str, Task] = {}
    seen: list[dict] = []
    for raw in row_store.rows_on_disk(p):
        if "id" not in raw:
            continue
        r = {k: v for k, v in raw.items() if k in Task.__dataclass_fields__}
        try:
            t = Task(**r)
        except TypeError:
            continue
        seen.append(raw)
        if t.state == RUNNING and not _owner_alive(t):
            t.state = LOST
            t.ended = t.ended or time.time()
        out[t.id] = t
    # ⚠️ THE BASELINE IS THE DISK ROWS WE KEPT, NOT WHAT WE NOW HOLD, and the
    # difference decides two things. Taken after the LOST stamping, that
    # stamping would not be in our delta and would never reach disk. Taken as
    # everything on disk, a row we could not parse would look like one we
    # DELETED and the next save would erase it. This is the set we both read
    # and hold, before we changed our mind about any of it.
    row_store.rebaseline(p, seen)
    return out


def _owner_alive(task: Task) -> bool:
    """Is the instance that started this task still running?

    🔴 THIS INSTANCE'S OWN ROWS ARE ALIVE; OUR PID ON SOMEONE ELSE'S ROW IS NOT.
    Bind reloads a conversation's store mid-session, so a `running` row we
    started ourselves is now something `load` can meet, and `_INSTANCE_ID` (minted
    per process, stamped on every row we create) is what says it is ours.

    A row carrying our PID but not our instance id, or no instance id at all, is
    a DEAD predecessor that held this number: Windows hands pids out again.
    Without that rule `pid_is_live` answers True about us, the row stays
    `running` for the life of the instance, and again on every future boot that
    draws the same pid. (Sentinel, message 94cedaec.)

    `pid_is_live` treats an unopenable pid as ALIVE (access denied is not death),
    which is the right direction here too: a wrong "alive" costs a row that stays
    `running` until the next boot, and a wrong "dead" tells the human their task
    was killed while it is still producing output in the other window.
    """
    if task.owner_instance == _INSTANCE_ID:
        return True
    if task.owner_pid is None or task.owner_pid == os.getpid():
        return False
    if task.owner_created is not None:
        from litetui.task_supervisor import process_creation_identity
        current = process_creation_identity(task.owner_pid)
        if current is not None:
            return current == task.owner_created
    return router_record.pid_is_live(task.owner_pid)
