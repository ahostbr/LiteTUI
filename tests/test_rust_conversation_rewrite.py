"""Owned Edit/Retry publication; provider and OS effects stay forbidden."""
from __future__ import annotations

import asyncio
import copy
import uuid

import pytest
import pytest_asyncio
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import conversation_rewrite as rewrite
from litetui import gui_rpc, hook_host, native_history
from litetui import history_projection as projection
from litetui.conversation import ConversationRepository
from litetui.launch_options import LaunchOptions

switch_app = _controlled_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def app(switch_app, monkeypatch):
    app = switch_app
    app._launch_options = LaunchOptions(history_projection_version=1)
    app.conversation = [{"role": "system", "content": "Hidden exact system", "hidden": {"keep": True}},
                        {"role": "user", "content": "First question"},
                        {"role": "assistant", "content": "Keep prior answer"},
                        {"role": "user", "content": "Original selected text"},
                        {"role": "assistant", "content": "Discard suffix"}]
    app._snapshot("Rewrite controlled fixture")
    app._hook_lifecycle = []
    app._hook_lock = asyncio.Lock()
    app._hook_source = "typed"
    app.rejected_prompts = []
    monkeypatch.setattr(app, "watch_pending_image", lambda _value: None)
    app.pending_image = None
    app._rewrite_test_requests = []
    monkeypatch.setattr(app, "_submit_text", forbidden)
    monkeypatch.setattr(app, "_handle_command", forbidden)

    async def stream(origin):
        app._validate_owned_execution()
        app._rewrite_test_requests.append((origin, copy.deepcopy(app.conversation)))
        app._append({"role": "assistant", "content": "Controlled response"})

    monkeypatch.setattr(app, "_stream_worker", stream)
    yield app
    for worker in app.workers:
        if not worker.is_finished:
            worker.cancel()


def selection(app, index=3):
    common = {"version": 1, "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
              "backend": app.backend.name, "model": app.model_id}
    descriptor = native_history.snapshot(app, {**common, "from_session_id": app.convo_id,
                                             "scope": "current", "catalog_snapshot": None})
    return {**common, "id": "inspect", "history_snapshot": descriptor["snapshot"],
            "context_sha256": descriptor["context_sha256"], "message_index": index,
            "message_sha256": projection.digest(projection.encoded(app.conversation[index]))}


def reviewed(app, action="edit", text="  Replacement \u00e9\U0001f9eae\u0301\nline  "):
    command = selection(app)
    state = rewrite.snapshot(app, command)
    return rewrite.validate(app, {**command, "id": "validate", "snapshot": state["snapshot"],
                                 "action": action, **({"text": text} if action == "edit" else {})})


async def apply(app, review, operation="rewrite-execution"):
    return await gui_rpc.async_dispatch(app, {**review, "id": operation, "type": rewrite.PREFIX + "apply"})


async def settle(app, review, operation="rewrite-execution"):
    return await gui_rpc.async_dispatch(app, {**{key: review[key] for key in rewrite.COMMON},
        "type": rewrite.PREFIX + "settled", "id": "settle", "action_id": review["action_id"], "operation_id": operation})


@pytest.mark.parametrize("action", ["edit", "retry"])
async def test_review_is_pure_then_one_snapshot_precedes_exact_worker_context(app, action):
    old = copy.deepcopy(app.conversation)
    before = disk(app)
    review = reviewed(app, action)
    assert disk(app) == before and app.conversation == old
    result = await apply(app, review)
    assert result["turn"] == "accepted", result
    assert result["persistence"] == {"snapshot_written": True, "readback_verified": True}
    expected = old[:3] + [{"role": "user", "content": review["text"]}]
    assert result["prepared_context_sha256"] == projection.digest(projection.encoded(expected))
    proof = await settle(app, review)
    assert proof["worker_settled"] and proof["mutated"] and not proof["close_required"]
    assert len(app._rewrite_test_requests) == 1
    origin, sent = app._rewrite_test_requests[0]
    assert sent == expected
    assert origin.operation_id == "rewrite-execution" and origin.source == "rpc"
    assert origin.session_id == app.convo_id and str(uuid.UUID(origin.execution_id)) == origin.execution_id
    assert ConversationRepository.read(app.convo_path)[1] == expected + [{"role": "assistant", "content": "Controlled response"}]
    assert proof["history"]["context_sha256"] == projection.digest(projection.encoded(app.conversation))
    assert rewrite.submission_refusal(app) is None
    with pytest.raises(ValueError):
        await apply(app, review)
    assert len(app._rewrite_test_requests) == 1


@pytest.mark.parametrize("value", ["", " \n", "/new", " \t/compact", "shot.png explain", "C:\\Folder\\shot.png", "x" * 65537, "\u754c" * 21846], ids=["empty", "space", "slash", "spaced-slash", "image", "win-image", "ascii-over", "unicode-over"])
async def test_invalid_edit_never_invokes_hooks_or_mutates_history(app, monkeypatch, value):
    command = selection(app)
    state = rewrite.snapshot(app, command)
    before, history = disk(app), copy.deepcopy(app.conversation)
    monkeypatch.setattr(hook_host, "admit_prompt_origin", forbidden)
    with pytest.raises(ValueError):
        rewrite.validate(app, {**command, "snapshot": state["snapshot"], "action": "edit", "text": value})
    assert disk(app) == before and app.conversation == history


