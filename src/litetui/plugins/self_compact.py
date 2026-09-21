"""Agent-requested compaction; the host consumes requests at a round boundary."""
from dataclasses import dataclass

from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.tool_policy import SELF_STORE, ToolPolicy


@dataclass(frozen=True)
class CompactionRequest:
    reason: str
    handoff: str
    conversation_id: str


class SelfCompaction:
    def __init__(self, app):
        self.app = app
        self.pending: CompactionRequest | None = None
        self.needs_progress = False

    def supported(self):
        return getattr(self.app.backend, "name", "") in {"ninfer", "lmstudio", "llamacpp"}

    def progress(self):
        self.needs_progress = False

    def compacting(self):
        return any(w.group == "chat" and w.name == "_compact" and w.is_running
                   for w in self.app.workers)

    def request(self, args):
        app = self.app
        if not self.supported():
            return "[error] self_compact is available only on LiteTUI's local backends."
        if self.compacting():
            return "[error] Already compacting; finish the current summary."
        if app._stop_requested:
            return "[error] Turn stopped; compaction was not requested."
        for key, limit in (("reason", 1000), ("handoff", 8000)):
            value = args.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return f"[error] {key} must be nonempty text of at most {limit} characters."
        if self.pending is not None:
            return "[error] A self-compaction request is already queued for this round."
        if self.needs_progress:
            return "[error] Continue useful work or wait for new user input before requesting compaction again."
        body = [m for m in app.conversation if m.get("role") != "system"]
        tail = app._safe_tail(body, app.settings.compact_keep_recent)
        if len(body) <= len(tail):
            return "[error] Nothing older than the preserved recent messages to compact."
        self.pending = CompactionRequest(args["reason"].strip(), args["handoff"].strip(), app.convo_id)
        return "Compaction requested, not completed. LiteTUI will finish this tool round, compact, and resume with your handoff."

    def take(self):
        request, self.pending = self.pending, None
        if request is not None:
            self.needs_progress = True
        return request


def _register(ctx):
    app = ctx.app
    app.self_compaction = state = SelfCompaction(app)
    ctx.tool(
        tool_schemas.load("self_compact"),
        lambda args: app.call_from_thread(state.request, args),
        gate=lambda: state.supported() and not state.compacting(),
        policy=ToolPolicy(frozenset({SELF_STORE}), "Summarize this conversation and preserve its handoff"),
    )


PLUGIN = PluginManifest(id="self_compact", register=_register)
