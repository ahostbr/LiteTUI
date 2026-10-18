"""Suite-only request attribution; no listener, MCP process, or desktop input."""
import json
from pathlib import Path

import pytest

from litetui import mcp_client


@pytest.mark.parametrize("url,name,expected", [
    ("http://localhost:7423/mcp", "litesuite-tools", True),
    ("http://127.0.0.1:7423/mcp", "litesuite-tools", True),
    ("http://[::1]:7423/mcp", "litesuite-tools", True),
    ("http://example.com/mcp", "litesuite-tools", False),
    ("https://example.com/mcp", "litesuite-tools", False),
    ("http://localhost:7423/other", "litesuite-tools", False),
    ("http://localhost:7423/mcp", "unrelated-mcp", False),
])
def test_real_http_client_propagates_only_known_suite_seat_id(monkeypatch, url, name, expected):
    monkeypatch.setenv("LITEHARNESS_AGENT_ID", "worker-123")
    monkeypatch.setenv("LITEHARNESS_AGENT_NAME", "UNTRUSTED_NAME")
    monkeypatch.setenv("LITEHARNESS_TIER", "orchestrator")
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"refused"}]}}'
    def urlopen(request, **kwargs):
        seen.append(request)
        return Response()
    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", urlopen)
    client = mcp_client.HTTPMCPServer(name, {"url": url}, Path.cwd(), None, agent_id="worker-123")
    assert client.call("pccontrol", {"action": "click", "x": 1, "y": 2}) == "refused"
    headers = {key.lower(): value for key, value in seen[0].header_items()}
    assert headers.get("x-litesuite-agent-id") == ("worker-123" if expected else None)
    assert "UNTRUSTED_NAME" not in json.dumps(headers)
    assert "orchestrator" not in json.dumps(headers)
    assert json.loads(seen[0].data)["params"]["arguments"] == {"action": "click", "x": 1, "y": 2}


@pytest.mark.parametrize("identity", [None, "../secret", "invalid\nheader"])
def test_absent_or_invalid_seat_id_is_not_fabricated(monkeypatch, identity):
    for key in ("LITEHARNESS_AGENT_ID", "LITESUITE_AGENT_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_COMPANION_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    if identity is not None:
        monkeypatch.setenv("LITEHARNESS_AGENT_ID", identity)
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"jsonrpc":"2.0","id":1,"result":{}}'
    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", lambda request, **kwargs: seen.append(request) or Response())
    client = mcp_client.HTTPMCPServer("litesuite-tools", {"url": "http://localhost:7423/mcp"}, Path.cwd(), None, agent_id=identity)
    client._request("tools/call", {"name": "pccontrol", "arguments": {"action": "type", "text": "not sent to headers"}})
    assert "x-litesuite-agent-id" not in {key.lower() for key, _ in seen[0].header_items()}
