import asyncio
import json
import os
import socket
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

import httpx
import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import (
    gui_rpc,
    native_connection_preferences,
    settings_runtime,
    settings_scope,
)
from litetui.native_connection_preferences import FIELDS, SPECS

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio


def call(owner, operation, **kw):
    return gui_rpc.dispatch(
        owner,
        {
            "type": native_connection_preferences.PREFIX + operation,
            "id": "connection-" + operation,
            "version": 1,
            **work.identity(owner),
            "work_instance": work.bridge(owner).instance,
            **kw,
        },
    )


def block_network_and_processes(stack, owner):
    """Any connection, model load, reconnect, probe or process start fails the test."""

    def forbidden(*_args, **_kwargs):
        pytest.fail("Connection preferences contacted a host, loaded a model or started a process")

    for target, name in (
        (httpx.Client, "send"),
        (httpx.AsyncClient, "send"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (subprocess.Popen, "__init__"),
        (os, "system"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (owner, "_apply_context_length"),
        (owner, "apply_backend_change"),
        (owner, "connect"),
        (owner.backend, "load"),
        (owner.backend, "unload"),
        (owner.backend, "list_models"),
        (owner.backend, "ensure_running"),
        (settings_runtime, "prepare_reconnect"),
    ):
        stack.enter_context(patch.object(target, name, forbidden))


def path_of(owner, scope):
    if scope == "device":
        return owner._agent_session.store.data_root / "settings.json"
    return owner.convo_dir / "settings.json"


def saved_value(owner, field):
    document = json.loads(path_of(owner, SPECS[field].scope).read_bytes())
    if SPECS[field].scope == "conversation":
        document = document.get("execution", document)
    return document[field]


async def test_every_field_matches_the_registry_and_free_tier_keys_stay_unreadable(armed):
    current = call(armed, "preferences")
    assert tuple(current["fields"]) == FIELDS
    for key, row in current["fields"].items():
        registered = settings_scope.SETTING_SPECS[key]
        assert (row["scope"], row["timing"]) == (registered.scope.value, registered.apply_timing)
        assert "Saving never reconnects" in row["effect"]
    # Names only, never a value and never even whether one is set.
    from litetui.sidecar_settings import SECRET_FIELDS

    assert current["key_settings"] == {"names": sorted(SECRET_FIELDS), "readable": False}
    assert not (set(FIELDS) & SECRET_FIELDS)


async def test_secret_values_never_reach_a_response_or_error(armed):
    armed.settings.groq_api_key = "gsk_SECRET_SENTINEL_VALUE"
    armed.settings.github_models_token = "ghp_SECRET_SENTINEL_VALUE"
    path = armed._agent_session.store.data_root / "settings.json"
    document = json.loads(path.read_bytes())
    document["groq_api_key"] = "gsk_SECRET_SENTINEL_VALUE"
    path.write_text(json.dumps(document), encoding="utf-8")
    current = call(armed, "preferences")
    review = call(
        armed, "review", revision=current["revision"], field="pin_default_model", value=True
    )
    applied = call(armed, "apply", token=review["token"])
    observed = call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert "SECRET_SENTINEL" not in json.dumps([current, review, applied, observed])
    with pytest.raises(ValueError) as refusal:
        call(
            armed,
            "review",
            revision=observed["after"],
            field="custom_api_key_env",
            value="gsk_SECRET_SENTINEL_VALUE",
        )
    assert "SECRET_SENTINEL" not in str(refusal.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lm_host", "http://127.0.0.1:19999"),
        ("pin_default_model", True),
        ("default_context_length", 32768),
        ("custom_base_url", "http://127.0.0.1:19998/v1"),
        ("custom_api_key_env", "LITETUI_TRIPWIRE_KEY"),
        ("custom_context_length", 4096),
        ("claude_executable", "C:/tools/claude.exe"),
        ("llama_host", "http://127.0.0.1:19997"),
        ("llama_attach_hosts", ["http://127.0.0.1:19996", "https://example.test:8443"]),
        ("llama_models_max", 3),
        ("lms_load_timeout_s", 900),
    ],
)
async def test_review_save_readback_follows_registry_timing_without_side_effects(
    armed, monkeypatch, field, value
):
    for env in ("LITETUI_LM_HOST", "LITETUI_MODEL", "LITETUI_LLAMA_HOST"):
        monkeypatch.delenv(env, raising=False)
    owner = armed
    owner._launch_overrides.pop(field, None)
    owner._invocation_saved_values.pop(field, None)
    spec = SPECS[field]
    effective_before = getattr(owner.settings, field)
    device_before = path_of(owner, "device").read_bytes()
    with ExitStack() as guard:
        block_network_and_processes(guard, owner)
        current = call(owner, "preferences")
        row = current["fields"][field]
        assert row["editable"], row
        review = call(owner, "review", revision=current["revision"], field=field, value=value)
        assert review["timing"] == spec.timing and review["scope"] == spec.scope
        assert "Saving never reconnects" in review["effect"]
        if spec.scope == "conversation":
            assert path_of(owner, "device").read_bytes() == device_before
        applied = call(owner, "apply", token=review["token"])
        with pytest.raises(ValueError):
            call(owner, "apply", token=review["token"])
        observed = call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    shown = observed["preferences"]["fields"][field]
    assert saved_value(owner, field) == value
    assert shown["saved"] == value
    immediately = spec.timing in ("next_turn", "immediate")
    # A reconnect/restart/reload field stays pending; only the documented timing applies it.
    assert getattr(owner.settings, field) == (value if immediately else effective_before)
    assert shown["effective"] == (value if immediately else effective_before)
    assert observed["after"] != current["revision"]
    if spec.scope == "conversation":
        assert path_of(owner, "device").read_bytes() == device_before


async def test_default_context_length_save_does_not_reload_the_model_unlike_the_dialog(armed):
    loaded = []
    with ExitStack() as guard:
        block_network_and_processes(guard, armed)
        guard.enter_context(
            patch.object(armed, "_apply_context_length", lambda **kw: loaded.append(kw))
        )
        current = call(armed, "preferences")
        review = call(
            armed, "review", revision=current["revision"], field="default_context_length", value=65536
        )
        assert "never does" in review["effect"]
        call(armed, "apply", token=review["token"])
    assert loaded == []
    assert armed.settings.default_context_length != 65536


@pytest.mark.parametrize(
    "field,value",
    [
        ("lm_host", "ftp://example.test"),
        ("lm_host", "http://user:pw@example.test"),
        ("lm_host", "http://example.test/?token=abc"),
        ("lm_host", "http://example.test/#frag"),
        ("lm_host", "localhost:1234"),
        ("lm_host", 1234),
        ("custom_base_url", "http://user@example.test/v1"),
        ("llama_attach_hosts", ["http://a.test", "http://a.test"]),
        ("llama_attach_hosts", "http://a.test"),
        ("custom_api_key_env", "sk-abcdefghijklmnop"),
        ("custom_api_key_env", "gsk_AbCdEf123456"),
        ("custom_api_key_env", "MixedCaseKey"),
        ("custom_api_key_env", "WITH SPACE"),
        ("default_context_length", 0),
        ("default_context_length", True),
        ("custom_context_length", -1),
        ("llama_models_max", 0),
        ("lms_load_timeout_s", 29),
        ("pin_default_model", 1),
        ("default_model", "x/y"),
        ("claude_executable", "bad\nline"),
        ("claude_executable", " padded "),
        ("llama_executable", "C:/definitely/not/a/llama-server.exe"),
        ("groq_api_key", "anything"),
        ("max_tokens_chat", 5),
    ],
)
async def test_source_validator_and_credential_rules_refuse_before_any_review(armed, field, value):
    current = call(armed, "preferences")
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field=field, value=value)
    assert getattr(armed, "_native_connection_review", None) is None


