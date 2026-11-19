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
            }.get(self.path)
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
    assert len(fields) == 11
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


# ── the start notice ─────────────────────────────────────────────────────────

def test_the_start_notice_names_a_lossy_setting_only_when_one_is_on():
    plain = BasaltBackend(Settings(backend="basalt")).start_notice
    assert plain.startswith("Starting Basalt") and "WARNING" not in plain
    lossy = BasaltBackend(Settings(backend="basalt", basalt_extra_settings='{"route-tail-skip": 7}')).start_notice
    assert lossy.startswith(plain) and "WARNING: route-tail-skip is on" in lossy and "changes what the model writes" in lossy


# ── the Settings dialog ──────────────────────────────────────────────────────

BASALT_FIELDS = ("basalt_host", "basalt_root", "basalt_config", "basalt_max_context", "basalt_kv",
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
        assert [getattr(untouched, name) for name in BASALT_FIELDS] == ["", "", "", None, None, None, None, None, None, None, None]
        body.query_one("#f-basalt_max_context", Input).value = "150000"
        body.query_one("#f-basalt_kv", Select).value = "k8v4"
        body.query_one("#f-basalt_vram_reserve_mib", Input).value = "2000"
        body.query_one("#f-basalt_expert_cache", Input).value = "auto"
        body.query_one("#f-basalt_vision", Select).value = "dynamic"
        body.query_one("#f-basalt_extra_settings", Input).value = '{"prefill": "auto:16384"}'
        out = body._collect()
    assert (out.basalt_max_context, out.basalt_kv, out.basalt_vram_reserve_mib, out.basalt_expert_cache,
            out.basalt_vision, out.basalt_extra_settings) == (150000, "k8v4", 2000, "auto", "dynamic", '{"prefill": "auto:16384"}')


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
