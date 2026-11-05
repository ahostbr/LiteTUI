"""Verify manual Codex compaction against native events and persisted history."""
import re
from dataclasses import dataclass

from litetui.backend_session import SessionState
from litetui.model_transport import ProviderError


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value) is not None


@dataclass(frozen=True)
class VerifiedCompaction:
    thread_id: str
    turn_id: str
    item_id: str


@dataclass(frozen=True, eq=False)
class Binding:
    transport: object
    server: object
    process: object
    session: object
    generation: int
    thread_id: str

    @classmethod
    def capture(cls, app, *, locked=False):
        from litetui.codex_app_server import AppServerTransport

        server = getattr(app.backend, "app_server", None)
        transport = getattr(app.backend, "_transport", None)
        if (app.backend.name != "codex" or not isinstance(transport, AppServerTransport)
                or transport.app is not app or transport.server is not server):
            raise ValueError("Start a turn on this Codex thread before compacting it")
        thread = next((m["provider_metadata"]["app_server_thread_id"] for m in reversed(app.conversation)
                       if isinstance(m.get("provider_metadata"), dict)
                       and m["provider_metadata"].get("app_server_thread_id")), None)
        session = server.session
        activity = server.runtime_activity
        if (not _identifier(thread) or thread != transport.thread_id or transport.turn_id is not None
                or transport.lock.locked() != locked or not server.initialized or server.process is None
                or transport.process is not server.process or server.process.returncode is not None
                or session.state != SessionState.READY or session.active_turn is not None
                or session.failure or session.cleanup_errors or session.generation < 1
                or not activity.connected or activity.uncertain or activity.requests or activity.commands):
            raise ValueError("Codex compaction requires the current idle, connected native thread")
        return cls(transport, server, server.process, session, session.generation, thread)

    def check(self, app, *, locked=False):
        current = Binding.capture(app, locked=locked)
        if (current.transport is not self.transport or current.server is not self.server
                or current.process is not self.process or current.session is not self.session
                or current.generation != self.generation or current.thread_id != self.thread_id):
            raise ValueError("The reviewed Codex lifecycle or native thread changed")


class CompactionEvents:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.turn_id = None
        self.item_id = None
        self.completed = False

    def observe(self, method, params):
        if params.get("threadId") != self.thread_id:
            return
        if method == "turn/started":
            turn_id = (params.get("turn") or {}).get("id")
            if self.turn_id is not None or not _identifier(turn_id):
                raise ProviderError("Codex compaction received overlapping or invalid turn identities.")
            self.turn_id = turn_id
            return
        if method not in ("item/started", "item/completed"):
            return
        item = params.get("item") or {}
        if item.get("type") != "contextCompaction":
            raise ProviderError("Unexpected native work appeared in the Codex compaction turn.")
        item_id = item.get("id")
        if not self.turn_id or params.get("turnId") != self.turn_id or not _identifier(item_id):
            raise ProviderError("Codex compaction item belongs to another or unknown turn.")
        if method == "item/started" and self.item_id is None:
            self.item_id = item_id
        elif method == "item/completed" and self.item_id == item_id and not self.completed:
            self.completed = True
        else:
            raise ProviderError("Codex compaction item changed or repeated during execution.")

    async def idle_thread(self, server):
        response = await server.request("thread/read", {"threadId": self.thread_id, "includeTurns": False})
        thread = response.get("thread") if isinstance(response, dict) else None
        if (not isinstance(thread, dict) or thread.get("id") != self.thread_id
                or (thread.get("status") or {}).get("type") != "idle"):
            raise ProviderError("Codex compaction requires the exact idle native thread.")
        return thread

    async def readback(self, server, terminal_turn):
        if (not self.completed or terminal_turn.get("id") != self.turn_id
                or terminal_turn.get("status") != "completed"):
            raise ProviderError("Codex compaction lacks a matching completed native item and turn.")
        thread = await self.idle_thread(server)
        mode = thread.get("historyMode", "legacy")
        if mode == "paginated":
            # Manual compaction owns an idle thread exclusively. A newer turn is
            # a changed target; searching older pages would hide that change.
            response = await server.request("thread/turns/list", {"threadId": self.thread_id,
                "itemsView": "full", "sortDirection": "desc", "limit": 1})
            turns = response.get("data") if isinstance(response, dict) else None
            if not isinstance(turns, list) or len(turns) != 1:
                raise ProviderError("Codex compaction readback omitted the latest turn.")
            turn = turns[0]
        elif mode == "legacy":
            response = await server.request("thread/read", {"threadId": self.thread_id, "includeTurns": True})
            thread = response.get("thread") if isinstance(response, dict) else None
            if (not isinstance(thread, dict) or thread.get("id") != self.thread_id
                    or thread.get("historyMode", "legacy") != mode):
                raise ProviderError("Codex compaction history changed identity or mode.")
            turns = thread.get("turns")
            if not isinstance(turns, list) or not 0 < len(turns) <= 1024:
                raise ProviderError("Legacy Codex compaction history is missing or exceeds the readback limit.")
            turn = turns[-1]
        else:
            raise ProviderError("Codex compaction requires a supported native history mode.")
        if (not isinstance(turn, dict) or turn.get("id") != self.turn_id or turn.get("status") != "completed"
                or turn.get("itemsView", "full") != "full"):
            raise ProviderError("Latest full Codex history does not confirm the completed compaction turn.")
        items = turn.get("items")
        if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                or items[0].get("type") != "contextCompaction" or items[0].get("id") != self.item_id):
            raise ProviderError("Persisted Codex history does not confirm the exact native compaction item.")
        return VerifiedCompaction(self.thread_id, self.turn_id, self.item_id)
