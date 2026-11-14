"""T1306-E (prefix-cache audit P8): the prompt cache across an owned-engine restart.

Nothing here starts an engine. The only server is a thread in this process that models what the
audit read from source: Strata's `POST /slots/0?action=save|restore {"filename"}` and the llama.cpp
router's `POST /slots/:id?action=... {"model", "filename"}`. Its `timings.cache_n` is computed from
what it holds live, so the first chat request after a "restart" is answered as cached only if the
restore really came first. Requests go through the real OpenAI client and `OpenAITransport`, so
the bytes compared are the bytes on the wire.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from openai import AsyncOpenAI

from litetui import model_transport, prefix_persist, strata_engine, strata_options
from litetui.llm_backend import LlamaCppBackend, _Owned
from litetui.prefix_persist import SaveStore, prompt_key
from litetui.settings import Settings
from litetui.strata_backend import StrataBackend

TOKENS_PER_MESSAGE = 1000          # the fake's tokenizer: every message is 1000 tokens
LOAD_ROUTES = ("/models/load", "/v1/load")


# ── the fake engine ─────────────────────────────────────────────────────────

class FakeEngine:
    """One engine process. `restart()` is a new process: everything live is gone, files stay."""

    def __init__(self, kind: str, folder: Path, model: str = "m1") -> None:
        self.kind, self.folder, self.model = kind, Path(folder), model
        self.calls: list[tuple[str, str, bytes]] = []
        self.loaded = True
        self.chat_delay_s = 0.0           # delay for max_tokens == 1 requests (the replay)
        self.save_delay_s = 0.0
        self.restart()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                engine.calls.append(("GET", self.path, b""))
                self._json(*engine.get(self.path))

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                engine.calls.append(("POST", self.path, raw))
                if urlsplit(self.path).path == "/v1/chat/completions":
                    return engine.chat(self, json.loads(raw))
                self._json(*engine.post(self.path, json.loads(raw or b"{}")))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def restart(self) -> None:
        n = 4 if self.kind == "llama" else 1
        self.slots = [{"messages": [], "tokens": 0} for _ in range(n)]
        self.live_slot = 0

    # -- routes ---------------------------------------------------------------

    def get(self, path: str):
        route = urlsplit(path).path
        if route == "/health":
            return 200, {"status": "ok", "loaded": self.loaded}
        if route == "/models":
            return 200, {"data": [{"id": self.model, "status": {"value": "loaded" if self.loaded else "unloaded"}}]}
        if route == "/slots":
            return 200, [{"id": i, "n_ctx": 150000, "is_processing": False, "n_prompt_tokens": s["tokens"]}
                         for i, s in enumerate(self.slots)]
        return 404, {"error": {"message": "not found"}}

    def post(self, path: str, body: dict):
        parts = urlsplit(path)
        if parts.path in LOAD_ROUTES:
            return 500, {"error": {"message": "a test engine never loads"}}
        if not parts.path.startswith("/slots/"):
            return 404, {"error": {"message": "not found"}}
        if self.kind == "llama" and body.get("model") != self.model:
            return 400, {"error": {"message": "model name is missing from the request"}}
        slot = int(parts.path.rsplit("/", 1)[1])
        action = parse_qs(parts.query).get("action", [""])[0]
        target = self.folder / body["filename"]
        if action == "save":
            held = self.slots[slot]
            target.write_text(json.dumps({"partial": True}))      # the engine writes as it goes
            if self.save_delay_s:
                time.sleep(self.save_delay_s)
            target.write_text(json.dumps(held))
            return 200, {"id_slot": slot, "filename": body["filename"], "n_saved": held["tokens"],
                         "n_written": target.stat().st_size}
        if action == "restore":
            if not target.is_file():
                return 404, {"error": {"message": "no saved session"}}
            self.slots[slot] = json.loads(target.read_text())
            return 200, {"id_slot": slot, "filename": body["filename"], "n_restored": self.slots[slot]["tokens"]}
        return 400, {"error": {"message": "action must be save or restore"}}

    def chat(self, handler, body: dict) -> None:
        if body.get("max_tokens") == 1 and self.chat_delay_s:
            time.sleep(self.chat_delay_s)
        messages = body["messages"]
        # Like an engine: reuse the slot whose held messages share the longest prefix.
        def shared(s):
            n = 0
            for a, b in zip(s["messages"], messages):
                if a != b:
                    break
                n += 1
            return n
        best = max(range(len(self.slots)), key=lambda i: (shared(self.slots[i]), -i))
        if shared(self.slots[best]) == 0:
            best = next((i for i, s in enumerate(self.slots) if not s["tokens"]), 0)
        cache_n = shared(self.slots[best]) * TOKENS_PER_MESSAGE
        prompt = len(messages) * TOKENS_PER_MESSAGE
        self.slots[best] = {"messages": messages, "tokens": prompt}
        self.live_slot = best
        chunks = [
            {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [],
             "usage": {"prompt_tokens": prompt, "completion_tokens": 1, "total_tokens": prompt + 1},
             "timings": {"cache_n": cache_n, "prompt_n": prompt - cache_n}},
        ]
        raw = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    # -- what the tests read --------------------------------------------------

    def routes(self) -> list[str]:
        out = []
        for method, path, raw in self.calls:
            parts = urlsplit(path)
            if parts.path == "/v1/chat/completions":
                out.append("chat1" if json.loads(raw).get("max_tokens") == 1 else "chat")
            elif parts.path.startswith("/slots/"):
                out.append(parse_qs(parts.query)["action"][0])
            else:
                out.append(f"{method} {parts.path}")
        return out

    def bodies(self, route: str = "/v1/chat/completions") -> list[bytes]:
        return [raw for m, p, raw in self.calls if urlsplit(p).path == route]

    def close(self) -> None:
        self.httpd.shutdown()


# ── fixtures ────────────────────────────────────────────────────────────────

def _tools():
    return [{"type": "function", "function": {"name": "read_file", "description": "read",
                                              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def _main_kwargs(model: str, n_user: int = 1) -> dict:
    msgs = [{"role": "system", "content": "BASE + MEMORY convo-1 + TOOLS"}]
    for i in range(n_user):
        msgs.append({"role": "user", "content": f"question {i}"})
        if i < n_user - 1:
            msgs.append({"role": "assistant", "content": f"answer {i}", "reasoning_content": "thinking"})
    return {"model": model, "messages": msgs, "stream": True, "max_tokens": 16384,
            "stream_options": {"include_usage": True}, "tools": _tools(),
            "extra_body": {"reasoning_effort": "medium", "chat_template_kwargs": {"enable_thinking": True},
                           "top_k": 20}}


def _side_kwargs(model: str) -> dict:
    """The card summary's shape: its own short prompt, reasoning off, no tools."""
    return {"model": model, "messages": [{"role": "user", "content": "Summarise in one line: ok"}],
            "max_tokens": 64, "stream": True, "stream_options": {"include_usage": True},
            "extra_body": {"reasoning_effort": "none"}}


