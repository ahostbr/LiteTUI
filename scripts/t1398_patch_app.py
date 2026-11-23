"""T1398: anchored, EOL-preserving edits to files committed with mixed line endings (app.py).

Each edit's anchor is written with \\n; it is matched in the file with \\r\\n or \\n, whichever occurs
exactly once, and the replacement is written with the same line ending. Anything else is an error,
so a moved anchor never silently patches the wrong place. Usage: python scripts/t1398_patch_app.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "litetui" / "app.py"
SWITCH = ROOT / "src" / "litetui" / "plugins" / "model_switch.py"

APP_EDITS = [
    # E1: the per-model scale cache.
    ("""        self._model_thinking_levels: list[str] | None = None
""", """        self._model_thinking_levels: list[str] | None = None
        # T1398: each served model's effort scale, read from its own template (effort_scale.py),
        # keyed (backend, host, model) so a switch or another engine never reuses a stale one.
        self._effort_scales: dict = {}
""", 1),
    # E2: reading the scale.
    ("""    @work(thread=True, group="thinking-probe", exclusive=True)
    def _probe_thinking(self) -> None:
""", """    def _effort_scale_key(self) -> tuple:
        backend = getattr(self, "backend", None)
        try:
            host = backend.host() if callable(getattr(backend, "host", None)) else ""
        except Exception:
            host = ""
        return (getattr(backend, "name", ""), host, getattr(self, "model_id", None))

    def _cached_effort_scale(self):
        \"\"\"T1398: the current model's EffortScale if it has been read, else None. Never blocks
        (the header, /think and the settings screen read this).\"\"\"
        return (getattr(self, "_effort_scales", None) or {}).get(self._effort_scale_key())

    def _effort_scale_now(self):
        \"\"\"T1398: the current model's EffortScale, read on first use. BLOCKING (renders through
        the engine's /apply-template, never generates): call it in a thread. None for a backend
        that reads no template. A scale whose template could not be read is not cached, so an
        engine that comes up later is asked again.\"\"\"
        reader = getattr(getattr(self, "backend", None), "effort_scale", None)
        if not callable(reader) or not getattr(self, "model_id", None):
            return None
        key = self._effort_scale_key()
        scales = getattr(self, "_effort_scales", None)
        if scales is None:
            scales = self._effort_scales = {}
        if key in scales:
            return scales[key]
        try:
            scale = reader(self.model_id)
        except Exception:
            scale = None
        if scale is not None and scale.template_read:
            scales[key] = scale
        return scale

    @work(thread=True, group="effort-scale", exclusive=True)
    def _discover_effort_scale(self) -> None:
        \"\"\"T1398: read the model's scale after connect or a switch, so /think, the header and the
        settings screen show the number each level sends.\"\"\"
        self._effort_scale_now()
        try:
            self.call_from_thread(self.update_header)
        except Exception:
            pass

    @work(thread=True, group="thinking-probe", exclusive=True)
    def _probe_thinking(self) -> None:
""", 1),
    # E3: read it after connect, for every backend that can.
    ("""                if self.backend.name == "lmstudio" and self._cli_thinking_level is None:
                    self._probe_thinking()
                self._rpc_emit_model_state()
""", """                if self.backend.name == "lmstudio" and self._cli_thinking_level is None:
                    self._probe_thinking()
                if callable(getattr(self.backend, "effort_scale", None)):
                    self._discover_effort_scale()
                self._rpc_emit_model_state()
""", 1),
    # E4: the header says what is actually sent.
    ("""        else:
            think = f"think:{level}"
        cwd = str(Path.cwd())
""", """        else:
            from litetui.effort_scale import describe
            think = describe(self.thinking_level, self._cached_effort_scale(),
                             getattr(self.backend, "label", None) or self.backend.name)[0]
        cwd = str(Path.cwd())
