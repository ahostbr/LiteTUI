"""Reviewed manual compaction authority, canonical no-op and publication boundaries."""
from __future__ import annotations

import copy
from types import MethodType

import pytest
from test_rust_backend_transition import disk, forbidden
from test_rust_conversation_rewrite import app as _app
from test_rust_conversation_rewrite import switch_app as _switch_app
from textual.worker import WorkerFailed

from litetui import conversation_action as action
from litetui import conversation_compact as compact
from litetui import gui_rpc, native_history
from litetui import history_projection as projection

app = _app
switch_app = _switch_app
pytestmark = pytest.mark.asyncio


def selection(app):
    common = action._identity(app)
    history = native_history.snapshot(app, {**common, "from_session_id": app.convo_id,
                                            "scope": "current", "catalog_snapshot": None})
    return {**common, "id": "compact-read", "history_snapshot": history["snapshot"],
            "context_sha256": history["context_sha256"]}


def reviewed(app, hint="  Guidance \u00e9\U0001f9eae\u0301\n  "):
    command = selection(app)
    snapshot = compact.snapshot(app, command)
    return compact.validate(app, {**command, "id": "compact-validate", "snapshot": snapshot["snapshot"], "hint": hint})


async def apply(app, review):
    return await gui_rpc.async_dispatch(app, {**review, "id": "compact-operation", "type": compact.PREFIX + "apply"})


async def settled(app, review):
    return await gui_rpc.async_dispatch(app, {**{key: review[key] for key in compact.COMMON},
        "id": "compact-settle", "type": compact.PREFIX + "settled", "action_id": review["action_id"],
        "operation_id": "compact-operation"})


def fake_root(app, monkeypatch, callback):
    async def _compact(self, _hint):
        return await callback(self)
    monkeypatch.setattr(app, "_compact", MethodType(action.captured_work("compaction", exclusive=True, group="chat")(_compact), app))


async def test_pure_review_binds_exact_safe_tail_prompt_configuration_and_hint(app, monkeypatch):
    app.settings.compact_keep_recent = 2
    call = {"id": "safe-tail", "type": "function", "function": {"name": "fixture", "arguments": "{}"}}
    app.conversation.extend([{"role": "assistant", "content": None, "tool_calls": [call]},
                             {"role": "tool", "tool_call_id": "safe-tail", "content": "saved"},
                             {"role": "assistant", "content": "tail"}])
    app._snapshot("safe tail fixture")
    before = disk(app)
    monkeypatch.setattr(app, "_compact", forbidden)
    review = reviewed(app)
    assert review["plan"]["tail_messages"] == 3
    assert review["plan"]["tail_context_sha256"] == projection.digest(projection.encoded(app.conversation[-3:]))
    assert review["plan"]["config_sha256"] == projection.digest(projection.encoded(review["config"]))
    assert review["request_prompt_sha256"] == projection.digest((compact._prompt() + "\n\n" + review["hint"]).encode())
    assert disk(app) == before and not app._rewrite_test_requests


@pytest.mark.parametrize("which", ["too_short", "already_recent"])
async def test_actual_canonical_noop_has_root_pair_zero_streams_and_typed_witness(app, monkeypatch, which):
    if which == "too_short":
        app.conversation = app.conversation[:2]
        app._snapshot("short")
    else:
        app.settings.compact_keep_recent = 100
    events = []
    monkeypatch.setattr(app, "_rpc_emit", lambda data, *, origin=None: events.append((copy.deepcopy(data), origin)))
    review = reviewed(app)
    before = disk(app)
    admission = await apply(app, review)
    assert admission["status"] == "accepted", admission
    proof = await settled(app, review)
    assert proof["outcome"] == "no_change" and proof["reason_code"] == which
    assert proof["execution"] == {"segments": 0, "last_execution_id": None}
    assert proof["witness"]["root_started"] and not proof["witness"]["publication_attempted"]
    assert not proof["history_changed"] and disk(app) == before
    markers = [(data, origin) for data, origin in events if data["type"] == "compact_maintenance"]
    assert [data["phase"] for data, _ in markers] == ["start", "end"]
    assert all(data["root"] and data["after_execution_id"] is None and
               origin.execution_id == review["root_execution_id"] and origin.operation_id == "compact-operation"
               for data, origin in markers)