class FakeProc:
    pid = 4242

    def poll(self):
        return None


def _app(backend):
    client = AsyncOpenAI(base_url=backend.base_url(), api_key="x", max_retries=0)
    said: list[str] = []
    app = SimpleNamespace(backend=backend, client=client, system_message=said.append, said=said)
    app._client_binding = model_transport.bind_client(client, backend)
    return app


async def _send(app, kwargs) -> tuple[dict, dict]:
    """One request through the app's transport: (usage dict, timings dict)."""
    stream = await model_transport.for_app(app).create(**kwargs)
    usage, timings = {}, {}
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = {"prompt_tokens": chunk.usage.prompt_tokens, "completion_tokens": chunk.usage.completion_tokens}
        t = getattr(chunk, "timings", None) or (chunk.model_extra or {}).get("timings")
        if t:
            timings = dict(t)
    return usage, timings


def _strata_owned(tmp_path: Path, url: str, ctx: int = 150000):
    """An OwnedEngine as strata_engine.start leaves it: argv of serve/server.py with --config."""
    root = tmp_path / "strata"
    python = root / ".venv" / "python.exe"
    exe = root / "engine" / "strata.exe"
    native = tmp_path / "model-00001-of-00002.gguf"
    # The install is written once: a restart does not touch it, and its files' mtimes are identity.
    for path, data in ((root / "serve" / "server.py", b""), (python, b""), (exe, b"MZ"), (native, b"\0" * 1000)):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    cfg = tmp_path / "strata-iq3.litetui.json"
    cfg.write_text(json.dumps({"exe": str(exe), "model_name": "flash", "port": 8080,
                               "args": ["--native", str(native), "--max-context", str(ctx), "--kv", "int8"]}))
    argv = (str(python), str(root / "serve" / "server.py"), "--engine", "strata", "--config", str(cfg),
            "--port", url.rsplit(":", 1)[1])
    return strata_engine.OwnedEngine(proc=FakeProc(), host=url, log_path=tmp_path / "s.log", log_file=None,
                                     model_id="flash", args=argv, registered=False)


