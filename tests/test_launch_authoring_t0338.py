"""Explicit launch revisions use the shared audit publisher before real resume."""
import asyncio
import json
import sys
from uuid import uuid4

import pytest

from litetui import app as app_mod
from litetui import cli, harness, image_viewer, owner_mode
from litetui import prompt_overrides as prompts

RECAP = "\n\nEnd each FINAL answer with <recap>two short lines: what you did and result, about 40 tokens</recap>. Do not put this tag in interim tool calls."


@pytest.mark.parametrize("parented", [False, True])
def test_existing_owner_conversation_explicit_launch_revision(tmp_path, monkeypatch, parented):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    notes = tmp_path / "owner-notes"
    notes.mkdir()
    for filename in owner_mode.OWNER_FILES:
        (notes / filename).write_text(f"FROZEN OWNER NOTE {filename}", encoding="utf-8")
    lore = tmp_path / "lore.md"
    lore.write_text("FROZEN LORE EXACT\n", encoding="utf-8")
    monkeypatch.setenv("LITEHARNESS_LORE_FILE", str(lore))
    default = tmp_path / "default.md"
    default.write_text("# OWNER ROLE\nOLD DEFAULT ROLE BODY", encoding="utf-8")
    method = tmp_path / "method.md"
    method.write_text("# POLYMATHIC GAMMA\nEXACT SELECTED METHOD", encoding="utf-8")
    custom = tmp_path / "custom.md"
    custom.write_text("NEW AUTHORED OWNER BODY", encoding="utf-8")
    identity, parent = str(uuid4()), str(uuid4()) if parented else None
    name = "ExistingOwner"
    saved = {}
    observed = []
    # External executable availability and terminal image setup, not ownership,
    # transcript loading, store identity, lease acquisition or publication.
    monkeypatch.setattr(owner_mode, "require_handoff", lambda: None)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)

    def capture(app, **_kwargs):
        session = app._agent_session
        home = session.memory_root
        assert session.authority.agent_id == identity
        assert session.authority.name == name
        assert app._owner_memory_dir == notes
        if not saved:
            app._materialise_convo()
            first = app._request_messages()[0]
            assert "NEW AUTHORED OWNER BODY" not in first["content"]
            frozen = first["content"][len(app.conversation[0]["litetui_prompt_base"]):]
            assert "FROZEN LORE EXACT" in frozen and "FROZEN OWNER NOTE NOW.md" in frozen
            # Exact synthetic pre-feature two-SYSTEM transcript. It has never
            # contained the new custom body and has no per-home source manifest.
            legacy = default.read_text(encoding="utf-8") + "\n\n" + method.read_text(encoding="utf-8")
            app.conversation[:] = [{"role": "system", "content": legacy + frozen},
                                   {"role": "system", "content": app.plugins.compose_prompt() + RECAP},
                                   {"role": "user", "content": "OLD USER HISTORY EXACT"}]
            app._snapshot("synthetic pre-feature owner transcript")
            saved.update(home=home, cid=app.convo_id, path=app.convo_path, frozen=frozen)
            assert not (home / "prompts" / "audit.jsonl").exists()
            return

        assert home == saved["home"]
        assert app._cli_convo_id == saved["cid"]
        assert app.conversation[0]["content"].count("NEW AUTHORED OWNER BODY") == 1
        # At the entry to the terminal adapter, before ANY request composition,
        # the constructor has already used the real shared on-fly audit publisher.
        rows = [json.loads(line) for line in (home / "prompts" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        applied = [r for r in rows if r["phase"] == "applied"]
        assert len(applied) == 1
        row = applied[0]
        assert row["name"] == "role.md" and row["convo"] == saved["cid"]
        assert row["who"] == (parent or identity)
        provenance = row["actor_provenance"]
        assert provenance["version"] == 1 and provenance["route"] == "owned-launch-role"
        assert provenance["home_owner"] == identity and provenance["actual_role"] == "leader"
        assert provenance["input_text_sha256"] == prompts.sha("NEW AUTHORED OWNER BODY")
        assert provenance["body_sha256"] == prompts.sha("NEW AUTHORED OWNER BODY")
        assert provenance["actor_kind"] == ("launch-envelope parent assertion" if parent else "unparented OS-user launch; actor is home identity")
        assert "not private-RPC" in provenance["authentication"]
        assert not [r for r in rows if r["phase"] == "notification"]
        assert (home / "prompts" / "role.md").read_text(encoding="utf-8") == "NEW AUTHORED OWNER BODY"
        assert prompts.metadata(home, "role.md")["request_id"] == row["request_id"]
        assert app.seat.spawned_by == parent  # consumed env is NOT the parent oracle
        assert "LITEHARNESS_SPAWNED_BY" not in __import__("os").environ
        assert (home / "prompts" / "revisions" / f"{row['request_id']}.json").is_file()
        # Follow real on_mount order: startup ownership/settings restoration
        # precedes backend connect, then _apply_cli_args rehydrates and submits.
        # External backend readiness/registration and terminal view are adapted;
        # neither _resume_cli_conversation, _resume nor _apply_cli_args is mocked.
        app._system = lambda *_args, **_kwargs: None
        app._render_resumed = lambda *_args, **_kwargs: None
        assert app._resume_cli_conversation()
        assert app.convo_id == saved["cid"]
        app._connect_settled = True
        app._connect_done.set()
        app._gui_connection_success = True
        app._cli_initial_model = None
        app._cli_thinking_level = None

        async def registered():
            return True

        async def ready(**_kwargs):
            return True

        def submit(text, **_kwargs):
            assert text == "FIRST NEW USER INPUT"
            assert app.convo_id == saved["cid"]
            assert app.convo_path == saved["path"]
            assert app.store.agent_session is session and app.store.owned
            messages = app._request_messages()
            assert [m["role"] for m in messages].count("system") == 1
            system = messages[0]["content"]
            assert system.count("# OWNER ROLE — Owner Mode") == 1
            assert system.count("# POLYMATHIC GAMMA") == 1
            assert system.count("NEW AUTHORED OWNER BODY") == 1
            assert "OLD DEFAULT ROLE BODY" not in system
            assert system.endswith(saved["frozen"]), "frozen lore/store suffix changed"
            assert "NEW DISK MUST NOT APPEAR" not in system
            assert {"role": "user", "content": "OLD USER HISTORY EXACT"} in messages
            observed.append((app.convo_id, system))

        app._register_owned_startup = registered
        app._ensure_chat_ready = ready
        app._submit_text = submit  # provider boundary capture, no model call
        asyncio.run(app_mod.LiteTUI._apply_cli_args.__wrapped__(app))
        assert not app._cli_launch_error

    monkeypatch.setattr(app_mod.LiteTUI, "run", capture)

    def launch(extra):
        envelope = {harness.SPAWN_IDENTITY_MARKER: "1", "LITEHARNESS_AGENT_ID": identity,
                    "LITEHARNESS_AGENT_NAME": name, "LITEHARNESS_TIER": "leader"}
        if parent:
            envelope["LITEHARNESS_SPAWNED_BY"] = parent
        for key, value in envelope.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["litetui", "--cwd", str(workspace), "--owner", name,
                                          "--memory", str(notes), "--backend", "codex", "--model", "synthetic-model",
                                          "--thinking-level", "high", *extra])
        cli.main()  # real CLI finally releases OwnerLock, transcript and HOME

    launch([])
    (saved["home"] / ".prompt-launch.json").unlink()  # synthetic pre-feature state
    for filename in owner_mode.OWNER_FILES:
        (notes / filename).write_text("NEW DISK MUST NOT APPEAR", encoding="utf-8")
    lore.write_text("NEW DISK MUST NOT APPEAR", encoding="utf-8")
    launch(["--convo", saved["cid"], "--role-prompt-file", str(default), "--cognitive-file", str(method),
            "--system-prompt-file", str(custom), "--prompt", "FIRST NEW USER INPUT"])
    assert len(observed) == 1


@pytest.mark.parametrize("invalid", ["oversized-comment", "nul-comment", "wrong-role", "method-source", "audit-tail"])
def test_launch_revision_refuses_before_active_body_or_audit_change(tmp_path, invalid):
    from _owned_seat import owned_app

    method = tmp_path / "method.md"
    method.write_text("# POLYMATHIC GAMMA\nORIGINAL METHOD", encoding="utf-8")
    app = owned_app(system_prompt="PREVIOUS AUTHORED BODY", cognitive_file=str(method))
    root = app._agent_session.memory_root
    folder = root / "prompts"
    audit = folder / "audit.jsonl"
    text = "NEW VALID BODY"
    match = "bounded UTF-8"
    if invalid == "oversized-comment":
        text = "<!--" + "x" * prompts.MAX_BYTES + "-->\n# BUILDER ROLE\nSMALL BODY"
    elif invalid == "nul-comment":
        text = "<!--\0-->\n# BUILDER ROLE\nSMALL BODY"
    elif invalid == "wrong-role":
        text, match = "# OWNER ROLE\nWRONG ROLE", "actual role"
    elif invalid == "method-source":
        method.write_text("# POLYMATHIC GAMMA\nCHANGED METHOD SOURCE", encoding="utf-8")
        match = "source changed"
    else:
        with audit.open("a", encoding="utf-8", newline="") as out:
            out.write("{incomplete")
        match = "incomplete tail"
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
              if p.is_file() and (p.suffix == ".md" or p.name.endswith((".json", ".jsonl")))}
    app.store.release()
    app._agent_session.release()
    with pytest.raises(prompts.PromptError, match=match):
        owned_app(system_prompt=text, cognitive_file=str(method))
    for relative, data in before.items():
        assert (root / relative).read_bytes() == data, relative
    assert (folder / "role.md").read_text(encoding="utf-8") == "PREVIOUS AUTHORED BODY"