@pytest.mark.parametrize("fault", ["pending_image", "native_codex", "native_claude", "pending_input", "budget"])
async def test_source_eligibility_is_actual_capability_or_preflight_without_effects(app, monkeypatch, fault):
    if fault == "pending_image":
        app.pending_image = "NEVER_READ_ATTACHMENT"
    elif fault == "native_codex":
        app.backend.app_server = object()
    elif fault == "native_claude":
        app.backend.owns_native_turns = True
    elif fault == "pending_input":
        app._pending_input.append({"content": "HELD"})
    else:
        monkeypatch.setattr(app, "_oversize_refusal", lambda _text: "Controlled budget refusal")
    before = disk(app)
    command = selection(app)
    if fault == "pending_input":
        with pytest.raises(ValueError, match="queued"):
            rewrite.snapshot(app, command)
        assert disk(app) == before and not app._rewrite_test_requests
        return
    state = rewrite.snapshot(app, command)
    assert not state["actions"]["retry"]["eligible"]
    with pytest.raises(ValueError):
        rewrite.validate(app, {**command, "snapshot": state["snapshot"], "action": "retry"})
    assert disk(app) == before and not app._rewrite_test_requests


@pytest.mark.parametrize("fault", ["history", "disk", "settings", "policy", "attachments", "budget"])
async def test_drift_or_refusal_before_first_append_retains_entire_history(app, monkeypatch, fault):
    review = reviewed(app)
    if fault == "history":
        app.conversation[-1]["content"] = "External edit"
        app._snapshot("Coherent external edit")
    elif fault == "disk":
        app.store.write_record({"type": "rename", "name": "External"})
    elif fault == "settings":
        app.settings.max_tokens_chat += 1
    elif fault == "policy":
        app._active_tool_profile = "interactive"
    elif fault == "attachments":
        app.pending_image = "HELD"
    else:
        monkeypatch.setattr(app, "_oversize_refusal", lambda _text: "Budget changed")
    before, history = disk(app), copy.deepcopy(app.conversation)
    result = await apply(app, review)
    assert result["turn"] == "refused" and not result["mutated"] and not result["close_required"], result
    assert result["persistence"] == {"snapshot_written": False, "readback_verified": False}
    assert disk(app) == before and app.conversation == history and not app._rewrite_test_requests
    assert result["refusal_history"]["context_sha256"] == projection.digest(projection.encoded(history))
    proof = await settle(app, review)
    assert proof["history"] == result["refusal_history"]


async def test_hook_refusal_retains_suffix_and_cannot_execute(app, monkeypatch):
    review = reviewed(app)
    before, history = disk(app), copy.deepcopy(app.conversation)

    async def dispatch(_app, event, _data, **_kwargs):
        from litetui.lifecycle_hooks import HookResult
        assert event == "prompt_before" and _kwargs["allow_prompt"] is False
        return HookResult(False, "Controlled gate denial")

    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    result = await apply(app, review)
    assert result["turn"] == "refused" and not result["mutated"], result
    assert disk(app) == before and app.conversation == history
    proof = await settle(app, review)
    assert proof["turn"] == "refused" and not app._rewrite_test_requests


@pytest.mark.parametrize("fault", ["write", "readback", "receipts", "schedule"])
async def test_uncertain_publication_closes_admission_without_replay(app, monkeypatch, fault):
    review = reviewed(app)
    if fault == "write":
        def write(_record):
            app.store.persist_error = "Controlled write failure"
        monkeypatch.setattr(app.store, "write_record", write)
    elif fault == "readback":
        original = app.store.write_record
        def write(record):
            original({**record, "messages": [{"role": "user", "content": "wrong"}]})
        monkeypatch.setattr(app.store, "write_record", write)
    elif fault == "receipts":
        original = app.store.write_record
        def write(record):
            original({**record, "store_injected": not record["store_injected"]})
        monkeypatch.setattr(app.store, "write_record", write)
    else:
        monkeypatch.setattr(app, "_stream", lambda **_kwargs: None)
    result = await apply(app, review)
    assert result["turn"] == "failed" and result["close_required"] and result["mutated"], result
    assert rewrite.submission_refusal(app) == rewrite.CLOSE_REQUIRED
    assert not app._rewrite_test_requests
    with pytest.raises(ValueError):
        await apply(app, review)


