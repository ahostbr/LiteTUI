"""T1398: discovering a served model's effort scale from a loopback fake engine. No model, no GPU.

Prefix (89d35d9e): "The probe must never generate or load anything. Assert that in the fake-server
test: no /v1/chat/completions during discovery."

The fake engine answers the three things discovery may touch: GET /props, GET /models (llama.cpp
router) and POST /apply-template. Every other request is recorded and fails the test. It emulates
what each engine's code does with an effort (read 2026-11-23, see effort_scale.py):

  NInfer today   reasoning_effort must be a string (openai_chat_request.cpp:907, translate.cpp:198-206);
                 a name the template rejects renders as its nearest accepted one (chat_template.cpp:91-103)
  NInfer + B     the same, but a number in chat_template_kwargs reaches the template (the T1398-B change)
  llama.cpp      chat_template_kwargs pass as raw JSON (server-common.cpp:1481-1485); a top-level string
                 overwrites the kwargs one, and a top-level "none" erases it (:1498-1505)

The template behaviour is emulated with tables written out here, independent of effort_scale.py:
Inkling's map (inkling2.jinja:1, numbers in [0, 0.99]) and Qwen3.8's accepted low/medium/xhigh.
"""
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from litetui.effort_scale import EffortScale
from litetui.llm_backend import LlamaCppBackend
from litetui.ninfer_backend import NInferBackend
from litetui.ninfer_engine import read_artifact_text_resource
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "effort_templates"
INKLING_MAP = {"none": 0.0, "minimal": 0.1, "low": 0.2, "medium": 0.7, "high": 0.9, "xhigh": 0.99, "max": 0.99}
QWEN_ON_NINFER = {"minimal": "low", "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh",
                  "max": "xhigh"}
NAMES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class _Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _inkling_prompt(effort):
    """inkling2.jinja's emit_thinking_effort: unset -> 0.9; a name -> its map value; a number as is."""
    if effort is None:
        number = 0.9
    elif isinstance(effort, str):
        if effort not in INKLING_MAP:
            raise _Refused(500, f"Unknown reasoning_effort: {effort}")
        number = INKLING_MAP[effort]
    else:
        number = float(effort)
        if not 0.0 <= number <= 0.99:
            raise _Refused(500, "reasoning_effort must be in [0.0, 0.99]")
    return f"<|message_system|><|content_text|>Thinking effort level: {0 if number == 0 else number}<|end_message|>probe"


def _qwen_prompt(effort, thinking):
    if not thinking:
        return "<think>\n\n</think>probe"
    level = "xhigh" if effort in (None, "high") else effort
    if level not in ("low", "medium", "xhigh"):
        raise _Refused(500, f"Unexpected reasoning effort {effort}")
    return f"[effort {level}]probe"