async def test_stop_before_scheduled_root_still_closes_root_without_callback(app, monkeypatch):
    fake_root(app, monkeypatch, forbidden)
    events = []
    monkeypatch.setattr(app, "_rpc_emit", lambda data, *, origin=None: events.append(data))
    review = reviewed(app)
    assert (await apply(app, review))["status"] == "accepted"
    assert action.stop(app)
    proof = await settled(app, review)
    assert proof["outcome"] == "cancelled" and not proof["history_changed"]
    assert [event["phase"] for event in events if event["type"] == "compact_maintenance"] == ["start", "end"]


@pytest.mark.parametrize("fault", ["owner", "codex", "native", "attachment"])
async def test_capabilities_not_provider_name_determine_readonly_review(app, monkeypatch, fault):
    if fault == "owner":
        app._owner_memory_dir = app.convo_dir
    elif fault == "codex":
        app.backend.app_server = object()
    elif fault == "native":
        app.backend.owns_native_turns = True
    else:
        app.pending_image = "NEVER_READ"
    monkeypatch.setattr(app, "_compact", forbidden)
    before = disk(app)
    command = selection(app)
    state = compact.snapshot(app, command)
    assert not state["eligible"]
    with pytest.raises(ValueError):
        compact.validate(app, {**command, "snapshot": state["snapshot"], "hint": ""})
    assert disk(app) == before


@pytest.mark.parametrize("fault", ["version", "nested_integer", "nested_boolean", "hint", "root"])
async def test_apply_exact_typed_echo_is_not_python_numeric_equality(app, monkeypatch, fault):
    review = reviewed(app)
    wrong = copy.deepcopy(review)
    if fault == "version": wrong["version"] = True
    elif fault == "nested_integer": wrong["config"]["keep_recent"] = float(wrong["config"]["keep_recent"])
    elif fault == "nested_boolean": wrong["effects"]["provider_execution"] = 1
    elif fault == "hint": wrong["hint"] += "x"
    else: wrong["root_execution_id"] = "foreign"
    monkeypatch.setattr(app, "_compact", forbidden)
    before = disk(app)
    with pytest.raises(ValueError, match="exactly echo"):
        await apply(app, wrong)
    assert disk(app) == before and action._attempt(app) is None


@pytest.mark.parametrize("fault", ["settings", "history"])
async def test_pre_effect_drift_refuses_with_fresh_checkpoint(app, fault):
    review = reviewed(app)
    if fault == "settings": app.settings.compact_max_tokens += 1
    else:
        app.conversation[-1]["content"] = "External coherent edit"
        app._snapshot("external")
    before = disk(app)
    receipt = await apply(app, review)
    assert receipt["status"] == "refused" and not receipt["close_required"]
    proof = await settled(app, review)
    assert proof["history"] == receipt["refusal_history"] and proof["root_history"] is None
    assert proof["reason_code"] == "admission_refused" and disk(app) == before


@pytest.mark.parametrize("fault", ["none", "silent", "wrong", "receipts", "rawcap"])
async def test_exact_publication_readback_precedes_success_witness(app, monkeypatch, fault):
    expected = [copy.deepcopy(app.conversation[0]), {"role": "user", "content": "Summary \u754c"},
                {"role": "assistant", "content": "Retained context"}]
    original = app.store.write_record
    if fault == "silent": monkeypatch.setattr(app.store, "write_record", lambda _record: None)
    elif fault == "wrong":
        monkeypatch.setattr(app.store, "write_record", lambda record: original({**record, "prepend": []}))
    elif fault == "receipts":
        def corrupt_receipts(record):
            original(record)
            original(app.store.edit_record(0, expected[0], "corrupt receipt", store_injected=False))
        monkeypatch.setattr(app.store, "write_record", corrupt_receipts)
    async def publish(self):
        publication = compact.begin_publication(self, expected)
        record = self.store.truncate_record(len(self.conversation), expected[1:], True, "test")
        if fault == "receipts":
            record["store_injected"] = False
        if fault == "rawcap":
            raw = native_history.capture_file(self.convo_path).raw or b""
            monkeypatch.setattr(native_history, "MAX_TRANSCRIPT", len(raw) + len(self.store.record_bytes(record)) - 1)
        compact.write_publication_record(self, record)
        self.conversation = copy.deepcopy(expected)
        compact.verify_publication(self, publication)
        return compact.CompactOutcome("compacted", "compacted", publication_attempted=True, publication_verified=True)
    fake_root(app, monkeypatch, publish)
    review = reviewed(app)
    assert (await apply(app, review))["status"] == "accepted"
    if fault == "none":
        proof = await settled(app, review)
        assert proof["outcome"] == "compacted" and proof["witness"]["publication_verified"]
        assert proof["history"]["context_sha256"] == projection.digest(projection.encoded(expected))
    else:
        with pytest.raises(WorkerFailed):
            await settled(app, review)
        assert action.submission_refusal(app) == action.CLOSE_REQUIRED


