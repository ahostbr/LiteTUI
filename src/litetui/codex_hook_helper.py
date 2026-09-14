"""Codex command-hook client. No imports from the application or payload logging."""

import json
import os
import socket
import sys

LIMIT = 16 * 1024 * 1024


def main():
    try:
        payload = sys.stdin.buffer.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("Hook input too large")
        token = os.environ["LITETUI_CODEX_HOOK_KEY"]
        port = int(os.environ["LITETUI_CODEX_HOOK_PORT"])
        request = {"token": token, "event": json.loads(payload)}
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(300)
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                reply = stream.readline(LIMIT + 1)
        if not reply or len(reply) > LIMIT:
            raise ValueError("Missing hook response")
        document = json.loads(reply)
        print(json.dumps(document))
    except (OSError, ValueError, KeyError):
        # Exit 2 is Codex's explicit blocking failure contract. Never echo
        # exception text: it could contain tool arguments or credentials.
        print("LiteTUI native tool policy is unavailable.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
