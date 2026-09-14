"""Codex command-hook client. No imports from the application or payload logging."""

import json
import os
import socket
import sys

LIMIT = 16 * 1024 * 1024


def main():
    event = {}
    try:
        payload = sys.stdin.buffer.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("Hook input too large")
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise TypeError("Hook input must be an object")
        token = os.environ["LITETUI_CODEX_HOOK_KEY"]
        port = int(os.environ["LITETUI_CODEX_HOOK_PORT"])
        request = {"token": token, "event": event}
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(300)
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                reply = stream.readline(LIMIT + 1)
        if not reply or len(reply) > LIMIT:
            raise ValueError("Missing hook response")
        document = json.loads(reply)
        print(json.dumps(document))
    except (OSError, ValueError, KeyError, TypeError):
        # PowerShell maps a native exit 2 to exit 1. Return the event-specific
        # denial with exit zero; PreToolUse rejects universal continue:false.
        # Never echo exception text: it may contain arguments or credentials.
        reason = "LiteTUI native tool policy is unavailable."
        response = (
            {"decision": "block", "reason": reason}
            if isinstance(event, dict) and event.get("hook_event_name") == "PostToolUse"
            else {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
        print(json.dumps(response))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
