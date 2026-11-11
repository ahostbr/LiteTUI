"""Owned optional Sidecar process and deferred desktop proposals.

Pipe callbacks only enqueue bounded immutable messages. The source application
executes each proposal through an existing reviewed native transaction, and the
child receives a final reply only after that transaction's readback is emitted.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import queue
import secrets
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from litetui import (
    backend_catalog,
    backend_transition,
    native_context,
    native_settings,
    settings_runtime,
)
from litetui import native_sidecar_routes as routes
from litetui.shutdown_wait import join_task
from litetui.sidecar_launch import VIEWS, SidecarWindow

PREFIX = "gui.sidecar.native_"
MAX_WIRE = 1024 * 1024
MAX_PROPOSALS = 16
MAX_PAYLOAD = 16 * 1024
DELEGATE: ContextVar[tuple[Manager, object] | None] = ContextVar("native_sidecar_delegate", default=None)


def encoded(value):
    return backend_catalog._encoded(value, MAX_WIRE)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def executable():
    from litetui import paths
    return Path(os.environ.get("LITETUI_SIDECAR_EXE") or
                paths.data_root() / "bin" / ("litetui-sidecar.exe" if sys.platform == "win32" else "litetui-sidecar"))


def executable_facts(path):
    """Pin the exact optional file without launching it or following a changed path."""
    from litetui.agent_store import _unlinked
    path = _unlinked(Path(path).absolute())
    result = {"path": str(path), "available": False, "sha256": None, "size": None}
    try:
        before = path.stat()
    except FileNotFoundError:
        return result
    if not path.is_file() or before.st_size > 256 * 1024 * 1024:
        raise ValueError("Sidecar executable must be a regular file at most 256 MiB.")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        finished = os.fstat(stream.fileno())
    after = path.stat()
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if len({stamp(s) for s in (before, opened, finished, after)}) != 1:
        raise ValueError("Sidecar executable changed while reading.")
    return {**result, "available": True, "sha256": h.hexdigest(), "size": before.st_size}


def public_settings(app):
    from litetui.plugins.sidecar_plugin import launch_overrides
    from litetui.sidecar_settings import public_snapshot
    service = settings_runtime.service_for(app)
    before = service.snapshot(app.convo_id, read_only=True, max_bytes=MAX_WIRE)
    _paths, _documents, revisions = native_settings.owned_documents(service, app.convo_id, app._agent_session)
    if revisions != before.revisions:
        raise ValueError("Settings changed while preparing the Sidecar.")
    snap = public_snapshot(before, backend=app.backend, model_id=app.model_id,
                           models=list(getattr(app, "available_models", ())), launch=launch_overrides(app))
    snap["conversation_id"] = app.convo_id
    for key, row in snap["fields"].items():
        if "effective" in row:
            row["effective"] = copy.deepcopy(getattr(app.settings, key))
            row["pending"] = (row["apply_timing"] in ("reload", "restart", "reconnect")
                              and not native_settings._same(row["saved"], row["effective"]))
    return routes.eligibility(app, snap)


def public_jobs(app):
    from litetui import native_work_controls, scheduler, sidecar_jobs
    rows, revision = native_work_controls.read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
    # Canonical disk rows, not a possibly stale copy held by the preview window.
    jobs = [scheduler.Job(**row) for row in rows]
    result = sidecar_jobs.public_jobs(jobs, app)
    result.update(conversation_id=app.convo_id, revision=revision, native_review=True)
    encoded(result)
    return result


@dataclass
class Proposal:
    token: str
    process: object
    capability: str
    event_id: int
    generation: str
    binding: dict
    command: str
    payload: dict
    initial: dict
    status: str = "queued"
    groups: list = field(default_factory=list)
    index: int = 0
    verified: list = field(default_factory=list)
    current: object = None
    error: str | None = None
    replied: bool = False
    local: bool = False

    def wire(self):
        return {"proposal": self.token, "event_id": self.event_id, "generation": self.generation,
                "binding": self.binding, "command": self.command, "payload": self.payload,
                "status": self.status, "index": self.index, "count": len(self.groups),
                "verified": [{"index": row["index"], "group": row["group"],
                              "verification": row["verification"]} for row in self.verified],
                "error": self.error, "replied": self.replied, "local": self.local}


@dataclass
class Operation:
    token: str
    action: str
    view: str | None
    before: dict
    binding: dict
    executable: dict
    delivered: dict | None
    command: bytes | None = None
    result: object = None
    raw: bytes | None = None
    owner_task: object = None
    phase: str | None = None
    published: bool = False
    publication: object = None
    apply_id: str | None = None
    after: dict | None = None
    failed: bool = False


@dataclass
class Step:
    group: dict
    review: dict
    draft: dict
    expected: dict
    before: dict
    revisions: dict
    destinations: dict
    applied: dict | None = None
    command: dict | None = None
    result: object = None
    raw: bytes | None = None
    owner_task: object = None
    phase: str = "review"
    published: bool = False
    publication: object = None
    outer_command: bytes | None = None
    final_reply: dict | None = None


@dataclass
class Publication:
    manager: object
    owner: object
    phase: str
    app: object
    owner_task: object
    command: bytes
    result: object
    raw: bytes
    nested: object = None
    consumed: bool = False


class Manager:
    def __init__(self, app, binding):
        self.app = app
        self.binding = dict(binding)
        self.references = (app._agent_session, app.store, app.backend)
        self.instance = secrets.token_hex(32)
        self.window = SidecarWindow(executable())
        self.window.on_event = self.enqueue
        self.window.warn = lambda message: setattr(self, "error", str(message)[:2048])
        self.window.settings_write = True
        self.window.jobs_write = True
        self.inbox: queue.Queue[tuple[object, str, str | None, dict | None, dict]] = queue.Queue(maxsize=64)
        self.seen = set()
        self.generation = None
        self.view = None
        self.delivered = None
        self.acknowledged = None
        self.proposals = []
        self.operation = None
        self.worker = None
        self.reply_worker = None
        self.retirement_worker = None
        self.active = None
        self.hold = False
        self.closing = False
        self.error = None
        self.overflow = False
        self.retired = False

    def current(self):
        from litetui import background_input
        app = self.app
        bridge = background_input.bridge(app)
        if (bridge is None or not bridge.active or bridge.instance != self.binding["work_instance"]
                or self.references != (app._agent_session, app.store, app.backend)
                or any(getattr(app, k) != self.binding[v] for k, v in (("convo_id", "session_id"), ("model_id", "model")))):
            raise ValueError("Sidecar belongs to a previous conversation or execution owner. Close it explicitly.")
        bridge.check()
        app._validate_owned_identity()

    def enqueue(self, frame):
        # Called by the retained pipe reader. No application call, write or wait.
        if self.closing:
            return
        try:
            encoded(frame)
            item = (self.window.process, self.window.token, self.generation,
                    copy.deepcopy(self.delivered), copy.deepcopy(frame))
            self.inbox.put_nowait(item)
        except (ValueError, queue.Full):
            self.overflow = True

    def process_facts(self):
        window = self.window
        process, reader = window.process, window._reader
        return {"generation": self.generation, "retained": process is not None,
                "pid": getattr(process, "pid", None), "running": process is not None and process.poll() is None,
                "reader_alive": reader is not None and reader.is_alive(), "closing": window._closing,
                "view": self.view, "snapshot_sha256": self.acknowledged}

    async def call_worker(self, method, *args, **kwargs):
        if self.worker is not None:
            raise ValueError("A retained Sidecar worker is still owned.")
        self.worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        try:
            try:
                return await asyncio.shield(self.worker)
            except asyncio.CancelledError:
                try:
                    await join_task(self.worker)
                finally:
                    raise asyncio.CancelledError()
        finally:
            if self.worker.done():
                self.worker = None

    async def reply(self, proposal, result):
        if proposal.replied:
            raise ValueError("Sidecar proposal already has a final reply.")
        # Attempt exactly once, including a broken pipe; a retry could consume a
        # restarted child's different event with the same integer ID.
        proposal.replied = True
        try:
            await self.call_worker(self.window.send_event_reply, proposal.event_id, result,
                                   expected_process=proposal.process, expected_token=proposal.capability)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = "Verified result could not reach its original Sidecar: " + str(exc)[:1024]

    async def drain(self):
        if self.overflow:
            self.error = "Sidecar queue exceeded its bounded capacity. Close this window."
        while not self.inbox.empty():
            process, capability, generation, delivered, frame = self.inbox.get_nowait()
            identity = (generation, frame["id"])
            if identity in self.seen:
                continue
            if len(self.seen) >= 4096:
                self.overflow = True
                break
            self.seen.add(identity)
            if (process is not self.window.process or capability != self.window.token or generation != self.generation
                    or frame.get("token") != capability or delivered is None or generation is None):
                continue
            proposal = Proposal(secrets.token_hex(32), process, capability, frame["id"], generation,
                                copy.deepcopy(delivered["binding"]), frame["command"], frame["payload"],
                                copy.deepcopy(delivered))
            if frame["command"] in ("settings_request", "jobs_request"):
                try:
                    self.current()
                    snapshot = public_settings(self.app) if frame["command"] == "settings_request" else public_jobs(self.app)
                    await self.reply(proposal, {"snapshot" if frame["command"] == "settings_request" else "jobs": snapshot})
                except (ValueError, OSError, TypeError) as exc:
                    await self.reply(proposal, {"saved": False, "error": str(exc)[:2048]})
            elif (not isinstance(proposal.payload, dict) or len(encoded(proposal.payload)) > MAX_PAYLOAD
                  or proposal.command == "settings_patch" and (
                      not isinstance(proposal.payload.get("changes"), list)
                      or not 1 <= len(proposal.payload["changes"]) <= 32
                      or any(not isinstance(change, dict) or change.get("key") not in routes.registry()
                             for change in proposal.payload["changes"])
                  )):
                await self.reply(proposal, {"saved": False, "error": "Unsupported or oversized native desktop proposal; no values were forwarded."})
            elif not self.make_room():
                await self.reply(proposal, {"saved": False, "error": "Terminal Sidecar proposal history is full; close and reopen."})
            else:
                self.proposals.append(proposal)

    def make_room(self):
        # Bound history without discarding an unsettled or partially applied
        # proposal. Final replies and durable saved values are already settled.
        if len(self.proposals) >= MAX_PROPOSALS:
            completed = next((p for p in self.proposals
                              if p.replied and p.status in ("complete", "cancelled")), None)
            if completed is not None:
                self.proposals.remove(completed)
        return len(self.proposals) < MAX_PROPOSALS

    def find(self, token):
        proposal = next((p for p in self.proposals if p.token == token), None)
        if proposal is None:
            raise ValueError("Unknown Sidecar proposal.")
        return proposal

    def check_proposal(self, proposal):
        self.current()
        if proposal.binding != self.binding:
            raise ValueError("Sidecar proposal belongs to a retired conversation.")
        if proposal.local:
            if self.hold:
                raise ValueError("Sidecar awaits its exact application publication.")
            return
        if (self.closing or self.overflow or proposal.process is not self.window.process
                or proposal.capability != self.window.token or proposal.generation != self.generation
                or self.window.process is None or self.window.process.poll() is not None
                or proposal.binding != self.binding):
            raise ValueError("Sidecar proposal no longer belongs to its exact child and conversation.")
        if self.hold:
            raise ValueError("Sidecar awaits its exact application/readback publication.")


def manager(app, binding=None):
    current = getattr(app, "_native_sidecar", None)
    if binding is not None and current is not None and (current.retired or binding != current.binding):
        retired = (current.window.process is None
                   and (current.window._reader is None or not current.window._reader.is_alive())
                   and current.worker is None
                   and (current.reply_worker is None or current.reply_worker.done())
                   and current.retirement_worker is None)
        if retired and (current.retired or not current.hold and current.operation is None):
            current = None
    if current is None and binding is not None:
        current = Manager(app, binding)
        app._native_sidecar = current
    return current


def hold(app):
    owner = manager(app)
    if owner is None or not owner.hold:
        return False
    delegated = DELEGATE.get()
    return delegated != (owner, task())


def require_admission(app, owner):
    """Only this exact Sidecar task may exclude its own wrapper hold."""
    from litetui import native_runtime_controls

    marker = DELEGATE.set((owner, task()))
    try:
        reason = native_runtime_controls.mutation_hold(app)
    finally:
        DELEGATE.reset(marker)
    if reason:
        raise ValueError(reason)


def allowed(app, cmd):
    if hold(app) and not cmd.get("type", "").startswith(PREFIX):
        raise ValueError("Finish the exact Sidecar application/readback or close this runtime.")


def _common(app, cmd, operation, fields=()):
    current = manager(app)
    if operation in ("apply", "readback", "cancel", "step_apply", "step_readback") and current is not None:
        from litetui import background_input as work
        binding = (current.operation.binding if operation in ("apply", "readback")
                   and isinstance(current.operation, Operation) else current.binding)
        work._keys(cmd, ("type", "id", "version", *binding, *fields))
        work.text(cmd["id"])
        if (cmd["type"] != PREFIX + operation or type(cmd["version"]) is not int or cmd["version"] != 1
                or any(cmd[k] != v for k, v in binding.items())):
            raise ValueError("Sidecar command changed its captured ownership.")
        return current
    _bridge, binding = native_context.read_binding(app, cmd, PREFIX + operation, *fields)
    return manager(app, binding)


def _state(owner, binding):
    try:
        owner.current()
        reason = None
    except (ValueError, OSError) as exc:
        reason = str(exc)[:2048]
    facts = executable_facts(owner.window.executable)
    return {"kind": "sidecar_snapshot", "version": 1, **binding, "instance": owner.instance,
            "child_binding": copy.deepcopy(owner.binding),
            "enabled": bool(owner.app.settings.sidecar_enabled), "executable": facts,
            "process": owner.process_facts(), "stale": reason, "error": owner.error,
            "hold": owner.hold, "proposals": [p.wire() for p in owner.proposals]}


def _retain(owner, command, result, phase):
    owner.command = encoded(command) if isinstance(owner, Operation) else owner.command
    if isinstance(owner, Step):
        owner.outer_command = encoded(command)
    owner.result, owner.raw, owner.owner_task, owner.phase = result, encoded(result), task(), phase
    owner.published, owner.publication = False, None
    return result


async def _lifecycle(app, owner, cmd, op):
    if op == "review":
        action, view = cmd["action"], cmd["view"]
        recovery = action == "close" and isinstance(owner.operation, Operation) and owner.operation.failed and owner.worker is None
        if owner.hold and not recovery:
            raise ValueError("Sidecar is awaiting its exact readback.")
        if recovery:
            owner.hold = False
        if action not in ("open", "refresh", "close") or (action != "close" and view not in VIEWS) or (action == "close" and view is not None):
            raise ValueError("Choose a Sidecar view, Refresh, or Close.")
        if action != "close":
            require_admission(app, owner)
            owner.current()
            backend_transition._quiet(app, require_unowned_server=False)
            if owner.overflow or not app.settings.sidecar_enabled:
                raise ValueError("Sidecar is disabled or requires cleanup.")
        before = owner.process_facts()
        if action == "close" and not before["retained"]:
            raise ValueError("No Sidecar child is retained.")
        if action == "refresh" and not before["running"]:
            raise ValueError("Refresh requires the current running Sidecar.")
        facts = executable_facts(owner.window.executable)
        if action != "close" and not facts["available"]:
            raise ValueError("Optional Sidecar is not installed at " + facts["path"] + ". Set LITETUI_SIDECAR_EXE to its installed executable.")
        delivered = None if action == "close" else {"binding": dict(owner.binding), "settings": public_settings(app), "jobs": public_jobs(app)}
        binding = {key: cmd[key] for key in owner.binding}
        review = Operation(secrets.token_hex(32), action, view, before, binding, facts, delivered)
        owner.operation = review
        return {"kind": "sidecar_review", "version": 1, **review.binding, "instance": owner.instance,
                "token": review.token, "action": action, "view": view, "before": before, "executable": facts,
                "snapshot_sha256": None if delivered is None else digest(delivered),
                "consequence": "Close and join the exact child and reader. Remaining proposals are discarded; previously verified saves remain." if action == "close" else
                "Open the optional desktop window. Desktop Save/Create queues a proposal here; each change requires terminal review."}
    review = owner.operation
    if not isinstance(review, Operation) or review.token != cmd["token"]:
        raise ValueError("Review the exact Sidecar lifecycle again.")
    if op == "apply":
        if review.apply_id is not None or owner.hold:
            raise ValueError("Sidecar lifecycle review was consumed.")
        if review.before != owner.process_facts():
            raise ValueError("Sidecar child changed after review.")
        if review.action != "close":
            require_admission(app, owner)
            owner.current()
            backend_transition._quiet(app, require_unowned_server=False)
            if executable_facts(owner.window.executable) != review.executable:
                raise ValueError("Sidecar executable changed after review.")
            current = {"binding": dict(owner.binding), "settings": public_settings(app), "jobs": public_jobs(app)}
            if current != review.delivered:
                raise ValueError("Sidecar snapshot changed after review.")
        review.apply_id, owner.hold = cmd["id"], True
        if owner.window.process is None and review.action != "close":
            owner.generation = secrets.token_hex(32)
            owner.seen.clear()
        owner.delivered = copy.deepcopy(review.delivered)
        def perform():
            if owner.closing:
                return False
            if review.action == "close":
                return owner.window.close()
            if not owner.window.open(review.view):
                return False
            if owner.closing:
                owner.window.close()
                return False
            delivered = review.delivered
            if delivered is None:
                raise ValueError("Sidecar Open requires its retained snapshots.")
            for name in ("settings", "jobs"):
                reply = owner.window._exchange(name + "_snapshot", delivered[name])
                if reply.get(name + "_snapshot") is not True:
                    raise ValueError("Sidecar did not acknowledge its exact " + name + " snapshot.")
            return True
        try:
            done = await owner.call_worker(perform)
            if not done:
                raise ValueError(owner.error or "Sidecar operation was not completed.")
            if review.action == "close":
                owner.view, owner.acknowledged, owner.delivered = None, None, None
            else:
                owner.view = review.view
                owner.acknowledged = digest(review.delivered)
            review.after = owner.process_facts()
            if review.action != "close" and (not review.after["running"] or not review.after["reader_alive"]):
                raise ValueError("Sidecar child or reader ended before acknowledgement.")
            result = {"kind": "sidecar_applied", "version": 1, **review.binding, "instance": owner.instance,
                      "token": review.token, "apply_id": review.apply_id, "action": review.action, "view": review.view,
                      "before": review.before, "after": review.after, "verified": False}
            return _retain(review, cmd, result, "apply")
        except BaseException:
            review.failed = True
            owner.error = "Sidecar operation is unsettled; retained resources require Close or runtime shutdown."
            raise
    if not review.published or review.phase != "apply" or cmd["apply_id"] != review.apply_id:
        raise ValueError("Sidecar readback requires its exact published application.")
    current = owner.process_facts()
    if current != review.after:
        raise ValueError("Sidecar lifecycle changed before independent readback.")
    if not isinstance(review.result, dict):
        raise TypeError("Sidecar readback lacks its retained application receipt.")
    result = {**review.result, "kind": "sidecar_readback", "verified": True}
    return _retain(review, cmd, result, "readback")


async def _delegate(app, owner, command):
    from litetui import gui_rpc
    marker = DELEGATE.set((owner, task()))
    try:
        return await gui_rpc.async_dispatch(app, command)
    finally:
        DELEGATE.reset(marker)


async def _step(app, owner, cmd, op):
    proposal = owner.find(cmd["proposal"])
    if op == "step_review":
        require_admission(app, owner)
        owner.check_proposal(proposal)
        if owner.active not in (None, proposal.token):
            raise ValueError("Finish or discard the active Sidecar proposal first.")
        owner.active = proposal.token
        if proposal.status == "queued":
            current = public_settings(app)
            if proposal.command == "settings_patch":
                proposal.groups = routes.groups(app, proposal.payload, current)
            elif proposal.command == "job_create":
                from litetui.sidecar_jobs import JOB_CREATE_FIELDS
                value = proposal.payload
                if (not isinstance(value, dict) or set(value) - JOB_CREATE_FIELDS or value.get("kind", "cron") != "cron"):
                    raise ValueError("The desktop may propose one cron job.")
                proposal.groups = [{"route": "job", "field": "job", "value": {**value, "kind": "cron"},
                                    "scope": "data_root", "timing": "next_scheduler_tick"}]
            else:
                raise ValueError("Unknown desktop proposal.")
            proposal.status = "reviewing"
        if (proposal.status != "reviewing" or proposal.index >= len(proposal.groups)
                or proposal.current is not None and proposal.current.phase != "review"):
            raise ValueError("This proposal has no unconsumed review step.")
        # An abandoned, unapplied review can be reconstructed from fresh exact
        # revisions. An attempted application is never replayed.
        proposal.current = None
        group = proposal.groups[proposal.index]
        # Refresh at every group. Later groups must retain the exact revisions
        # established by the preceding verified group, never a blind rebase.
        saved = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True)
        expected_revisions = proposal.payload.get("expected_revisions") if proposal.index == 0 else proposal.verified[-1]["verification"].get("revisions")
        if expected_revisions is not None and not routes._same(saved.revisions, expected_revisions):
            raise ValueError("Settings changed outside this Sidecar proposal; keep its verified outcomes and refresh.")
        snapshot_command = {**routes.snapshot_command(app, group), "id": cmd["id"] + "/snapshot"}
        snap = await _delegate(app, owner, snapshot_command)
        draft = {**routes.review_command(app, group, snap), "id": cmd["id"] + "/review"}
        reviewed = await _delegate(app, owner, draft)
        expected = routes.expected_values(app, group)
        before = {key: copy.deepcopy(getattr(app.settings, key)) for key in expected}
        paths, _documents, revisions = native_settings.owned_documents(settings_runtime.service_for(app), app.convo_id, app._agent_session)
        destinations = {key: str(paths["conversation" if SETTING_SCOPE(key) == "conversation" else "global"]) for key in expected}
        step = Step(copy.deepcopy(group), copy.deepcopy(reviewed), copy.deepcopy(draft), copy.deepcopy(expected), copy.deepcopy(before), copy.deepcopy(revisions), copy.deepcopy(destinations))
        proposal.current = step
        return {"kind": "sidecar_step_review", "version": 1, **owner.binding, "instance": owner.instance,
                "proposal": proposal.token, "index": proposal.index, "count": len(proposal.groups),
                "group": group, "expected": expected, "before": before, "destinations": destinations,
                "revisions": revisions, "command": draft, "review": reviewed, "native_snapshot": snap, "verified": proposal.wire()["verified"]}
    step = proposal.current
    if not isinstance(step, Step) or type(cmd["index"]) is not int or cmd["index"] != proposal.index:
        raise ValueError("Sidecar step does not identify the retained exact native review.")
    if op == "step_apply":
        require_admission(app, owner)
        owner.check_proposal(proposal)
        if step.applied is not None or step.phase != "review":
            raise ValueError("Sidecar step was already attempted.")
        current = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True)
        if current.revisions != step.revisions:
            raise ValueError("Canonical settings changed after Sidecar review.")
        owner.hold = True
        command = {**routes.apply_command(app, step.group, step.review, step.draft), "id": cmd["id"] + "/apply"}
        step.phase = "attempted"
        try:
            applied = await _delegate(app, owner, command)
            step.applied = applied
            if (applied.get("status") in ("refused", "failed") or applied.get("close_required")
                    or applied.get("kind", "").endswith("refused")):
                raise ValueError("Native owner refused or could not verify the Sidecar save.")
            step.command = command
            result = {"kind": "sidecar_step_applied", "version": 1, **owner.binding, "instance": owner.instance,
                      "proposal": proposal.token, "index": proposal.index, "command": command, "result": applied,
                      "group": step.group, "expected": step.expected, "destinations": step.destinations}
            return _retain(step, cmd, result, "apply")
        except BaseException:
            proposal.status = "uncertain"
            proposal.error = "This step was attempted but has no published readback. Close this runtime; never replay it."
            raise
    if not step.published or step.phase != "apply" or step.command is None or cmd["apply_id"] != step.command["id"]:
        raise ValueError("Sidecar readback requires its exact published application.")
    command = {**routes.readback_command(app, step.group, step.review, step.applied), "id": cmd["id"] + "/readback"}
    result = await _delegate(app, owner, command)
    verification = ({"job": result, "revisions": settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True).revisions}
                    if step.group["route"] == "job" else routes.verify_settings(app, step.expected, step.before, step.group))
    if step.group["route"] == "job" and result.get("verified") is not True:
        raise ValueError("Canonical job readback was not verified.")
    step.command = command
    response = {"kind": "sidecar_step_readback", "version": 1, **owner.binding, "instance": owner.instance,
                "proposal": proposal.token, "index": proposal.index, "command": command, "result": result,
                "group": step.group, "expected": step.expected, "destinations": step.destinations,
                "verification": verification, "verified": True}
    if proposal.index + 1 == len(proposal.groups):
        reply: dict = {"saved": True, "verified": [*proposal.wire()["verified"],
                 {"index": proposal.index, "group": step.group, "verification": verification}]}
        if proposal.command == "settings_patch":
            reply["snapshot"] = public_settings(app)
            reply["revisions"] = reply["snapshot"]["revisions"]
            reply["runtime"] = [{"field": key, "status": "pending" if facts["pending"] else "applied"}
                                for entry in reply["verified"] for key, facts in entry["verification"].get("fields", {}).items()]
        else:
            reply["jobs"] = public_jobs(app)
            reply["job"] = verification["job"]["row"]
        encoded(reply)
        step.final_reply = reply
    return _retain(step, cmd, response, "readback")


def SETTING_SCOPE(key):
    from litetui.settings_scope import SETTING_SPECS
    return SETTING_SPECS[key].scope.value


async def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    fields = {"snapshot": (), "configure": ("value",), "review": ("action", "view"), "apply": ("token",),
              "readback": ("token", "apply_id"), "step_review": ("proposal",),
              "step_apply": ("proposal", "index"), "step_readback": ("proposal", "index", "apply_id"),
              "cancel": ("proposal",)}
    if op not in fields:
        raise ValueError("Unknown native Sidecar operation.")
    owner = _common(app, cmd, op, fields[op])
    encoded(cmd)
    if op == "configure":
        require_admission(app, owner)
        owner.current()
        if owner.hold or owner.active is not None or type(cmd["value"]) is not bool or not owner.make_room():
            raise ValueError("Finish the current Sidecar proposal before reviewing opt-in.")
        public = public_settings(app)
        payload = {"conversation_id": app.convo_id, "expected_revisions": public["revisions"],
                   "changes": [{"key": "sidecar_enabled", "value": cmd["value"], "scope": "device"}]}
        proposal = Proposal(secrets.token_hex(32), None, "", 0, owner.generation, dict(owner.binding),
                            "settings_patch", payload, {}, local=True)
        owner.proposals.append(proposal)
        return await _step(app, owner, {**cmd, "proposal": proposal.token}, "step_review")
    if op == "snapshot":
        if not owner.hold:
            await owner.drain()
        result = _state(owner, {key: cmd[key] for key in owner.binding})
        result["revision"] = digest(result)
        encoded({"type": "response", "ok": True, "id": cmd["id"], "result": result})
        return result
    if op in ("review", "apply", "readback"):
        return await _lifecycle(app, owner, cmd, op)
    if op == "cancel":
        proposal = owner.find(cmd["proposal"])
        if owner.hold or proposal.status in ("complete", "cancelled", "uncertain"):
            raise ValueError("This Sidecar proposal cannot be discarded after an unverified application.")
        proposal.status = "cancelled"
        proposal.current = None
        owner.active = None
        proposal.error = "Discarded in the terminal. " + str(len(proposal.verified)) + " previously verified group(s) remain saved."
        if not proposal.local:
            await owner.reply(proposal, {"saved": False, "error": proposal.error, "verified": proposal.wire()["verified"],
                                        "snapshot": public_settings(app)})
        return {"kind": "sidecar_cancelled", "version": 1, **owner.binding, "instance": owner.instance,
                "proposal": proposal.wire()}
    return await _step(app, owner, cmd, op)


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in {PREFIX + name for name in ("apply", "readback", "step_apply", "step_readback")}:
        return None
    owner = manager(app)
    if owner is None:
        raise ValueError("Sidecar publication has no retained owner.")
    item = owner.find(cmd["proposal"]).current if "proposal" in cmd else owner.operation
    command = item.outer_command if isinstance(item, Step) else item.command
    if (item.owner_task is not task() or command != encoded(cmd) or item.result is not result
            or item.raw != encoded(result) or item.published or item.publication is not None or not owner.hold):
        raise ValueError("Sidecar publication changed its exact task, command or result.")
    nested = None
    if isinstance(item, Step):
        from litetui import background_input
        nested = background_input.prepare_publication(app, item.command, result["result"])
    ticket = Publication(owner, item, item.phase, app, task(), encoded(cmd), result, encoded(result), nested)
    item.publication = ticket
    return ticket


def finish_publication(app, ticket):
    owner, item = ticket.manager, ticket.owner
    if (ticket.consumed or ticket.app is not app or ticket.owner_task is not task()
            or manager(app) is not owner or item.publication is not ticket or item.result is not ticket.result
            or item.raw != ticket.raw or encoded(item.result) != ticket.raw or item.phase != ticket.phase):
        raise ValueError("Sidecar publication lost its exact single-use owner.")
    if ticket.nested is not None:
        from litetui import background_input
        background_input.finish_publication(app, ticket.nested)
    ticket.consumed, item.published = True, True
    if ticket.phase != "readback":
        return
    owner.hold = False
    if isinstance(item, Operation):
        owner.operation = None
        if item.action == "close":
            owner.retired = True
        return
    proposal = next(p for p in owner.proposals if p.current is item)
    proposal.verified.append(copy.deepcopy(item.result))
    proposal.index += 1
    proposal.current = None
    if proposal.index < len(proposal.groups):
        return
    proposal.status = "complete"
    owner.active = None
    if proposal.local:
        proposal.replied = True
        return
    reply = item.final_reply
    if reply is None:
        raise ValueError("Final Sidecar readback lacks its precomputed child reply.")
    owner.hold = True
    async def send():
        try:
            await owner.reply(proposal, reply)
        finally:
            owner.hold = False
    owner.reply_worker = asyncio.create_task(send())


async def settle_before_shutdown(app):
    owner = manager(app)
    if owner is None:
        return
    owner.closing, owner.hold = True, True
    if owner.window.process is not None and owner.retirement_worker is None:
        owner.retirement_worker = asyncio.create_task(asyncio.to_thread(owner.window.close))
    if owner.reply_worker is not None:
        await join_task(owner.reply_worker)
    if owner.worker is not None:
        await join_task(owner.worker)
        owner.worker = None
    if owner.retirement_worker is not None:
        try:
            await join_task(owner.retirement_worker)
        finally:
            if owner.retirement_worker.done():
                owner.retirement_worker = None
    if owner.window.process is not None:
        await owner.call_worker(owner.window.close)
    if owner.window.process is not None or (owner.window._reader is not None and owner.window._reader.is_alive()):
        raise RuntimeError("Retained Sidecar child or reader still owns this conversation.")
    owner.retired = True

