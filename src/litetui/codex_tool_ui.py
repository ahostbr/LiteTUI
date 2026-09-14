"""Present official Codex item lifecycles through the shared tool cards."""

import json
import time

from litetui import sanitize
from litetui.widgets import ToolMessage


def clean(value):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, indent=2, ensure_ascii=False)
    )
    return sanitize.redact_secrets(sanitize.strip_escapes(text))


class CodexToolUI:
    def __init__(self, app):
        self.app = app
        self.calls = {}

    async def item(self, item, completed=False):
        kind, key = item.get("type"), item.get("id")
        if (
            not self.app
            or not key
            or kind
            in ("agentMessage", "userMessage", "reasoning", "plan", "contextCompaction")
        ):
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
            state = self.calls[key] = (widget, time.monotonic(), name)
            # Host tools already emit their RPC lifecycle through _execute_tool.
            if kind != "dynamicToolCall":
                self.app._rpc_emit(
                    {
                        "type": "tool_call",
                        "id": key,
                        "name": name,
                        "args": json.loads(clean(args))
                        if not isinstance(args, str)
                        else clean(args),
                    }
                )
        widget, started, name = state
        if not completed:
            return
        ok = (
            item.get("success") is not False
            and item.get("status")
            not in ("failed", "declined", "cancelled", "interrupted")
            and not item.get("error")
            and item.get("exitCode") in (None, 0)
        )
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
            sections.append(clean(item["result"]))
        result = "\n\n".join(sections)
        duration = item.get("durationMs")
        elapsed = (
            max(0, duration / 1000)
            if isinstance(duration, (int, float))
            else time.monotonic() - started
        )
        if widget:
            widget.set_result(result, ok, elapsed=elapsed)
            self.app._tool_end(widget)
            self.app._scroll_down()
        if kind != "dynamicToolCall":
            self.app._rpc_emit(
                {
                    "type": "tool_result",
                    "id": key,
                    "name": name,
                    "result": result,
                    "ok": ok,
                    "durationMs": round(elapsed * 1000),
                }
            )
        del self.calls[key]

    def finish(self):
        for widget, _, _ in self.calls.values():
            if widget:
                widget.set_result(
                    "Tool ended without a completion event (turn stopped or connection closed).",
                    False,
                )
                self.app._tool_end(widget)
        self.calls.clear()
