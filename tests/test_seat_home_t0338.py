"""Per-home publication, catalog readers, reset and actual-tier invariants."""
from uuid import uuid4

import pytest
from _owned_seat import owned_app
from test_prompt_commands_t0338 import owners, request  # noqa: F401 (shared fixture)

from litetui import paths, seat_prompts
from litetui import prompt_commands as commands
from litetui import prompt_overrides as prompts


@pytest.mark.asyncio
async def test_parent_can_edit_complete_home_before_materialized_turn_and_after_new_convo(owners, monkeypatch):  # noqa: F811 - imported pytest fixture
    leader, worker = owners
    worker.store.pending = True
    worker.seat.tier = "worker"

    async def attach(identity, command):
        assert identity == worker.seat.agent_id
        return await commands.dispatch(worker, command)

    monkeypatch.setattr(commands, "attach", attach)
    cmd = request(worker, convo=None, name="systemprompt.md", text="CUSTOM HOME BASE",
                  global_sha=prompts.sha(prompts.read_text(paths.prompt_file("systemprompt.md"))))
    result = await commands.dispatch(leader, cmd)
    assert result["status"] == "APPLIED"
    home = worker._agent_session.memory_root
    assert (home / "prompts" / "systemprompt.md").read_text() == "CUSTOM HOME BASE"
    assert not (worker.convo_dir / "prompts").exists()
    worker.convo_id = str(uuid4())
    worker.convo_dir = worker._agent_session.conversation_directory(worker.convo_id)
    worker.store.convo_id, worker.store.convo_dir = worker.convo_id, worker.convo_dir
    assert prompts.refresh(worker).texts["systemprompt.md"] == "CUSTOM HOME BASE"
    reset = {**cmd, "id": str(uuid4()), "action": "reset", "text": ""}
    result = await commands.dispatch(leader, reset)
    assert result["status"] == "APPLIED"
    assert prompts.refresh(worker).texts["systemprompt.md"] == prompts.read_text(paths.prompt_file("systemprompt.md"))
    assert (home / "prompts" / "systemprompt.md").read_text() == "CUSTOM HOME BASE"


@pytest.mark.parametrize("name", ["role.md", "method.md"])
def test_dynamic_reset_is_explicitly_unsupported(name, tmp_path):
    cmd = {"type": commands.APPLY, "id": str(uuid4()), "action": "reset", "seat": str(uuid4()),
           "convo": None, "name": name, "reason": "reset", "text": "", "global_sha": prompts.sha(""),
           "actor": str(uuid4()), "notification_sent": None}
    with pytest.raises(ValueError, match="no global default"):
        commands.validate(cmd, applying=True)
    with pytest.raises(ValueError, match="no global default"):
        commands.apply(tmp_path, cmd)
    assert not (tmp_path / "prompts").exists()


def test_full_home_readers_and_live_snapshot_keep_frozen_store(tmp_path):
    from litetui import owner_mode
    from litetui.textfmt import load_prompt, tool_denied
    app = owned_app()
    app._materialise_convo()
    root = app._agent_session.memory_root
    folder = root / "prompts"
    (root / "memory.md").write_text("FROZEN STORE BODY", encoding="utf-8")
    app._plan_mode = True
    first = app._request_messages()[0]["content"]
    (root / "memory.md").write_text("NEW DISK BODY", encoding="utf-8")
    for name, text in {"systemprompt.md": "NEW BASE", "ordinary-instructions.md": "NEW ORDINARY",
                       "tools.md": "NEW TOOLS", "plan-mode.md": "NEW PLAN",
                       "harness-capabilities.md": "NEW CAPABILITIES", "claude-tools.md": "NEW CLAUDE TOOLS",
                       "conversation-store.md": "NEW ADDRESS {convo_id} {store_path}",
                       "compact-owner.md": "OWNER {dir}\nSTEP 2 SUMMARY"}.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "tool-denied.md").write_text("## profile\nCUSTOM DENIED {name}: {reason}\n", encoding="utf-8")
    app._plan_mode = True
    assert app._request_messages()[0]["content"] == first, "midturn prompt snapshot changed"
    prompts.refresh(app)
    text = app._request_messages()[0]["content"]
    for expected in ("NEW BASE", "NEW ORDINARY", "NEW TOOLS", "NEW PLAN", "NEW ADDRESS", "FROZEN STORE BODY"):
        assert expected in text
    assert "NEW DISK BODY" not in text
    assert load_prompt("harness-capabilities", app=app) == "NEW CAPABILITIES"
    assert load_prompt("claude-tools", app=app) == "NEW CLAUDE TOOLS"
    assert tool_denied("profile", app=app, name="write", reason="blocked") == "CUSTOM DENIED write: blocked"
    assert owner_mode.compact_prompt(tmp_path, app=app).startswith("OWNER ")


def test_nonexistent_wrapper_never_uses_existing_pure_counterpart(tmp_path):
    pure = tmp_path / "pure"
    pure.mkdir()
    (pure / "gamma.md").write_text("# POLYMATHIC GAMMA\nMETHOD", encoding="utf-8")
    with pytest.raises(ValueError, match="does not exist"):
        seat_prompts.method_text(tmp_path / "workers" / "gamma.md")


def test_system_command_retains_actual_role_and_selected_method(tmp_path):
    from litetui.plugins.convo import _cmd_system
    method = tmp_path / "method.md"
    method.write_text("# POLYMATHIC GAMMA\nMETHOD", encoding="utf-8")
    app = owned_app(cognitive_file=str(method))
    app.system_message = lambda _: None
    _cmd_system(app, "/system", "NEW CALLER BODY")
    text = app._request_messages()[0]["content"]
    assert text.count("# BUILDER ROLE") == 1 and text.count("# POLYMATHIC GAMMA") == 1
    assert "NEW CALLER BODY" in text
