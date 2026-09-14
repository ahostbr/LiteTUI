"""Exercise a wheel's export CLI in a fresh process outside the checkout."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def probe(wheel, output):
    with tempfile.TemporaryDirectory(prefix="litetui packaged export ") as folder:
        root = Path(folder)
        package = root / "package"
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(package)
        required = ["codex_history.py", "codex_trace.py", "conversation_export.py",
                    "codex_async_questions.py", "codex_hook_helper.py"]
        assert all((package / "litetui" / name).is_file() for name in required)
        image = b"\x89PNG\r\n\x1a\nsynthetic probe payload"
        metadata = {"provider": "codex", "app_server_thread_id": "thread",
                    "display_trace": {"version": 1, "items": [
                        {"id": "tool", "turnId": "turn", "kind": "dynamicToolCall", "name": "echo",
                         "state": "completed", "durationMs": 0, "result": "ECHO_RESULT"},
                        {"id": "answer", "turnId": "turn", "kind": "agentMessage", "phase": "final_answer",
                         "state": "completed", "result": "FINAL_ANSWER"}]},
                    "async_questions": [{"id": "ask", "threadId": "thread", "version": 1,
                                         "state": "pending", "questions": [{"title": "QUESTION_TITLE"}]}]}
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "PROMPT"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}],
                     "provider_metadata": metadata},
                    {"role": "assistant", "content": "FINAL_ANSWER", "provider_metadata": metadata}]
        source, destination = root / "convo.jsonl", root / "export with spaces.md"
        source.write_text(json.dumps({"type": "snapshot", "messages": messages}), encoding="utf-8")
        before = source.read_bytes()
        code = """
import os
from pathlib import Path
from litetui import app, cli, codex_history, conversation_export
root = Path(os.environ['LITETUI_PROBE_PACKAGE'])
assert all(Path(module.__file__).is_relative_to(root) for module in (app, cli, codex_history, conversation_export))
def forbidden(*args, **kwargs):
    raise AssertionError('export must not construct the application')
app.LiteTUI = forbidden
cli.main()
"""
        env = {**os.environ, "PYTHONPATH": str(package), "LITETUI_PROBE_PACKAGE": str(package)}
        command = [sys.executable, "-c", code, "--export-conversation", str(source),
                   "--export-output", str(destination)]
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, "packaged CLI export failed"
        text = destination.read_text(encoding="utf-8")
        assert text.count("ECHO_RESULT") == text.count("FINAL_ANSWER") == text.count("QUESTION_TITLE") == 1
        assert "completed · 0s" in text and "State: pending" in text
        assets = list((root / "export with spaces.md.assets").iterdir())
        assert len(assets) == 1 and assets[0].read_bytes() == image
        assert "export%20with%20spaces.md.assets/" in text and "data:image" not in text
        assert source.read_bytes() == before
        saved_output = destination.read_bytes()
        repeated = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=30, check=False)
        assert repeated.returncode != 0 and destination.read_bytes() == saved_output
        evidence = {"wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "required_modules_present": True, "imports_from_extracted_wheel": True,
                    "fresh_process_export_passed": True, "application_construction_forbidden": True,
                    "native_items_and_question_once": True, "zero_duration_preserved": True,
                    "image_bytes_match": True, "relative_asset_link": True,
                    "source_unchanged": True, "existing_output_preserved": True}
    evidence["temporary_directory_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    probe(args.wheel, args.output)