@pytest.mark.parametrize("fault", ["orphan-call", "orphan-result", "missing-id", "duplicate-id"])
async def test_incomplete_or_malformed_prefix_refuses_instead_of_repairing(app, fault):
    call = {"id": "call-1", "type": "function", "function": {"name": "fixture", "arguments": "{}"}}
    prefix: list[dict] = [{"role": "assistant", "content": "", "tool_calls": [call]},
              {"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    if fault == "orphan-call":
        prefix.pop()
    elif fault == "orphan-result":
        prefix.pop(0)
    elif fault == "missing-id":
        call.pop("id")
        prefix[1]["tool_call_id"] = None
    else:
        prefix[0]["tool_calls"].append(copy.deepcopy(call))
    app.conversation = prefix + [{"role": "user", "content": "Selected"}]
    app._snapshot("Malformed controlled prefix")
    before = disk(app)
    command = selection(app, len(prefix))
    state = rewrite.snapshot(app, command)
    assert not state["actions"]["edit"]["eligible"]
    assert disk(app) == before


@pytest.mark.parametrize("tail", [b"", b"\n", b"torn"])
async def test_canonical_snapshot_estimate_matches_actual_unicode_reason_receipts_and_separator(app, tail):
    app.convo_path.write_bytes(app.convo_path.read_bytes().rstrip(b"\n") + tail)
    record = ConversationRepository.snapshot_record([{"role": "user", "content": "ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂºÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âªe\u0301\n"}],
        "reason ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂºÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âª\n", store_identity={"mode": "fixture"}, store_injected=True)
    before = app.convo_path.read_bytes()
    encoded = ConversationRepository.record_bytes(record, separator=bool(before and not before.endswith(b"\n")))
    app.store.write_record(record)
    assert app.convo_path.read_bytes() == before + encoded


async def test_exact_raw_snapshot_cap_refuses_before_append(app, monkeypatch):
    review = reviewed(app)
    stored = app._conversation_rewrite_review
    messages = app.conversation[:3] + [{"role": "user", "content": review["text"]}]
    monkeypatch.setattr("litetui.conversation.time.time", lambda: 123.5)
    record = rewrite._snapshot_record(app, stored, messages)
    exact = len(stored.raw) + len(ConversationRepository.record_bytes(record))
    monkeypatch.setattr(native_history, "MAX_TRANSCRIPT", exact - 1)
    before, history = disk(app), copy.deepcopy(app.conversation)
    result = await apply(app, review)
    assert result["turn"] == "refused", result
    assert disk(app) == before and app.conversation == history and not app._rewrite_test_requests


@pytest.mark.parametrize("field,value", [("version", True), ("message_index", 3.0), ("prefix_messages", 3.0)])
async def test_apply_rejects_scalar_type_alias_without_attempt_or_effect(app, field, value):
    review = reviewed(app)
    before = disk(app)
    with pytest.raises(ValueError, match="exactly echo"):
        await apply(app, {**review, field: value})
    assert disk(app) == before and not app._rewrite_test_requests
    assert rewrite._attempt(app) is None


async def test_incoherent_drift_and_second_refusal_drift_never_get_success(app):
    review = reviewed(app)
    app.conversation[-1]["content"] = "Unpersisted external mutation"
    before = disk(app)
    result = await apply(app, review)
    assert result["turn"] == "failed" and result["close_required"]
    assert result["refusal_history"] is None and disk(app) == before


async def test_refusal_settlement_rejects_a_second_coherent_change(app, monkeypatch):
    review = reviewed(app)
    monkeypatch.setattr(app, "_oversize_refusal", lambda _text: "Changed budget")
    result = await apply(app, review)
    assert result["turn"] == "refused" and result["refusal_history"]
    app.conversation[-1]["content"] = "Another change"
    app._snapshot("Second coherent drift")
    with pytest.raises(ValueError, match="history changed"):
        await settle(app, review)
    assert rewrite.submission_refusal(app)


@pytest.mark.parametrize("stop_after_initial", [False, True])
async def test_exact_aftercare_chain_joins_context_and_preserves_inline_and_wake_origins(app, monkeypatch, stop_after_initial):
    from litetui.event_origin import EventOrigin
    from litetui.plugins.self_compact import CompactionRequest

    events, streams, contexts, denied = [], [], [], []
    context_started, release_context = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(app, "_rpc_emit", lambda data, *, origin=None: events.append((copy.deepcopy(data), origin or rewrite.event_origin(app))))
    monkeypatch.setattr(app.self_compaction, "supported", lambda: True)

    @rewrite.captured_work("context", exclusive=True, group="ctx")
    async def context(_app):
        context_started.set()
        with pytest.raises(ValueError):
            app._validate_owned_execution()
        denied.append("context-has-no-execution")
        await release_context.wait()
        contexts.append("joined")

    def _wake_after_compact():
        app._stream()

    @rewrite.captured_work("compaction", exclusive=True, group="chat")
    async def _compact(_app, _extra="", *, handoff=None):
        app._validate_owned_execution()
        assert handoff == "Keep this exact handoff"
        outer = rewrite.event_origin(app)
        assert outer.execution_id and outer.operation_id == "rewrite-execution"
        await app._stream_inline(origin=EventOrigin(source="compact"))
        assert rewrite.event_origin(app) == outer
        rewrite.after_refresh(app, _wake_after_compact)

    monkeypatch.setattr(app, "_compact", lambda *args, **kwargs: _compact(app, *args, **kwargs))

    async def stream(origin):
        app._validate_owned_execution()
        marker = rewrite.segment(app, origin)
        streams.append((origin, marker["rewrite_segment"]))
        app._append({"role": "assistant", "content": f"Segment {len(streams)}"})
        async def inherited_child():
            with pytest.raises(ValueError):
                app._validate_owned_execution()
            assert rewrite.event_origin(app) is None
            with pytest.raises(ValueError):
                app._stream()
            with pytest.raises(ValueError):
                context(app)
            with pytest.raises(ValueError):
                rewrite.card_summary(app, forbidden)
            with pytest.raises(ValueError):
                rewrite.after_refresh(app, forbidden)
            denied.append("inherited-child")
        await asyncio.create_task(inherited_child())
        if len(streams) == 1:
            context(app)
            request = CompactionRequest("Controlled maintenance", "Keep this exact handoff", app.convo_id)
            rewrite.after_refresh(app, app._run_self_compaction, request)
            if stop_after_initial:
                assert app.stop_turn_over_rpc()

    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    result = await apply(app, review)
    assert result["turn"] == "accepted", result
    await asyncio.wait_for(context_started.wait(), 2)
    settling = asyncio.create_task(settle(app, review))
    await asyncio.sleep(0)
    assert not settling.done() and not contexts
    release_context.set()
    proof = await asyncio.wait_for(settling, 2)
    assert contexts == ["joined"] and "context-has-no-execution" in denied
    assert len(streams) == (1 if stop_after_initial else 3)
    assert proof["execution"] == {"segments": len(streams), "last_execution_id": streams[-1][0].execution_id}
    for index, (origin, marker) in enumerate(streams):
        assert origin.source == "rpc" and origin.operation_id == "rewrite-execution" and origin.session_id == app.convo_id
        assert marker == {"action_id": review["action_id"], "index": index,
                          "previous_execution_id": streams[index - 1][0].execution_id if index else None}
    aftercare = [(event, origin) for event, origin in events if event["type"] == "rewrite_aftercare"]
    if stop_after_initial:
        assert not aftercare
    else:
        assert len(aftercare) == 2
        first, last = aftercare
        assert first[0]["phase"] == "start" and last[0]["phase"] == "end"
        assert first[0]["aftercare_id"] == last[0]["aftercare_id"] == first[1].execution_id == last[1].execution_id
        assert first[0]["after_execution_id"] == last[0]["after_execution_id"] == streams[0][0].execution_id
        assert first[0]["kind"] == "compaction" and first[0]["action_id"] == review["action_id"]
    attempt = rewrite._attempt(app)
    assert attempt is not None and attempt.owned is not None
    assert all(item.worker.is_finished for item in attempt.owned)
    assert attempt.drain.done() and rewrite.submission_refusal(app) is None


async def test_unset_ordinary_prompt_admission_keeps_interactive_hook_default(app, monkeypatch):
    from litetui.lifecycle_hooks import HookResult
    app._launch_options = LaunchOptions()
    observed = []
    async def dispatch(_app, event, _data, **kwargs):
        observed.append((event, kwargs["allow_prompt"]))
        return HookResult(True)
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    previous = copy.deepcopy(app.conversation)
    origin = await hook_host.admit_prompt_origin(app, {"content": "Ordinary input", "text": "Ordinary input", "source": "rpc"})
    assert origin is not None and observed == [("prompt_before", True)]
    assert app.conversation == previous + [{"role": "user", "content": "Ordinary input"}]
    assert not app._rewrite_test_requests


async def test_snapshot_refuses_incoherent_initial_store_receipts(app):
    app._store_injected = not app._store_injected
    before = disk(app)
    with pytest.raises(ValueError, match="store receipts"):
        rewrite.snapshot(app, selection(app))
    assert disk(app) == before and not app._rewrite_test_requests


async def test_ordinary_no_tools_compaction_stop_never_publishes_summary(make_app, monkeypatch):
    from types import SimpleNamespace

    from test_compaction_ui import _app, _Chunk, _seed

    from litetui import model_transport

    app = _app(make_app)
    _seed(app)
    app._inbox_monitor = lambda: None
    app._kick_card_summary = lambda *_: None
    app._stream = forbidden
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    app._rpc_emit = lambda event, **_kwargs: events.append(event)

    class HeldSummary:
        def __aiter__(self):
            return self.chunks()
        async def chunks(self):
            entered.set()
            await release.wait()
            yield _Chunk(content="A summary which Stop must never publish")
        async def close(self):
            pass

    async def create(**kwargs):
        assert kwargs["purpose"] == "compaction"
        return HeldSummary()
    async def ready(**_kwargs):
        pass
    app._ensure_chat_ready = ready
    monkeypatch.setattr(model_transport, "for_app", lambda _: SimpleNamespace(create=create))
    async with app.run_test(size=(100, 30)):
        app._materialise_convo()
        app._snapshot("Controlled pre-compaction state")
        history, before = copy.deepcopy(app.conversation), disk(app)
        worker = app._compact()
        await asyncio.wait_for(entered.wait(), 2)
        assert app.stop_turn_over_rpc()
        release.set()
        await asyncio.wait_for(worker.wait(), 2)
        assert app.conversation == history and disk(app) == before
        notices = [event for event in events if event.get("type") == "compaction"]
        assert len(notices) == 1 and notices[0]["reason"] == "cancelled"
        assert app._turn_abandoned and not any(event.get("reason") == "compacted" for event in notices)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_thread_attribution_is_exact_event_only_and_retired_before_settlement(app, monkeypatch, shutdown):
    import contextvars
    import threading

    from litetui import tasks
    from litetui.question_result import current_lifetime

    entered, release = threading.Event(), threading.Event()
    observations, copied_contexts = [], []
    loop = asyncio.get_running_loop()
    started = asyncio.Event()

    def tool(_args):
        origin = rewrite.event_origin(app)
        assert origin is not None and origin.execution_id
        with pytest.raises(ValueError):
            app._validate_owned_execution()
        copied = contextvars.copy_context()
        def foreign_thread():
            assert rewrite.event_origin(app) is None
            with pytest.raises(ValueError):
                app._validate_owned_execution()
            observations.append("foreign-thread-denied")
        thread = threading.Thread(target=lambda: copied.run(foreign_thread))
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        async def foreign_task():
            assert rewrite.event_origin(app) is None
            with pytest.raises(ValueError):
                app._validate_owned_execution()
        asyncio.run(foreign_task())
        copied_contexts.append(contextvars.copy_context())
        entered.set()
        loop.call_soon_threadsafe(started.set)
        if shutdown:
            assert current_lifetime().wait(2)
        else:
            assert release.wait(2)
        observations.append("thread-retired")
        return "controlled answer"

    async def stream(origin):
        rewrite.segment(app, origin)
        fn, call = rewrite.capture_tool(app, tool)
        aw = asyncio.to_thread(fn, {})
        async def observed():
            result = await aw
            assert rewrite.event_origin(app) == origin
            with pytest.raises(ValueError):
                app._validate_owned_execution()
            return result
        future = rewrite.track_tool(app, call, observed())
        done, task = await tasks.wait_or_promote(future, 0)
        assert done and task.result() == "controlled answer"
        with pytest.raises(ValueError, match="replayed"):
            fn({})

    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    result = await asyncio.create_task(apply(app, review))
    assert result["turn"] == "accepted"
    await asyncio.wait_for(started.wait(), 2)
    assert entered.is_set()
    if shutdown:
        proof = await asyncio.wait_for(rewrite.settle_before_shutdown(app), 3)
        assert proof["worker_settled"] and proof["effects_verified"] is False
    else:
        settling = asyncio.create_task(settle(app, review))
        await asyncio.sleep(0)
        assert not settling.done()
        release.set()
        proof = await asyncio.wait_for(settling, 3)
        assert proof["worker_settled"]
    assert observations == ["foreign-thread-denied", "thread-retired"]
    attempt = rewrite._required_attempt(app)
    assert attempt.tool_calls and all(call.future.done() and call.producer.done() and call.retired.is_set()
                                      for call in attempt.tool_calls)
    assert await asyncio.to_thread(copied_contexts[0].run, rewrite.event_origin, app) is None


async def test_captured_tool_starts_under_canonical_current_and_process_slot(app, monkeypatch):
    from litetui import tasks
    current, slot = object(), tasks.ProcessSlot()
    observed = []
    def tool(_args):
        observed.append((tasks.CURRENT.get(), tasks.PROCESS_SLOT.get()))
        return "done"
    async def stream(origin):
        rewrite.segment(app, origin)
        fn, call = rewrite.capture_tool(app, tool)
        future = rewrite.track_tool(app, call, asyncio.to_thread(fn, {}))
        assert call.producer is None
        token = tasks.CURRENT.set(current)
        try:
            assert await tasks.run_in_process_slot(future, slot) == "done"
        finally:
            tasks.CURRENT.reset(token)
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await apply(app, review))["turn"] == "accepted"
    await settle(app, review)
    assert observed == [(current, slot)]


@pytest.mark.parametrize("cleanup_proven", [True, False])
async def test_real_thread_tool_after_hook_keeps_policy_and_requires_process_proof(app, monkeypatch, cleanup_proven):
    import json
    import sys

    from litetui import lifecycle_hooks, tool_policy

    app.tools_enabled = True
    app.hook_config.project_path.write_text(json.dumps({"version": 1, "hooks": [{
        "id": "after", "mode": "observe", "events": ["tool_after"],
        "executable": sys.executable, "argv": ["-c", "print('controlled')"]}]}))
    hook = app.hook_config.read("project")[0]
    workspace = app._hook_workspace
    arguments = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": {}}
    app.settings.tool_always_allow = [tool_policy.rule_key(
        hook.approval_name(workspace), tool_policy.SHELL_POLICY.classify(arguments, workspace))]
    app.plugins.add_tool("test", {"type": "function", "function": {"name": "rewrite_probe",
        "description": "controlled", "parameters": {"type": "object", "properties": {}}}},
        lambda _: "controlled result", policy=tool_policy.READ_POLICY)
    calls = []
    async def run_hook(_hook, document, _workspace, **options):
        assert document["event"] == "tool_after"
        assert rewrite.event_origin(app).operation_id == "rewrite-execution"
        assert not rewrite._authorized(app)
        with pytest.raises(ValueError):
            app._validate_owned_execution()
        result = lifecycle_hooks.HookResult(True, exit_code=0)
        options["on_settled"](result, {"process_started": True, "process_exited": cleanup_proven,
            "pipes_settled": cleanup_proven, "kill_requested": False, "kill_confirmed": True})
        calls.append(document)
        return result
    monkeypatch.setattr(lifecycle_hooks, "run_hook", run_hook)
    async def stream(origin):
        rewrite.segment(app, origin)
        result, ok = await app._execute_tool("rewrite_probe", {})
        assert ok and result == "controlled result"
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await apply(app, review))["turn"] == "accepted"
    if cleanup_proven:
        assert (await settle(app, review))["worker_settled"]
    else:
        with pytest.raises(ValueError, match="subprocess cleanup"):
            await settle(app, review)
        assert rewrite.submission_refusal(app)
    assert len(calls) == 1 and calls[0]["data"]["ok"] is True


