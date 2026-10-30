"""Engine review must not materialize a launch or inspect live hardware."""
from __future__ import annotations

import json
import socket
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import strata_engine as engine
from litetui import ttyguard
from litetui.llm_backend import BackendError


def forbidden(*_args, **_kwargs):
    raise AssertionError("Launch inspection must not run resource, process or network operations")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "install"
    python = engine.server_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / "serve").mkdir()
    (root / "serve/server.py").write_bytes(b"")
    config = root / "strata-selected.json"
    config.write_text(json.dumps({"exe": str(root / "strata.exe"), "model_name": "selected",
                                 "port": 8191, "args": ["--max-context", "4096"]}), encoding="utf-8")
    logs = tmp_path / "retained" / "logs" / "strata.log"
    monkeypatch.setattr(engine, "log_path", lambda: logs)
    for module, names in ((subprocess, ("Popen",)), (socket, ("create_connection",)),
                          (urllib.request, ("urlopen",)), (ttyguard, ("popen", "run")),
                          (engine, ("memory_free_bytes", "gpu_memory_mib", "engine_process_alive",
                                    "refuse_reason", "start"))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    settings = SimpleNamespace(strata_root=str(root), strata_config=str(config),
                               strata_host="", strata_max_context=None)
    return settings, config, logs


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("context", [None, 4096, 65536])
def test_inspection_preserves_files_and_reports_canonical_recipe(installation, tmp_path, context):
    settings, config, logs = installation
    settings.strata_max_context = context
    before = files(tmp_path)
    plan = engine.inspect_launch(settings)
    assert plan.host == "http://127.0.0.1:8191"
    assert plan.cwd == Path(settings.strata_root)
    assert plan.config["model_name"] == "selected"
    assert engine.config_context(plan.config) == (context or 4096)
    chosen = Path(plan.argv[plan.argv.index("--config") + 1])
    if context == 65536:
        assert plan.derived_config == logs.with_name("strata-selected.litetui.json") == chosen
        assert plan.config["args"][-2:] == ["--kv", "int8"]
        assert not chosen.exists()
    else:
        assert plan.derived_config is None and chosen == config
    assert not logs.parent.exists()
    assert files(tmp_path) == before


def test_existing_launch_plan_materializes_the_inspected_copy_only(installation):
    settings, config, _logs = installation
    settings.strata_max_context = 65536
    original = config.read_bytes()
    reviewed = engine.inspect_launch(settings)
    assert reviewed.derived_config is not None and not reviewed.derived_config.exists()
    argv, cwd, actual, host = engine.launch_plan(settings)
    assert (tuple(argv), cwd, actual, host) == (reviewed.argv, reviewed.cwd, reviewed.config, reviewed.host)
    assert json.loads(reviewed.derived_config.read_text(encoding="utf-8")) == reviewed.config
    assert config.read_bytes() == original


@pytest.mark.parametrize("fault", ["host", "context", "install", "config"])
def test_invalid_inspection_cannot_write_a_derived_config(installation, tmp_path, fault):
    settings, config, logs = installation
    settings.strata_max_context = 65536
    if fault == "host":
        settings.strata_host = "http://127.0.0.1:no-port"
    elif fault == "context":
        settings.strata_max_context = 999999
    elif fault == "install":
        engine.server_python(Path(settings.strata_root)).unlink()
    else:
        config.write_text("invalid", encoding="utf-8")
    before = files(tmp_path)
    with pytest.raises(BackendError):
        engine.inspect_launch(settings)
    assert not logs.parent.exists()
    assert files(tmp_path) == before