async def test_environment_and_launch_ownership_lock_fields(armed, monkeypatch):
    monkeypatch.setenv("LITETUI_LM_HOST", "http://127.0.0.1:1")
    armed._launch_overrides["custom_base_url"] = "http://127.0.0.1:2/v1"
    current = call(armed, "preferences")
    assert not current["fields"]["lm_host"]["editable"]
    assert "LITETUI_LM_HOST" in current["fields"]["lm_host"]["reason"]
    assert not current["fields"]["custom_base_url"]["editable"]
    for field, value in (("lm_host", "http://127.0.0.1:3"), ("custom_base_url", "http://127.0.0.1:4/v1")):
        with pytest.raises(ValueError):
            call(armed, "review", revision=current["revision"], field=field, value=value)


async def test_stored_url_credentials_are_redacted_and_the_field_is_locked(armed):
    path = path_of(armed, "conversation")
    document = json.loads(path.read_bytes())
    target = document.get("execution", document)
    target["lm_host"] = "http://user:hunter2@example.test:1234/?key=hunter2#hunter2"
    target["llama_attach_hosts"] = ["http://a.test", "https://tok:hunter2@b.test"]
    path.write_text(json.dumps(document), encoding="utf-8")
    armed.settings.lm_host = target["lm_host"]
    current = call(armed, "preferences")
    assert "hunter2" not in json.dumps(current)
    row = current["fields"]["lm_host"]
    assert row["saved"] == "http://***@example.test:1234/"
    assert not row["editable"] and "outside the native TUI" in row["reason"]
    assert not current["fields"]["llama_attach_hosts"]["editable"]
    assert current["fields"]["llama_attach_hosts"]["saved"][1] == "https://***@b.test"
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field="lm_host", value="http://ok.test")


async def test_unrepresentable_stored_values_are_hidden_not_echoed(armed):
    armed.settings.claude_executable = "x" * 5000
    armed.settings.custom_api_key_env = "bad\x1b[31mname"
    current = call(armed, "preferences")
    assert "xxxxx" not in json.dumps(current) and "\x1b" not in json.dumps(current)
    for field in ("claude_executable", "custom_api_key_env"):
        assert not current["fields"][field]["editable"]
    assert current["fields"]["custom_api_key_env"]["effective"] == native_connection_preferences.HIDDEN


async def test_stale_revision_wrong_owner_replay_and_changed_readback_are_refused(armed):
    current = call(armed, "preferences")
    review = call(armed, "review", revision=current["revision"], field="pin_default_model", value=True)
    path = path_of(armed, "conversation")
    saved = path.read_bytes()
    data = json.loads(saved)
    data["retained_other_tool"] = {"value": 42}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        call(armed, "apply", token=review["token"])
    assert json.loads(path.read_bytes()) == data
    path.write_bytes(saved)

    current = call(armed, "preferences")
    review = call(armed, "review", revision=current["revision"], field="pin_default_model", value=True)
    with pytest.raises(ValueError, match="again"):
        call(armed, "apply", token="0" * 64)
    applied = call(armed, "apply", token=review["token"])
    changed = path.read_bytes()
    data = json.loads(changed)
    target = data.get("execution", data)
    target["custom_context_length"] = 123
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after saving"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="again"):
        call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])


async def test_active_work_blocks_review(armed):
    current = call(armed, "preferences")
    armed._pending_input = ["queued"]
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field="pin_default_model", value=True)
