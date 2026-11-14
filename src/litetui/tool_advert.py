"""The tool advertisement, frozen per conversation on a prefix-cache backend (T1306-D, prefix audit P5).

Two things a chat request carries name the tools: the DEFERRED_TOOLS names list inside message 0 and the `tools`
array. Both used to be rebuilt from live state on every request, so the first `tool_search` hit, the first call of a
deferred tool by name, a lazy MCP server starting, or a tool whose gate opened after the first request changed them,
and a prefix-cache server re-read everything after the first changed byte (NInfer instance 171605, 2026-11-11 11:50:
tools 22 -> 23 between requests 1 and 3, 21k tokens re-read in 24 s; `mine_ninfer.py` on `.ninfer/requests.jsonl`).

On a backend whose server keeps a prefix cache (`app._prefix_cache_backend()`), per conversation:
  * the names list is the text the conversation's first request sent;
  * the array keeps the first request's order, and a tool activated later (tool_search, or a call by name) is
    APPENDED in activation order. Each activation still costs one re-read of everything after the tools JSON: that is
    the accepted price of lazy loading (Ryan 2026-09-10: "lazy load ... same way claude does it");
  * a tool that became available after the first request is withheld, i.e. treated as deferred: tool_search finds it
    and a call by name dispatches it, and either activates it.
A rebuild of message 0 (`_system_prompt_text`: /new, Ctrl+T, plan mode, /system) or another conversation (resume,
switch) starts a new snapshot; a compaction does not rebuild message 0 on these backends, so the snapshot survives it.
Elsewhere both stay live, as before.
"""
from __future__ import annotations

from contextlib import contextmanager


class ToolAdvert:
    def __init__(self) -> None:
        self._key = None
        self._capturing = False
        self.order: list[str] | None = None
        self.deferred_text: str | None = None

    def reset(self, registry) -> None:
        self.order = None
        self.deferred_text = None
        registry.withheld.clear()
        registry.activations.clear()

    def _frozen(self, app) -> bool:
        key = (app.convo_id, bool(app._prefix_cache_backend()))
        if key != self._key:
            self._key = key
            self.reset(app.plugins)
        return key[1]

    @contextmanager
    def capturing(self):
        """The names list is captured while a REQUEST composes message 0, not when a rebuild does: MCP servers land
        after the first frame (T239), and the first request should list what has landed by then."""
        self._capturing = True
        try:
            yield
        finally:
            self._capturing = False

    def deferred(self, app, live) -> str:
        if not self._frozen(app):
            return live()
        if self.deferred_text is not None:
            return self.deferred_text
        text = live()
        if self._capturing:
            self.deferred_text = text
        return text

    def tools(self, app) -> list[dict]:
        registry = app.plugins
        if not self._frozen(app):
            return registry.tool_specs()
        live = registry.tool_specs()
        by_name = {s["function"]["name"]: s for s in live}
        if self.order is None:
            self.order = list(by_name)
            return live
        self.order += [n for n in registry.activations if n in by_name and n not in self.order]
        registry.withheld.update(n for n in by_name if n not in self.order)
        return [by_name[n] for n in self.order if n in by_name]
