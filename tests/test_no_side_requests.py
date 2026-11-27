"""T1433: a session sends the serving model its turns and NOTHING else.

Ryan 2026-11-26 (via Sentinel b5049a76): "i deft never wanted a extra call for it ... and espically
one that clears the cache". On a one-slot engine (Basalt) a short side request evicts the
conversation's cached prefix and the next turn re-reads all of it (~330K tokens, ~6 min a hit).

A fake OpenAI-compatible server logs EVERY request. A real LiteTUI (owned_app, LlamaCppBackend,
AsyncOpenAI) runs three turns through `_stream`, the way a user turn runs: one answer ends with a
<recap>, two do not. Asserted: exactly one chat request per turn, and each turn's messages are the
previous turn's messages plus the new ones (prefix-stable, so a real server reuses its cache).
T1433_LOG=<file> writes the request log.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _owned_seat import owned_app
from openai import AsyncOpenAI

from litetui import model_transport
from litetui.llm_backend import LlamaCppBackend
from litetui.settings import Settings
from litetui.widgets import AssistantMessage

MODEL = "inkling-2-mini"
ANSWERS = [
    "Here is the plan for the parser.\n\nIt reads each line once.\n\n<recap>Planned the parser\nOne pass per line</recap>",
    "## Result\n\nThe second answer has no recap tag at all.",
    "Third answer, also without a recap.",
]


class LoggingEngine:
    """Answers chat completions from a script and logs every request it receives."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.log: list[dict] = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _json(self, body, status=200):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                engine.log.append({"method": "GET", "path": self.path})
                if self.path.startswith("/props"):
                    return self._json({"model_path": "x", "chat_template": "{{ messages }}"})
                if self.path in ("/models", "/v1/models"):
                    return self._json({"data": [{"id": MODEL, "max_model_len": 200000}]})
                if self.path == "/health":
                    return self._json({"status": "ok"})
                self._json({"error": "not here"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                messages = body.get("messages") or []
                engine.log.append({"method": "POST", "path": self.path, "stream": bool(body.get("stream")),
                                   "max_tokens": body.get("max_tokens"), "messages": messages,
                                   "prompt_chars": sum(len(json.dumps(m)) for m in messages)})
                if self.path != "/v1/chat/completions":
                    return self._json({"error": "not here"}, 404)
                answer = engine.answers.pop(0) if engine.answers else "side answer"
                if not body.get("stream"):
                    return self._json({"id": "x", "object": "chat.completion", "model": MODEL, "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def chunk(delta, finish=None, usage=None):
                    data = {"id": "c", "object": "chat.completion.chunk", "model": MODEL,
                            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    if usage:
                        data["usage"] = usage
                    return f"data: {json.dumps(data)}\n\n".encode()
                for i in range(0, len(answer), 8):
                    self.wfile.write(chunk({"content": answer[i:i + 8]}))
                self.wfile.write(chunk({}, "stop"))
                self.wfile.write(chunk(None, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chats(self):
        return [r for r in self.log if r["method"] == "POST" and r["path"] == "/v1/chat/completions"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _app(host):
    s = Settings()
    s.llama_host = host
    s.llama_attach_hosts = []
    s.llama_scan_litesuite = s.llama_scan_lmstudio = s.llama_scan_hf_cache = False
    backend = LlamaCppBackend(s)
    a = owned_app()
    a.seat.registered = True
    a._inbox_monitor = lambda: None
    a.connect = a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.convo_dir = None
    a.skills = []
    a.settings.tools_enabled = a.tools_enabled = False
    a.settings.autocompact_enabled = False
    a.backend = backend
    a.model_id = MODEL
    a.client = AsyncOpenAI(base_url=backend.base_url(), api_key="t1433")
    a._client_binding = model_transport.bind_client(a.client, backend)
    return a


async def _turn(a, pilot, text):
    a._append({"role": "user", "content": text})
    a._stream()
    async with asyncio.timeout(20):
        while a._chat_running():
            await pilot.pause()
            await asyncio.sleep(0.01)
    # A side request is fired after the card settles (a worker): give it time to arrive.
    for _ in range(20):
        await pilot.pause()
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_session_sends_only_its_turns_and_each_turn_extends_the_last():
    engine = LoggingEngine(ANSWERS)
    try:
        a = _app(engine.host)
        async with a.run_test(size=(120, 40)) as pilot:
            a.conversation = [{"role": "system", "content": "Test host. End each FINAL answer with <recap>.</recap>"}]
            for text in ("first question", "second question", "third question"):
                await _turn(a, pilot, text)
            cards = list(a.query(AssistantMessage))
            titles = [card.summary for card in cards]
    finally:
        engine.close()
        if os.environ.get("T1433_LOG"):
            with open(os.environ["T1433_LOG"], "w", encoding="utf-8") as fh:
                for row in engine.log:
                    fh.write(json.dumps({k: v for k, v in row.items() if k != "messages"}
                                        | ({"roles": [m.get("role") for m in row["messages"]],
                                            "last": str(row["messages"][-1].get("content"))[:80]}
                                           if row.get("messages") else {})) + "\n")
    chats = engine.chats()
    assert len(chats) == 3, ("side requests to the serving model: "
                             + json.dumps([{"stream": c["stream"], "prompt_chars": c["prompt_chars"],
                                            "last": str(c["messages"][-1].get("content"))[:60]} for c in chats]))
    for before, after in zip(chats, chats[1:]):
        assert after["messages"][:len(before["messages"])] == before["messages"], \
            "a turn's prompt must start with the previous turn's, byte for byte"
    # The recap still titles its card; the cards without one show their own first line.
    assert titles == ["Planned the parser / One pass per line", "Result", "Third answer, also without a recap."]


def test_a_card_without_a_recap_is_titled_from_its_own_text_without_a_request():
    from litetui.app import _first_line

    assert _first_line("## Heading here\n\nBody.") == "Heading here"
    assert _first_line("```python\nx = 1\n```\nAfter the code.") == "After the code."
    assert _first_line("- **bold** item with `code`\n- two") == "bold item with code"
    assert _first_line("\n\n> quoted first line\nmore") == "quoted first line"
    assert _first_line("") == ""
