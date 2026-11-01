"""Independent reviewed compaction preference tests; no external execution."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import gui_rpc, settings, settings_runtime, settings_service
from litetui.app import LiteTUI
from litetui.settings_apply import RuntimeSettingStatus, SettingsSaveResult

switch_app = _controlled_app
PREFIX = "gui.settings.native_compact_"
VALUES = {
    "compact_keep_recent": 2, "compact_max_tokens": 1024, "compact_max_tool_iters": 3,
    "compact_thinking_level": "high", "wake_after_compact": True,
    "clear_screen_after_compact": False,
}
BOUNDS = {"compact_keep_recent": (0, 1000000), "compact_max_tokens": (256, 2147483647),
          "compact_max_tool_iters": (1, 512)}
CHOICES = ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


@pytest.fixture(autouse=True)
def unmounted_attachment(monkeypatch):
    # These pure service tests do not mount Textual; attachment authority is empty.
    monkeypatch.setattr(LiteTUI, "pending_image", None)


def common(app):
    return {"version": 1, "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "model": app.model_id}


async def call(app, operation, **fields):
    return await gui_rpc.async_dispatch(app, {"type": PREFIX + operation, "id": "preference-" + operation,
                                             **common(app), **fields})


async def draft(app, field="compact_keep_recent", value=2):
    state = await call(app, "snapshot")
    return {"snapshot": state["snapshot"], "revisions": state["revisions"], "field": field, "value": value}


async def reviewed(app, field="compact_keep_recent", value=2):
    return await call(app, "validate", **await draft(app, field, value))


async def save(app, review):
    return await call(app, "save", **{key: value for key, value in review.items() if key != "kind"})


def target(app, field):
    return ((app._agent_session.store.data_root if field == "clear_screen_after_compact" else app.convo_dir)
            / "settings.json")


def preserve(app, field=None):
    prefs = asdict(app.settings)
    if field:
        prefs.pop(field)
    return {"settings": prefs, "history": copy.deepcopy(app.conversation),
            "policy": copy.deepcopy(gui_rpc._tool_policy(app)), "authority": app._agent_session.authority,
            "pending": copy.deepcopy(app._pending_input), "store": (app.store.pending, app.store.convo_id),
            "handles": tuple(id(value) for value in (app.settings, app.backend, app.client, app.store,
                                                     app._agent_session, app.conversation))}


@pytest.mark.asyncio
async def test_snapshot_exact_fields_types_scopes_and_pure_review(switch_app):
    app = switch_app
    before, frozen = disk(app), preserve(app)
    state = await call(app, "snapshot")
    assert list(state["fields"]) == list(VALUES)
    assert state["destinations"] == {"global": str(target(app, "clear_screen_after_compact")),
                                    "conversation": str(target(app, "compact_keep_recent"))}
    for field, row in state["fields"].items():
        device = field == "clear_screen_after_compact"
        assert row["scope"] == ("device" if device else "conversation")
        assert row["apply_timing"] == ("immediate" if device else "next_turn")
        assert row["destination"] == ("global" if device else "conversation")
        assert row["saved"] == row["effective"] == getattr(app.settings, field)
        assert row["editable"] and row["reason"] is None
        if field in BOUNDS:
            assert row["type"] == "integer" and (row["minimum"], row["maximum"]) == BOUNDS[field]
        elif field == "compact_thinking_level":
            assert row["type"] == "choice" and row["choices"] == CHOICES
        else:
            assert row["type"] == "boolean" and type(row["saved"]) is bool
    assert state["tools"]["effective"] is app.tools_enabled and not state["tools"]["editable"]
    review = await reviewed(app)
    assert review["destination"] == str(target(app, "compact_keep_recent"))
    assert disk(app) == before and preserve(app) == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", VALUES.items())
async def test_exact_single_destination_save_preserves_unknowns_and_other_revisions(switch_app, field, value):
    app = switch_app
    path = target(app, field)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["unknown-extension"] = {"literal": "retain 雪", "enabled": True}
    path.write_text(json.dumps(raw), encoding="utf-8")
    before, frozen = disk(app), preserve(app, field)
    state = await call(app, "snapshot")
    review = await reviewed(app, field, value)
    result = await save(app, review)
    assert result["status"] == "saved" and result["mutated"] and not result["close_required"], json.dumps(result, ensure_ascii=False)
    assert result["saved"] == result["effective"] == value
    assert type(result["saved"]) is type(value)
    assert result["persistence"][0]["destination"] == str(path)
    assert result["persistence"][0]["fields"] == (field,)
    assert result["runtime"][0]["field"] == field and result["runtime"][0]["status"] == "applied"
    assert preserve(app, field) == frozen
    assert {name for name, body in disk(app).items() if before.get(name) != body} == {str(path)}
    destination = "global" if field == "clear_screen_after_compact" else "conversation"
    for key in state["revisions"]:
        if key != destination:
            assert result["revisions"][key] == state["revisions"][key]
    after_raw = json.loads(path.read_text(encoding="utf-8"))
    expected = copy.deepcopy(raw)
    (expected if destination == "global" else expected["execution"])[field] = value
    assert after_raw == expected
    fresh = await call(app, "snapshot")
    reopened = settings_service.SettingsService(app._agent_session.store.data_root,
                                                agent_session=app._agent_session).snapshot(app.convo_id, read_only=True)
    assert getattr(reopened.saved, field) == value
    assert fresh["revisions"] == result["revisions"]
    assert fresh["fields"][field]["saved"] == fresh["fields"][field]["effective"] == value


VALID_EDGES: list[tuple[str, Any]] = [(field, value) for field, pair in BOUNDS.items() for value in pair]
VALID_EDGES += [("compact_thinking_level", value) for value in CHOICES]
VALID_EDGES += [(field, value) for field in ("wake_after_compact", "clear_screen_after_compact")
                for value in (False, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", VALID_EDGES)
async def test_boundaries_and_exact_choice_vocabulary_validate_without_effects(switch_app, field, value):
    before = disk(switch_app), preserve(switch_app)
    review = await reviewed(switch_app, field, value)
    assert review["value"] == value and type(review["value"]) is type(value)
    assert (disk(switch_app), preserve(switch_app)) == before


INVALID: list[tuple[str, Any]] = [(field, value) for field, (low, high) in BOUNDS.items()
           for value in (low - 1, high + 1, True, False, None, "3", 1.5)]
BAD_BOOLEAN: tuple[Any, ...] = (0, 1, "true", None, [], {})
INVALID += [(field, value) for field in ("wake_after_compact", "clear_screen_after_compact")
            for value in BAD_BOOLEAN]
BAD_CHOICE: tuple[Any, ...] = ("none", "HIGH", "", True, 1, None, [], {})
INVALID += [("compact_thinking_level", value) for value in BAD_CHOICE]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", INVALID)
async def test_invalid_types_and_bounds_refuse_without_mutation(switch_app, field, value):
    cmd = await draft(switch_app, field, value)
    before = disk(switch_app), preserve(switch_app)
    with pytest.raises(ValueError):
        await call(switch_app, "validate", **cmd)
    assert (disk(switch_app), preserve(switch_app)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["version", "session_id", "agent_id", "backend", "model", "id", "extra"])
async def test_snapshot_identity_envelope_is_strict(switch_app, fault):
    command = {"type": PREFIX + "snapshot", "id": "valid", **common(switch_app)}
    command[fault] = {"version": True, "session_id": "foreign", "agent_id": "foreign", "backend": "foreign",
                      "model": "foreign", "id": "🛰" * 43, "extra": True}[fault]
    before = disk(switch_app), preserve(switch_app)
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(switch_app, command)
    assert (disk(switch_app), preserve(switch_app)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["version", "agent_id", "backend", "model", "snapshot", "revisions", "field",
                                  "value", "scope", "apply_timing", "destination", "saved_before",
                                  "effective_before", "resolved_value", "review", "action_id", "extra"])
async def test_full_review_tampering_consumes_review_and_cannot_replay(switch_app, key):
    review = await reviewed(switch_app)
    altered = copy.deepcopy(review)
    altered[key] = True if key in {"version", "value", "saved_before", "effective_before"} else "tampered"
    before = disk(switch_app), preserve(switch_app)
    result = await save(switch_app, altered)
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"], json.dumps(result, ensure_ascii=False)
    replay = await save(switch_app, review)
    assert replay["status"] == "refused" and not replay["mutated"], replay
    assert (disk(switch_app), preserve(switch_app)) == before


@pytest.mark.asyncio
async def test_superseded_review_is_refused_and_save_is_single_use(switch_app):
    old = await reviewed(switch_app)
    current = await reviewed(switch_app)
    assert old["review"] != current["review"] and old["action_id"] != current["action_id"]
    before = disk(switch_app)
    refused = await save(switch_app, old)
    assert refused["status"] == "refused" and disk(switch_app) == before
    current = await reviewed(switch_app)
    await call(switch_app, "snapshot")
    saved = await save(switch_app, current)
    assert saved["status"] == "saved", saved
    after = disk(switch_app)
    again = await save(switch_app, current)
    assert again["status"] == "refused" and disk(switch_app) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["global", "conversation", "agent", "history", "tools", "effective", "levels", "held", "busy"])
async def test_stale_review_refuses_before_writes(switch_app, monkeypatch, fault):
    app = switch_app
    review = await reviewed(app)
    if fault in {"global", "conversation", "agent"}:
        path = {"global": target(app, "clear_screen_after_compact"), "conversation": target(app, "compact_keep_recent"),
                "agent": app._agent_session.memory_root / "settings.json"}[fault]
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["independent-revision"] = "concurrent"
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif fault == "history":
        app.conversation.append({"role": "user", "content": "Concurrent history"})
    elif fault == "tools":
        app.tools_enabled = not app.tools_enabled
    elif fault == "effective":
        app.settings.compact_max_tokens += 1
    elif fault == "levels":
        monkeypatch.setattr(app.backend, "reasoning_levels", lambda _: ["high"])
    elif fault == "held":
        app._pending_input.append({"content": "Do not replay"})
    else:
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    before = disk(app), preserve(app)
    result = await save(app, review)
    assert result["status"] == "refused" and not result["mutated"], json.dumps(result, ensure_ascii=False)
    assert (disk(app), preserve(app)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["claude", "codex", "environment", "chat-overrides", "owner-mode"])
async def test_canonical_capability_and_environment_ownership(switch_app, monkeypatch, owner):
    app = switch_app
    if owner == "claude":
        app.backend.owns_native_turns = True
    elif owner == "codex":
        app.backend.app_server = SimpleNamespace(runtime_activity=SimpleNamespace(
            uncertain=False, commands={}, requests={}, connected=True), process=None)
    elif owner == "environment":
        monkeypatch.setitem(settings.ENV_OVERRIDES, "compact_max_tokens", "LITETUI_TEST_COMPACT_CAP")
        monkeypatch.setenv("LITETUI_TEST_COMPACT_CAP", "777")
    elif owner == "chat-overrides":
        from litetui.launch_options import LaunchOptions
        app._launch_options = LaunchOptions(max_tokens=777)
        app._cli_thinking_level = "high"
        app.settings.model_infer_overrides[app.model_id] = {"max_tokens": 333, "reasoning_effort": "off"}
    else:
        app._owner_memory_dir = app._agent_session.memory_root
    before = disk(app)
    state = await call(app, "snapshot")
    disabled = set(VALUES) if owner == "codex" else set(list(VALUES)[:4]) if owner == "claude" else (
        {"compact_max_tokens"} if owner == "environment" else set())
    for field, row in state["fields"].items():
        assert row["editable"] is (field not in disabled), (owner, field, row)
        if field in disabled:
            assert row["reason"]
            with pytest.raises(ValueError):
                await call(app, "validate", **await draft(app, field, VALUES[field]))
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["compact_max_tokens", "clear_screen_after_compact"])
@pytest.mark.parametrize("fault", ["silent", "torn", "wrong-field", "drop-unknown", "write-then-error",
                                   "receipt-destination", "receipt-fields", "receipt-revision", "runtime-raised"])
async def test_uncertain_publication_latches_closed_for_both_scopes(switch_app, monkeypatch, field, fault):
    app = switch_app
    path = target(app, field)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["unknown-must-survive"] = {"nested": 7}
    path.write_text(json.dumps(raw), encoding="utf-8")
    review = await reviewed(app, field, VALUES[field])
    write, persist = settings_service._write, settings_service.SettingsService.save_patch

    def faulty_write(destination, document, **kwargs):
        if destination != path:
            return write(destination, document, **kwargs)
        if fault == "silent":
            return settings_service._read(path)[1]
        if fault == "torn":
            path.write_text('{"torn":', encoding="utf-8")
            raise OSError("Controlled torn settings document")
        data = copy.deepcopy(document)
        if fault == "wrong-field":
            (data if field == "clear_screen_after_compact" else data["execution"])["compact_keep_recent"] = 999
        if fault == "drop-unknown":
            data.pop("unknown-must-survive")
        revision = write(destination, data, **kwargs)
        if fault == "write-then-error":
            raise OSError("Controlled write acknowledgement lost")
        return revision

    def faulty_receipt(service, *args, **kwargs):
        result = persist(service, *args, **kwargs)
        changes: dict[str, dict[str, Any]] = {"receipt-destination": {"destination": str(path.with_name("other.json"))},
                   "receipt-fields": {"fields": ("tools_enabled",)}, "receipt-revision": {"revision": "0" * 64}}
        if fault in changes:
            return replace(result, persistence=(replace(result.persistence[0], **changes[fault]),))
        return result

    monkeypatch.setattr(settings_service, "_write", faulty_write)
    monkeypatch.setattr(settings_service.SettingsService, "save_patch", faulty_receipt)
    if fault == "runtime-raised":
        monkeypatch.setattr(settings_runtime, "apply_saved_result", forbidden)
    result = await save(app, review)
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], json.dumps(result, ensure_ascii=False)
    with pytest.raises(ValueError, match="close this runtime"):
        app._validate_owned_execution()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["compact_max_tokens", "clear_screen_after_compact"])
@pytest.mark.parametrize("fault", ["refused", "runtime-failed"])
async def test_known_failure_reports_durable_and_effective_values_separately(switch_app, monkeypatch, field, fault):
    app = switch_app
    value, old = VALUES[field], getattr(app.settings, field)
    review = await reviewed(app, field, value)
    before = disk(app)
    if fault == "refused":
        monkeypatch.setattr(settings_service, "_write", lambda *_, **__: (_ for _ in ()).throw(OSError("Controlled refusal")))
    else:
        def failed_apply(_app, candidate, result):
            return SettingsSaveResult(result.persistence, (RuntimeSettingStatus(field, review["scope"],
                "failed", "retry", getattr(candidate, field), old, "Controlled runtime rejection"),))
        monkeypatch.setattr(settings_runtime, "apply_saved_result", failed_apply)
    result = await save(app, review)
    assert result["status"] == ("refused" if fault == "refused" else "partial"), json.dumps(result, ensure_ascii=False)
    assert not result["close_required"] and result["effective"] == old
    assert result["saved"] == (old if fault == "refused" else value)
    if fault == "refused":
        assert not result["mutated"] and disk(app) == before
    else:
        assert result["mutated"] and getattr(app.settings, field) == old


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["compact_keep_recent", "clear_screen_after_compact"])
async def test_absent_conversation_review_stays_absent_and_device_save_does_not_create_it(switch_app, field):
    path = switch_app.convo_dir / "settings.json"
    path.unlink()
    before = disk(switch_app)
    review = await reviewed(switch_app, field, VALUES[field])
    assert not path.exists() and disk(switch_app) == before
    result = await save(switch_app, review)
    assert result["status"] == "saved", json.dumps(result, ensure_ascii=False)
    assert path.exists() is (field != "clear_screen_after_compact")


@pytest.mark.asyncio
@pytest.mark.parametrize("location,field,value", [
    ("conversation", "compact_max_tokens", "1024"), ("conversation", "compact_max_tokens", True),
    ("conversation", "compact_keep_recent", False), ("conversation", "compact_thinking_level", "none"),
    ("conversation", "wake_after_compact", 1), ("global", "clear_screen_after_compact", 0),
    ("global-inherited", "compact_max_tool_iters", "3")])
async def test_invalid_raw_saved_preferences_are_not_silently_coerced(switch_app, location, field, value):
    app = switch_app
    if location == "global-inherited":
        (app.convo_dir / "settings.json").unlink()
    path = (app.convo_dir if location == "conversation" else app._agent_session.store.data_root) / "settings.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    (raw["execution"] if location == "conversation" else raw)[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = disk(app), preserve(app)
    with pytest.raises((ValueError, TypeError)):
        await call(app, "snapshot")
    assert (disk(app), preserve(app)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("level,resolved", [("default", None), ("off", "low"), ("high", "high"), ("ultra", "low")])
async def test_reasoning_explains_canonical_wire_resolution_without_probe(switch_app, monkeypatch, level, resolved):
    app = switch_app
    monkeypatch.setattr(app.backend, "reasoning_levels", lambda _: ["low", "high"])
    monkeypatch.setattr(app, "_probe_thinking", forbidden)
    app.settings.model_infer_overrides[app.model_id] = {"reasoning_effort": "max"}
    before = disk(app)
    review = await reviewed(app, "compact_thinking_level", level)
    assert review["resolved_value"] == resolved
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["persistence", "effective-type", "action", "applied-error"])
async def test_malformed_runtime_receipt_cannot_be_claimed_verified(switch_app, monkeypatch, fault):
    app = switch_app
    field, value = "wake_after_compact", True
    review = await reviewed(app, field, value)
    actual = settings_runtime.apply_saved_result

    def corrupt(*args, **kwargs):
        result = actual(*args, **kwargs)
        if fault == "persistence":
            return replace(result, persistence=(replace(result.persistence[0], destination="wrong.json"),))
        row = result.runtime[0]
        changes: dict[str, dict[str, Any]] = {"effective-type": {"effective": 1}, "action": {"action": "invented"},
                   "applied-error": {"reason": "Not actually applied"}}
        return replace(result, runtime=(replace(row, **changes[fault]),))

    monkeypatch.setattr(settings_runtime, "apply_saved_result", corrupt)
    result = await save(app, review)
    assert result["status"] == "failed" and result["close_required"], json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_legacy_conversation_materialization_keeps_unknown_keys(switch_app):
    app = switch_app
    path = app.convo_dir / "settings.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["unknown"] = {"keep": ["雪", False]}
    raw["execution"].pop("temperature")
    raw["execution"]["extension"] = "retain"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = disk(app)
    review = await reviewed(app)
    assert disk(app) == before
    result = await save(app, review)
    assert result["status"] == "saved", json.dumps(result, ensure_ascii=False)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["unknown"] == raw["unknown"] and after["execution"]["extension"] == "retain"
    assert after["execution"]["temperature"] == app.settings.temperature


def test_bounded_canonical_writer_counts_exact_platform_bytes_before_replace(tmp_path):
    import hashlib
    import os

    path = tmp_path / "bounded-settings.json"
    document = {"unicode": "雪", "nested": {"keep": [True, 3]}, "line": "literal\nvalue"}
    encoded = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").replace("\n", os.linesep).encode("utf-8")
    revision = settings_service._write(path, document, max_bytes=len(encoded))
    assert path.read_bytes() == encoded and revision == hashlib.sha256(encoded).hexdigest()
    assert settings_service._read(path, max_bytes=len(encoded)) == (document, revision)
    before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir() if entry.is_file()}
    with pytest.raises(ValueError):
        settings_service._write(path, document, max_bytes=len(encoded) - 1)
    with pytest.raises(ValueError):
        settings_service._read(path, max_bytes=len(encoded) - 1)
    assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir() if entry.is_file()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["capability", "levels", "attachment"])
async def test_post_apply_capability_or_input_drift_requires_close(switch_app, monkeypatch, fault):
    app = switch_app
    review = await reviewed(app)
    actual = settings_runtime.apply_saved_result

    def drift(*args, **kwargs):
        result = actual(*args, **kwargs)
        if fault == "capability":
            app.backend.owns_native_turns = True
        elif fault == "levels":
            monkeypatch.setattr(app.backend, "reasoning_levels", lambda _: ["high"])
        else:
            app.pending_image = "unrelated concurrent attachment"
        return result

    monkeypatch.setattr(settings_runtime, "apply_saved_result", drift)
    result = await save(app, review)
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], json.dumps(result)


@pytest.mark.asyncio
async def test_post_apply_client_replacement_cannot_claim_verified_save(switch_app, monkeypatch):
    app = switch_app
    review = await reviewed(app)
    actual, client = settings_runtime.apply_saved_result, app.client

    def replace_client(*args, **kwargs):
        result = actual(*args, **kwargs)
        app.client = object()  # Identity fault only; no provider client is constructed.
        return result

    monkeypatch.setattr(settings_runtime, "apply_saved_result", replace_client)
    try:
        result = await save(app, review)
    finally:
        app.client = client
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], json.dumps(result)
