"""Bind reviewed composer input to the actual running Codex steering owner."""

from __future__ import annotations

from dataclasses import dataclass

from litetui import background_input as work
from litetui.backend_session import SessionState
from litetui.codex_app_server import AppServerTransport
from litetui.codex_compaction_evidence import _identifier
from litetui.codex_steering import HostSteering


@dataclass(frozen=True, eq=False)
class Binding:
    transport: object
    server: object
    process: object
    session: object
    generation: int
    lane: object
    thread: str
    turn: str

    @classmethod
    def capture(cls, app):
        server = getattr(app.backend, "app_server", None)
        transport = getattr(app.backend, "_transport", None)
        lane = getattr(app, "_native_work_provider_lane", None)
        if (
            app.backend.name != "codex"
            or not isinstance(transport, AppServerTransport)
            or transport.app is not app
            or transport.server is not server
            or lane is None
            or lane.app is not app
            or lane.bridge is not work.bridge(app)
            or not isinstance(lane.owner, HostSteering)
            or lane.owner.server is not server
            or lane.worker is None
            or lane.worker.is_finished
            or lane.task is None
            or lane.task.done()
            or lane.parent.is_finished
            or lane.bridge.executions.get(lane.origin.execution_id)
            != (lane.parent, lane.origin)
        ):
            raise ValueError(
                "Codex input requires its exact running native steering owner"
            )
        session = server.session
        if (
            not _identifier(transport.thread_id)
            or not _identifier(transport.turn_id)
            or not transport.lock.locked()
            or not server.initialized
            or server.process is None
            or transport.process is not server.process
            or server.process.returncode is not None
            or session.state != SessionState.READY
            or session.failure
            or session.cleanup_errors
            or not 1 <= session.generation <= 2**53 - 1
            or lane.owner.thread_id != transport.thread_id
            or lane.owner.turn_id != transport.turn_id
            or not server.runtime_activity.connected
            or server.runtime_activity.uncertain
        ):
            raise ValueError(
                "The captured Codex thread or turn is no longer accepting input"
            )
        return cls(
            transport,
            server,
            server.process,
            session,
            session.generation,
            lane,
            transport.thread_id,
            transport.turn_id,
        )

    def value(self):
        return {
            "provider": "codex",
            "generation": self.generation,
            "thread_id": self.thread,
            "turn_id": self.turn,
        }

    def check(self, app):
        current = Binding.capture(app)
        if (
            current.transport is not self.transport
            or current.server is not self.server
            or current.process is not self.process
            or current.session is not self.session
            or current.lane is not self.lane
            or current.value() != self.value()
        ):
            raise ValueError("The reviewed Codex native delivery changed")

    def prepare(self, app, payload, review):
        self.check(app)
        ledger = self.lane.owner.ledger
        if any(
            entry.get("item", {}).get("operation_id") == review
            for entry in ledger.entries
        ):
            raise ValueError("This Codex input review was already prepared")
        native = ledger.enqueue(
            {**payload, "operation_id": review}, self.thread, self.turn
        )
        native["conversationId"] = app.convo_id
        native["instructions_digest"] = self.lane.owner.metadata.get(
            "instructions_digest"
        )
        ledger.save()
        return {"_codex_entry": native, "_codex_ledger": ledger}

    def receipt(self, app, item, review):
        # Terminal completion may retire the steering worker while publication is
        # waiting. The original connected process and native journal remain exact.
        if (
            app.backend.app_server is not self.server
            or app.backend._transport is not self.transport
            or self.server.process is not self.process
            or self.transport.process is not self.process
            or self.process.returncode is not None
            or self.server.session is not self.session
            or self.session.generation != self.generation
            or self.session.state != SessionState.READY
            or self.session.failure
            or self.session.cleanup_errors
            or not self.server.initialized
            or not self.server.runtime_activity.connected
            or self.server.runtime_activity.uncertain
            or self.transport.thread_id != self.thread
            or self.transport.turn_id not in (None, self.turn)
        ):
            raise ValueError("Codex queued input lost its original native owner")
        native, ledger = item["_codex_entry"], item["_codex_ledger"]
        if (
            ledger is not self.lane.owner.ledger
            or not any(row is native for row in ledger.entries)
            or native["state"] != "queued"
            or native["threadId"] != self.thread
            or native["turnId"] != self.turn
            or native["conversationId"] != app.convo_id
            or native["item"]
            != {**work.entry_for(app, item).payload, "operation_id": review}
        ):
            raise ValueError("Codex queued delivery changed before confirmation")
        work.canonical_id(native["id"])
        work.bind_native_journal(app, work.entry_for(app, item), native, ledger)
        return {
            "id": native["id"],
            "thread_id": self.thread,
            "turn_id": self.turn,
            "state": "queued",
            "operation_id": review,
        }