""", 1),
    # E6: the chat turn takes the scale.
    ("""            kwargs = TurnEngine.chat_request(
                model_id=self.model_id,
""", """            effort_scale = await asyncio.to_thread(self._effort_scale_now)
            kwargs = TurnEngine.chat_request(
                model_id=self.model_id,
""", 1),
    ("""                thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
            )
            # The chat template prints the effort""", """                thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
                effort_scale=effort_scale,
            )
            # The chat template prints the effort""", 1),
    # E7: remember the chat turn's effort AND where it went.
    ("""            self._last_chat_effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
""", """            from litetui.turn_engine import chat_effort
            self._last_chat_effort, self._last_chat_effort_placement = chat_effort(kwargs.get("extra_body") or {})
""", 1),
    # E9: the compaction takes the same scale.
    ("""            round_no, round_budget = 0, self.settings.compact_max_tool_iters
""", """            round_no, round_budget = 0, self.settings.compact_max_tool_iters
            compact_scale = await asyncio.to_thread(self._effort_scale_now)
""", 1),
    ("""                    thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
                    pinned_effort=""", """                    thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
                    effort_scale=compact_scale,
                    pinned_effort=""", 1),
    ("""                            thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
                            pinned_effort=""", """                            thinking_budget=TurnEngine.thinking_budget_for(self.settings, self.backend.name),
                            effort_scale=compact_scale,
                            pinned_effort=""", 1),
    # E8: the re-pin keeps the chat turn's placement (both copies).
    ("""                if compact_tools is not None:
                    effort_body = dict(kwargs.get("extra_body") or {})
                    chat_effort = getattr(self, "_last_chat_effort", None)
                    if chat_effort:
                        effort_body["reasoning_effort"] = chat_effort
                    else:
                        effort_body.pop("reasoning_effort", None)
                    kwargs["extra_body"] = effort_body
""", """                if compact_tools is not None:
                    from litetui.turn_engine import pin_effort
                    kwargs["extra_body"] = pin_effort(kwargs.get("extra_body") or {},
                                                      getattr(self, "_last_chat_effort", None),
                                                      getattr(self, "_last_chat_effort_placement", None))
""", 1),
    ("""                        if compact_tools is not None:
                            effort_body = dict(kwargs.get("extra_body") or {})
                            chat_effort = getattr(self, "_last_chat_effort", None)
                            if chat_effort:
                                effort_body["reasoning_effort"] = chat_effort
                            else:
                                effort_body.pop("reasoning_effort", None)
                            kwargs["extra_body"] = effort_body
""", """                        if compact_tools is not None:
                            from litetui.turn_engine import pin_effort
                            kwargs["extra_body"] = pin_effort(kwargs.get("extra_body") or {},
                                                              getattr(self, "_last_chat_effort", None),
                                                              getattr(self, "_last_chat_effort_placement", None))
""", 1),
    # E10: the two side calls on the current model take the scale.
    ("""            resp = await model_transport.for_app(self).create(
                purpose="tool-summary",
""", """            from litetui.effort_scale import effort_body
            side_scale = (await asyncio.to_thread(self._effort_scale_now)
                          if fold_model == self.model_id else None)
            resp = await model_transport.for_app(self).create(
                purpose="tool-summary",
""", 1),
    ("""                extra_body={"reasoning_effort": side_call_reasoning_effort(
                    "low", self.backend.name, fold_model,""", """                extra_body=effort_body(side_scale, "low") if side_scale is not None else {"reasoning_effort": side_call_reasoning_effort(
                    "low", self.backend.name, fold_model,""", 1),
    ("""            extra = {"reasoning_effort": effort} if effort else {}
            resp = await model_transport.for_app(self).create(
""", """            extra = {"reasoning_effort": effort} if effort else {}
            # T1398: on the current model the scale decides "off" (Inkling's "none" through the
            # template, which a top-level "none" on llama.cpp would have erased).
            from litetui.effort_scale import effort_body
            card_scale = await asyncio.to_thread(self._effort_scale_now) if model == self.model_id else None
            if card_scale is not None:
                extra = effort_body(card_scale, "off")
            resp = await model_transport.for_app(self).create(
""", 1),
]

SWITCH_EDITS = [
    ("""    if app.backend.name == "lmstudio":
        app._probe_thinking()
    app._rpc_emit_model_state()
    return True
""", """    if app.backend.name == "lmstudio":
        app._probe_thinking()
    if callable(getattr(app.backend, "effort_scale", None)) and hasattr(app, "_discover_effort_scale"):
        app._discover_effort_scale()       # T1398: the new model's own scale
    app._rpc_emit_model_state()
    return True
""", 1),
    ("""        if getattr(app.backend, "name", "") == "lmstudio":
            app._probe_thinking()
        app._rpc_emit_model_state()
""", """        if getattr(app.backend, "name", "") == "lmstudio":
            app._probe_thinking()
        if callable(getattr(app.backend, "effort_scale", None)) and hasattr(app, "_discover_effort_scale"):
            app._discover_effort_scale()   # T1398: the new model's own scale
        app._rpc_emit_model_state()
""", 1),
]


