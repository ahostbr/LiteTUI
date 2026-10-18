"""Producer-shaped custom fresh launch -> default-only named CLI resume."""
import json
import sys
from uuid import uuid4

import pytest

from litetui import app as app_mod
from litetui import cli, harness, image_viewer
from litetui import prompt_overrides as prompts


@pytest.mark.parametrize("role,modern", [("leader", "OWNER"), ("worker", "BUILDER"), ("reviewer", "CRITIC")])
@pytest.mark.parametrize("live", [False, True])
def test_custom_fresh_default_only_named_resume(tmp_path, monkeypatch, role, modern, live):
    """Use real CLI, AgentSession acquire/release and constructor/request composer.

    Only the terminal event loop and terminal-image setup are substituted. Like
    the producer, resume supplies no fresh-only --system-prompt[-file] flag.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    default = tmp_path / "default.md"
    default.write_text(f"# {modern} ROLE\nDEFAULT FALLBACK BODY", encoding="utf-8")
    custom = tmp_path / "custom.md"
    custom.write_text("EXPLICIT CUSTOM BODY", encoding="utf-8")
    method = tmp_path / "method.md"
    method.write_text("# POLYMATHIC GAMMA\nSELECTED METHOD", encoding="utf-8")
    identity = str(uuid4())
    name = f"Custom-{role}"
    launch_env = {harness.SPAWN_IDENTITY_MARKER: "1", "LITEHARNESS_AGENT_ID": identity,
                  "LITEHARNESS_AGENT_NAME": name, "LITEHARNESS_TIER": role}
    seen = []
    homes = []

    def capture(app, **_kwargs):
        messages = app._request_messages()
        assert [m["role"] for m in messages].count("system") == 1
        seen.append(messages[0]["content"])
        homes.append(app._agent_session.memory_root)

    monkeypatch.setattr(app_mod.LiteTUI, "run", capture)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    common = ["--cwd", str(workspace), "--role-prompt-file", str(default), "--cognitive-file", str(method)]

    def launch(selection, extra=()):
        # Identity is deliberately consumed once, as in a fresh producer process.
        for key, value in launch_env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["litetui", *selection, *common, *extra])
        cli.main()  # real finally releases both conversation and home leases

    launch(["--create-agent", name, "--agent-id", identity, "--backend", "codex",
            "--model", "synthetic-model", "--thinking-level", "high"],
           ["--system-prompt-file", str(custom)])
    home = homes[0]
    manifest_path = home / ".prompt-launch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {"version": 2, "role_source": "custom", "sources": {
        "role.md": prompts.sha("EXPLICIT CUSTOM BODY"),
        "method.md": prompts.sha("# POLYMATHIC GAMMA\nSELECTED METHOD")}}
    expected_body = "LIVE AUTHORED BODY" if live else "EXPLICIT CUSTOM BODY"
    expected_method = "LIVE METHOD DETAIL" if live else "SELECTED METHOD"
    if live:
        (home / "prompts" / "role.md").write_text(expected_body, encoding="utf-8")
        (home / "prompts" / "method.md").write_text(
            f"# POLYMATHIC GAMMA\n{expected_method}", encoding="utf-8")
    launch(["--agent", name])
    assert homes == [home, home]
    assert manifest_path.read_text(encoding="utf-8") == json.dumps(manifest, sort_keys=True)
    text = seen[-1]
    expected_header = {"OWNER": "# OWNER ROLE — Owner Mode", "BUILDER": "# BUILDER ROLE — Builder Mode",
                       "CRITIC": "# CRITIC ROLE — Critic Mode"}[modern]
    assert text.count(expected_header) == 1
    assert text.count("# POLYMATHIC GAMMA") == 1
    assert expected_body in text and expected_method in text
    assert "DEFAULT FALLBACK BODY" not in text
    for other in {"OWNER", "BUILDER", "CRITIC"} - {modern}:
        assert f"# {other} ROLE" not in text

    # Fallback exemption must not authorize an explicitly changed custom source.
    custom.write_text("CHANGED EXPLICIT CUSTOM", encoding="utf-8")
    with pytest.raises(prompts.PromptError, match="source changed"):
        launch(["--agent", name], ["--system-prompt-file", str(custom)])
    assert (home / "prompts" / "role.md").read_text(encoding="utf-8") == expected_body
    method.write_text("# POLYMATHIC GAMMA\nCHANGED EXPLICIT METHOD", encoding="utf-8")
    with pytest.raises(prompts.PromptError, match="source changed"):
        launch(["--agent", name])
    method.write_text("# POLYMATHIC GAMMA\nSELECTED METHOD", encoding="utf-8")
    wrong = "CRITIC" if modern != "CRITIC" else "BUILDER"
    default.write_text(f"# {wrong} ROLE\nWRONG ACTUAL ROLE", encoding="utf-8")
    with pytest.raises(prompts.PromptError, match="role"):
        launch(["--agent", name])

    # Old hash-only metadata is readable but cannot prove custom-vs-default origin.
    default.write_text(f"# {modern} ROLE\nDEFAULT FALLBACK BODY", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest["sources"]), encoding="utf-8")
    with pytest.raises(prompts.PromptError, match="source changed"):
        launch(["--agent", name])