@pytest.fixture
def strata(tmp_path):
    settings = Settings(backend="strata", slot_persist_dir=str(tmp_path / "saves"))
    backend = StrataBackend(settings)
    folder = prefix_persist.persist_root(settings) / "strata"
    prefix_persist.ensure_folder(folder)
    engine = FakeEngine("strata", folder, model="flash")

    def start(ctx: int = 150000):
        """What `start_engine` does on a successful spawn: publish the handle, then note_start."""
        backend._owned = _strata_owned(tmp_path, engine.url, ctx)
        backend._host = engine.url
        backend._persist_event("note_start")

    start()
    yield SimpleNamespace(backend=backend, engine=engine, folder=folder, start=start, settings=settings)
    engine.close()


def _records(folder: Path) -> list[dict]:
    return SaveStore(folder).records()


def _lt_files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("lt-")) if folder.is_dir() else []


def _ordered(raw: bytes) -> list:
    """A request body as ORDERED pairs: the engine renders key order as sent (ordered_json)."""
    return json.loads(raw, object_pairs_hook=list)


def _without(pairs: list, key: str) -> list:
    return [kv for kv in pairs if kv[0] != key]


def run(coro):
    return asyncio.run(coro)


# ── 1. clean stop -> save with the identity record ──────────────────────────

def test_clean_stop_saves_the_main_conversation_with_its_identity_record(strata):
    app = _app(strata.backend)
    kwargs = _main_kwargs("flash")

    async def go():
        usage, _ = await _send(app, kwargs)
        prefix_persist.note_main(strata.backend, kwargs, usage)
        return await prefix_persist.save_before_stop(app, strata.backend)

    said = run(go())
    assert said and said.startswith("saved the conversation cache")
    # The live state was the main request, so nothing was re-sent: slots, then save.
    assert strata.engine.routes()[-2:] == ["GET /slots", "save"]
    [rec] = _records(strata.folder)
    assert rec["prefix"] == prompt_key(kwargs) and rec["messages"] == len(kwargs["messages"])
    assert rec["tokens"] == rec["prompt_tokens"] == len(kwargs["messages"]) * TOKENS_PER_MESSAGE
    ident = rec["identity"]
    assert "--max-context" in ident["config"]["args"] and "150000" in ident["config"]["args"]
    files = {Path(f["path"]).name: f for f in ident["files"]}
    assert {"strata.exe", "model-00001-of-00002.gguf"} <= set(files)
    assert all({"size", "mtime_ns"} <= set(f) for f in files.values())
    assert "<config>" in ident["launch"]          # the copy's path is not identity; its content is


# ── 2. next start -> restore BEFORE the first chat request (the boundary) ────

def _resume_kwargs(model: str, prior: dict) -> dict:
    nxt = json.loads(json.dumps(prior))
    nxt["messages"] += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "and now?"}]
    return nxt


def test_restore_runs_before_the_first_chat_request_and_that_order_is_the_cache_boundary(strata):
    app = _app(strata.backend)
    first = _main_kwargs("flash")

    async def session_one():
        usage, _ = await _send(app, first)
        prefix_persist.note_main(strata.backend, first, usage)
        await prefix_persist.save_before_stop(app, strata.backend)

    run(session_one())
    strata.engine.restart()                     # a new engine process: nothing live
    strata.start()
    strata.engine.calls.clear()
    nxt = _resume_kwargs("flash", first)

    async def session_two():
        await prefix_persist.restore_before_main(app, nxt)
        return await _send(app, nxt)

    usage, timings = run(session_two())
    assert strata.engine.routes() == ["GET /health", "GET /slots", "restore", "chat"]
    assert timings["cache_n"] == len(first["messages"]) * TOKENS_PER_MESSAGE > 0
    assert not any(p.startswith(LOAD_ROUTES) for _, p, _ in strata.engine.calls)
    assert any("restored the conversation cache" in s for s in app.said)


