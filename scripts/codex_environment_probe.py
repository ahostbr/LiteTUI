"""Inspect native execution-environment readiness without starting a model turn."""

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from litetui.codex_app_server import AppServer
from litetui.model_transport import credential_path


async def probe(output):
    evidence = {"model_turn_started": False, "environments": []}
    with tempfile.TemporaryDirectory(prefix="litetui-env-probe-") as directory:
        root = Path(directory)
        shutil.copy2(credential_path("codex"), root / "auth.json")
        server = AppServer(config_overrides=["features.unified_exec=true",
                                            "features.multi_agent=false", "features.multi_agent_v2=false"])
        server.environment["CODEX_HOME"] = str(root)
        try:
            await server.start()
            opened = await server.request("thread/start", {"model": "gpt-6-astra", "cwd": str(root),
                "approvalPolicy": "never", "sandbox": "read-only"})
            thread = opened["thread"]
            selection = thread.get("environments")
            evidence["selection_reported"] = isinstance(selection, list)
            evidence["selected_count"] = len(selection) if isinstance(selection, list) else None
            for environment in selection or []:
                ident = environment["environmentId"]
                status = await server.request("environment/status", {"environmentId": ident})
                evidence["environments"].append({
                    "status": status.get("status") if status.get("status") in (
                        "ready", "pending", "disconnected", "unknown") else "other"})
        except Exception as exc:  # noqa: BLE001 - bounded metadata-only diagnostic
            evidence["failure_type"] = type(exc).__name__
        finally:
            await server.close()
            evidence["app_server_closed"] = server.process is None or server.process.returncode is not None
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(probe(args.output))
