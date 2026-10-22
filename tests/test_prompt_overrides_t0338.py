"""Scoped text and actual host admission/request seams; isolated stores only."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from litetui import paths, prompt_commands as commands, prompt_overrides as prompts, hook_host
from _owned_seat import owned_app as LiteTUI


def command(directory, name="ordinary-instructions.md", text="new guidance", **changes):
    return {"type": commands.APPLY, "id": str(uuid4()), "action": "set",
            "seat": str(uuid4()), "convo": directory.name, "name": name,
            "reason": "improve this seat", "text": text,
            "global_sha": prompts.sha(prompts.read_text(paths.prompt_file(name))),
            "actor": str(uuid4()), "notification_sent": None, **changes}


def layer(tmp_path, name, text):
    folder = tmp_path / "prompts"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder / name


def test_resolution_partial_and_reset_retains_revision(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "card-summary.md").write_text("repo card", encoding="utf-8")
    monkeypatch.setattr(paths, "_REPO_PROMPTS", repo)
    layer(tmp_path, "compact.md", "seat compact")
    assert paths.prompt_file("compact.md", convo_dir=tmp_path).read_text() == "seat compact"
    assert paths.prompt_file("card-summary.md", convo_dir=tmp_path).read_text() == "repo card"
    assert paths.prompt_file("wake-after-compact.md", convo_dir=tmp_path).parent == paths.PROMPTS_DIR
    cmd = command(tmp_path, "compact.md", "next compact")
    commands.apply(tmp_path, cmd)
    reset = {**cmd, "id": str(uuid4()), "action": "reset", "text": ""}
    commands.apply(tmp_path, reset)
    assert (tmp_path / "prompts" / "compact.md").read_text() == "next compact"
    assert paths.prompt_file("compact.md", convo_dir=tmp_path) == paths.prompt_file("compact.md")
    assert len(list((tmp_path / "prompts" / "revisions").iterdir())) == 2


@pytest.mark.parametrize("name", ["tool-denied.md", "systemprompt.md", "tools.md", "claude-tools.md",
                                  "plan-mode.md", "conversation-store.md", "harness-capabilities.md", "security.md"])
def test_forbidden_override_refuses_even_inactive(tmp_path, name):
    layer(tmp_path, name, "unsafe")
    (tmp_path / "prompts" / (name + ".json")).write_text(json.dumps({"active": False}))
    with pytest.raises(prompts.PromptError, match="metadata|global-only"):
        paths.prompt_file(name, convo_dir=tmp_path)
    with pytest.raises(prompts.PromptError, match="metadata|global-only"):
        prompts.refresh(SimpleNamespace(convo_dir=tmp_path))


@pytest.mark.parametrize("name", ["../compact.md", "COMPACT.md", "compact.md:stream", "a/b.md", "a\\b.md"])
def test_name_refusal(tmp_path, name):
    with pytest.raises(ValueError):
        paths.prompt_file(name, convo_dir=tmp_path)


def test_link_refusal(tmp_path, monkeypatch):
    # Exercise the real storage resolver against OS link metadata without
    # creating a symlink/junction as a side effect of this task's test run.
    import stat
    from pathlib import Path

    original = Path.lstat

    def lstat(path):
        if path == tmp_path / "prompts":
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(prompts.PromptError, match="Linked"):
        paths.prompt_file("compact.md", convo_dir=tmp_path)


def test_actual_turn_admission_next_request_and_midturn_stability(tmp_path, monkeypatch):
    app = LiteTUI()
    app._system = lambda text: None
    monkeypatch.setattr(hook_host, "refuse_below_floor", lambda *args: False)
    app._materialise_convo()
    app._request_messages()  # perform the existing one-time store injection first
    original = app.conversation[0]["content"]
    layer(app._agent_session.memory_root, "ordinary-instructions.md", "FIRST ORDINARY")
    assert hook_host.accept_prompt(app, {"content": "first user", "source": "typed"})
    first = app._request_messages()[0]["content"]
    assert "FIRST ORDINARY" in first and "Shared-worktree caveat" in first
    layer(app._agent_session.memory_root, "ordinary-instructions.md", "SECOND ORDINARY")
    assert app._request_messages()[0]["content"] == first
    # A side-call fresh read must not mutate the main turn snapshot.
    assert app._ordinary_prompt_text("ordinary-instructions", fresh=True) == "SECOND ORDINARY"
    assert app._request_messages()[0]["content"] == first
    assert hook_host.accept_prompt(app, {"content": "second user", "source": "typed"})
    second = app._request_messages()[0]["content"]
    assert "SECOND ORDINARY" in second and "FIRST ORDINARY" not in second
    assert app.conversation[0]["content"] == original
    assert app.conversation[-1]["content"] == "second user"
    reset = command(app._agent_session.memory_root, action="reset", text="")
    commands.apply(app._agent_session.memory_root, reset)
    hook_host.accept_prompt(app, {"content": "third user", "source": "typed"})
    third = app._request_messages()[0]["content"]
    assert "SECOND ORDINARY" not in third and "## Engineering practices" in third


def test_custom_system_delimiters_are_not_host_owned(tmp_path):
    app = LiteTUI()
    custom = "custom\n\n[LiteTUI ordinary instructions]\nDO NOT CHANGE\n[End LiteTUI mutable instructions]"
    app.conversation[:] = [{"role": "system", "content": custom}]
    app._store_injected = None
    app._system = lambda _: None
    assert app._request_messages()[0]["content"] == custom


def test_branch_global_drift_and_audit_dedup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compact.md").write_text("global v1\n")
    monkeypatch.setattr(paths, "_REPO_PROMPTS", repo)
    cmd = command(tmp_path, "compact.md", "seat v1\n")
    result, _ = commands.apply(tmp_path, cmd)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN" and result["notification_sent"] is None
    result, _ = commands.apply(tmp_path, cmd)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN"
    (repo / "compact.md").write_text("global v2\n")
    report = commands.inspect(tmp_path, "compact.md", diff=True)
    assert "Global drift: true" in report and "-global v1" in report and "+global v2" in report
    result, _ = commands.apply(tmp_path, {**cmd, "notification_sent": False})
    assert result["status"] == "APPLIED_NOTIFICATION_FAILED"
    rows = [json.loads(s) for s in (tmp_path / "prompts" / "audit.jsonl").read_text().splitlines()]
    assert [r["phase"] for r in rows] == ["intent", "applied", "notification"]
    assert rows[0]["who"] == cmd["actor"] and rows[0]["reason"] == cmd["reason"]
    assert "+seat v1" in rows[0]["diff"]
    with pytest.raises(ValueError, match="reused"):
        commands.apply(tmp_path, {**cmd, "text": "other"})
    with pytest.raises(ValueError, match="Conflicting"):
        commands.apply(tmp_path, {**cmd, "notification_sent": True})