async def test_both_negotiated_kinds_share_join_but_keep_exact_last_identity(app):
    from test_rust_conversation_rewrite import apply as rewrite_apply
    from test_rust_conversation_rewrite import reviewed as rewrite_reviewed
    from test_rust_conversation_rewrite import settle as rewrite_settle
    first = rewrite_reviewed(app)
    await rewrite_apply(app, first)
    await rewrite_settle(app, first)
    app.settings.compact_keep_recent = 100
    second = reviewed(app)
    await apply(app, second)
    await settled(app, second)
    proofs = await action.settle_all_before_shutdown(app)
    assert proofs["conversation_rewrite_shutdown"]["action_id"] == first["action_id"]
    assert proofs["conversation_compact_shutdown"]["action_id"] == second["action_id"]
    assert app._conversation_compact_shutdown_task.result() == proofs["conversation_compact_shutdown"]
    assert app._conversation_rewrite_shutdown_task.result() == proofs["conversation_rewrite_shutdown"]
    assert not any(value["pending_at_close"] for value in proofs.values())

@pytest.mark.parametrize("key,value", [("compact_keep_recent", -1), ("compact_keep_recent", True),
    ("compact_max_tokens", 255), ("compact_max_tool_iters", 513), ("compact_thinking_level", "unknown")])
async def test_configuration_outside_wire_bounds_never_gets_an_action(app, key, value):
    setattr(app.settings, key, value)
    before = disk(app)
    with pytest.raises(ValueError, match="Unsupported compaction"):
        compact.snapshot(app, selection(app))
    assert disk(app) == before and action._attempt(app) is None


@pytest.mark.parametrize("hint", ["", " \n", "x" * 65536, "\u754c" * 21845, "\x00", "x" * 65537],
                         ids=["empty", "whitespace", "ascii-limit", "utf8-limit", "nul", "over-limit"])
async def test_literal_hint_limits_are_utf8_without_command_dispatch(app, monkeypatch, hint):
    monkeypatch.setattr(app, "_handle_command", forbidden)
    if "\x00" in hint or len(hint.encode()) > 65536:
        with pytest.raises(ValueError, match="65536"):
            reviewed(app, hint)
    else:
        review = reviewed(app, hint)
        assert review["hint"] == hint and review["hint_sha256"] == projection.digest(hint.encode())
    assert not app._rewrite_test_requests


async def test_root_worker_retirement_without_typed_outcome_never_means_success(app, monkeypatch):
    async def untyped(_app):
        return None
    fake_root(app, monkeypatch, untyped)
    review = reviewed(app)
    assert (await apply(app, review))["status"] == "accepted"
    with pytest.raises(WorkerFailed, match="typed invocation"):
        await settled(app, review)
    assert action.submission_refusal(app) == action.CLOSE_REQUIRED


async def test_exact_readback_scope_does_not_transfer_to_inherited_task(app, monkeypatch):
    import asyncio
    denied = []
    async def probe(self):
        async def foreign():
            assert not action.readback_allowed(self) and not action._authorized(self)
            with pytest.raises(ValueError):
                self._validate_owned_execution()
            denied.append(True)
        with action.readback(self):
            assert action.readback_allowed(self)
            await asyncio.create_task(foreign())
        assert not action.readback_allowed(self)
        return compact.CompactOutcome("no_change", "too_short")
    fake_root(app, monkeypatch, probe)
    review = reviewed(app)
    await apply(app, review)
    await settled(app, review)
    assert denied == [True]