@pytest.mark.parametrize("may_background,flag,threshold,refused", [
    (True, True, 0, True), (True, False, 3, True), (True, None, 3, True),
    (True, False, 0, False), (True, None, 0, False), (False, True, 3, False),
])
async def test_background_boundary_refuses_before_hooks_or_effects_and_keeps_foreground(
        app, monkeypatch, may_background, flag, threshold, refused):
    from litetui import tasks, tool_policy, tool_schemas
    effects, materialized, outcomes = [], [], []
    schema = {"type": "function", "function": {"name": "rewrite_probe", "description": "controlled",
        "parameters": {"type": "object", "properties": {"background": {"type": "boolean"}} if may_background else {}}}}
    def tool(_args):
        effects.append(tasks.PROCESS_SLOT.get())
        return "done"
    app.plugins.add_tool("test", schema, tool, policy=tool_policy.READ_POLICY)
    monkeypatch.setattr(tool_schemas, "load", lambda name: schema if name == "rewrite_probe" else forbidden())
    monkeypatch.setattr(tasks, "backgroundable", tasks.backgroundable.__wrapped__)
    app.tools_enabled = True
    app.settings.tool_auto_background_s = threshold
    async def stream(origin):
        rewrite.segment(app, origin)
        monkeypatch.setattr(app, "_materialise_convo", lambda: materialized.append(True))
        if refused:
            monkeypatch.setattr(app, "_authorize_action", forbidden)
            monkeypatch.setattr(app, "_dispatch_for", forbidden)
            monkeypatch.setattr(hook_host, "dispatch", forbidden)
        args = {} if flag is None else {"background": flag}
        assert tasks.backgroundable("rewrite_probe") is may_background, schema
        outcomes.append(await app._execute_tool("rewrite_probe", args, hooks_enabled=False))
        if refused:
            assert args == ({} if flag is None else {"background": flag})
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await apply(app, review))["turn"] == "accepted"
    await settle(app, review)
    if refused:
        assert not effects and not materialized
        assert outcomes[0][1] is False and "no work started" in outcomes[0][0]
    else:
        assert outcomes == [("done", True)] and len(effects) == 1
        assert bool(materialized) is may_background
        assert isinstance(effects[0], tasks.ProcessSlot) if may_background else effects[0] is None


