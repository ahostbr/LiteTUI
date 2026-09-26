"""T982: Codex prompt-cache affinity headers, mirrored from the official client's
HTTP/SSE path (codex 0.154 `stream_responses_api` / `build_responses_options`).

Measured before this: consecutive requests on one growing prefix flipped between 0%
and 94% cached, because nothing on the request tied it to a session, an install or
a turn. The body's prompt_cache_key alone does not route the request.
"""

import base64
import json
import time
import uuid

import httpx
import pytest

from litetui import model_transport as mt

DONE = {"type": "response.completed",
        "response": {"usage": {"input_tokens": 5, "output_tokens": 1}, "output": []}}


def auth_file(tmp_path):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
    token = "h." + payload.decode().rstrip("=") + ".s"
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt",
                                "tokens": {"access_token": token, "account_id": "acct"}}))
    return path


def recorder(turn_states=()):
    """MockTransport that records every request and answers with the next turn-state."""
    seen, states = [], list(turn_states)

    def handle(request):
        seen.append(request)
        headers = {"x-codex-turn-state": states.pop(0)} if states else {}
        return httpx.Response(200, headers=headers, text="data: " + json.dumps(DONE) + "\n\n")

    return seen, httpx.MockTransport(handle)


def transport(tmp_path, http, *, store=None, turn_key=None, key="convo-1"):
    return mt.OAuthTransport("codex", credential_path=auth_file(tmp_path), http_transport=http,
                             prompt_cache_key=key, turn_store=store, turn_key=turn_key)


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()


@pytest.mark.asyncio
async def test_required_headers_present(tmp_path):
    seen, http = recorder()
    await transport(tmp_path, http).create(model="gpt-6-astra", messages=[], stream=False)
    h = seen[0].headers
    assert h["session_id"] == "convo-1"
    assert h["thread_id"] == "convo-1"
    assert h["originator"] == "litetui"
    assert h["x-codex-routing-hint"] == "model=gpt-6-astra"
    uuid.UUID(h["x-codex-installation-id"])
    # Websocket-handshake-only in the reference (build_websocket_headers), never HTTP.
    assert "openai-beta" not in h and "x-client-request-id" not in h
    assert "x-codex-turn-state" not in h  # nothing captured yet


@pytest.mark.asyncio
async def test_session_id_stable_and_equals_prompt_cache_key(tmp_path):
    seen, http = recorder()
    for _ in range(2):
        await transport(tmp_path, http).create(model="m", messages=[], stream=False)
    bodies = [json.loads(r.content) for r in seen]
    assert [r.headers["session_id"] for r in seen] == ["convo-1", "convo-1"]
    assert all(b["prompt_cache_key"] == r.headers["session_id"] for b, r in zip(bodies, seen))


@pytest.mark.asyncio
async def test_installation_id_persists_across_restarts(tmp_path):
    seen, http = recorder()
    await transport(tmp_path, http).create(model="m", messages=[], stream=False)
    first = seen[0].headers["x-codex-installation-id"]
    assert (tmp_path / "data" / ".codex_installation_id").read_text().strip() == first
    # A new process reads the file: no in-memory cache can fake this arm.
    assert mt.codex_installation_id() == first


@pytest.mark.asyncio
async def test_turn_state_replayed_within_turn_not_across(tmp_path):
    seen, http = recorder(["ts-A", "ts-IGNORED", "ts-B"])
    store = {}
    await transport(tmp_path, http, store=store, turn_key=1.0).create(model="m", messages=[], stream=False)
    await transport(tmp_path, http, store=store, turn_key=1.0).create(model="m", messages=[], stream=False)
    await transport(tmp_path, http, store=store, turn_key=2.0).create(model="m", messages=[], stream=False)
    sent = [r.headers.get("x-codex-turn-state") for r in seen]
    # Turn 1: first request has none, second replays A (set once: the second
    # response's value does not overwrite). Turn 2: starts clean.
    assert sent == [None, "ts-A", None]


@pytest.mark.asyncio
async def test_side_calls_never_carry_turn_state(tmp_path):
    seen, http = recorder(["ts-A"])
    store = {}
    await transport(tmp_path, http, store=store, turn_key=1.0).create(model="m", messages=[], stream=False)
    await transport(tmp_path, http, store=store, turn_key=1.0).create(
        purpose="compaction", model="m", messages=[], stream=False)
    assert seen[1].headers.get("x-codex-turn-state") is None


def test_for_app_wires_convo_and_turn(tmp_path):
    class Backend:
        name, models = "codex", None

    class App:
        backend, convo_id, _active_turn_started_at = Backend(), "convo-9", 42.0

    app = App()
    t = mt.for_app(app)
    assert t.prompt_cache_key == "convo-9"
    assert t.turn_key == 42.0
    assert t.turn_store is mt.for_app(app).turn_store  # one store per app
