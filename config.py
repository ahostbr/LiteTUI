"""The LM Studio endpoint, in exactly one place.

Before this file, localhost:1234 lived in three places in app.py — two
functional (the OpenAI-compat base_url at the client, the native
/api/v0/models call for the context window) and one inside an
error-message string. The string is the one that rots silently: point the
client at another host and it still reports "Could not connect to
localhost:1234" — and a wrong error message is worse than no error message.

Override: LITETUI_LM_HOST (scheme + host + port, no path), mirroring the
LM_TOOL_ITERS env-knob precedent already in app.py.
"""

from __future__ import annotations

import os

#: e.g. http://localhost:1234 — scheme, host, port. No trailing path.
LM_HOST = os.environ.get("LITETUI_LM_HOST", "http://localhost:1234").rstrip("/")

#: OpenAI-compatible chat-completions endpoint (AsyncOpenAI base_url).
BASE_URL = f"{LM_HOST}/v1"

#: LM Studio's native API — model list, context window.
API_URL = f"{LM_HOST}/api/v0/models"
