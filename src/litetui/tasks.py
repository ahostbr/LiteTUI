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
`output/tasks/<id>.log` and the wake carries an excerpt (head + tail) plus the
path — the same discipline `tool_context` applies to large results, for the
same reason: a model that cannot tell "the output did not contain X" from
"X was cut" reports absence as fact.

This module is pure — no Textual, no app, no threads — so the store and the
wake text are testable without a terminal.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

#: Set by the runner around the tool call; `core_tools._run_shell` reads it to
#: park the child on the TASK instead of the one foreground cancel slot, so a
#: background shell never hijacks the cancel button of a foreground one.
CURRENT: contextvars.ContextVar = contextvars.ContextVar("litetui_task", default=None)

STORE = "background-tasks.json"
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


def label_of(tool: str, args: dict) -> str:
    """What the task is, in one short line: the command's first line, trimmed."""
    raw = (args or {}).get("command") or (args or {}).get("prompt") or ""
    cmd = str(raw).strip().splitlines()
    head = cmd[0] if cmd else ""
    return (head[:60] + "…") if len(head) > 60 else head


def new_task(tool: str, args: dict, convo_id: str) -> Task:
    return Task(
        id="t-" + uuid.uuid4().hex[:6],
        tool=tool,
        label=label_of(tool, args),
        convo_id=convo_id or "",
        started=time.time(),
    )


def log_path(task: Task, root: Path | str) -> Path:
    return Path(root).joinpath(*LOG_DIR) / f"{task.id}.log"


@functools.lru_cache(maxsize=None)
def backgroundable(tool: str) -> bool:
    """May this tool run in the background at all?

    Ryan (2026-09-08 13:3x): "not everything should be backgroundable ... only
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
    running — the caller hands the future to a background task (T517: Ryan,
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
    """What the MODEL gets back immediately — the tool result of a background call."""
    rel = Path(*LOG_DIR) / f"{task.id}.log"
    head = (
        f"[task {task.id} started · {task.tool} · {task.label}]\n" if not promoted_after else
        f"[task {task.id} · {task.tool} · {task.label} — still running after {promoted_after:g}s, "
        f"moved to the background; its own timeout still applies]\n"
    )
    return (
        head +
        f"Running in the background. Its output arrives later as a message tagged "
        f"[inbox from task]; the full output will be at {rel.as_posix()} "
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
        task.log = p.relative_to(Path(root)).as_posix()
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
    """Atomic write, same shape as the scheduler's store: temp file + replace."""
    p = Path(root) / STORE
    rows = [t.to_row() for t in tasks]
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tasks-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(root: Path | str) -> dict[str, Task]:
    """The store, with every row that was still running marked LOST.

    Called at boot. A task cannot survive the app: the shell runner puts the
    child in a kill-on-close Job Object, so when the app went, the tree went.
    """
    p = Path(root) / STORE
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Task] = {}
    for r in rows:
        if not isinstance(r, dict) or "id" not in r:
            continue
        r = {k: v for k, v in r.items() if k in Task.__dataclass_fields__}
        try:
            t = Task(**r)
        except TypeError:
            continue
        if t.state == RUNNING:
            t.state = LOST
            t.ended = t.ended or time.time()
        out[t.id] = t
    return out