async def test_unset_mode_keeps_background_admission_unchanged(app, monkeypatch):
    from litetui import tasks
    app._launch_options = LaunchOptions()
    app.settings.tool_auto_background_s = 5
    monkeypatch.setattr(tasks, "backgroundable", forbidden)
    assert rewrite.tool_refusal(app, "powershell", {"background": True}) is None


@pytest.mark.parametrize("phase", ["scheduled", "maintenance", "readiness"])
async def test_stop_before_first_request_emits_cancelled_segment_without_provider(app, monkeypatch, phase):
    from types import SimpleNamespace

    from litetui import model_transport
    from litetui.app import LiteTUI

    events, entered, release = [], asyncio.Event(), asyncio.Event()
    async def gate():
        entered.set()
        await release.wait()
    async def ready():
        return None
    monkeypatch.setattr(app, "_await_mcp_maintenance", gate if phase == "maintenance" else ready)
    monkeypatch.setattr(app, "_ensure_chat_ready", gate if phase == "readiness" else forbidden)
    monkeypatch.setattr(app, "_stream_worker", LiteTUI._stream_worker.__get__(app))
    monkeypatch.setattr(app, "_resync_ctx_if_stale", lambda: None)
    monkeypatch.setattr(app, "_paint_now", lambda: None)
    monkeypatch.setattr(app, "_settle_turn_stop_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_thinking_done", lambda: None)
    monkeypatch.setattr(app, "_glassbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_all_tools", list)
    monkeypatch.setattr(app, "_request_messages", lambda: copy.deepcopy(app.conversation))
    monkeypatch.setattr(app, "_assistant_bubble", lambda: SimpleNamespace(body=SimpleNamespace(content="")))
    monkeypatch.setattr(app, "_elapsed", SimpleNamespace(start=lambda *a, **kw: None, stop_body=lambda: None))
    monkeypatch.setattr(model_transport, "for_app", forbidden)
    monkeypatch.setattr(app, "_rpc_emit", lambda event, **kwargs: events.append((event, kwargs.get("origin"))))
    review = reviewed(app)
    assert (await apply(app, review))["turn"] == "accepted"
    if phase != "scheduled":
        await asyncio.wait_for(entered.wait(), 2)
    before = disk(app)
    assert app.stop_turn_over_rpc()
    release.set()
    proof = await asyncio.wait_for(settle(app, review), 3)
    assert disk(app) == before and app._turn_abandoned
    assert [event["type"] for event, _origin in events] == ["turn_start", "turn_end"]
    assert events[1][0]["stopReason"] == "cancelled"
    assert events[0][0]["rewrite_segment"] == {"action_id": review["action_id"], "index": 0, "previous_execution_id": None}
    assert proof["execution"] == {"segments": 1, "last_execution_id": events[0][1].execution_id}


