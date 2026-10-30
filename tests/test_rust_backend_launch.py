"""Actual CLI/owned process contract; external engines, providers and hardware are synthetic."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/backend_runtime.py"
BACKENDS = ("custom", "lmstudio", "llamacpp", "ninfer", "strata", "cline", "free")


def launch(tmp_path, backend, *flags, case="success", gpu="5090", thinking="default", driver="self"):
    root = tmp_path / "data"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({
        "backend": backend, "default_model": "fixture-model", "backend_chosen": True,
        "user_name_asked": True, "tools_enabled": False, "mcp_enabled": False,
        "skills_enabled": False, "footer_task_manager": False, "tts_enabled": False,
        "sidecar_enabled": False, "tool_policy_profile": "strict",
        "autocompact_enabled": False, "tool_auto_background_s": 0,
    }), encoding="utf-8")
    environment = dict(os.environ, RUST_BACKEND_SOURCE=str(ROOT), LITETUI_DATA_ROOT=str(root),
                       RUST_BACKEND_CASE=case, RUST_BACKEND_GPU=gpu, RUST_BACKEND_DRIVER=driver, PYTHONPATH=str(ROOT / "src"),
                       FIXTURE_BACKEND_KEY="synthetic-only-token", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_CODEX_ENGINE", None)
    args = [sys.executable, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
            "--backend", backend, "--model", "fixture-model", "--thinking-level", thinking,
            "--tool-profile", "strict", "--mcp-servers", "none", "--server-timeout", "5"]
    if backend in BACKENDS[:5]:
        if "--server-mode" not in flags and "--start-server" not in flags:
            args.extend(["--server-mode", "connect"])
        if backend != "ninfer" or "--start-server" not in flags:
            args.extend(["--base-url", "http://127.0.0.1:45123/v1"])
    args.extend(flags)
    commands = [
        {"type": "gui.hello", "id": "hello", "protocol_version": 1},
        {"type": "gui.state", "id": "state"},
        {"type": "prompt", "id": "supervised-operation", "message": "Controlled RPC prompt", "tool_profile": "strict"},
        {"type": "shutdown", "id": "shutdown"},
    ] if driver == "rpc" else []
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            input="".join(json.dumps(command) + "\n" for command in commands),
                            text=True, encoding="utf-8", timeout=40, check=False)
    (tmp_path / "source-events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "source-stderr.txt").write_text(result.stderr, encoding="utf-8")
    return result, root


def rows(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("backend", BACKENDS)
def test_actual_owned_startup_produces_generic_events_without_implicit_load_or_start(tmp_path, backend):
    events = rows(launch(tmp_path, backend)[0])
    ready = next(row for row in events if row["type"] == "ready")
    assert ready["launch_status"] == "ready", events
    assert ready["backend"] == backend and ready["model"] == "fixture-model"
    assert ready["tool_profile"] == ready["tool_profile_requested"] == "strict"
    assert ready["approval_route"] == "host" and ready["tools_enabled"] is False
    report = next(row for row in events if row["type"] == "fixture_report")
    assert report["authority"] == {"backend": backend, "model": "fixture-model"}
    assert report["backend"] == backend and not report["denied"]
    assert not any(call["effect"] in {"load", "engine-start", "server-start"} for call in report["calls"])
    assert report["requests"] == [{"backend": backend, "model": "fixture-model", "max_tokens": 4096}]
    envelopes = [row for row in events if row["type"] == "gui.event"
                 and row["event"]["type"] in {"turn_start", "text_delta", "reasoning_delta", "turn_end"}]
    assert {row["event"]["type"] for row in envelopes} == {"turn_start", "text_delta", "reasoning_delta", "turn_end"}
    assert len({row["execution_id"] for row in envelopes}) == 1
    assert all(row["session_id"] == ready["conversation_id"] and
               row["operation_id"] == "backend-operation" for row in envelopes)
    terminal = next(row["event"] for row in envelopes if row["event"]["type"] == "turn_end")
    assert terminal["stopReason"] == "stop" and terminal.get("error") is None
    assert any(row["type"] == "usage" for row in events)


@pytest.mark.parametrize("backend", BACKENDS[:5])
def test_selected_endpoint_failure_blocks_source_ready_and_never_submits(tmp_path, backend):
    events = rows(launch(tmp_path, backend, case="dead")[0])
    ready = next(row for row in events if row["type"] == "ready")
    report = next(row for row in events if row["type"] == "fixture_report")
    assert ready["launch_status"] == "blocked"
    assert report["requests"] == [] and report["backend"] == backend
    assert not any(call["effect"] in {"load", "engine-start", "server-start"} for call in report["calls"])
    if backend == "llamacpp":
        assert not any(call["effect"] == "ensure" for call in report["calls"])


@pytest.mark.parametrize("gpu", ["5080", "", "absent", "error"])
def test_real_ninfer_gate_rejects_ineligible_observation_before_agent_creation(tmp_path, gpu):
    result, root = launch(tmp_path, "ninfer", gpu=gpu)
    assert result.returncode != 0 and "NInfer requires an RTX 5090" in result.stderr
    assert not (root / ".agents").exists()
    assert '"type": "ready"' not in result.stdout



@pytest.mark.parametrize("backend", BACKENDS)
def test_unavailable_selected_model_never_falls_back_or_submits(tmp_path, backend):
    events = rows(launch(tmp_path, backend, case="unavailable")[0])
    ready = next(row for row in events if row["type"] == "ready")
    report = next(row for row in events if row["type"] == "fixture_report")
    assert ready["launch_status"] == "blocked"
    assert report["requests"] == [] and report["authority"]["model"] == "fixture-model"


@pytest.mark.parametrize("backend", BACKENDS[:5])
def test_explicit_start_reaches_only_the_selected_controlled_backend(tmp_path, backend):
    flags = ["--start-server"]
    if backend == "custom":
        flags.extend(["--server-command", json.dumps(["fixture-server", "--model", "{model}", "--port", "{port}"])])
    elif backend in {"ninfer", "strata"}:
        flags.extend(["--model-path", "synthetic-model-artifact", "--context-length", "8192"])
    events = rows(launch(tmp_path, backend, *flags)[0])
    ready = next(row for row in events if row["type"] == "ready")
    report = next(row for row in events if row["type"] == "fixture_report")
    assert ready["launch_status"] == "ready" and report["launch"]["server_mode"] == "start"
    assert report["requests"][0]["backend"] == backend
    effects = [call["effect"] for call in report["calls"]]
    assert "load" not in effects
    if backend in {"ninfer", "strata"}:
        assert effects.count("engine-start") == 1
    elif backend in {"custom", "lmstudio"}:
        assert effects.count("server-start") == 1
    if backend == "custom":
        assert effects.count("server-stop") == 1


@pytest.mark.parametrize("backend", ["llamacpp", "lmstudio"])
def test_explicit_load_precedes_model_ready_and_stays_invocation_only(tmp_path, backend):
    flags = ["--load-model", "--context-length", "8192"]
    if backend == "llamacpp":
        flags.append("--start-server")
    events = rows(launch(tmp_path, backend, *flags)[0])
    report = next(row for row in events if row["type"] == "fixture_report")
    effects = [call["effect"] for call in report["calls"]]
    assert effects.count("load") == 1 and effects.index("load") < effects.index("chat-ready")
    loaded = next(call for call in report["calls"] if call["effect"] == "load")
    assert loaded == {"effect": "load", "backend": backend, "model": "fixture-model", "context": 8192}


def test_unsupported_explicit_thinking_is_refused_without_downgrade(tmp_path):
    events = rows(launch(tmp_path, "lmstudio", thinking="low")[0])
    ready = next(row for row in events if row["type"] == "ready")
    report = next(row for row in events if row["type"] == "fixture_report")
    assert ready["launch_status"] == "blocked" and "unsupported" in ready["launch_error"]
    assert ready["thinking_level"] == "low" and report["requests"] == []


def test_custom_token_limit_and_credential_name_are_invocation_only(tmp_path):
    result, _root = launch(tmp_path, "custom", "--max-tokens", "17", "--api-key-env", "FIXTURE_BACKEND_KEY")
    events = rows(result)
    report = next(row for row in events if row["type"] == "fixture_report")
    assert report["requests"][0]["max_tokens"] == 17
    assert report["launch"]["api_key_env"] == "FIXTURE_BACKEND_KEY"
    assert "synthetic-only-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("backend", (*BACKENDS, "claude", "codex"))
def test_default_effort_means_omission_in_the_canonical_resolver(backend):
    from litetui.turn_engine import TurnEngine
    assert TurnEngine.resolve_reasoning_effort("default", backend, "fixture-model", ["fixture-model"]) is None
    assert TurnEngine.resolve_reasoning_effort("off", backend, "fixture-model", ["fixture-model"]) == "none"


@pytest.mark.parametrize("backend,level,expected", [
    ("custom", "high", "high"), ("llamacpp", "low", "low"), ("ninfer", "high", "xhigh"),
    ("strata", "max", "high"), ("lmstudio", "medium", "medium"),
])
def test_explicit_effort_mapping_is_preserved(backend, level, expected):
    from litetui.turn_engine import TurnEngine
    assert TurnEngine.resolve_reasoning_effort(level, backend, "fixture-model", ["fixture-model"]) == expected


@pytest.mark.parametrize("flag,environment,expected", [
    (None, None, None), (None, "native", "native"), (None, "http", "http"),
    ("http", "native", "http"), ("native", "http", "native"),
])
def test_codex_engine_source_precedence_is_preserved(monkeypatch, flag, environment, expected):
    import argparse

    from litetui.launch_options import add_arguments, from_args
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="codex")
    add_arguments(parser)
    if environment is None:
        monkeypatch.delenv("LITETUI_CODEX_ENGINE", raising=False)
    else:
        monkeypatch.setenv("LITETUI_CODEX_ENGINE", environment)
    options = from_args(parser.parse_args(["--codex-engine", flag] if flag else []))
    assert options.codex_engine == expected


@pytest.mark.parametrize("backend", ["custom", "lmstudio"])
def test_controlled_rpc_driver_uses_actual_prompt_dispatch_and_source_envelopes(tmp_path, backend):
    result, root = launch(tmp_path, backend, driver="rpc")
    events = rows(result)
    assert not any(row["type"] == "fixture_report" for row in events)
    report = json.loads((root / "backend-report.json").read_text())
    assert report["requests"] and not report["denied"]
    commands = [json.loads(line) for line in (root / "backend-commands.jsonl").read_text().splitlines()]
    assert [row["type"] for row in commands] == ["gui.hello", "gui.state", "prompt", "shutdown"]
    assert any(row["type"] == "response" and row["id"] == "shutdown" and row["ok"] for row in events)
    terminal = [row for row in events if row["type"] == "gui.event" and row["event"]["type"] == "turn_end"]
    assert len(terminal) == 1 and terminal[0]["operation_id"] == "supervised-operation"
    assert terminal[0]["event"]["stopReason"] == "stop"


@pytest.mark.parametrize("mode", ["connect", "auto"])
def test_llama_context_rebuild_without_start_is_rejected_before_agent_creation(tmp_path, mode):
    result, root = launch(tmp_path, "llamacpp", "--server-mode", mode, "--load-model",
                          "--context-length", "8192")
    assert result.returncode != 0 and "context changes require --start-server" in result.stderr
    assert not (root / ".agents").exists()
    assert '"type": "ready"' not in result.stdout