def test_control_without_the_restore_the_same_first_request_is_answered_cold(strata):
    app = _app(strata.backend)
    first = _main_kwargs("flash")

    async def session_one():
        usage, _ = await _send(app, first)
        prefix_persist.note_main(strata.backend, first, usage)
        await prefix_persist.save_before_stop(app, strata.backend)

    run(session_one())
    strata.engine.restart()
    strata.start()
    _usage, timings = run(_send(app, _resume_kwargs("flash", first)))      # no restore_before_main
    assert timings["cache_n"] == 0


# ── 3. CONTROL: identity mismatch -> no restore, stale save deleted ──────────

def test_an_identity_mismatch_deletes_the_save_and_restores_nothing(strata):
    app = _app(strata.backend)
    first = _main_kwargs("flash")

    async def session_one():
        usage, _ = await _send(app, first)
        prefix_persist.note_main(strata.backend, first, usage)
        await prefix_persist.save_before_stop(app, strata.backend)

    run(session_one())
    assert len(_records(strata.folder)) == 1
    strata.engine.restart()
    strata.start(ctx=100000)                    # the next start runs another context
    strata.engine.calls.clear()
    _usage, timings = run(_restore_then_send(app, _resume_kwargs("flash", first)))
    assert "restore" not in strata.engine.routes()
    assert timings["cache_n"] == 0
    assert _records(strata.folder) == [] and _lt_files(strata.folder) == []


async def _restore_then_send(app, kwargs):
    await prefix_persist.restore_before_main(app, kwargs)
    return await _send(app, kwargs)


def test_a_request_that_does_not_continue_the_saved_prompt_restores_nothing(strata):
    app = _app(strata.backend)
    first = _main_kwargs("flash")

    async def session_one():
        usage, _ = await _send(app, first)
        prefix_persist.note_main(strata.backend, first, usage)
        await prefix_persist.save_before_stop(app, strata.backend)

    run(session_one())
    strata.engine.restart()
    strata.start()
    strata.engine.calls.clear()
    other = _main_kwargs("flash")
    other["messages"][0]["content"] = "BASE + MEMORY convo-2 + TOOLS"     # another conversation
    run(_restore_then_send(app, other))
    assert "restore" not in strata.engine.routes()
    assert len(_records(strata.folder)) == 1                          # not stale, just not this one


# ── 4. the card summary holds the engine: replay the main request, byte for byte ──

def test_a_side_call_left_live_is_replaced_by_a_byte_identical_replay_then_saved(strata):
    app = _app(strata.backend)
    main, side = _main_kwargs("flash", n_user=2), _side_kwargs("flash")

    async def go():
        usage, _ = await _send(app, main)
        prefix_persist.note_main(strata.backend, main, usage)
        await _send(app, side)                         # the card summary, after the turn
        return await prefix_persist.save_before_stop(app, strata.backend)

    said = run(go())
    assert said and said.startswith("saved")
    assert strata.engine.routes()[-4:] == ["GET /slots", "chat1", "GET /slots", "save"]
    main_raw, side_raw, replay_raw = strata.engine.bodies()
    main_body, replay_body = _ordered(main_raw), _ordered(replay_raw)
    assert dict(replay_body)["max_tokens"] == 1 and dict(main_body)["max_tokens"] == 16384
    # Same bytes apart from max_tokens: messages, tools, extra_body keys, and their ORDER.
    assert _without(replay_body, "max_tokens") == _without(main_body, "max_tokens")
    # CONTROL: the same comparison tells a different request apart.
    assert _without(_ordered(side_raw), "max_tokens") != _without(main_body, "max_tokens")
    [rec] = _records(strata.folder)
    assert rec["tokens"] == len(main["messages"]) * TOKENS_PER_MESSAGE


