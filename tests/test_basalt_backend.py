"""BasaltBackend (T1355) — StrataBackend with Basalt's launcher, settings, name and identity,
against a local fake server shaped like Basalt's. Nothing here starts an engine: the only
server is a thread in this process.

Ryan, 2026-11-19: "makes sure everything loads and acts exactly like any other backend and
model in Litetui"."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from litetui import basalt_engine, llm_backend, strata_engine, turn_engine
from litetui.basalt_backend import BASALT_REASONING_LEVELS, BasaltBackend
from litetui.launch_options import LaunchOptions
from litetui.llm_backend import BackendError
from litetui.settings import Settings
from litetui.strata_backend import StrataBackend

MODEL = "Qwen3.8-FN-GSQ-IQ3_S"


@pytest.fixture(autouse=True)
def _no_real_machine(monkeypatch, tmp_path):
    """Never this machine's LiteSuite config or its ports (a dev box may run a real Basalt)."""
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    monkeypatch.setattr(strata_engine, "port_holder", lambda host: strata_engine.Holder())


@pytest.fixture
def server():
    state = {"loaded": True, "service": "basalt", "unloads": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            value = "loaded" if state["loaded"] else "loading" if state.get("loading") else "unloaded"
            body = {
                "/health": {"status": "ok", "loaded": state["loaded"], "loading": bool(state.get("loading")),
                            "service": state["service"]},
                "/v1/models": {"object": "list", "data": [{
                    "id": MODEL, "owned_by": "basalt", "status": {"value": value}, "meta": {"n_ctx": 150000},
                    "architecture": {"input_modalities": ["text"]}}]},
                "/props": state.get("props"),
            }.get(self.path.split("?")[0])
            state.setdefault("gets", []).append(self.path)
            self._send(200 if body else 404, body or {})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/v1/unload":
                state["unloads"] += 1
                state["loaded"] = False
                self._send(200, {"status": "unloaded"})
            elif self.path == "/v1/load":
                state["loaded"] = True
                self._send(200, {"status": "loaded"})
            else:
                self._send(404, {})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["host"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


def _backend(host: str, **settings) -> BasaltBackend:
    return BasaltBackend(Settings(backend="basalt", basalt_host=host, **settings))


# ── listed and built like the others ─────────────────────────────────────────

def test_basalt_is_a_listed_engine_backend_the_factory_builds():
    assert "basalt" in llm_backend.BACKEND_NAMES and "basalt" in llm_backend.ENGINE_BACKENDS
    assert "Flash-Next" in llm_backend.backend_label("basalt")
    assert "basalt" in dict(llm_backend.visible_backends())
    backend = llm_backend.make_backend(Settings(backend="basalt"))
    assert isinstance(backend, BasaltBackend) and backend.name == "basalt" and backend.label == "Basalt"
    assert backend._engine is basalt_engine and backend.host() == "http://127.0.0.1:8091"


def test_it_answers_everything_strata_answers():
    public = lambda cls: {a for a in dir(cls) if not a.startswith("_")}  # noqa: E731
    assert public(StrataBackend) <= public(BasaltBackend)


def test_every_table_that_names_the_local_engines_names_basalt():
    from litetui import agent_admission, context_choice, subagent_local, subagent_routing
    from litetui.settings_scope import SETTING_SPECS, SettingScope

    assert context_choice.MAIN_KEY["basalt"] == "basalt_max_context"
    assert "basalt" in context_choice.APPLIES_ON_ENGINE_START
    assert "basalt" in agent_admission._LOCAL and "basalt" in subagent_routing.LOCAL_BACKENDS
    assert "basalt" in subagent_local.LOADS_ON_REQUEST
    assert "basalt" in Settings().prefix_cache_backends
    fields = [name for name in SETTING_SPECS if name.startswith("basalt_")]
    assert len(fields) == 12
    for name in fields:
        spec = SETTING_SPECS[name]
        if name == "basalt_host":      # where to connect: per conversation, applied by a reconnect
            assert (spec.scope, spec.apply_timing) == (SettingScope.CONVERSATION, "reconnect"), name
        else:                          # what the engine is started with: this machine, next engine start
            assert (spec.scope, spec.apply_timing) == (SettingScope.DEVICE, "restart"), name


# ── a server that is there ───────────────────────────────────────────────────

def test_a_loaded_server_is_ready_and_reports_its_own_context(server):
    backend = _backend(server["host"])
    assert asyncio.run(backend.ensure_running()) == "ok"
    assert backend.base_url() == server["host"] + "/v1"
    rows = asyncio.run(backend.list_models())
    assert [(r.key, r.loaded) for r in rows] == [(MODEL, True)]
    assert asyncio.run(backend.model_info(MODEL)) == (150000, "llm", True)
    asyncio.run(backend.ensure_chat_ready(MODEL))
    with pytest.raises(BackendError, match="this Basalt server serves"):
        asyncio.run(backend.ensure_chat_ready("qwen"))
    assert "attached Basalt server" in backend.engine_status()


def test_a_server_still_loading_or_unloaded_is_not_ready_for_a_turn(server):
    server.update(loaded=False, loading=True)
    backend = _backend(server["host"])
    with pytest.raises(BackendError, match="Basalt server .* unloaded"):
        asyncio.run(backend.ensure_running())
    assert backend.loaded_models() == [] and "UNLOADED" in backend.engine_status()
    with pytest.raises(BackendError, match="has unloaded its model"):
        asyncio.run(backend.ensure_chat_ready(MODEL))


def test_a_strata_server_on_that_port_is_not_taken_for_basalt(server):
    """Both forks answer /health and /v1/models alike; only the service name tells them apart."""
    server["service"] = "strata"
    backend = _backend(server["host"])
    assert BasaltBackend._health_state(server["host"]) is None
    with pytest.raises(BackendError, match="no Basalt server is answering"):
        asyncio.run(backend.ensure_running())
    assert StrataBackend._health_state(server["host"]) == "loaded"      # and Strata still knows its own


def test_no_server_names_basalts_settings_and_repo():
    backend = _backend("http://127.0.0.1:9")
    with pytest.raises(BackendError, match="set basalt_host") as exc:
        asyncio.run(backend.ensure_running())
    assert basalt_engine.BASALT_REPO_URL in str(exc.value)
    assert "no Basalt server" in backend.engine_status() and backend.stop_engine() == "no engine is running."
    assert backend.empty_state_hint() == "/model to pick a Basalt pack, then /engine start"


def test_the_card_is_freed_and_taken_back_through_the_servers_own_switch(server):
    backend = _backend(server["host"])
    with pytest.raises(BackendError, match="basalt_max_context"):
        asyncio.run(backend.load("x"))
    rec = backend.seat_snapshot(MODEL)
    assert rec == {"identifier": MODEL, "context": 150000, "parallel": 1, "status": "idle", "queued": 0}
    assert backend.seat_suspend(rec) is None and server["unloads"] == 1 and backend.loaded_models() == []
    assert backend.seat_resume(rec) is None and backend.loaded_models() == [MODEL]


def test_start_goes_through_the_shared_lifecycle_and_the_basalt_launcher(monkeypatch, server):
    backend = _backend(server["host"])
    owned = basalt_engine.OwnedEngine(proc=type("P", (), {"pid": 9, "poll": lambda self: None})(),
                                      host=server["host"], log_path="log", log_file=None,
                                      model_id=MODEL, registered=False)
    monkeypatch.setattr(basalt_engine, "start", lambda settings, healthy, notice=None, **_: owned)
    assert asyncio.run(backend.start_engine()) == f"started Basalt pid 9 at {server['host']} ({MODEL})"
    assert "LiteTUI-owned Basalt server" in backend.engine_status()


# ── thinking ─────────────────────────────────────────────────────────────────

def test_thinking_levels_are_the_servers_and_the_two_it_lacks_fold():
    assert BasaltBackend(Settings()).reasoning_levels("any") == list(BASALT_REASONING_LEVELS)
    wire = {level: turn_engine._resolve_reasoning_effort(level, "basalt") for level in
            ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")}
    assert wire == {"off": "none", "minimal": "low", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max"}
    assert set(wire.values()) <= set(BASALT_REASONING_LEVELS)


def test_the_thinking_budget_is_stratas_settings_number_scaled_by_level():
    """Ryan (T1355): the same number as Strata. Through the Custom backend it was never sent."""
    settings = Settings(strata_reasoning_budget_tokens=16384)
    assert turn_engine.THINKING_BUDGET_KEY["basalt"] == "reasoning_budget_tokens"
    assert turn_engine.thinking_budget_for(settings, "basalt") == 16384
    assert turn_engine.thinking_budget_for(Settings(), "basalt") is None
    assert [turn_engine.level_budget("basalt", 16384, level) for level in ("xhigh", "high", "medium", "low")] == [
        16384, 12288, 8192, 4096]
    assert turn_engine.level_budget("custom", 16384, "low") == 16384        # not a backend that scales


def _chat(backend_name, level, **more):
    return turn_engine.TurnEngine.chat_request(
        model_id=MODEL, messages=[], tools_enabled=True, max_tokens_tools=16384, max_tokens_chat=4096,
        request_overrides={}, thinking_level=level, tools=[], backend_name=backend_name, thinking_budget=16384, **more)


def test_a_basalt_request_gets_the_thinking_room_but_not_the_budget_key():
    """Basalt 0.4.1 takes a request's thinking budget as the cap of the WHOLE reply: Ryan's test ended at 16,384
    tokens with a `write` call cut mid-arguments, where the request asked for 32,768."""
    for level, room in (("xhigh", 16384), ("low", 4096)):
        basalt, strata = _chat("basalt", level), _chat("strata", level)
        assert basalt["max_tokens"] == strata["max_tokens"] == 16384 + room      # the same total as Strata
        assert strata["extra_body"]["reasoning_budget_tokens"] == room
        assert "reasoning_budget_tokens" not in basalt["extra_body"]
        assert basalt["extra_body"]["reasoning_effort"] == level                 # the level still goes
    off = _chat("basalt", "off")
    assert off["max_tokens"] == 16384 and off["extra_body"] == {"reasoning_effort": "none"}
    compact = turn_engine.TurnEngine.compact_request(
        model_id=MODEL, messages=[], max_tokens=12288, thinking_level="xhigh", tools_enabled=True,
        backend_name="basalt", thinking_budget=16384)
    assert compact["max_tokens"] == 12288 + 16384
    assert "reasoning_budget_tokens" not in (compact.get("extra_body") or {})


# ── a tool call cut off at the output limit ──────────────────────────────────

def test_a_cut_off_call_is_named_as_that_and_other_bad_arguments_keep_their_words():
    cut = '{"path": "lamp.py", "content": "import pygame\\n'
    error = turn_engine.tool_arguments_error(cut)
    assert error.startswith("Unterminated string") and turn_engine.tool_arguments_error('{"a": 1}') is None
    assert turn_engine.tool_arguments_error("") is None and turn_engine.tool_arguments_error("[1]") == "arguments must be a JSON object"
    said = turn_engine.unusable_tool_call_result("write", error, finish_reason="length", completion_tokens=16384, size=len(cut))
    assert said.startswith("[error] the write call was cut off: the reply reached its output limit (16,384 tokens)")
    assert "Nothing was run" in said and "write a first part" in said and "Unterminated" not in said
    plain = turn_engine.unusable_tool_call_result("write", error, finish_reason="tool_calls", completion_tokens=90, size=len(cut))
    assert plain == f"[error] invalid tool arguments: {error}"


def test_broken_arguments_never_go_back_to_the_server_and_other_rows_keep_their_identity():
    """Basalt answered the next request with HTTP 400 "Unterminated string starting at: line 1 column 12", and
    would have answered every later one the same way: the conversation was dead."""
    cut = '{"content": "import pygame'
    call = lambda cid, args: {"id": cid, "type": "function", "function": {"name": "write", "arguments": args}}  # noqa: E731
    rows = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": None, "tool_calls": [call("a", '{"path": "x"}'), call("b", cut)]},
            {"role": "tool", "tool_call_id": "b", "content": "[error] cut off"},
            {"role": "assistant", "content": "fine", "tool_calls": [call("c", '{"path": "y"}')]}]
    wire = turn_engine.with_valid_tool_arguments(rows)
    assert [w is r for w, r in zip(wire, rows)] == [True, True, False, True, True]
    assert [c["function"]["arguments"] for c in wire[2]["tool_calls"]] == ['{"path": "x"}', "{}"]
    assert wire[2]["tool_calls"][0] is rows[2]["tool_calls"][0] and wire[2]["tool_calls"][1]["id"] == "b"
    assert rows[2]["tool_calls"][1]["function"]["arguments"] == cut            # the conversation keeps what was written
    healthy = rows[:2] + rows[4:]
    assert turn_engine.with_valid_tool_arguments(healthy) is healthy


# ── the start notice ─────────────────────────────────────────────────────────

def test_the_start_notice_names_a_lossy_setting_only_when_one_is_on():
    plain = BasaltBackend(Settings(backend="basalt")).start_notice
    assert plain.startswith("Starting Basalt") and "WARNING" not in plain
    lossy = BasaltBackend(Settings(backend="basalt", basalt_extra_settings='{"route-tail-skip": 7}')).start_notice
    assert lossy.startswith(plain) and "WARNING: route-tail-skip is on" in lossy and "changes what the model writes" in lossy


# ── the model chosen for the next start (what /agent-new creates an agent with) ──

def test_the_model_the_next_start_serves_is_known_before_any_server(tmp_path):
    """Ryan, testing: /model picked the pack and /agent new still said "Choose a model first"."""
    packs = tmp_path / "basalt" / "packs"
    packs.mkdir(parents=True)
    one, two, root = packs / f"{MODEL}.basalt", packs / "Other.basalt", str(tmp_path / "basalt")
    one.write_bytes(b"x")
    assert BasaltBackend(Settings(basalt_root=root)).chosen_model() == MODEL             # the only pack
    two.write_bytes(b"x")
    backend = BasaltBackend(Settings(basalt_root=root))
    assert backend.chosen_model() is None                                                # two, none chosen
    assert backend.chosen_model(Settings(basalt_root=root, basalt_config=str(two))) == "Other"   # the caller's settings
    assert BasaltBackend(Settings(basalt_root=root, basalt_config=str(packs / "gone.basalt"))).chosen_model() is None


def _pack_with(path, sections):
    """A file with a real `.basalt` header (Basalt's tools/model_file.py layout) and no payload."""
    import struct
    head = struct.pack("<8sIIQ40s", b"BASALTMF", 2, len(sections), 64 << 10, b"")
    head += b"".join(struct.pack("<64sQQ32s16s", name.encode(), 0, 0, b"", b"") for name in sections)
    path.write_bytes(head.ljust(64 << 10, b"\0"))


def test_view_image_is_told_why_basalt_takes_no_pictures_and_what_to_do(tmp_path):
    """Ryan, 2026-11-20: his Basalt was started with images off; view_image's report said "this model
    cannot see images" and read as a broken tool. With a projector in the pack it says what to do."""
    packs = tmp_path / "basalt" / "packs"
    packs.mkdir(parents=True)
    root, pack = str(tmp_path / "basalt"), packs / f"{MODEL}.basalt"
    _pack_with(pack, ["dense", "experts", "vision/mmproj", "meta"])
    started_off = BasaltBackend(Settings(basalt_root=root)).image_input_off_note()
    assert "started with image input off" in started_off and "/engine stop, then /engine start" in started_off
    assert started_off.startswith("[view_image:") and started_off.endswith("]\n")
    switched_off = BasaltBackend(Settings(basalt_root=root, basalt_vision="off")).image_input_off_note()
    assert "switched off for Basalt" in switched_off and "Automatic or dynamic" in switched_off
    backend = BasaltBackend(Settings(basalt_root=root))
    assert backend.image_input_off_note(Settings(basalt_root=root, basalt_vision="off")) == switched_off   # the caller's settings
    _pack_with(pack, ["dense", "experts", "meta"])                    # no projector: "cannot see images" is true
    assert BasaltBackend(Settings(basalt_root=root)).image_input_off_note() is None
    assert BasaltBackend(Settings(basalt_root=str(tmp_path / "nowhere"))).image_input_off_note() is None


# ── the model's own effort scale (T1398-B) ───────────────────────────────────
#
# Ryan, 2026-11-26: "double check on the litetui thinking level were sending for inklinking ... it wants a
# value 0.1-1 and i still only see worded thinking values like low med high etc ... but litetui doesnt
# display the "0.5" value like i wanted for inklinking yet". T1398-A read the scale on NInfer and
# llama.cpp; Inkling moved to Basalt (a2a5d1d5), which had no reader, so its numbers never showed.

INKLING_TEMPLATE = (__import__("pathlib").Path(__file__).parent / "fixtures" / "effort_templates"
                    / "inkling2.jinja").read_text(encoding="utf-8")
INKLING_MODEL = "Inkling-2-Mini-NVFP4-MX8"
INKLING_MAP = {"none": 0.0, "minimal": 0.1, "low": 0.2, "medium": 0.7, "high": 0.9, "xhigh": 0.99, "max": 0.99}


def _pack_holding(path, sections: dict):
    """A `.basalt` file whose sections carry bytes (the header's offsets point at them)."""
    import struct
    offset, entries, payload = 64 << 10, b"", b""
    for name, data in sections.items():
        entries += struct.pack("<64sQQ32s16s", name.encode(), offset + len(payload), len(data), b"", b"")
        payload += data
    head = struct.pack("<8sIIQ40s", b"BASALTMF", 2, len(sections), offset, b"") + entries
    path.write_bytes(head.ljust(offset, b"\0") + payload)


def _inkling_pack(tmp_path, arch="inkling2", template=INKLING_TEMPLATE):
    packs = tmp_path / "basalt" / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    pack = packs / f"{INKLING_MODEL}.basalt"
    _pack_holding(pack, {"tokenizer/chat_template.jinja": template.encode("utf-8"),
                         "meta": json.dumps({"arch": arch}).encode()})
    return str(tmp_path / "basalt"), pack


def test_a_served_inklings_scale_is_its_templates_map_and_numbers_go_as_hundredths(server, tmp_path):
    root, pack = _inkling_pack(tmp_path)
    server["props"] = {"chat_template": INKLING_TEMPLATE, "model_alias": INKLING_MODEL, "model_path": str(pack),
                       "basalt": {"version": "0.5.6", "capabilities": []}}
    backend = _backend(server["host"], basalt_root=root)
    scale = backend.effort_scale(INKLING_MODEL)
    assert scale.template_read and scale.numbers == INKLING_MAP
    assert scale.names == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    assert scale.number_wire and scale.number_step == 0.01
    assert scale.source == "the template Basalt /props reports"
    assert scale.label("high") == "high · 0.9" and scale.label("minimal") == "minimal · 0.1"
    resolved = scale.resolve(0.5)
    assert (resolved.placement, resolved.value) == ("top", 50)
    assert [p.split("?")[0] for p in server["gets"]] == ["/props"], "one read, and nothing that generates"
    assert server["gets"][0] == f"/props?model={INKLING_MODEL}"


def test_the_chosen_packs_own_template_is_read_before_any_server_answers(tmp_path):
    """/think shows the numbers before /engine start: the pack carries the template the server will serve."""
    root, pack = _inkling_pack(tmp_path)
    backend = BasaltBackend(Settings(backend="basalt", basalt_root=root, basalt_host="http://127.0.0.1:9"))
    scale = backend.effort_scale(INKLING_MODEL)
    assert scale.numbers == INKLING_MAP and scale.number_step == 0.01
    assert scale.source == f"template in {pack.name}"
    assert backend.effort_scale("Some-Other-Model") is None, "another model's pack is not this model's scale"
    assert basalt_engine.pack_template(pack) == INKLING_TEMPLATE
    assert basalt_engine.pack_template(tmp_path / "gone.basalt") is None


def test_a_model_whose_template_maps_no_numbers_keeps_the_backends_own_levels(server, tmp_path):
    """Qwen3.8 on Basalt: names only. No scale, so the turn keeps the path it has."""
    root, _pack = _inkling_pack(tmp_path, arch="qwen35", template="{%- set role = {\"user\": \"<u>\"} -%}hello")
    server["props"] = {"chat_template": "{{ messages }}", "model_alias": INKLING_MODEL, "basalt": {}}
    assert _backend(server["host"], basalt_root=root).effort_scale(INKLING_MODEL) is None
    assert BasaltBackend(Settings(backend="basalt", basalt_root=root,
                                  basalt_host="http://127.0.0.1:9")).effort_scale(INKLING_MODEL) is None


def test_an_effort_map_on_an_architecture_with_no_number_rule_takes_names_only(server, tmp_path):
    """The step is the Inkling dialect's (hundredths). A map on any other architecture gets no number wire."""
    root, pack = _inkling_pack(tmp_path, arch="qwen35")
    server["props"] = {"chat_template": INKLING_TEMPLATE, "model_alias": INKLING_MODEL, "model_path": str(pack),
                       "basalt": {}}
    scale = _backend(server["host"], basalt_root=root).effort_scale(INKLING_MODEL)
    assert scale.numbers == INKLING_MAP and scale.number_wire is False and scale.number_step is None
    assert "cannot send a number" in scale.refuse_number(0.5)


def test_a_server_that_is_not_basalt_is_not_read_for_a_template(server, tmp_path):
    root, _pack = _inkling_pack(tmp_path)
    server["props"] = {"chat_template": "{%- set m = {\"low\": 1, \"high\": 2} -%}", "model_alias": INKLING_MODEL}
    scale = _backend(server["host"], basalt_root=root).effort_scale(INKLING_MODEL)
    assert scale.numbers == INKLING_MAP and scale.source.startswith("template in "), "the pack's, not the stranger's"


def test_a_turn_on_a_served_inkling_sends_the_name_or_the_count_never_a_float(server, tmp_path):
    root, pack = _inkling_pack(tmp_path)
    server["props"] = {"chat_template": INKLING_TEMPLATE, "model_alias": INKLING_MODEL, "model_path": str(pack),
                       "basalt": {}}
    scale = _backend(server["host"], basalt_root=root).effort_scale(INKLING_MODEL)

    def sent(level):
        req = turn_engine.TurnEngine.chat_request(
            model_id=INKLING_MODEL, messages=[], tools_enabled=False, max_tokens_tools=1, max_tokens_chat=1,
            request_overrides={}, thinking_level=level, backend_name="basalt", effort_scale=scale)
        return req.get("extra_body", {})

    assert sent("high") == {"reasoning_effort": "high"}
    assert sent("minimal") == {"reasoning_effort": "minimal"}, "0.1 reaches Inkling; it was folded to low (0.2)"
    assert sent(0.5) == {"reasoning_effort": 50}
    assert sent("off") == {"chat_template_kwargs": {"reasoning_effort": "none"}}


def test_strata_answers_the_same_question_from_its_configs_model_name(tmp_path):
    root = tmp_path / "strata"
    root.mkdir()
    named, bare = root / "strata-iq3_s.json", root / "strata-bare.json"
    named.write_text(json.dumps({"exe": "strata.exe", "args": [], "model_name": "qwen3.8-flash-next-iq3_s"}), encoding="utf-8")
    bare.write_text(json.dumps({"exe": "strata.exe", "args": []}), encoding="utf-8")
    backend = StrataBackend(Settings(strata_root=str(root), strata_config=str(named)))
    assert backend.chosen_model() == "qwen3.8-flash-next-iq3_s"
    assert backend.chosen_model(Settings(strata_root=str(root), strata_config=str(bare))) is None   # names none: not guessed
    assert StrataBackend(Settings(strata_root=str(root))).chosen_model() is None          # two, none chosen


# ── the Settings dialog ──────────────────────────────────────────────────────

BASALT_FIELDS = ("basalt_host", "basalt_root", "basalt_config", "basalt_packs_dir", "basalt_max_context", "basalt_kv",
                 "basalt_vram_reserve_mib", "basalt_expert_cache", "basalt_conversation_cache_mib",
                 "basalt_spec", "basalt_vision", "basalt_extra_settings")


def _dialog(settings):
    from textual.app import App

    from litetui.settings_screen import SettingsScreen

    class Host(App):
        def on_mount(self):
            self.push_screen(SettingsScreen(settings))

    return Host()


@pytest.mark.asyncio
async def test_the_dialog_has_a_control_for_every_basalt_field_and_saves_them_typed():
    """A Settings field with no control makes the whole dialog refuse to save."""
    from textual.widgets import Input, Select

    from litetui.settings_screen import SettingsBody

    app = _dialog(Settings())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        for name in BASALT_FIELDS:
            body.query_one(f"#f-{name}")
        untouched = body._collect()
        assert [getattr(untouched, name) for name in BASALT_FIELDS] == ["", "", "", "", None, None, None, None, None, None, None, None]
        body.query_one("#f-basalt_packs_dir", Input).value = "G:/Models/litesuite/basalt"
        body.query_one("#f-basalt_max_context", Input).value = "150000"
        body.query_one("#f-basalt_kv", Select).value = "k8v4"
        body.query_one("#f-basalt_vram_reserve_mib", Input).value = "2000"
        body.query_one("#f-basalt_expert_cache", Input).value = "auto"
        body.query_one("#f-basalt_vision", Select).value = "dynamic"
        body.query_one("#f-basalt_extra_settings", Input).value = '{"prefill": "auto:16384"}'
        out = body._collect()
    assert (out.basalt_max_context, out.basalt_kv, out.basalt_vram_reserve_mib, out.basalt_expert_cache,
            out.basalt_vision, out.basalt_extra_settings) == (150000, "k8v4", 2000, "auto", "dynamic", '{"prefill": "auto:16384"}')
    assert out.basalt_packs_dir == "G:/Models/litesuite/basalt"


@pytest.mark.asyncio
async def test_a_bad_basalt_value_blocks_the_save_by_name_and_saved_values_come_back():
    from textual.widgets import Input, Select

    from litetui.settings_screen import SettingsBody

    app = _dialog(Settings(basalt_vram_reserve_mib=1500, basalt_kv="q4_0", basalt_spec=4))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        assert body.query_one("#f-basalt_vram_reserve_mib", Input).value == "1500"
        assert body.query_one("#f-basalt_kv", Select).value == "q4_0"
        assert body.query_one("#f-basalt_spec", Input).value == "4"
        body.query_one("#f-basalt_expert_cache", Input).value = "lots"
        with pytest.raises(ValueError, match="basalt_expert_cache"):
            body._collect()
        body.query_one("#f-basalt_expert_cache", Input).value = ""
        body.query_one("#f-basalt_extra_settings", Input).value = "route-tail-skip"
        with pytest.raises(ValueError, match="basalt_extra_settings: not valid JSON"):
            body._collect()


# ── the command line ─────────────────────────────────────────────────────────

def test_cli_launch_options_map_onto_the_basalt_settings():
    start = LaunchOptions(base_url="http://127.0.0.1:8091", server_mode="start",
                          context_length=150000, model_path="D:/Example/basalt/packs/model.basalt")
    assert start.overrides(Settings(), "basalt", None) == {
        "basalt_host": "http://127.0.0.1:8091", "basalt_max_context": 150000,
        "basalt_config": "D:/Example/basalt/packs/model.basalt"}
    with pytest.raises(ValueError, match="Basalt context is fixed at startup"):
        LaunchOptions(context_length=131072).overrides(Settings(), "basalt", None)
    with pytest.raises(ValueError, match="Basalt has no server executable"):
        LaunchOptions(server_executable="x.exe", server_mode="start").overrides(Settings(), "basalt", None)