@pytest.mark.parametrize("role", ["compaction", "continuation"])
async def test_stop_retires_scheduled_descendants_before_new_boundary_or_effect(app, monkeypatch, role):
    events = []
    @rewrite.captured_work(role, group="controlled-descendant", exit_on_error=False)
    async def child(_app):
        forbidden()
    async def stream(origin):
        rewrite.segment(app, origin)
        child(app)
        assert app.stop_turn_over_rpc()
    monkeypatch.setattr(app, "_stream_worker", stream)
    monkeypatch.setattr(app, "_rpc_emit", lambda *args, **kwargs: events.append(args))
    review = reviewed(app)
    assert (await apply(app, review))["turn"] == "accepted"
    proof = await settle(app, review)
    assert proof["execution"]["segments"] == 1 and not events
    attempt = rewrite._required_attempt(app)
    assert attempt.owned and all(item.worker.is_finished for item in attempt.owned)


@pytest.mark.parametrize("phase", ["published", "before-publication"])
async def test_rpc_stop_question_keeps_exact_origin_without_abort_task_admission(app, monkeypatch, phase):
    import threading

    from litetui import ask_user_question as auq
    from litetui import rpc

    events, results = [], []
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    def emit(event):
        events.append(copy.deepcopy(event))
        if event["type"] == "user_input_requested":
            loop.call_soon_threadsafe(entered.set)
    def begin_wait(*_args):
        if phase == "before-publication":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    monkeypatch.setattr(app, "_begin_wait", begin_wait)
    monkeypatch.setattr(app, "_end_wait", lambda *_args: None)
    states = auq._parse_questions({"questions": [{"label": "Controlled", "question": "Continue?",
        "options": [{"title": "Yes", "description": "controlled"}]}]})
    async def stream(origin):
        rewrite.segment(app, origin)
        fn, call = rewrite.capture_tool(app, lambda _: auq._run_over_rpc(states, app))
        results.append(await rewrite.track_tool(app, call, asyncio.to_thread(fn, {})))
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await asyncio.create_task(apply(app, review)))["turn"] == "accepted"
    await asyncio.wait_for(entered.wait(), 2)
    assert rewrite.event_origin(app) is None and not rewrite._authorized(app)
    with pytest.raises(ValueError):
        app._validate_owned_execution()
    if phase == "published":
        ask_id, pending = next(iter(app._rpc_pending_asks.items()))
        assert rewrite.question_abort_origins(app)[ask_id].operation_id == "rewrite-execution"
        replaced = (pending[0], pending[1], pending[2])
        app._rpc_pending_asks[ask_id] = replaced
        assert ask_id not in rewrite.question_abort_origins(app)
        rewrite.forget_question(app, ask_id, replaced)
        assert ask_id in rewrite._required_attempt(app).questions
        app._rpc_pending_asks[ask_id] = pending
    rpc._dispatch(app, {"type": "abort", "id": "different-abort-operation"})
    release.set()
    proof = await asyncio.wait_for(settle(app, review), 3)
    envelopes = [event for event in events if event["type"] == "gui.event"]
    if phase == "published":
        requested, resolved = envelopes
        assert requested["event"]["type"] == "user_input_requested"
        assert resolved["event"]["type"] == "user_input_resolved" and resolved["event"]["reason"] == "abort"
        assert resolved["event"]["id"] == requested["event"]["id"]
        for field in ("operation_id", "execution_id", "session_id", "source"):
            assert resolved[field] == requested[field]
        assert resolved["operation_id"] == "rewrite-execution"
        assert resolved["execution_id"] == proof["execution"]["last_execution_id"]
    else:
        assert not envelopes
    assert len(results) == 1 and "ABORTED" in results[0]
    assert not app._rpc_pending_asks and not rewrite._required_attempt(app).questions
    assert rewrite.event_origin(app) is None and not rewrite._authorized(app)
    replies = [event for event in events if event["type"] == "response"]
    assert replies[-1]["result"]["stopped"]


