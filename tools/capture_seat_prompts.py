"""Capture candidate CLI/app SYSTEM composition with isolated synthetic homes.

No seat is started, backend request sent, or human-look gate satisfied. Only
LiteTUI.run and image-terminal setup are substituted; CLI, lease, constructor,
composer and emitted request messages are the shipped implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True, help="canonical cognitive-architectures directory")
    parser.add_argument("--output", type=Path, required=True, help="sanitized JSON report, no full prompt bodies")
    args = parser.parse_args()
    catalog = args.catalog.resolve()
    cwd = Path.cwd()
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    captures = []
    with tempfile.TemporaryDirectory(prefix="t0338-composer-") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        lore = root / "synthetic-lore.md"
        lore.write_text("## SYNTHETIC CAPTURE LORE\nNo live seat memory is used.\n", encoding="utf-8")
        env = {"LITETUI_NO_HARNESS": "1", "LITETUI_DATA_ROOT": str(root / "data"),
               "HOME": str(root / "profile"), "USERPROFILE": str(root / "profile"),
               "LITEHARNESS_LORE_FILE": str(lore), "CLAUDE_SKILL_DIR": str(root / "skills")}
        with patch.dict(os.environ, env):
            from litetui import app as app_mod
            from litetui import cli, harness, image_viewer, seat_prompts

            def capture(app, **_kwargs):
                messages = app._request_messages()
                systems = [m["content"] for m in messages if m.get("role") == "system"]
                assert len(systems) == 1
                text = systems[0]
                headers = re.findall(r"^# (?:[A-Z]+ ROLE|POLYMATHIC)[^\n]*$", text, re.MULTILINE)
                assert len(seat_prompts.ROLE_HEADER.findall(text)) == 1
                assert len(seat_prompts.METHOD_HEADER.findall(text)) == 1
                assert not seat_prompts.MODE_HEADER.search(text)
                expected = {"leader": ("OWNER", "# OWNER ROLE — Owner Mode"),
                            "worker": ("BUILDER", "# BUILDER ROLE — Builder Mode"),
                            "reviewer": ("CRITIC", "# CRITIC ROLE — Critic Mode")}
                modern, expected_header = expected[app.seat.tier]
                assert headers == [expected_header, "# POLYMATHIC GAMMA"]
                captures.append({"role": modern, "wire_role": app.seat.tier, "system_messages": len(systems),
                                 "characters": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                 "headers": headers, "prompt_files": sorted(p.name for p in (app._agent_session.memory_root / "prompts").glob("*.md"))})

            with patch.object(app_mod.LiteTUI, "run", capture), patch.object(image_viewer, "init_image_backend"):
                for role in ("leader", "worker", "reviewer"):
                    identity = str(uuid4())
                    launch_env = {harness.SPAWN_IDENTITY_MARKER: "1", "LITEHARNESS_AGENT_ID": identity,
                                  "LITEHARNESS_AGENT_NAME": f"Synthetic-{role}", "LITEHARNESS_TIER": role}
                    argv = ["litetui", "--cwd", str(workspace), "--create-agent", f"Synthetic-{role}",
                            "--agent-id", identity, "--backend", "codex", "--model", "synthetic-model",
                            "--thinking-level", "high", "--role-prompt-file", str(catalog.parent / "preambles" / f"{role}-preamble.md"),
                            "--cognitive-file", str(catalog / "workers" / "gamma.md")]
                    with patch.dict(os.environ, launch_env), patch.object(sys, "argv", argv):
                        try:
                            cli.main()
                        finally:
                            os.chdir(cwd)
            module = str(Path(app_mod.__file__).resolve())
    os.chdir(cwd)
    report = {"kind": "isolated candidate CLI/app composer capture; NOT running-seat proof",
              "git_head": sha, "module": module, "captures": captures,
              "privacy": "Temporary synthetic HOME, data root, cwd, lore and agent homes; full SYSTEM bodies not retained"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
