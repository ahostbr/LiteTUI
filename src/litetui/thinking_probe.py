"""T540 — discover a model's real reasoning-effort level set by in-band probe.

The probe fires one request per graded level (minimal, low, medium, high,
xhigh), groups them by the reasoning_content they produce (hash), and reports
which levels are aliases of each other. A model whose five levels all land in
one class has only on/off — "on" being the server default, spelled xhigh.

🔴 Do NOT compare against the field OMITTED: on the nvfp4 the omitted output
IS the graded class, which is exactly what made xhigh look like it worked.

Pure module: no Textual, no app, no threads. The caller (app.py) supplies
the transport.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request

GRADED_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
PROBE_PROMPT = "Reply with exactly one word: PROBE"
PROBE_MAX_TOKENS = 200


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


def probe_levels(
    host: str,
    model: str,
    *,
    transport=None,
) -> dict[str, list[str]]:
    """Fire one request per graded level, return classes grouped by hash.

    Returns {hash: [level, ...]}. One class = all levels are aliases (on/off
    only). Multiple classes = graded control works.

    `transport` is a callable(url, body_bytes, timeout) -> response_dict for
    testing without the network.
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
    """The levels this model actually distinguishes, including off.

    One class → on/off only: returns ["off", "xhigh"].
    Multiple classes → return "off" + one representative per class (the highest
    in the canonical order, since LM Studio snaps lower values up).
    """
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


# Session cache: model_id -> effective level list
_cache: dict[str, list[str]] = {}


def get_effective_levels(
    host: str,
    model: str,
    seed_models: tuple[str, ...] | list[str] = (),
    *,
    transport=None,
) -> list[str]:
    """Cached lookup. Probes only on first call per model_id."""
    key = model.strip().lower()
    if key in _cache:
        return _cache[key]
    # Seed: if the model is in the allowlist, assume full graded control
    if any(key == m.strip().lower() for m in seed_models):
        levels = ["off"] + list(GRADED_LEVELS)
        _cache[key] = levels
        return levels
    classes = probe_levels(host, model, transport=transport)
    levels = effective_levels(classes)
    _cache[key] = levels
    return levels


def clear_cache(model: str | None = None) -> None:
    if model is None:
        _cache.clear()
    else:
        _cache.pop(model.strip().lower(), None)
