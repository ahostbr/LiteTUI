"""T540/T542 — discover a model's real reasoning-effort level set.

Two strategies, tried in order:
1. REFUSAL PROBE (LM Studio /api/v1/chat only): send an unsupported level,
   read the enumerated set from the 400 body. One request, authoritative.
2. CLASSIFIER (any OpenAI-compatible endpoint): fire one request per graded
   level, group by reasoning_content hash. Fallback when the refusal path
   is unavailable.

The refusal probe discriminates `invalid_value` + `param: "reasoning"` (the
MODEL's set) from `invalid_enum_value` (the ENDPOINT's vocabulary, identical
for every model). Only the former is trusted.

Pure module: no Textual, no app, no threads. The caller supplies the
transport.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request

GRADED_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
PROBE_PROMPT = "Reply with exactly one word: PROBE"
PROBE_MAX_TOKENS = 200
REFUSAL_PROBE_SETTING = "high"
REFUSAL_FOLLOW_UP = "low"
SUPPORTED_RE = re.compile(r"Supported settings:\s*(.+?)\s*\.?$", re.IGNORECASE)

# /api/v1/chat uses a different vocabulary than /v1/chat/completions.
# "none" on the wire = "off" natively, "on" exists only natively.
NATIVE_TO_WIRE = {"off": "off", "on": "xhigh", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "xhigh"}


def _parse_supported(message: str) -> list[str] | None:
    """Extract the level list from a refusal message."""
    m = SUPPORTED_RE.search(message.strip())
    if not m or not m.group(1):
        return None
    settings = re.findall(r"'([^']+)'", m.group(1))
    return settings if settings else None


def _translate_native_to_wire(native_levels: list[str]) -> list[str]:
    """Convert the /api/v1/chat vocabulary to our wire vocabulary."""
    result = []
    for n in native_levels:
        wire = NATIVE_TO_WIRE.get(n, n)
        if wire not in result:
            result.append(wire)
    return result


def _interpret_refusal(status: int, body: dict) -> tuple[str, list[str] | None]:
    """Interpret a /api/v1/chat response.

    Returns (kind, levels):
      "known"   -> authoritative enumeration
      "partial" -> the probe setting was accepted (no enumeration)
      "unknown" -> unrecognised or endpoint-level error
    """
    if status == 200:
        return "partial", None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "unknown", None
    code = error.get("code", "")
    param = error.get("param", "")
    message = error.get("message", "")
    if code == "invalid_enum_value":
        return "unknown", None
    if code == "invalid_value" and param == "reasoning":
        levels = _parse_supported(message)
        if levels:
            return "known", levels
        return "unknown", None
    return "unknown", None


def probe_via_refusal(
    host: str,
    model: str,
    *,
    transport=None,
) -> list[str] | None:
    """Try the /api/v1/chat refusal path. Returns the wire-vocabulary level
    list on success, None on failure or when the endpoint is unavailable."""
    url = f"{host.rstrip('/')}/api/v1/chat"
    payload = {
        "model": model,
        "input": "hi",
        "max_output_tokens": 1,
        "reasoning": REFUSAL_PROBE_SETTING,
    }
    body_bytes = json.dumps(payload).encode()
    try:
        if transport:
            status, data = transport(url, body_bytes, 15)
        else:
            req = urllib.request.Request(
                url, data=body_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status, data = resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    data = json.loads(e.read())
                except Exception:
                    data = {}
    except Exception:
        return None

    kind, levels = _interpret_refusal(status, data)
    if kind == "known" and levels:
        return _translate_native_to_wire(levels)

    if kind == "partial":
        # The setting was accepted. Follow up with a different level to get
        # a refusal that enumerates the set.
        payload2 = {**payload, "reasoning": REFUSAL_FOLLOW_UP}
        body2 = json.dumps(payload2).encode()
        try:
            if transport:
                status2, data2 = transport(url, body2, 15)
            else:
                req2 = urllib.request.Request(
                    url, data=body2,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req2, timeout=15) as resp2:
                        status2, data2 = resp2.status, json.loads(resp2.read())
                except urllib.error.HTTPError as e2:
                    status2 = e2.code
                    try:
                        data2 = json.loads(e2.read())
                    except Exception:
                        data2 = {}
        except Exception:
            return None
        kind2, levels2 = _interpret_refusal(status2, data2)
        if kind2 == "known" and levels2:
            return _translate_native_to_wire(levels2)

    return None


# ── classifier fallback (5-request) ────────────────────────────────────


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


def probe_levels(
    host: str,
    model: str,
    *,
    transport=None,
) -> dict[str, list[str]]:
    """Fire one request per graded level, return classes grouped by hash.

    `transport` is callable(url, body_bytes, timeout) -> response_dict.
    """
    url = f"{host.rstrip('/')}/v1/chat/completions"
    hashes: dict[str, str] = {}
    for level in GRADED_LEVELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            "reasoning_effort": level,
        }
        body = json.dumps(payload).encode()
        try:
            if transport:
                data = transport(url, body, 30)
            else:
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            reasoning = ((data.get("choices") or [{}])[0].get("message") or {}).get("reasoning_content") or ""
            hashes[level] = _hash(reasoning)
        except Exception:
            return {}
    classes: dict[str, list[str]] = {}
    for level, h in hashes.items():
        classes.setdefault(h, []).append(level)
    return classes


def effective_levels(classes: dict[str, list[str]]) -> list[str]:
    """The levels this model actually distinguishes, including off."""
    if not classes:
        return list(GRADED_LEVELS) + ["off"]
    if len(classes) == 1:
        return ["off", "xhigh"]
    result = ["off"]
    for _hash, levels in classes.items():
        best = max(levels, key=lambda l: GRADED_LEVELS.index(l))
        result.append(best)
    result.sort(key=lambda l: (GRADED_LEVELS + ("off",)).index(l) if l in GRADED_LEVELS else -1)
    return result


# ── session cache ──────────────────────────────────────────────────────

_cache: dict[str, list[str]] = {}


def get_effective_levels(
    host: str,
    model: str,
    seed_models: tuple[str, ...] | list[str] = (),
    *,
    transport=None,
    refusal_transport=None,
) -> list[str]:
    """Cached lookup. Tries refusal first, then classifier."""
    key = model.strip().lower()
    if key in _cache:
        return _cache[key]
    if any(key == m.strip().lower() for m in seed_models):
        levels = ["off"] + list(GRADED_LEVELS)
        _cache[key] = levels
        return levels
    # Fast path: LM Studio refusal probe
    refusal_levels = probe_via_refusal(host, model, transport=refusal_transport)
    if refusal_levels:
        _cache[key] = refusal_levels
        return refusal_levels
    # Fallback: 5-request classifier
    classes = probe_levels(host, model, transport=transport)
    levels = effective_levels(classes)
    _cache[key] = levels
    return levels


def clear_cache(model: str | None = None) -> None:
    if model is None:
        _cache.clear()
    else:
        _cache.pop(model.strip().lower(), None)
