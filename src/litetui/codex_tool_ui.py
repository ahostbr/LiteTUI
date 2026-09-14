"""Present official Codex item lifecycles through the shared tool cards."""

import asyncio
import json
import time

from rich.text import Text

from litetui import sanitize
from litetui.widgets import CompactionCard, FoldBlock, ToolMessage


def clean(value):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, indent=2, ensure_ascii=False)
    )
    return sanitize.redact_secrets(sanitize.strip_escapes(text))


def mcp_display_result(result):
    """Keep standard MCP attachments out of display/persistence text."""
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    content = []
    for entry in result["content"]:
        if not isinstance(entry, dict):
            content.append(entry)
        elif entry.get("type") in ("image", "audio"):
            content.append({"type": entry["type"], "mimeType": entry.get("mimeType"),
                            "text": f"[{entry['type'].title()} returned to Codex]"})
        elif entry.get("type") == "resource" and isinstance(entry.get("resource"), dict):
            resource = entry["resource"]
            if "blob" in resource:
                resource = {key: value for key, value in resource.items() if key != "blob"}
                resource["attachment"] = "[Binary resource returned to Codex]"
            content.append({**entry, "resource": resource})
        else:
            content.append(entry)
    return {**result, "content": content}


class CodexToolUI:
    def __init__(
        self,
        app,
        on_record=None,
        *,
        thread_id=None,
        turn_id=None,
        automatic_compaction=True,
    ):
        self.app = app
        self.thread_id, self.turn_id = thread_id, turn_id
        self.automatic_compaction = automatic_compaction
        self.on_record = on_record
        self.records = {}
        self.finished = set()
        self.calls = {}
        self.output = {}
        self.progress_timers = {}
        self.progress_at = {}
        self.plan_card = None
        self.proposed_plan_cards = {}
        self.proposed_plan_text = {}
        self.compactions = {}
        self.compaction_started = {}

    def emit(self, event):
        from litetui.codex_tasks import update

        update(self.app, {"threadId": self.thread_id, "turnId": self.turn_id, **event})
        if event.get("type") == "tool_result":
            # LiteSuite's existing adapter reads text; LiteGUI reads result.
            event = {**event, "text": event.get("result", "")}
        self.app._rpc_emit(
            {
                "eventVersion": 1,
                "provider": "codex",
                "threadId": self.thread_id,
                "turnId": self.turn_id,
                **event,
            }
        )

    def record(self, key, **fields):
        record = self.records.setdefault(key, {"id": key})
        record.update(fields)
        if self.on_record:
            self.on_record(dict(record))

    def agent_delta(self, payload):
        key = payload.get("itemId")
        if key:
            record = self.records.setdefault(
                key,
                {"id": key, "kind": "agentMessage", "state": "running", "result": ""},
            )
            record["result"] = record.get("result", "") + payload.get("delta", "")

    def progress(self, payload):
        key = payload.get("itemId")
        state = self.calls.get(key)
        if not state:
            return
        delta = payload.get("delta", payload.get("message", ""))
        text = self.output.get(key, "") + str(delta)
        if len(text) > 32768:
            text = (
                "[Earlier live output omitted; final result follows]\n" + text[-32768:]
            )
        self.output[key] = text
        delay = self.progress_at.get(key, 0) + 0.1 - time.monotonic()
        if delay <= 0:
            self.flush_progress(key)
        elif key not in self.progress_timers:
            self.progress_timers[key] = asyncio.get_running_loop().call_later(
                delay, self.flush_progress, key
            )

    def flush_progress(self, key):
        timer = self.progress_timers.pop(key, None)
        if timer:
            timer.cancel()
        state = self.calls.get(key)
        if not state:
            return
        self.progress_at[key] = time.monotonic()
        text = clean(self.output.get(key, ""))
        widget, _, name = state
        if widget:
            widget.set_progress(text)
            self.app._scroll_down()
        self.emit({"type": "tool_progress", "id": key, "name": name, "text": text})

    async def plan(self, payload):
        if not self.app:
            return
        text = "\n".join(
            f"{step.get('status', 'pending')}: {step.get('step', '')}"
            for step in payload.get("plan", [])
        )
        if payload.get("explanation"):
            text = str(payload["explanation"]) + "\n\n" + text
        if not getattr(self.app, "_rpc", False):
            if self.plan_card is None:
                self.plan_card = FoldBlock("Codex plan", "", expanded=True)
                await self.app.query_one("#chat-log").mount(self.plan_card)
            self.plan_card.body.content = Text(clean(text))
            self.app._scroll_down()
        self.emit(
            {
                "type": "plan_update",
                "threadId": payload.get("threadId"),
                "turnId": payload.get("turnId"),
                "text": clean(text),
            }
        )
        self.record(
            "plan:" + str(payload.get("turnId", "")),
            kind="plan",
            name="Codex plan",
            result=clean(text),
            state="completed",
        )

    async def proposed_plan(self, item, completed=False, *, delta=False):
        key = item.get("itemId") if delta else item.get("id")
        if not self.app or not key or key in self.finished:
            return
        if not delta and not completed and key in self.records:
            return  # A late/replayed start cannot erase streamed content.
        text = ((self.proposed_plan_text.get(key, "") + item.get("delta", ""))
                if delta else item.get("text", ""))
        self.proposed_plan_text[key] = text
        displayed = clean(text)
        if not getattr(self.app, "_rpc", False):
            card = self.proposed_plan_cards.get(key)
            if card is None:
                card = FoldBlock("Codex proposed plan", "", expanded=True)
                self.proposed_plan_cards[key] = card
                await self.app.query_one("#chat-log").mount(card)
            card.body.content = Text(displayed)
            self.app._scroll_down()
        state = "completed" if completed else "running"
        self.record(key, kind="plan", name="Codex proposed plan", result=displayed, state=state)
        self.emit({"type": "plan_update", "id": key, "planType": "proposed",
                   "text": displayed, "status": state})
        if completed:
            self.finished.add(key)
            self.proposed_plan_text.pop(key, None)

    async def compaction(self, item, completed):
        key = item["id"]
        if not completed and key not in self.compactions:
            self.compactions[key] = None
            self.compaction_started[key] = time.monotonic()
        card = self.compactions.get(key)
        if card is None and not getattr(self.app, "_rpc", False):
            card = CompactionCard(
                "Codex is compacting its native context",
                "Managed by the official Codex engine.",
                auto=self.automatic_compaction,
            )
            await self.app.query_one("#chat-log").mount(card)
            self.compactions[key] = card
            self.app._compact_card = card
            elapsed = getattr(self.app, "_elapsed", None)
            if elapsed:
                elapsed.ensure_running()
        if completed and card:
            card.finish("Native context compacted; displayed conversation preserved.")
            if getattr(self.app, "_compact_card", None) is card:
                self.app._compact_card = None
        started = self.compaction_started.get(key)
        duration = (
            round((time.monotonic() - started) * 1000) if started is not None else None
        )
        if completed:
            self.compactions.pop(key, None)
            self.compaction_started.pop(key, None)
        self.emit(
            {
                "type": "compaction_end" if completed else "compaction_start",
                "id": key,
                "provider": "codex",
                "automatic": self.automatic_compaction,
                **(
                    {
                        "outcome": "success",
                        "will_resume": self.automatic_compaction,
                        "durationMs": duration,
                    }
                    if completed
                    else {}
                ),
            }
        )
        self.record(
            key,
            kind="contextCompaction",
            name="Codex compaction",
            args="",
            result="Native context compacted"
            if completed
            else "Compacting native context",
            state="completed" if completed else "running",
            ok=completed,
            durationMs=duration if completed else None,
        )

    async def item(self, item, completed=False):
        kind, key = item.get("type"), item.get("id")
        if not self.app or not key or kind in ("userMessage", "reasoning"):
            return
        if kind == "plan":
            await self.proposed_plan(item, completed)
            return
        if kind == "agentMessage":
            self.record(
                key,
                kind=kind,
                phase=item.get("phase"),
                result=item.get("text", ""),
                state="completed" if completed else "running",
            )
            return
        if key in self.finished:
            return
        if kind == "contextCompaction":
            await self.compaction(item, completed)
            if completed:
                self.finished.add(key)
            return
        name = item.get("tool") or {
            "commandExecution": "command",
            "fileChange": "file changes",
            "webSearch": "web search",
        }.get(kind, kind or "Codex tool")
        name = name.removeprefix("litetui_")
        if item.get("server"):
            name = f"{item['server']}/{name}"
        args = item.get("arguments")
        if args is None:
            args = {
                k: v
                for k, v in item.items()
                if k
                not in (
                    "id",
                    "type",
                    "status",
                    "aggregatedOutput",
                    "durationMs",
                    "exitCode",
                )
            }
        state = self.calls.get(key)
        if state is None:
            widget = None
            if not getattr(self.app, "_rpc", False):
                thinking_done = getattr(self.app, "_thinking_done", None)
                if thinking_done:
                    thinking_done()
                widget = ToolMessage(clean(name))
                await self.app.query_one("#chat-log").mount(widget)
                self.app._tool_begin(widget)
                widget.set_args(clean(args))
                self.app._scroll_down()
            state = self.calls[key] = (
                widget,
                None if completed else time.monotonic(),
                name,
            )
            self.record(
                key, name=clean(name), args=clean(args), state="running", kind=kind
            )
            # Native items own the lifecycle, including host dynamic tools.
            self.emit(
                {
                    "type": "tool_call",
                    "id": key,
                    "name": name,
                    "status": "running",
                    "args": json.loads(clean(args))
                    if not isinstance(args, str)
                    else clean(args),
                }
            )
        widget, started, name = state
        if not completed:
            return
        timer = self.progress_timers.pop(key, None)
        if timer:
            timer.cancel()
        self.progress_at.pop(key, None)
        ok = (
            item.get("success") is not False
            and item.get("status")
            not in ("failed", "declined", "cancelled", "interrupted")
            and not item.get("error")
            and item.get("exitCode") in (None, 0)
        )
        outcome = item.get("status")
        if outcome not in ("failed", "declined", "cancelled", "interrupted"):
            outcome = "completed" if ok else "failed"
        details = {
            k: v
            for k, v in item.items()
            if k
            not in (
                "id",
                "type",
                "arguments",
                "tool",
                "namespace",
                "server",
                "aggregatedOutput",
                "contentItems",
                "result",
            )
        }
        sections = [clean(details)]
        if item.get("aggregatedOutput") is not None:
            sections.append(clean(item["aggregatedOutput"]))
        for content in item.get("contentItems") or []:
            sections.append(
                "[Image returned to Codex]"
                if content.get("type") == "inputImage"
                else clean(content.get("text", content))
            )
        if item.get("result") is not None:
            result_value = (mcp_display_result(item["result"])
                            if kind == "mcpToolCall" else item["result"])
            sections.append(clean(result_value))
        result = "\n\n".join(sections)
        duration = item.get("durationMs")
        elapsed = (
            max(0, duration / 1000)
            if isinstance(duration, (int, float))
            else time.monotonic() - started
            if started is not None
            else None
        )
        if widget:
            widget.set_result(
                result, ok, elapsed=elapsed, duration_unknown=elapsed is None
            )
            self.app._tool_end(widget)
            self.app._scroll_down()
        self.record(
            key,
            result=result,
            ok=ok,
            durationMs=round(elapsed * 1000) if elapsed is not None else None,
            state=outcome,
        )
        self.emit(
            {
                "type": "tool_result",
                "id": key,
                "name": name,
                "result": result,
                "ok": ok,
                "status": outcome,
                "durationMs": round(elapsed * 1000) if elapsed is not None else None,
            }
        )
        del self.calls[key]
        self.finished.add(key)
        self.output.pop(key, None)

    def finish(self):
        for timer in self.progress_timers.values():
            timer.cancel()
        self.progress_timers.clear()
        self.progress_at.clear()
        for key, record in list(self.records.items()):
            if (
                record.get("kind") in ("agentMessage", "plan")
                and record.get("state") == "running"
            ):
                self.record(key, state="interrupted")
                if record.get("kind") == "plan":
                    self.emit({"type": "plan_update", "id": key, "planType": "proposed",
                               "text": record.get("result", ""), "status": "interrupted"})
                    self.finished.add(key)
        self.proposed_plan_text.clear()
        for key, (widget, started, name) in self.calls.items():
            if widget:
                widget.set_result(
                    "Tool ended without a completion event (turn stopped or connection closed).",
                    False,
                )
                self.app._tool_end(widget)
            self.record(
                key,
                result="Tool ended without a completion event.",
                ok=False,
                durationMs=round((time.monotonic() - started) * 1000),
                state="interrupted",
            )
            self.emit(
                {
                    "type": "tool_result",
                    "id": key,
                    "name": name,
                    "result": "Tool ended without a completion event.",
                    "ok": False,
                    "status": "interrupted",
                    "durationMs": round((time.monotonic() - started) * 1000),
                }
            )
            self.finished.add(key)
        self.calls.clear()
        self.output.clear()
        for key, card in self.compactions.items():
            if card:
                card.fail("Compaction ended without a completion event.")
            if card and getattr(self.app, "_compact_card", None) is card:
                self.app._compact_card = None
            self.emit(
                {
                    "type": "compaction_end",
                    "id": key,
                    "outcome": "cancelled"
                    if getattr(self.app, "_stop_requested", False)
                    else "error",
                    "will_resume": False,
                    "error": "Compaction ended without a completion event.",
                }
            )
            self.record(
                key,
                state="interrupted",
                ok=False,
                result="Compaction ended without a completion event.",
            )
            self.finished.add(key)
        self.compactions.clear()
        self.compaction_started.clear()