@pytest.mark.parametrize("closing", [False, True])
async def test_retiring_thread_tool_never_opens_a_new_hook_approval(app, monkeypatch, closing):
    import json
    import sys
    import threading

    from litetui import lifecycle_hooks, tool_approval, tool_policy

    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    app.tools_enabled = True
    app.hook_config.project_path.write_text(json.dumps({"version": 1, "hooks": [{
        "id": "after", "mode": "observe", "events": ["tool_after"],
        "executable": sys.executable, "argv": ["-c", "print('must need approval')"]}]}))
    def tool(_args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return "controlled result"
    app.plugins.add_tool("test", {"type": "function", "function": {"name": "rewrite_probe",
        "description": "controlled", "parameters": {"type": "object", "properties": {}}}},
        tool, policy=tool_policy.READ_POLICY)
    monkeypatch.setattr(lifecycle_hooks, "run_hook", forbidden)
    monkeypatch.setattr(tool_approval, "approve_over_rpc", forbidden)
    async def stream(origin):
        rewrite.segment(app, origin)
        await app._execute_tool("rewrite_probe", {})
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await asyncio.create_task(apply(app, review)))["turn"] == "accepted"
    await asyncio.wait_for(entered.wait(), 2)
    before = disk(app)
    if closing:
        finished = asyncio.create_task(rewrite.settle_before_shutdown(app))
        await asyncio.sleep(0)
    else:
        assert app.stop_turn_over_rpc()
        finished = asyncio.create_task(settle(app, review))
    release.set()
    assert (await asyncio.wait_for(finished, 3))["worker_settled"]
    assert disk(app) == before
    result = app.hook_results[("project", "after")]
    assert not result.allowed and "approval unavailable" in result.reason
    attempt = rewrite._required_attempt(app)
    assert attempt.tool_calls and all(call.future.done() and call.producer.done() and call.retired.is_set()
                                      for call in attempt.tool_calls)


