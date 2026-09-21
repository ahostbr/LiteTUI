"""ActivitySnapshot producer — read the reviewed ownership sources and build a
fail-closed ActivitySnapshot plus diagnostics, without touching the app, disk,
or starting anything.

Sources and their exact symbols are documented in
ws7-activity-flags-followup-fluxwedge.md. Fail-closed rules (OpenBolt review):

- REQUIRED capability missing / None / raising sets its dimension(s) busy and is
  named in `unreadable`. Required: app.workers, app.store, app.screen_stack
  (early boot with no store is NOT reload-ready).
- OPTIONAL lazy-lifecycle object that is simply ABSENT contributes nothing; but
  if PRESENT and unreadable/malformed, its dimension is busy and it is named in
  `unreadable`. Optional-lazy: app._agent_operations, app._monitor_threads.
- The injected `children_pending` probe must return an EXACT bool; None supplied,
  a non-bool return, or a raise => children busy (unknown => busy).
- Every NONTERMINAL worker counts. An unknown group is conservatively
  management-busy so a future worker group cannot silently bypass the gate.
- ttyguard.CANCELLABLE is PROCESS-GLOBAL: it sets tool busy (fail-closed) but is
  tagged scope="process", so `app_active` separates "your App is active" from a
  process-wide shell that may belong to a sibling instance.

Read-only, no disk, no swap, no cancellation, no loads.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from textual.worker import WorkerState

from litetui.plugin_reload_state import ActivitySnapshot

_TERMINAL = frozenset({WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED})


#: The attribute on `app` holding the Worker OBJECTS currently in an idle
#: infrastructure phase (registered by idle_infra_phase). produce_activity
#: excludes exactly these workers, by object identity (`is`), while they are
#: idle-polling — a proven, producer-owned identity, never a name/group
#: heuristic and never an integer id (which could be reused after GC).
_IDLE_INFRA_ATTR = "_idle_infra_workers"


def _contains(seq, worker) -> bool:
    """Identity membership: never eq/hash (a Worker could overload them)."""
    try:
        return any(worker is x for x in seq)
    except TypeError:
        return False


@contextmanager
def idle_infra_phase(app):
    """Mark the CURRENT worker as idle infrastructure for the duration of a
    poll / sleep / heartbeat await, so produce_activity does not read that
    worker as management activity.

    Registered by EXACT running Worker OBJECT (get_current_worker), cleared in
    `finally` — including on cancellation — BEFORE the worker leaves the idle
    section to deliver or dispatch, so a firing poller is visible again the
    moment it does real work. The registry is a list used as a refcount: a
    nested phase for the SAME worker pushes again and pops one on each exit, so
    an inner exit never clears an outer phase. If the current worker cannot be
    resolved, nothing is registered (fail-closed — it stays counted)."""
    from textual.worker import get_current_worker
    try:
        worker = get_current_worker()
    except Exception:
        worker = None
    if worker is None:
        yield
        return
    reg = getattr(app, _IDLE_INFRA_ATTR, None)
    if not isinstance(reg, list):
        reg = []
        try:
            setattr(app, _IDLE_INFRA_ATTR, reg)
        except Exception:
            yield
            return
    reg.append(worker)                      # refcount: one push per (possibly nested) enter
    try:
        yield
    finally:
        for i in range(len(reg) - 1, -1, -1):   # pop ONE occurrence, by identity
            if reg[i] is worker:
                del reg[i]
                break


def _idle_infra_workers(app) -> list:
    """The registered idle-infra Worker objects, fail-closed: a missing or
    non-list attribute means NOTHING is excluded (every worker stays counted)."""
    reg = getattr(app, _IDLE_INFRA_ATTR, None)
    return reg if isinstance(reg, list) else []

#: Worker group -> the ActivitySnapshot field it primarily drives. Everything not
#: listed here (inbox, native-history, init, AND any future/unknown group) is
#: conservatively treated as management_active.
_GROUP_PRIMARY: dict[str, str] = {
    "chat": "turn_active",
    "tasks": "tool_active",
    "child-wake": "children_active",
    "mcp": "mcp_active",
}
_KNOWN_MANAGEMENT = frozenset({"inbox", "native-history", "init"})
_FIELDS = ("turn_active", "tool_active", "management_active", "children_active", "mcp_active")
_WORKER_FIELDS = _FIELDS  # app.workers being unreadable clouds every worker-derived dimension


@dataclass(frozen=True)
class ActivityReason:
    field: str      # which ActivitySnapshot field this contributes to
    source: str     # the symbol/signal, e.g. "worker:chat", "app.store", "ttyguard.CANCELLABLE"
    scope: str      # "app" (this App is genuinely busy) | "process" (may be a sibling instance)
    detail: str


@dataclass(frozen=True)
class ActivityReport:
    snapshot: ActivitySnapshot
    reasons: tuple[ActivityReason, ...]
    unreadable: tuple[str, ...]   # required capabilities that could not be read (=> forced busy)

    @property
    def app_active(self) -> bool:
        """Diagnostics only, NOT a permission. True when at least one reason is
        app-scoped — i.e. THIS App is genuinely active, as opposed to being
        blocked solely by a process-global signal (a sibling instance's shell)."""
        return any(r.scope == "app" for r in self.reasons)


def produce_activity(
    app: Any,
    *,
    children_pending: Callable[[], bool | None] | None = None,
    ignore_workers: Any = (),
    ignore_screen: Any = None,
) -> ActivityReport:
    """Build a fail-closed ActivitySnapshot from the reviewed ownership sources.

    The snapshot is the permission gate; `reasons` and `unreadable` are diagnostics.
    Nothing here mutates the app, touches disk, cancels work, or loads anything.

    ignore_workers / ignore_screen exclude ONLY the caller's OWN dialog
    identity (its host Worker OBJECT, and its own modal screen, both by `is`)
    so an owned dialog does not see itself as busy. Registered idle-infra
    pollers (idle_infra_phase) are excluded the same way. Every OTHER signal is
    kept: a DIFFERENT worker in the same group, a SECOND modal, a busy store, a
    durable child, an active tool all still count. Defaults are empty — the
    command producer is unchanged and stays fail-closed.
    """
    idle_infra = _idle_infra_workers(app)
    fields: dict[str, bool] = {k: False for k in _FIELDS}
    reasons: list[ActivityReason] = []
    unreadable: list[str] = []

    def mark(field: str, source: str, scope: str, detail: str) -> None:
        fields[field] = True
        reasons.append(ActivityReason(field, source, scope, detail))

    # 1. Textual workers (REQUIRED). Any nonterminal worker counts; unknown group
    #    => management, so a future group cannot bypass the gate.
    try:
        workers = list(app.workers)
    except Exception as e:  # noqa: BLE001 — required capability; fail closed
        for f in _WORKER_FIELDS:
            mark(f, "app.workers", "app", f"unreadable: {type(e).__name__}: {e}")
        unreadable.append("app.workers")
    else:
        for w in workers:
            try:
                state = w.state
                group = w.group
            except Exception as e:  # noqa: BLE001
                mark("management_active", "app.workers", "app", f"worker unreadable: {type(e).__name__}")
                unreadable.append("app.workers")
                continue
            if state in _TERMINAL:
                continue
            if _contains(ignore_workers, w) or _contains(idle_infra, w):
                continue   # the caller's OWN dialog host worker, or a registered
                           # idle-infra poller — not real activity
            field = _GROUP_PRIMARY.get(group, "management_active")
            if group in _GROUP_PRIMARY:
                note = f"nonterminal worker group {group!r}"
            elif group in _KNOWN_MANAGEMENT:
                note = f"nonterminal management worker group {group!r}"
            else:
                note = f"unknown nonterminal worker group {group!r} (conservative management)"
            mark(field, f"worker:{group}", "app", note)
            if group == "mcp":
                mark("management_active", "worker:mcp", "app", "mcp worker also counts as management")

    # 2. store (REQUIRED). Absent / None / unreadable => busy: early boot is not
    #    reload-ready. Management is the clearer dimension; turn is kept too.
    try:
        store = app.store
    except Exception as e:  # noqa: BLE001
        store = None
        _store_unreadable(mark, unreadable, f"{type(e).__name__}: {e}")
    else:
        if store is None:
            _store_unreadable(mark, unreadable, "app.store is None (early boot, not reload-ready)")
        else:
            try:
                busy = bool(store.pending) or bool(store.loading)
            except Exception as e:  # noqa: BLE001
                _store_unreadable(mark, unreadable, f"{type(e).__name__}: {e}")
            else:
                if busy:
                    mark("management_active", "app.store", "app", "conversation store pending/loading")
                    mark("turn_active", "app.store", "app", "conversation store pending/loading")

    # 3. screen_stack (REQUIRED). Absent / unreadable => management busy.
    try:
        stack = app.screen_stack
        depth = len(stack)
    except Exception as e:  # noqa: BLE001
        mark("management_active", "app.screen_stack", "app", f"unreadable: {type(e).__name__}")
        unreadable.append("app.screen_stack")
    else:
        # Exclude ONLY the caller's own dialog modal — a real modal ABOVE the
        # base (stack[0]), by `is`. NEVER the base screen: subtracting stack[0]
        # would let a FOREIGN modal at depth 2 read as depth 1 and bypass. A
        # SECOND modal still counts. (The caller must ALSO prove ignore_screen
        # is its own modal hosting its body — see MCPListBody._own_modal_screen;
        # this non-base guard is the producer-side floor.)
        if ignore_screen is not None and depth > 1 and _contains(stack[1:], ignore_screen):
            depth -= 1
        if depth > 1:
            mark("management_active", "app.screen_stack", "app", f"modal open (screen_stack depth {depth})")

    # 4. tasks.live_for_app — background processes + subagents. It reads via
    #    getattr defaults, so absence is safe (returns empty); an unexpected raise
    #    => tool+children busy (unreadable).
    try:
        from litetui import tasks as tasks_mod
        subs, bg = tasks_mod.live_for_app(app)
    except Exception as e:  # noqa: BLE001
        mark("tool_active", "tasks.live_for_app", "app", f"unreadable: {type(e).__name__}")
        mark("children_active", "tasks.live_for_app", "app", f"unreadable: {type(e).__name__}")
        unreadable.append("tasks.live_for_app")
    else:
        if bg:
            mark("tool_active", "tasks.live_for_app:bg", "app", f"{len(bg)} background process(es) running")
        if subs:
            mark("children_active", "tasks.live_for_app:subs", "app", f"{len(subs)} subagent(s) running")

    # 5. _agent_operations (OPTIONAL lazy). Absent => skip. Present but
    #    unreadable/malformed => children busy.
    ops = getattr(app, "_agent_operations", None)
    if ops is not None:
        try:
            live = [t for t in ops.tasks.values() if not t.done()]
        except Exception as e:  # noqa: BLE001
            mark("children_active", "app._agent_operations", "app", f"unreadable/malformed: {type(e).__name__}")
            unreadable.append("app._agent_operations")
        else:
            if live:
                mark("children_active", "app._agent_operations", "app", f"{len(live)} full-child op(s) in flight")

    # 6. monitor threads (OPTIONAL until first sweep). Absent => skip. Present but
    #    unreadable => tool busy. bool() the set; never iterate it (it is mutated
    #    by monitor threads — b7b71c3).
    if hasattr(app, "_monitor_threads"):
        try:
            busy = bool(app._monitor_threads)
        except Exception as e:  # noqa: BLE001
            mark("tool_active", "app._monitor_threads", "app", f"unreadable: {type(e).__name__}")
            unreadable.append("app._monitor_threads")
        else:
            if busy:
                mark("tool_active", "app._monitor_threads", "app", "monitor sweep thread(s) live")

    # 7. ttyguard.CANCELLABLE — PROCESS-GLOBAL shell slot. Fail-closed tool busy,
    #    scope="process" (may belong to a sibling instance in one process).
    try:
        from litetui import ttyguard
        proc = ttyguard.CANCELLABLE.get("proc")
    except Exception as e:  # noqa: BLE001
        mark("tool_active", "ttyguard.CANCELLABLE", "process", f"unreadable: {type(e).__name__}")
        unreadable.append("ttyguard.CANCELLABLE")
    else:
        if proc is not None:
            mark("tool_active", "ttyguard.CANCELLABLE", "process",
                 "a shell tool is in flight (process-global; may be a sibling instance)")

    # 8. children_pending probe (INJECTED). Exact bool required; None supplied,
    #    non-bool return, or raise => children busy (unknown => busy).
    if children_pending is None:
        mark("children_active", "children_pending", "app", "no children probe supplied (unknown => busy)")
    else:
        try:
            result = children_pending()
        except Exception as e:  # noqa: BLE001
            mark("children_active", "children_pending", "app", f"probe raised {type(e).__name__} (unknown => busy)")
        else:
            if result is True:
                mark("children_active", "children_pending", "app", "receipts/registry report pending children")
            elif result is not False:
                mark("children_active", "children_pending", "app",
                     f"probe returned non-bool {type(result).__name__} (unknown => busy)")

    snapshot = ActivitySnapshot(**fields)
    # dedupe unreadable, preserve order
    return ActivityReport(snapshot=snapshot, reasons=tuple(reasons),
                          unreadable=tuple(dict.fromkeys(unreadable)))


def _store_unreadable(mark, unreadable: list[str], detail: str) -> None:
    mark("management_active", "app.store", "app", f"store unreadable: {detail}")
    mark("turn_active", "app.store", "app", f"store unreadable: {detail}")
    unreadable.append("app.store")
