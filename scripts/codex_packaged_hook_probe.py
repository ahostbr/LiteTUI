"""Run the wheel's standalone policy client via Windows shells; no provider calls."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

from litetui.codex_hook_bridge import NativeHookBridge
from litetui.codex_native_policy import denial


async def probe(wheel):
    results = []
    with tempfile.TemporaryDirectory(prefix="LiteTUI packaged hook ") as folder:
        root = Path(folder)
        with zipfile.ZipFile(wheel) as archive:
            helper = root / "codex_hook_helper.py"
            payload = archive.read("litetui/codex_hook_helper.py")
            helper.write_bytes(payload)
            assert "litetui/codex_hook_bridge.py" in archive.namelist()
        for shell in ("cmd.exe", "powershell.exe"):
            binary = shutil.which(shell)
            if not binary:
                raise RuntimeError(f"Required shell is unavailable: {shell}")
            for decision in ("allow", "deny", "unavailable"):
                bridge = NativeHookBridge(None)

                async def handle(event, choice=decision):
                    assert event == {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                    }
                    return (
                        {} if choice == "allow" else denial("Synthetic policy denial")
                    )

                bridge.policy = NS(handle=handle)
                bridge.listener = await asyncio.start_server(
                    bridge.accept, "127.0.0.1", 0
                )
                port = bridge.listener.sockets[0].getsockname()[1]
                environment = {
                    **os.environ,
                    "PATH": str(Path(sys.executable).parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                    "LITETUI_CODEX_HOOK_KEY": bridge.token,
                    "LITETUI_CODEX_HOOK_PORT": str(port),
                }
                environment.pop("PYTHONPATH", None)
                command = f'{Path(sys.executable).name} "{helper}"'
                process = None
                try:
                    if decision == "unavailable":
                        bridge.close()
                        await bridge.listener.wait_closed()
                    options = {
                        "cwd": root,
                        "env": environment,
                        "stdin": asyncio.subprocess.PIPE,
                        "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.PIPE,
                    }
                    if shell == "cmd.exe":
                        # CMD consumes a command string, not CRT-escaped argv.
                        process = await asyncio.create_subprocess_shell(
                            command, executable=binary, **options
                        )
                    else:
                        process = await asyncio.create_subprocess_exec(
                            binary,
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            command,
                            **options,
                        )
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(
                            json.dumps(
                                {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
                            ).encode()
                        ),
                        20,
                    )
                    if decision == "unavailable":
                        assert process.returncode == 0 and not stderr
                        assert json.loads(stdout) == denial(
                            "LiteTUI native tool policy is unavailable."
                        )
                    else:
                        assert process.returncode == 0 and not stderr, (
                            shell,
                            decision,
                            process.returncode,
                            stderr.decode(errors="replace").replace(
                                bridge.token, "[redacted]"
                            ),
                        )
                        response = json.loads(stdout)
                        if decision == "allow":
                            assert response == {}
                        else:
                            assert (
                                response["hookSpecificOutput"]["permissionDecision"]
                                == "deny"
                            )
                    assert bridge.token.encode() not in stdout + stderr
                    results.append(
                        {
                            "shell": shell,
                            "decision": decision,
                            "exit_code": process.returncode,
                            "verified": True,
                        }
                    )
                finally:
                    if process and process.returncode is None:
                        process.kill()
                        await process.wait()
                    bridge.close()
                    await bridge.listener.wait_closed()
    return {
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(payload).hexdigest(),
        "path_with_spaces": True,
        "pythonpath_removed": True,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = asyncio.run(probe(args.wheel))
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))