class FakeEngine:
    """mode: "ninfer", "ninfer+B" or "llama". template: "inkling" or "qwen"."""

    def __init__(self, tmp_path, mode, template, *, apply_template=True, router_loaded=None,
                 serve_chat=False, model_id="m"):
        self.mode, self.template, self.apply_template = mode, template, apply_template
        self.router_loaded = router_loaded          # None: single-model; True/False: a router row
        #: Only the wire-capture tests answer chat; discovery tests leave it off and assert it was never asked.
        self.serve_chat, self.model_id = serve_chat, model_id
        self.requests: list[tuple[str, str, dict | None]] = []
        text = (FIXTURES / ("inkling2.jinja" if template == "inkling" else "qwen38_strata.jinja")).read_text("utf-8")
        self.template_text = text
        self.artifact = _write_artifact(tmp_path / "model.ninfer", text)
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                engine.requests.append(("GET", self.path, None))
                if self.path.startswith("/props"):
                    if engine.mode.startswith("llama"):
                        if engine.router_loaded is not None and "?model=" not in self.path:
                            return self._send(200, {"role": "router", "model_path": "none"})
                        return self._send(200, {"model_path": str(engine.artifact),
                                                "chat_template": engine.template_text})
                    return self._send(200, {"model_path": str(engine.artifact)})
                if self.path == "/models" and engine.mode.startswith("llama"):
                    rows = [{"id": "m"}] if engine.router_loaded is None else [
                        {"id": "m", "status": {"value": "loaded" if engine.router_loaded else "unloaded"}}]
                    return self._send(200, {"data": rows})
                if engine.serve_chat and self.path == "/health":
                    return self._send(200, {"status": "ok"})
                if engine.serve_chat and self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": engine.model_id, "max_model_len": 32768}]})
                self._send(404, {"error": "not here"})

            def _stream_reply(self):
                chunks = [
                    {"id": "c", "object": "chat.completion.chunk", "model": engine.model_id,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                    {"id": "c", "object": "chat.completion.chunk", "model": engine.model_id,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    {"id": "c", "object": "chat.completion.chunk", "model": engine.model_id, "choices": [],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}},
                ]
                raw = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw.encode())))
                self.end_headers()
                self.wfile.write(raw.encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                engine.requests.append(("POST", self.path, body))
                if engine.serve_chat and self.path == "/v1/chat/completions":
                    try:
                        engine.render(body)        # the gates a real engine applies before generating
                    except _Refused as refused:
                        return self._send(refused.status, {"error": {"message": str(refused)}})
                    return self._stream_reply()
                if self.path != "/apply-template" or not engine.apply_template:
                    return self._send(404, {"error": "not here"})
                try:
                    self._send(200, {"prompt": engine.render(body)})
                except _Refused as refused:
                    self._send(refused.status, {"error": {"message": str(refused)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def render(self, body):
        top = body.get("reasoning_effort")
        kwargs = dict(body.get("chat_template_kwargs") or {})
        thinking = True
        if self.mode.startswith("ninfer"):
            if top is not None and (not isinstance(top, str) or top not in NAMES):
                raise _Refused(400, "reasoning_effort must be a string or null")
            nested = kwargs.get("reasoning_effort")
            if nested is not None and not isinstance(nested, str) and self.mode == "ninfer":
                raise _Refused(400, "reasoning_effort must be a string")       # translate.cpp:199
            effort = top if top is not None else nested
            if effort == "none":
                thinking = False
            if self.template == "qwen" and isinstance(effort, str) and effort != "none":
                effort = QWEN_ON_NINFER[effort]                               # chat_template.cpp:91-103
        else:
            # "llama-old": a server that accepts chat_template_kwargs and silently ignores it.
            effort = None if self.mode == "llama-old" else kwargs.get("reasoning_effort")
            if isinstance(top, str) and top:
                if top == "none":
                    thinking, effort = False, None                            # erased, server-common.cpp:1502
                else:
                    effort = top
        if self.template == "inkling":
            return _inkling_prompt(effort)
        return _qwen_prompt(None if effort == "none" else effort, thinking)

    def paths(self):
        return [(method, path.split("?")[0]) for method, path, _ in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _write_artifact(path: Path, template: str) -> Path:
    """A minimal .ninfer v3 container holding one raw_bytes_v1 resource (reader.cpp:39-50)."""
    data = template.encode("utf-8")
    directory = json.dumps({
        "components": {"text": {"config": {}, "resources": {"chat_template.jinja": "resource/text/chat_template.jinja"}}},
        "objects": [{"id": "resource/text/chat_template.jinja", "encoding": "raw_bytes_v1", "offset": 0,
                     "bytes": len(data), "kind": "resource"}],
    }).encode("utf-8")
    head = b"NINFER\x00\x03" + struct.pack("<Q", len(directory)) + b"\x00" * 16
    payload_start = -(-(32 + len(directory)) // 4096) * 4096
    blob = head + directory
    path.write_bytes(blob + b"\x00" * (payload_start - len(blob)) + data)
    return path


@pytest.fixture
def engine(tmp_path, request):
    made = []

    def make(*args, **kwargs):
        made.append(FakeEngine(tmp_path, *args, **kwargs))
        return made[-1]
    yield make
    for e in made:
        e.close()


def _ninfer(host):
    s = Settings()
    s.ninfer_host = host
    return NInferBackend(s)


def _llama(host):
    s = Settings()
    s.llama_host = host
    s.llama_attach_hosts = []
    s.llama_scan_litesuite = s.llama_scan_lmstudio = s.llama_scan_hf_cache = False
    return LlamaCppBackend(s)


def _assert_never_generated(e: FakeEngine):
    allowed = {("GET", "/props"), ("GET", "/models"), ("POST", "/apply-template")}
    assert set(e.paths()) <= allowed, e.paths()
    assert ("POST", "/v1/chat/completions") not in e.paths()


# ── the template inside the artifact ─────────────────────────────────────────────────────────────


def test_the_template_is_read_out_of_the_artifact_byte_for_byte(tmp_path):
    text = (FIXTURES / "inkling2.jinja").read_text("utf-8")
    assert read_artifact_text_resource(_write_artifact(tmp_path / "a.ninfer", text), "chat_template.jinja") == text
    assert read_artifact_text_resource(tmp_path / "a.ninfer", "tokenizer.json") is None
    (tmp_path / "junk.ninfer").write_bytes(b"not an artifact")
    assert read_artifact_text_resource(tmp_path / "junk.ninfer", "chat_template.jinja") is None


# ── NInfer ──────────────────────────────────────────────────────────────────────────────────────


def test_ninfer_inkling_today_has_all_seven_names_and_no_number_channel(engine):
    e = engine("ninfer", "inkling")
    scale = _ninfer(e.host).effort_scale("m")
    assert scale.names == NAMES
    assert scale.numbers == INKLING_MAP
    assert scale.number_wire is False, "NInfer answers 400 to a number in chat_template_kwargs today"
    assert scale.template_read and scale.source == "template in model.ninfer"
    assert scale.resolve("high").value == "high" and scale.number_for("high") == 0.9
    assert scale.resolve(0.1).value == "minimal"
    _assert_never_generated(e)


def test_ninfer_with_the_engine_change_turns_the_number_channel_on_by_itself(engine):
    """T1398-B: once a number reaches the template, the same probe says so. No LiteTUI change."""
    e = engine("ninfer+B", "inkling")
    scale = _ninfer(e.host).effort_scale("m")
    assert scale.number_wire is True
    assert (scale.resolve(0.1).placement, scale.resolve(0.1).value) == ("kwargs", 0.1)
    _assert_never_generated(e)


def test_ninfer_qwen_names_come_from_the_probe_since_its_template_has_no_map(engine):
    """NInfer renders high and max as xhigh and minimal as low, so the render groups give
    low / medium / xhigh: Qwen's real set, with no static list involved."""
    e = engine("ninfer", "qwen")
    scale = _ninfer(e.host).effort_scale("m")
    assert scale.names == ("none", "low", "medium", "xhigh")
    assert scale.numbers == {} and scale.number_wire is False
    assert scale.resolve("high").note == "high -> xhigh: this model's template has no 'high'"
    _assert_never_generated(e)


def test_ninfer_with_nothing_to_read_falls_back_to_the_static_list_and_says_so(engine, tmp_path):
    e = engine("ninfer", "qwen", apply_template=False)
    e.artifact.write_bytes(b"not an artifact")
    scale = _ninfer(e.host).effort_scale("qwen3.8-27b")
    assert scale.names == ("none", "low", "medium", "xhigh") and not scale.template_read
    assert scale.source == "template not read"
    assert scale.resolve("high").note == "template not read" and scale.resolve("high").value == "high"
    _assert_never_generated(e)


def test_ninfer_with_no_served_model_has_no_scale():
    s = Settings()
    s.ninfer_host = "http://127.0.0.1:1"
    assert NInferBackend(s).effort_scale(None) is None


def test_a_server_that_answers_200_but_ignores_the_number_has_no_number_channel(engine):
    """200 alone is not proof: the number must render exactly like its name. A server that drops
    chat_template_kwargs renders Inkling's default 0.9 for 0.2, so the channel stays off."""
    e = engine("llama-old", "inkling")
    scale = _llama(e.host).effort_scale("m")
    assert scale.number_wire is False
    _assert_never_generated(e)


# ── llama.cpp ───────────────────────────────────────────────────────────────────────────────────


def test_llama_inkling_carries_a_number_through_the_template_kwargs(engine):
    e = engine("llama", "inkling")
    scale = _llama(e.host).effort_scale("m")
    assert scale.names == NAMES and scale.number_wire is True
    assert (scale.resolve(0.1).placement, scale.resolve(0.1).value) == ("kwargs", 0.1)
    _assert_never_generated(e)


def test_llama_qwen_names_are_its_templates_own(engine):
    """Qwen's template takes high as an alias of xhigh and raises on minimal and max."""
    e = engine("llama", "qwen")
    scale = _llama(e.host).effort_scale("m")
    assert scale.names == ("none", "low", "medium", "xhigh")
    _assert_never_generated(e)


def test_llama_router_model_that_is_not_loaded_is_never_rendered(engine):
    """A render request names the model, and a router could load it to answer."""
    e = engine("llama", "inkling", router_loaded=False)
    assert _llama(e.host).effort_scale("m") is None
    assert ("POST", "/apply-template") not in e.paths()
    _assert_never_generated(e)


def test_llama_router_model_that_is_loaded_is_read_with_its_own_props(engine):
    e = engine("llama", "inkling", router_loaded=True)
    scale = _llama(e.host).effort_scale("m")
    assert isinstance(scale, EffortScale) and scale.number_wire is True
    assert any(path.startswith("/props?model=m") for _, path, _ in e.requests)
    _assert_never_generated(e)
