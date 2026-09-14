"""Measure installed MCP catalog refresh without model turns or credentials."""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from litetui.codex_app_server import AppServer
from litetui.model_transport import ProviderError

FIXTURE = '''import json,sys,time,threading
from pathlib import Path
state=Path(sys.argv[1]); counts=Path(sys.argv[2]); lock=threading.Lock()
def send(value):
 with lock: print(json.dumps(value),flush=True)
def watch():
 previous=state.read_text()
 while True:
  time.sleep(.05)
  current=state.read_text()
  if current!=previous:
   previous=current
   send({"jsonrpc":"2.0","method":"notifications/tools/list_changed"})
threading.Thread(target=watch,daemon=True).start()
for line in sys.stdin:
 request=json.loads(line); method=request.get("method")
 if "id" not in request: continue
 if method=="initialize":
  result={"protocolVersion":request["params"]["protocolVersion"],"capabilities":{"tools":{"listChanged":True}},"serverInfo":{"name":"synthetic-catalog","version":"1"}}
 elif method=="tools/list":
  version=int(state.read_text())
  with counts.open("a") as output: output.write(str(version)+"\\n")
  tools=[{"name":"alpha","description":"Synthetic","inputSchema":{"type":"object","properties":{"value":{"type":"string" if version==1 else "integer"}}}}]
  if version==2: tools.append({"name":"beta","description":"Synthetic","inputSchema":{"type":"object","properties":{}}})
  result={"tools":tools}
 elif method in ("resources/list","resources/templates/list"):
  result={"resources":[],"resourceTemplates":[]}
 elif method=="ping": result={}
 else:
  send({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"Unsupported synthetic operation"}})
  continue
 send({"jsonrpc":"2.0","id":request["id"],"result":result})
'''


async def probe(output):
    evidence = {"scope": "threadless and loaded-thread catalogs; no model input", "model_turns": 0, "accepted": False}
    with tempfile.TemporaryDirectory(prefix="litetui-mcp-catalog-") as directory:
        root = Path(directory)
        fixture, state, counts = root / "fixture.py", root / "state", root / "counts"
        fixture.write_text(FIXTURE, encoding="utf-8")
        state.write_text("1", encoding="utf-8")

        def config(revision):
            (root / "config.toml").write_text(
                '[mcp_servers.catalog_probe]\ncommand = ' + json.dumps(sys.executable)
                + '\nargs = ' + json.dumps([str(fixture), str(state), str(counts)])
                + '\n[mcp_servers.catalog_probe.env]\nCATALOG_REVISION = '
                + json.dumps(str(revision)) + '\n', encoding="utf-8")

        config(1)
        server = AppServer()
        server.environment["CODEX_HOME"] = str(root)

        async def request(method, params):
            if method not in ("thread/start", "mcpServerStatus/list", "config/mcpServer/reload"):
                raise AssertionError("Probe attempted an operation outside its no-inference scope")
            return await server.request(method, params)

        async def catalog(thread=None):
            params = {"detail": "toolsAndAuthOnly"}
            if thread is not None:
                params["threadId"] = thread
            response = await request("mcpServerStatus/list", params)
            row = next(row for row in response["data"] if row["name"] == "catalog_probe")
            tools = row.get("tools", {})
            return {name: value["inputSchema"] for name, value in sorted(tools.items())}

        try:
            async with asyncio.timeout(45):
                await server.start()
                started = await request("thread/start", {"model": "gpt-6-astra", "cwd": str(root)})
                thread = started["thread"]["id"]
                evidence["thread_id"] = thread
                evidence["initial"] = await catalog()
                evidence["loaded_initial"] = await catalog(thread)
                state.write_text("2", encoding="utf-8")
                await asyncio.sleep(.3)  # Allow the fixture's list_changed notification.
                evidence["after_notification"] = await catalog()
                evidence["loaded_after_notification"] = await catalog(thread)
                await request("config/mcpServer/reload", {})
                evidence["after_unchanged_reload"] = await catalog()
                evidence["loaded_after_unchanged_reload"] = await catalog(thread)
                config(2)
                await request("config/mcpServer/reload", {})
                evidence["after_config_revision"] = await catalog()
                evidence["loaded_after_config_revision"] = await catalog(thread)
                evidence["tools_list_versions"] = [int(line) for line in counts.read_text().splitlines()]
                evidence["accepted"] = bool(evidence["initial"])
        except (OSError, KeyError, ValueError, TypeError, ProviderError, AssertionError) as exc:
            evidence["error_type"] = type(exc).__name__
        finally:
            await server.close()
            evidence["app_server_closed"] = server.process is None or server.process.returncode is not None
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Start an isolated app-server, never inference")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required")
    asyncio.run(probe(args.output))
