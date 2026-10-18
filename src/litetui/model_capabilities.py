"""Read-only prelaunch capability metadata; no app, inference or catalogue refresh."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def offline_capabilities(backend, model, *, cache=None):
    if backend == "claude":
        from litetui.claude_backend import STATIC_MODELS, STATIC_EFFORT, STATIC_NO_EFFORT_MODELS
        if model not in STATIC_MODELS:
            raise ValueError("Model is absent from the pinned Claude CLI catalogue")
        levels = [] if model in STATIC_NO_EFFORT_MODELS else list(STATIC_EFFORT)
        source = "pinned Claude CLI catalogue"
    elif backend == "codex":
        if cache is None:
            from litetui.model_transport import credential_path
            cache = credential_path("codex").parent / "models_cache.json"
        try:
            rows = json.loads(Path(cache).read_text(encoding="utf-8"))["models"]
            row = next(row for row in rows if row.get("slug") == model and row.get("visibility", "list") == "list")
            options = row["supported_reasoning_levels"]
            levels = [option["effort"] for option in options]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
            raise ValueError("Codex model capability is unavailable in the existing cache; no refresh was attempted") from exc
        if not isinstance(options, list) or any(not isinstance(level, str) for level in levels):
            raise ValueError("Cached reasoning capabilities are malformed")
        source = "existing Codex CLI model cache"
    else:
        raise ValueError("Offline capability metadata unavailable for this backend; use a connected LiteTUI parent")
    return {"backend": backend, "model": model,
            "thinking": {"levels": ["default", *("off" if level == "none" else level for level in levels)], "source": source}}


def main(argv):
    parser = argparse.ArgumentParser(prog="litetui --capabilities", description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(offline_capabilities(args.backend, args.model)))
    except ValueError as exc:
        parser.exit(2, f"Capability refused: {exc}\n")