async def test_invalid_selection_still_negotiates_identity_free_shutdown(app):
    command = selection(app)
    command["history_snapshot"] = "0" * 64
    with pytest.raises(ValueError, match="history changed"):
        compact.snapshot(app, command)
    proof = (await action.settle_all_before_shutdown(app))["conversation_compact_shutdown"]
    assert proof["worker_settled"] and not proof["pending_at_close"]
    assert all(proof[key] is None for key in ("action_id", "operation_id", "session_id", "agent_id", "backend", "model"))

async def test_projected_client_cannot_bypass_review_via_legacy_compact(app, monkeypatch):
    monkeypatch.setattr(app, "_handle_command", forbidden)
    before = disk(app)
    with pytest.raises(ValueError, match="reviewed native"):
        await gui_rpc.async_dispatch(app, {"id": "legacy-compact", "type": "gui.conversations.compact"})
    assert disk(app) == before and action._attempt(app) is None
    app._launch_options.history_projection_version = None
    action.operation_allowed(app, "gui.conversations.compact")

@pytest.mark.parametrize("route", ["prompt", "run_command", "handle", "submit"])
@pytest.mark.parametrize("text", ["/compact", " \t/CoMpAcT literal hint\n"])
async def test_legacy_slash_compact_refuses_before_receipt_or_dispatch(app, monkeypatch, route, text):
    from litetui import rpc
    from litetui.app import LiteTUI
    before = disk(app)
    responses, events = [], []
    monkeypatch.setattr(app, "_compact", forbidden)
    monkeypatch.setattr(app, "_rpc_emit", lambda data, **_kwargs: events.append(data))
    monkeypatch.setattr(rpc, "_respond", lambda ident, **fields: responses.append((ident, fields)))
    if route in {"prompt", "run_command"}:
        rpc._dispatch(app, {"type": route, "id": "slash", "message" if route == "prompt" else "line": text,
                            "tool_profile": "autonomous"})
        assert len(responses) == 1 and responses[0][1]["ok"] is False
        assert "reviewed native compaction" in responses[0][1]["error"]
    elif route == "handle":
        LiteTUI._handle_command(app, text)
    else:
        LiteTUI._submit_text(app, text, False, source="rpc")
        assert events[-1]["reason"] == "reviewed_compaction"
    assert disk(app) == before and action._attempt(app) is None and not app._rewrite_test_requests
    app._launch_options.history_projection_version = None
    assert compact.command_refusal(app, text) is None


async def test_compact_command_guard_does_not_claim_other_commands(app):
    assert compact.command_refusal(app, "/compactness") is None
    assert compact.command_refusal(app, "/new") is None
    assert compact.command_refusal(app, "say /compact") is None

@pytest.mark.parametrize("fault", ["identity", "readonly"])
async def test_late_transcript_identity_or_permission_drift_precedes_any_append(app, monkeypatch, fault):
    import stat
    changed = []
    original = app.store.write_record
    path = app.convo_path
    mode = path.stat().st_mode
    async def drift(self):
        if fault == "identity":
            original({"type": "meta", "id": "00000000-0000-4000-8000-000000000000"})
        else:
            path.chmod(stat.S_IREAD)
        changed.append(path.read_bytes())
        monkeypatch.setattr(self.store, "write_record", forbidden)
        compact.begin_publication(self, copy.deepcopy(self.conversation))
        raise AssertionError("publication should have refused")
    fake_root(app, monkeypatch, drift)
    review = reviewed(app)
    try:
        assert (await apply(app, review))["status"] == "accepted"
        with pytest.raises(WorkerFailed, match="not coherent"):
            await settled(app, review)
        assert path.read_bytes() == changed[0]
        assert not action._required_attempt(app).mutated
    finally:
        path.chmod(mode)


async def test_actual_hello_advertises_all_reviewed_compact_operations(app):
    hello = gui_rpc.dispatch(app, {"type": "gui.hello", "protocol_version": 1, "id": "hello"})
    assert all("gui.conversations.native_compact_" + name in hello["operations"]
               for name in ("snapshot", "validate", "apply", "settled"))
