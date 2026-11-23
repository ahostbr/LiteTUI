"""T1398: the JSON a real LiteTUI turn sends, captured by a loopback fake engine. No model, no GPU.

Brief step 4: "a scripted loopback fake server that captures the JSON LiteTUI actually sends". The
app is a real LiteTUI (owned_app) with a real NInferBackend / LlamaCppBackend pointed at the fake
engine of test_effort_discovery, a real AsyncOpenAI client, and `_stream()` (the user-turn path):
discovery, request assembly and the client's serialization all run as they do for Ryan.

Acceptance (Inkling): /think low sends 0.2; /think 0.1 sends 0.1; high sends 0.9, not 0.99. Each row
asserts both the body that left LiteTUI and the template line the fake engine renders from it.
"""
from __future__ import annotations

import asyncio

import pytest
from _owned_seat import owned_app
from openai import AsyncOpenAI
from test_effort_discovery import FakeEngine, _inkling_prompt, _llama, _ninfer

from litetui import model_transport
from litetui.plugins import misc

MODEL = "inkling-2-mini"


@pytest.fixture
def fake(tmp_path):
    made = []

    def make(mode, template="inkling"):
        made.append(FakeEngine(tmp_path, mode, template, serve_chat=True, model_id=MODEL))
        return made[-1]
    yield make
    for engine in made:
        engine.close()


def _app(backend):
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
    a.client = AsyncOpenAI(base_url=backend.base_url(), api_key="t1398")
    a._client_binding = model_transport.bind_client(a.client, backend)
    return a


async def _turn(a, pilot, level):
    """Choose `level` the way the user does (/think), then send one user turn through `_stream`."""
    a._effort_scale_now()                 # what connect's worker does
    misc._cmd_think(a, "think", str(level))
    a.conversation = [{"role": "system", "content": "Test host"}]
    a._append({"role": "user", "content": "hello"})
    a._stream()
    async with asyncio.timeout(20):
        while a._chat_running():
            await pilot.pause()
            await asyncio.sleep(0.01)
    await pilot.pause()


def _turn_bodies(engine):
    return [body for method, path, body in engine.requests if path == "/v1/chat/completions"]


@pytest.mark.parametrize("mode,level,top,kwargs,printed", [
    ("ninfer", "low", "low", None, "0.2"),
    ("ninfer", "high", "high", None, "0.9"),                # was xhigh -> 0.99
    ("ninfer", "0.1", "minimal", None, "0.1"),              # NInfer today: the equal name
    ("ninfer", "off", None, "none", "0"),
    ("llamacpp", "0.1", None, 0.1, "0.1"),                  # llama.cpp: the number itself
    ("llamacpp", "high", "high", None, "0.9"),
    ("llamacpp", "off", None, "none", "0"),                 # was a top-level none: erased -> 0.9
])
@pytest.mark.asyncio
async def test_what_a_real_turn_sends_for_inkling(fake, mode, level, top, kwargs, printed):
    engine = fake("ninfer" if mode == "ninfer" else "llama")
    backend = _ninfer(engine.host) if mode == "ninfer" else _llama(engine.host)
    a = _app(backend)
    async with a.run_test(size=(120, 40)) as pilot:
        await _turn(a, pilot, level)
    bodies = _turn_bodies(engine)
    assert bodies, f"no chat request reached the engine: {engine.paths()}"
    body = bodies[0]
    assert body.get("reasoning_effort") == top
    assert (body.get("chat_template_kwargs") or {}).get("reasoning_effort") == kwargs
    if kwargs is not None:
        assert type(body["chat_template_kwargs"]["reasoning_effort"]) is type(kwargs), "0.1 stays a JSON number"
    assert f"Thinking effort level: {printed}<" in engine.render(body)


@pytest.mark.asyncio
async def test_qwen_on_ninfer_gets_its_own_names_and_a_saved_high_lands_on_xhigh(fake):
    engine = fake("ninfer", "qwen")
    a = _app(_ninfer(engine.host))
    async with a.run_test(size=(120, 40)) as pilot:
        a.thinking_level = "high"                 # saved under another model; not offered for Qwen
        a._effort_scale_now()
        a.conversation = [{"role": "system", "content": "Test host"}]
        a._append({"role": "user", "content": "hello"})
        a._stream()
        async with asyncio.timeout(20):
            while a._chat_running():
                await pilot.pause()
                await asyncio.sleep(0.01)
        await pilot.pause()
        a.update_header()
        assert "think:high -> xhigh" in a.sub_title
    body = _turn_bodies(engine)[0]
    assert body["reasoning_effort"] == "xhigh"
    assert "[effort xhigh]" in engine.render(body)


async def _plain_turn(a, pilot):
    a.conversation = [{"role": "system", "content": "Test host"}]
    a._append({"role": "user", "content": "hello"})
    a._stream()
    async with asyncio.timeout(20):
        while a._chat_running():
            await pilot.pause()
            await asyncio.sleep(0.01)
    await pilot.pause()


def _probes(engine):
    return [path for method, path, _ in engine.requests if method == "POST" and path == "/apply-template"]


@pytest.mark.asyncio
async def test_a_scale_that_cannot_be_read_is_not_probed_again_on_the_next_turn(tmp_path, monkeypatch):
    """Prefix 5c1db80f: an engine with no /apply-template and no readable template must not get the
    same failed probes on every turn. Two turns, one round of probes; the turns themselves still go."""
    import litetui.app as app_mod

    engine = FakeEngine(tmp_path, "ninfer", "qwen", apply_template=False, serve_chat=True, model_id=MODEL)
    engine.artifact.write_bytes(b"not an artifact")
    try:
        a = _app(_ninfer(engine.host))
        async with a.run_test(size=(120, 40)) as pilot:
            await _plain_turn(a, pilot)
            first = len(_probes(engine))
            await _plain_turn(a, pilot)
            second = len(_probes(engine))
            assert first > 0, "the first turn must try to read the scale"
            assert second == first, f"turn 2 re-probed: {first} -> {second} /apply-template posts"
            # Each turn and its side call (the card summary) went out; neither re-probed.
            assert len(_turn_bodies(engine)) >= 2

            # A minute later it is asked again (an engine may have come up with the template).
            monkeypatch.setattr(app_mod, "EFFORT_SCALE_MISS_TTL", 0.0)
            await _plain_turn(a, pilot)
            assert len(_probes(engine)) > second
            third = len(_probes(engine))

            # Another model is another key: asked at once, inside the minute.
            monkeypatch.setattr(app_mod, "EFFORT_SCALE_MISS_TTL", 60.0)
            a.model_id = "another-model"
            a._effort_scale_now()
            assert len(_probes(engine)) > third
    finally:
        engine.close()


def test_the_render_helper_is_the_inkling_template_rule():
    """Guard for the table above: the fake's printer is inkling2.jinja's emit_thinking_effort."""
    assert _inkling_prompt(None).count("0.9") == 1
    assert "Thinking effort level: 0<" in _inkling_prompt("none")