def test_replay_over_the_cap_saves_nothing(tmp_path):
    settings = Settings(backend="strata", slot_persist_dir=str(tmp_path / "saves"), slot_persist_replay_s=1)
    backend = StrataBackend(settings)
    folder = prefix_persist.persist_root(settings) / "strata"
    prefix_persist.ensure_folder(folder)
    engine = FakeEngine("strata", folder, model="flash")
    try:
        backend._owned, backend._host = _strata_owned(tmp_path, engine.url), engine.url
        backend._persist_event("note_start")
        app = _app(backend)
        main = _main_kwargs("flash")

        async def go():
            usage, _ = await _send(app, main)
            prefix_persist.note_main(backend, main, usage)
            await _send(app, _side_kwargs("flash"))
            engine.chat_delay_s = 3.0
            started = time.monotonic()
            said = await prefix_persist.save_before_stop(app, backend)
            return said, time.monotonic() - started

        said, took = run(go())
        assert said and "over 1 s" in said
        assert took < 2.5
        assert "save" not in engine.routes() and _lt_files(folder) == []
    finally:
        engine.close()


# ── quitting: the save runs first; a second quit skips it, a partial is deleted ──

def test_quit_waits_for_the_save_then_exits_exactly_once(strata):
    app = _app(strata.backend)
    exits = []

    async def go():
        usage, _ = await _send(app, _main_kwargs("flash"))
        prefix_persist.note_main(strata.backend, _main_kwargs("flash"), usage)
        assert prefix_persist.defer_exit(app, lambda: exits.append("first")) is True
        assert exits == []                                     # deferred behind the save
        await app._slot_quit_task
        assert prefix_persist.defer_exit(app, lambda: exits.append("again")) is False

    run(go())
    assert exits == ["first"]
    assert any("quit again or Ctrl+C to skip" in s for s in app.said)
    assert len(_records(strata.folder)) == 1


def test_a_second_quit_skips_the_save_at_once_and_the_partial_save_is_deleted(strata):
    app = _app(strata.backend)
    exits = []
    strata.engine.save_delay_s = 1.5

    async def go():
        usage, _ = await _send(app, _main_kwargs("flash"))
        prefix_persist.note_main(strata.backend, _main_kwargs("flash"), usage)
        prefix_persist.defer_exit(app, lambda: exits.append("first"))
        for _ in range(100):                                   # until the engine is writing
            await asyncio.sleep(0.02)
            if _lt_files(strata.folder):
                break
        assert _lt_files(strata.folder), "the save never started"
        assert prefix_persist.quit_save_running(app)
        started = time.monotonic()
        assert prefix_persist.defer_exit(app, lambda: exits.append("second")) is True
        assert exits == ["second"] and time.monotonic() - started < 0.2
        await asyncio.sleep(2.0)                               # the engine finishes its write

    run(go())
    assert exits == ["second"]                                # the first exit never ran
    assert _records(strata.folder) == [] and _lt_files(strata.folder) == []


def test_a_partial_left_by_an_interrupted_save_is_swept_when_the_engine_stops(strata):
    (strata.folder / "lt-0123.state").write_text("{}")         # no record: never verified
    (strata.folder / "lt-0123.state.tmp").write_text("{}")
    strata.backend._owned = None
    strata.backend._persist_event("note_stopped")
    assert _lt_files(strata.folder) == []


FOREIGN = ("notes.txt", "model.gguf", "lt-notes.txt", "lt-mine.state", "lt-0123.statex", "LT-0123.state")


def test_a_users_own_files_survive_the_sweep_and_the_cap(tmp_path):
    """Prefix 8fb5d719: slot_persist_dir can name any folder. Only lt-<hex>.state|.json[.suffix] is ours."""
    folder = prefix_persist.ensure_folder(tmp_path / "saves" / "strata")
    for name in FOREIGN:
        (folder / name).write_bytes(b"theirs" * 100)
    store = SaveStore(folder)
    store.state("lt-0aa").write_bytes(b"\0" * 1000)
    store.commit({"schema": prefix_persist.SCHEMA, "name": "lt-0aa", "created": 1})
    (folder / "lt-0bb.state").write_text("{}")                      # partial: ours, unpaired
    assert store.sweep() == ["lt-0bb.state"]
    assert store.enforce_cap(0) == ["lt-0aa"]                       # the cap empties OUR saves only
    assert sorted(p.name for p in folder.iterdir()) == sorted(FOREIGN + (prefix_persist.MARKER,))


