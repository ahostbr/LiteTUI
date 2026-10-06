"""LiteSuite Voice API dispatch, not synthesis or remote playback ownership.

None means definitely unavailable before submission (local fallback is safe).
False means refused/uncertain dispatch: never replay that utterance locally.
True acknowledges dispatch only; LiteSuite owns its queue and playback.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import time

_LOG = logging.getLogger(__name__)
_HEALTH_TIMEOUT = 0.25
_SPEAK_TIMEOUT = 0.75
_HEALTH_TTL = 2.0
_health: tuple[int, float, bool] | None = None


def _available(port: int) -> bool:
    global _health
    if _health is not None and _health[0] == port and time.monotonic() < _health[1]:
        return _health[2]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=_HEALTH_TIMEOUT)
    up = False
    try:
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        body = json.loads(response.read(4096))
        up = response.status == 200 and isinstance(body, dict) and body.get("ok") is True and body.get("app") == "LiteSuite"
    except (OSError, http.client.HTTPException, ValueError):
        pass  # No utterance has been submitted; falling back cannot duplicate it.
    finally:
        try:
            connection.close()
        except OSError:
            _LOG.warning("LiteSuite health connection cleanup failed", exc_info=True)
    _health = (port, time.monotonic() + _HEALTH_TTL, up)
    return up


def speak(text: str) -> bool | None:
    """Try the selected LiteSuite voice, with no retries after submission."""
    global _health
    try:
        port = int(os.environ.get("LITESUITE_VOICE_API_PORT", "7438"))
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
    except ValueError:
        _LOG.warning("Invalid LITESUITE_VOICE_API_PORT; using LiteTUI speech")
        return None
    if not _available(port):
        return None

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=_SPEAK_TIMEOUT)
    try:
        # Only a definitive connect refusal is known safe after a cached probe.
        # Once request() starts, even a timeout or HTTP error can follow dispatch.
        try:
            connection.connect()
        except ConnectionRefusedError:
            _health = (port, time.monotonic() + _HEALTH_TTL, False)
            return None
        connection.request(
            "POST", "/v1/tts/speak",
            body=json.dumps({"text": text, "summarize": False}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-LiteSuite-Origin": "litetui"},
        )
        response = connection.getresponse()
        body = json.loads(response.read(4096))
        if response.status == 200 and isinstance(body, dict) and body.get("ok") is True and not body.get("dropped"):
            return True
        _LOG.warning("LiteSuite speech refused or unacknowledged; not replaying locally (HTTP %s)", response.status)
    except (OSError, http.client.HTTPException, ValueError):
        _LOG.warning("LiteSuite speech acknowledgement uncertain; not replaying locally", exc_info=True)
    finally:
        try:
            connection.close()
        except OSError:
            # Cleanup must never change dispatch acknowledgement or allow replay.
            _LOG.warning("LiteSuite speech connection cleanup failed", exc_info=True)
    return False
