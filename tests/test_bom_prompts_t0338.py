"""UTF-8 BOM handling through the shipped CLI/app, not a second composer."""
import sys
from uuid import uuid4

import pytest

from litetui import app as app_mod
from litetui import cli, harness, image_viewer
from litetui.prompt_overrides import PromptError


@pytest.mark.parametrize("declared,accepted", [("OWNER", True), ("BUILDER", False)])
def test_bom_caller_file_real_cli_actual_role_and_method(tmp_path, monkeypatch, declared, accepted):
    caller = tmp_path / "caller.md"
    default = tmp_path / "default.md"
    method = tmp_path / "method.md"
    caller.write_bytes((f"\ufeff# {declared} ROLE — {declared.title()} Mode\nBOM CALLER BODY").encode())
    default.write_text("# OWNER ROLE — Owner Mode\nDEFAULT MUST BE REPLACED", encoding="utf-8")
    method.write_bytes("\ufeff# POLYMATHIC GAMMA\nBOM METHOD BODY".encode("utf-8"))
    raw_caller, raw_method = caller.read_bytes(), method.read_bytes()
    identity = str(uuid4())
    monkeypatch.setenv(harness.SPAWN_IDENTITY_MARKER, "1")
    monkeypatch.setenv("LITEHARNESS_AGENT_ID", identity)
    monkeypatch.setenv("LITEHARNESS_AGENT_NAME", "BomOwner")
    monkeypatch.setenv("LITEHARNESS_TIER", "owner")
    captured = []

    def run(app, **kwargs):
        captured.append(app._request_messages())
        assert app._legacy_launch_candidates[0] == raw_caller.decode("utf-8") + "\n\n" + raw_method.decode("utf-8")

    monkeypatch.setattr(app_mod.LiteTUI, "run", run)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(sys, "argv", ["litetui", "--create-agent", "BomOwner", "--agent-id", identity,
                                    "--backend", "codex", "--model", "synthetic", "--thinking-level", "high",
                                    "--role-prompt-file", str(default), "--system-prompt-file", str(caller),
                                    "--cognitive-file", str(method)])
    if not accepted:
        with pytest.raises(PromptError, match="disagrees with actual role"):
            cli.main()
        assert not captured
    else:
        cli.main()
        messages = captured[0]
        assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
        text = messages[0]["content"]
        assert text.startswith("# OWNER ROLE")
        assert text.count("# OWNER ROLE") == 1 and text.count("# POLYMATHIC GAMMA") == 1
        assert "DEFAULT MUST BE REPLACED" not in text
        assert "BOM CALLER BODY" in text and "BOM METHOD BODY" in text and "\ufeff" not in text
    assert caller.read_bytes() == raw_caller and method.read_bytes() == raw_method
