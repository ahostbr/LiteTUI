"""Candidate app/CLI system-message contracts, not running-seat proof."""
from pathlib import Path

import pytest
from _owned_seat import owned_app

from litetui import prompt_overrides as prompts
from litetui import seat_prompts

CATALOG = Path("C:/Projects/LiteSuite/resources/liteharness-plugin/prompts/cognitive-architectures")


def source(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("role", ["leader", "worker", "reviewer"])
def test_one_actual_role_and_method_emitted_in_one_system(tmp_path, role):
    role_file = source(tmp_path, "role.md", f"# {role.upper()} ROLE — {role.title()} Mode\n\nGeneric role body")
    method = source(tmp_path, "method.md", "# POLYMATHIC GAMMA\n\nMethod only")
    app = owned_app(spawn_identity=("00000000-0000-4000-8000-000000000001", "probe", role),
                    role_prompt_file=role_file, cognitive_file=method)
    messages = app._request_messages()
    systems = [m["content"] for m in messages if m["role"] == "system"]
    assert len(systems) == 1
    assert systems[0].count(f"# {seat_prompts.DISPLAY_ROLES[role].upper()} ROLE") == 1
    assert systems[0].count("# POLYMATHIC GAMMA") == 1
    assert "Generic role body" in systems[0]


def test_caller_replaces_default_role_body_and_next_turn_changes_system(tmp_path):
    default = source(tmp_path, "default.md", "# WORKER ROLE — Worker Mode\n\nDEFAULT MUST GO")
    method = source(tmp_path, "method.md", "# POLYMATHIC GAMMA\n\nMethod")
    app = owned_app(system_prompt="CUSTOM BODY", role_prompt_file=default, cognitive_file=method)
    first = app._request_messages()[0]["content"]
    assert "CUSTOM BODY" in first and "DEFAULT MUST GO" not in first
    folder = app._agent_session.memory_root / "prompts"
    from uuid import uuid4

    from litetui import prompt_commands
    prompt_commands.apply(app._agent_session.memory_root, {
        "type": prompt_commands.APPLY, "id": str(uuid4()), "action": "set",
        "seat": app.seat.agent_id, "convo": None, "name": "role.md", "reason": "live update",
        "text": "LIVE BODY", "global_sha": prompts.sha(""),
        "actor": app.seat.agent_id, "notification_sent": None})
    prompts.refresh(app)
    second = app._request_messages()[0]["content"]
    assert "LIVE BODY" in second and "CUSTOM BODY" not in second
    app.conversation.clear()
    app._new_convo()
    app._load_system_prompt()
    assert "LIVE BODY" in app._request_messages()[0]["content"]
    assert all((folder / name).is_file() for name in prompts.MUTABLE)


@pytest.mark.parametrize("body", ["# LEADER ROLE — Leader Mode", "# WORKER ROLE\n# WORKER ROLE", "# POLYMATHIC GAMMA — Worker Mode"])
def test_role_scaffolds_refused(tmp_path, body):
    with pytest.raises(ValueError, match="role|Role|scaffold"):
        owned_app(system_prompt=body, spawn_identity=("00000000-0000-4000-8000-000000000001", "probe", "worker"))


def test_pure_catalog_not_header_splitter():
    if not CATALOG.is_dir():
        pytest.skip("canonical catalog not installed")
    for name in ("gamma", "miyamoto"):
        wrapped = CATALOG / "workers" / f"{name}.md"
        pure = CATALOG / "pure" / f"{name}.md"
        assert wrapped.is_file() and pure.is_file()
        assert seat_prompts.method_text(wrapped) == prompts.read_text(pure)
        assert not seat_prompts.MODE_HEADER.search(seat_prompts.method_text(wrapped))


@pytest.mark.parametrize("modern,wire", [("owner", "leader"), ("builder", "worker"), ("critic", "reviewer"), ("scout", "thinker"), ("orchestrator", "orchestrator")])
def test_aliases(modern, wire):
    assert seat_prompts.role_name(modern) == seat_prompts.role_name(wire) == wire
    assert seat_prompts.DISPLAY_ROLES[wire] == modern
    assert seat_prompts.role_body(f"# {modern.upper()} ROLE -- {modern.title()} Mode\nBODY", wire) == "BODY"
    assert seat_prompts.role_body(f"# {wire.upper()} ROLE -- {wire.title()} Mode\nBODY", wire) == "BODY"


@pytest.mark.parametrize("invalid", ["oversized-stripped-comment", "nul-stripped-comment"])
def test_human_system_raw_validation_preserves_active_revision(tmp_path, invalid):
    method = source(tmp_path, "method.md", "# POLYMATHIC GAMMA\nSELECTED METHOD")
    app = owned_app(system_prompt="PREVIOUS BODY", cognitive_file=method)
    first = app._request_messages()[0]["content"]
    root = app._agent_session.memory_root
    files = [root / "prompts" / name for name in ("role.md", "role.md.json", "audit.jsonl")]
    files.append(root / ".prompt-launch.json")
    before = {p: p.read_bytes() for p in files}
    prefix = "x" * prompts.MAX_BYTES if invalid == "oversized-stripped-comment" else "\0"
    raw = "<!--" + prefix + "-->\n# BUILDER ROLE\nSMALL BODY"
    assert seat_prompts.role_body(raw, "worker") == "SMALL BODY"
    with pytest.raises(prompts.PromptError, match="bounded UTF-8"):
        seat_prompts.set_system_body(app, raw)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert app._request_messages()[0]["content"] == first
    seat_prompts.set_system_body(app, "# BUILDER ROLE\nNORMAL VALID UPDATE")
    rendered = app._request_messages()[0]["content"]
    assert rendered.count("NORMAL VALID UPDATE") == 1 and "PREVIOUS BODY" not in rendered
    assert rendered.count("# BUILDER ROLE") == 1 and rendered.count("# POLYMATHIC GAMMA") == 1
    assert prompts.metadata(root, "role.md")["active"] is True
