"""T618: data-root opt-in never changes resources or the default anchor."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("override", [False, True])
def test_data_root_is_opt_in_and_shared(tmp_path, override):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONUTF8="1")
    env.pop("LITETUI_DATA_ROOT", None)
    if override:
        env["LITETUI_DATA_ROOT"] = str(tmp_path)
    code = """
import json
from litetui import paths, settings
print(json.dumps(dict(root=str(paths.ROOT), convos=str(paths.CONVO_DIR),
                     settings=str(settings.settings_path()), prompts=str(paths.PROMPTS_DIR))))
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    expected = tmp_path if override else root
    assert Path(data["root"]) == root
    assert Path(data["prompts"]) == root / "src" / "litetui" / "prompts"
    assert Path(data["convos"]) == expected / ".convos"
    assert Path(data["settings"]) == expected / "settings.json"


def test_unset_keeps_literal_live_background_location_without_io(monkeypatch):
    from litetui import paths, tasks
    monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    monkeypatch.setattr(paths, "ROOT", Path("C:/Projects/LiteTUI"))
    assert paths.data_root() / tasks.STORE == Path("C:/Projects/LiteTUI/background-tasks.json")


def test_override_persist_restart_resume(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONUTF8="1",
               LITETUI_DATA_ROOT=str(tmp_path))
    create = """
from litetui import paths, settings
from litetui.conversation import ConversationRepository
s = settings.Settings()
s.seat_name = 'isolated-seat'
settings.save(s)
p = paths.CONVO_DIR / 'probe' / 'convo.jsonl'
r = ConversationRepository()
r.adopt(p, 'probe')
r.record_msg({'role':'user', 'content':'survives restart'})
"""
    resume = """
from litetui import paths, settings
from litetui.conversation import ConversationRepository
assert settings.load().seat_name == 'isolated-seat'
assert ConversationRepository.read(paths.CONVO_DIR / 'probe' / 'convo.jsonl')[1] == [
    {'role':'user','content':'survives restart'}]
"""
    for code in (create, resume):
        subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=15, check=True)


def test_runtime_log_default_follows_override(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from litetui import runtime_log
    from litetui.plugins import runtime_log_plugin
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    captured = []
    monkeypatch.setattr(runtime_log, "install", captured.append)
    runtime_log_plugin.register(SimpleNamespace(observe=lambda fn: None))
    assert captured == [tmp_path / ".logs" / "runtime.jsonl"]


def test_unset_scheduler_and_log_paths_are_unchanged(monkeypatch):
    from litetui import paths, runtime_log, scheduler
    monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    monkeypatch.setattr(paths, "ROOT", Path("C:/Projects/LiteTUI"))
    assert scheduler.jobs_path(paths.data_root()) == Path("C:/Projects/LiteTUI/jobs.json")
    assert runtime_log.default_log_path(paths.data_root()) == Path("C:/Projects/LiteTUI/.logs/runtime.jsonl")