def patch(path: Path, edits) -> None:
    data = path.read_bytes().decode("utf-8")
    for old, new, count in edits:
        hits = []
        for eol in ("\r\n", "\n"):
            o = old.replace("\n", eol)
            n = data.count(o)
            if n:
                hits.append((eol, o, n))
        if len(hits) != 1 or hits[0][2] != count:
            sys.exit(f"{path.name}: anchor {old[:70]!r} matched {[(h[0].encode(), h[2]) for h in hits]}")
        eol, o, _ = hits[0]
        data = data.replace(o, new.replace("\n", eol))
    path.write_bytes(data.encode("utf-8"))
    print(f"patched {path.name}: {len(edits)} edits")


APP_EDITS_2 = [
    # E5: the footer chip says what the header says.
    ("""            add("think", f"think:{self.thinking_level or 'default'}", "#5c6370", chip="think")
""", """            from litetui.effort_scale import describe
            add("think", describe(self.thinking_level, self._cached_effort_scale(),
                                  getattr(self.backend, "label", None) or self.backend.name)[0],
                "#5c6370", chip="think")
""", 1),
]

APP_EDITS_3 = [
    ("""            from litetui.effort_scale import describe
            think = describe(self.thinking_level, self._cached_effort_scale(),
                             getattr(self.backend, "label", None) or self.backend.name)[0]
""", """            from litetui.effort_scale import think_chip
            think = think_chip(self)
""", 1),
    ("""            from litetui.effort_scale import describe
            add("think", describe(self.thinking_level, self._cached_effort_scale(),
                                  getattr(self.backend, "label", None) or self.backend.name)[0],
                "#5c6370", chip="think")
""", """            from litetui.effort_scale import think_chip
            add("think", think_chip(self), "#5c6370", chip="think")
""", 1),
]

APP_EDITS_4 = [
    ("""        session = getattr(self, '_agent_session', None)
        if session is not None and field in ('backend', 'model', 'thinking_level', 'reasoning_effort'):
""", """        if field in ('thinking_level', 'reasoning_effort'):
            # T1398: a manual number travels as its TEXT through the string-typed layers below (the
            # owned authority refuses a float; the conversation file keeps strings only), and
            # effort_scale.as_level reads it back as the number.
            from litetui.effort_scale import is_number
            if is_number(value):
                value = repr(value)
        session = getattr(self, '_agent_session', None)
        if session is not None and field in ('backend', 'model', 'thinking_level', 'reasoning_effort'):
""", 1),
]

APP_EDITS_5 = [
    # Prefix 5c1db80f item 2: a scale that could not be read is remembered for a minute, so a turn
    # on an engine with no /apply-template and no readable template does not re-probe every time.
    ("""        that reads no template. A scale whose template could not be read is not cached, so an
        engine that comes up later is asked again.\"\"\"
""", """        that reads no template. A scale whose template could not be read is remembered for EFFORT_SCALE_MISS_TTL
        seconds only (Prefix 5c1db80f: no turn waits on the same failed probes again), so an
        engine that comes up later is still asked again. The key is (backend, host, model):
        changing any of them asks at once.\"\"\"
""", 1),
    ("""        if key in scales:
            return scales[key]
        try:
            scale = reader(self.model_id)
        except Exception:
            scale = None
        if scale is not None and scale.template_read:
            scales[key] = scale
        return scale
""", """        if key in scales:
            return scales[key]
        misses = getattr(self, "_effort_scale_misses", None)
        if misses is None:
            misses = self._effort_scale_misses = {}
        miss = misses.get(key)
        if miss is not None and time.monotonic() - miss[0] < EFFORT_SCALE_MISS_TTL:
            return miss[1]
        try:
            scale = reader(self.model_id)
        except Exception:
            scale = None
        if scale is not None and scale.template_read:
            scales[key] = scale
            misses.pop(key, None)
        else:
            misses[key] = (time.monotonic(), scale)
        return scale
""", 1),
    ("""LEAST_THINKING_FALLBACK = "low"
""", """LEAST_THINKING_FALLBACK = "low"

#: T1398 (Prefix 5c1db80f): how long a model whose effort scale could not be read is not asked again.
EFFORT_SCALE_MISS_TTL = 60.0
""", 1),
]

if __name__ == "__main__" and sys.argv[1:] == ["5"]:
    patch(APP, APP_EDITS_5)
elif __name__ == "__main__" and sys.argv[1:] == ["4"]:
    patch(APP, APP_EDITS_4)
elif __name__ == "__main__" and sys.argv[1:] == ["3"]:
    patch(APP, APP_EDITS_3)
elif __name__ == "__main__" and sys.argv[1:] == ["2"]:
    patch(APP, APP_EDITS_2)
elif __name__ == "__main__":
    patch(APP, APP_EDITS)
    patch(SWITCH, SWITCH_EDITS)