def test_a_folder_litetui_did_not_create_is_never_swept(tmp_path):
    folder = tmp_path / "users-folder"
    folder.mkdir()
    (folder / "lt-0123.state").write_text("{}")                     # looks like our partial
    assert prefix_persist.ensure_folder(folder) == folder
    assert not (folder / prefix_persist.MARKER).exists()            # an existing folder is not claimed
    assert SaveStore(folder).sweep() == []
    assert (folder / "lt-0123.state").is_file()


def test_control_a_partial_in_litetuis_own_folder_is_still_removed(tmp_path):
    folder = prefix_persist.ensure_folder(tmp_path / "saves" / "llama")
    assert (folder / prefix_persist.MARKER).is_file()
    (folder / "lt-0123.state").write_text("{}")
    (folder / "lt-0123.json.tmp").write_text("{}")
    assert sorted(SaveStore(folder).sweep()) == ["lt-0123.json.tmp", "lt-0123.state"]


# ── attached engines, the cap, loads ────────────────────────────────────────

def test_an_attached_server_gets_no_save_or_restore_call(strata):
    strata.backend._owned = None                 # LiteSuite's or a hand-started server
    app = _app(strata.backend)
    kwargs = _main_kwargs("flash")

    async def go():
        usage, _ = await _send(app, kwargs)
        prefix_persist.note_main(strata.backend, kwargs, usage)
        assert prefix_persist.defer_exit(app, lambda: None) is False
        assert await prefix_persist.save_before_stop(app, strata.backend) is None
        await prefix_persist.restore_before_main(app, kwargs)

    run(go())
    assert [r for r in strata.engine.routes() if r != "chat"] == []


def test_the_size_cap_evicts_the_oldest_save_first(tmp_path):
    store = SaveStore(tmp_path)
    for i, name in enumerate(("lt-0a", "lt-0b", "lt-0c")):
        store.state(name).write_bytes(b"\0" * 1000)
        store.commit({"schema": prefix_persist.SCHEMA, "name": name, "created": 100 + i})
    over = sum(store.size(n) for n in ("lt-0a", "lt-0b", "lt-0c")) - 1
    assert store.enforce_cap(over, keep="lt-0a") == ["lt-0b"]     # oldest that is not the new save
    assert [r["name"] for r in store.records()] == ["lt-0a", "lt-0c"]
    assert store.enforce_cap(store.size("lt-0a") - 1, keep="lt-0a") == ["lt-0c", "lt-0a"]


def test_restore_never_loads_a_model_it_skips_when_the_model_is_unloaded(strata):
    app = _app(strata.backend)
    first = _main_kwargs("flash")

    async def session_one():
        usage, _ = await _send(app, first)
        prefix_persist.note_main(strata.backend, first, usage)
        await prefix_persist.save_before_stop(app, strata.backend)

    run(session_one())
    strata.engine.restart()
    strata.engine.loaded = False
    strata.start()
    strata.engine.calls.clear()
    run(prefix_persist.restore_before_main(app, _resume_kwargs("flash", first)))
    assert "restore" not in strata.engine.routes()
    assert not any(p.startswith(LOAD_ROUTES) for _, p, _ in strata.engine.calls)


# ── llama.cpp router ────────────────────────────────────────────────────────