@pytest.mark.parametrize("second_hook", [None, "confirm", "allow"])
async def test_shutdown_retires_existing_tool_after_approval_through_exact_producer(app, monkeypatch, second_hook):
    import json
    import sys

    from litetui import lifecycle_hooks, rpc, tool_policy

    entered, events = asyncio.Event(), []
    app.tools_enabled = True
    definitions = [{"id": "after", "mode": "observe", "events": ["tool_after"],
        "executable": sys.executable, "argv": ["-c", "print('must need approval')"]}]
    if second_hook is not None:
        definitions.append({"id": "after-next", "mode": "observe", "events": ["tool_after"],
            "executable": sys.executable, "argv": ["-c", "print('controlled cleanup')"]})
    app.hook_config.project_path.write_text(json.dumps({"version": 1, "hooks": definitions}))
    if second_hook == "allow":
        hook = app.hook_config.read("project")[1]
        workspace = app._hook_workspace
        arguments = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": {}}
        app.settings.tool_always_allow = [tool_policy.rule_key(
            hook.approval_name(workspace), tool_policy.SHELL_POLICY.classify(arguments, workspace))]
    app.plugins.add_tool("test", {"type": "function", "function": {"name": "rewrite_probe",
        "description": "controlled", "parameters": {"type": "object", "properties": {}}}},
        lambda _: "controlled result", policy=tool_policy.READ_POLICY)
    def emit(event):
        events.append(copy.deepcopy(event))
        if event["type"] == "tool_approval_requested":
            entered.set()
    cleanup = []
    async def run_hook(hook, _document, _workspace, **options):
        assert second_hook == "allow" and hook.id == "after-next"
        cleanup.append(hook.id)
        result = lifecycle_hooks.HookResult(True, exit_code=0)
        options["on_settled"](result, {"process_started": True, "process_exited": True,
            "pipes_settled": True, "kill_requested": False, "kill_confirmed": True})
        return result
    monkeypatch.setattr(lifecycle_hooks, "run_hook", run_hook)
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    monkeypatch.setattr(app, "_begin_wait", lambda *_args: None)
    monkeypatch.setattr(app, "_end_wait", lambda *_args: None)
    async def stream(origin):
        rewrite.segment(app, origin)
        await app._execute_tool("rewrite_probe", {})
    monkeypatch.setattr(app, "_stream_worker", stream)
    review = reviewed(app)
    assert (await asyncio.create_task(apply(app, review)))["turn"] == "accepted"
    await asyncio.wait_for(entered.wait(), 2)
    attempt = rewrite._required_attempt(app)
    assert attempt.tool_calls and attempt.tool_calls[0].retired.is_set()
    assert not attempt.tool_calls[0].producer.done()
    proof = await asyncio.wait_for(rewrite.settle_before_shutdown(app), 3)
    assert proof["worker_settled"] and proof["hooks_settled"]
    assert not app._approval_waiters and not app._approval_authority_records
    assert len([event for event in events if event["type"] == "tool_approval_requested"]) == 1
    assert len(app.conversation[-1]["approval_delivery"]) == 1
    assert cleanup == (["after-next"] if second_hook == "allow" else [])
    if second_hook == "confirm":
        result = app.hook_results[("project", "after-next")]
        assert not result.allowed and "approval unavailable" in result.reason
    requested = next(event for event in events if event["type"] == "gui.event" and event["event"]["type"] == "tool_approval_requested")
    closed = next(event for event in events if event["type"] == "gui.event" and event["event"]["type"] == "tool_approval_closed")
    assert closed["event"]["id"] == requested["event"]["id"]
    assert closed["event"]["outcome"] == "cancelled"
    for field in ("operation_id", "execution_id", "session_id", "source"):
        assert closed[field] == requested[field]
    audit = app.conversation[-1]["approval_delivery"][closed["event"]["id"]]
    assert audit["outcome"] == "cancelled"
    assert all(call.future.done() and call.producer.done() and call.retired.is_set() for call in attempt.tool_calls)