def test_llama_router_saves_the_slot_holding_the_conversation_and_restores_through_the_router(tmp_path):
    gguf = tmp_path / "m1.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 100)
    ini = tmp_path / "litetui-models.ini"
    ini.write_text(f"[m1]\nmodel = {gguf.as_posix()}\nctx-size = 65536\n\n[m2]\nmodel = x\n")
    settings = Settings(backend="llamacpp", slot_persist_llama=True, slot_persist_dir=str(tmp_path / "saves"))
    folder = prefix_persist.persist_root(settings) / "llama"
    prefix_persist.ensure_folder(folder)
    engine = FakeEngine("llama", folder, model="m1")
    try:
        backend = LlamaCppBackend(settings)
        backend._host = engine.url

        def start():
            backend._owned = _Owned(FakeProc(), tmp_path / "l.log", None, ini=ini)
            backend.slot_persistence.note_start()

        start()
        app = _app(backend)
        engine.slots[0] = {"messages": [{"role": "user", "content": "x"}], "tokens": 1000}   # a side slot
        first = _main_kwargs("m1")

        async def session_one():
            usage, _ = await _send(app, first)
            prefix_persist.note_main(backend, first, usage)
            return await prefix_persist.save_before_stop(app, backend)

        assert run(session_one()).startswith("saved")
        main_slot = engine.live_slot
        save = [(p, json.loads(raw)) for m, p, raw in engine.calls if "action=save" in p]
        assert save[0][0] == f"/slots/{main_slot}?action=save" and save[0][1]["model"] == "m1"
        [rec] = _records(folder)
        assert rec["identity"]["section"] == [f"model = {gguf.as_posix()}", "ctx-size = 65536"]

        engine.restart()
        start()
        engine.calls.clear()
        nxt = _resume_kwargs("m1", first)
        _usage, timings = run(_restore_then_send(app, nxt))
        assert engine.routes() == ["GET /models", "GET /slots", "restore", "chat"]
        assert timings["cache_n"] == len(first["messages"]) * TOKENS_PER_MESSAGE
        assert not any(p.startswith(LOAD_ROUTES) for _, p, _ in engine.calls)
    finally:
        engine.close()


# ── settings and launch ─────────────────────────────────────────────────────

def test_defaults_strata_on_llama_off_and_xolotl_never():
    s = Settings()
    assert s.slot_persist_strata is True and s.slot_persist_llama is False
    assert s.slot_persist_cap_gib == 32 and s.slot_persist_replay_s == 10
    from litetui.xolotl_backend import XolotlBackend
    assert XolotlBackend(s).slot_persistence.engine.why_off()


def test_strata_launch_passes_the_save_folder_to_the_server_only_when_on(tmp_path):
    on = Settings(slot_persist_dir=str(tmp_path / "saves"))
    assert prefix_persist.strata_launch_args(on) == ["--slot-save-path", str((tmp_path / "saves" / "strata").resolve())]
    assert (tmp_path / "saves" / "strata").is_dir()                     # the server requires a directory
    for off in (Settings(slot_persist_strata=False), Settings(strata_slot_save_path="D:/mine"),
                Settings(strata_parallel=2)):
        assert prefix_persist.strata_launch_args(off) == []
    assert prefix_persist.strata_launch_args(on, backend_name="xolotl") == []


def test_strata_launch_plan_carries_the_flag_and_still_serves_the_install_file(tmp_path):
    root = tmp_path / "strata"
    python = strata_engine.server_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / "serve").mkdir()
    (root / "serve" / "server.py").write_text("")
    cfg = root / "strata-a.json"
    cfg.write_text(json.dumps({"exe": "strata.exe", "port": 8080, "args": ["--max-context", "8192"]}))
    # Every catalog option blank (T1306 integration): T1306-A gives two of them defaults, and this test pins a launch
    # with NO option set, which serves the install's own file (test_strata_options does the same).
    blank = {name: None for name in strata_options.FIELD_NAMES}
    s = Settings(**blank, strata_root=str(root), slot_persist_dir=str(tmp_path / "saves"))
    argv, _cwd, run_cfg, _host = strata_engine.launch_plan(s)
    assert argv[argv.index("--slot-save-path") + 1] == str((tmp_path / "saves" / "strata").resolve())
    assert argv[argv.index("--config") + 1] == str(cfg) and "slot_save_path" not in run_cfg
    off_argv, *_ = strata_engine.launch_plan(Settings(**blank, strata_root=str(root), slot_persist_strata=False))
    assert "--slot-save-path" not in off_argv


def test_llama_router_argv_carries_slot_save_path_only_when_on(tmp_path):
    assert prefix_persist.llama_launch_args(Settings()) == []
    on = Settings(slot_persist_llama=True, slot_persist_dir=str(tmp_path / "s"))
    assert prefix_persist.llama_launch_args(on, lambda: frozenset({"slot-save-path"})) == [
        "--slot-save-path", str((tmp_path / "s" / "llama").resolve())]
    assert prefix_persist.llama_launch_args(on, lambda: frozenset({"ctx-size"})) == []   # build without it
