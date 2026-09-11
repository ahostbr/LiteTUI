"""Which models are actually loaded, asked once and answered the same way everywhere.

🔴 THE RESIDENCY LAW HAS TWO CALLERS NOW AND MUST NOT HAVE TWO COPIES (T640).
T594/T609/T611 established it for the subagent: a LOCAL backend JIT-loads
whatever a request names, so a headless or unattended call may name only a model
that is ALREADY resident, and a query about state must never change it. T640
gives the `llm-tool-summ` fold its own model setting, which puts a second caller
behind the same law. The query lives here so the two cannot drift — the failure
that shape produces is not a crash but a quiet divergence, where one path
refuses a cold model and the other loads 18 GB.

⚠️ READ-ONLY, DELIBERATELY, and that is the whole point rather than a nicety.
`loaded_models()` and `list_models()` report; neither is allowed to start a load.
"""
from __future__ import annotations

import asyncio
from typing import Any


def resident_models(app: Any) -> tuple[set[str], bool]:
    """(what is loaded right now, is the backend remote).

    Queried per call, not read from a UI snapshot: a backend switch between the
    snapshot and the call is exactly when a stale answer names a cold model.

    A remote backend loads nothing, so its "loaded" set is not meaningful and
    callers are expected to branch on the flag rather than on an empty set —
    those two states look identical and mean opposite things.
    """
    backend = getattr(app, "backend", None)
    remote = bool(getattr(backend, "remote", False))
    rows = getattr(app, "model_rows", {}) or {}
    if backend is not None and not remote:
        resident = getattr(backend, "loaded_models", None)
        listing = getattr(backend, "list_models", None)
        if resident is not None:
            return {m for m in resident() if m}, remote
        if listing is not None:
            return {r.key for r in asyncio.run(listing()) if r.loaded}, remote
    return {key for key, row in rows.items() if getattr(row, "loaded", False)}, remote


def resolve_side_call_model(app, configured: str | None) -> tuple[str, str | None]:
    """The model a side call should use, and a note when it is not the one asked for.

    🔴 IT FALLS BACK; IT DOES NOT RAISE, and that is the difference from
    `subagent_plugin._resolve_model`. A subagent call that cannot honour its
    model has nothing to do and says so. The `llm-tool-summ` fold is a
    best-effort fold of a tool result the turn ALREADY HAS — failing it would
    turn a configuration mistake into a degraded turn, and the existing fallback
    below it already treats "no summary" as a mask. So a cold pick degrades to
    the main model and reports why.

    ⚠️ THE NOTE IS THE POINT, not a courtesy. Silently using a different model
    than the one configured is the shape that makes a setting look broken with
    nothing anywhere saying it was ignored — the same reason the fold's own
    `why_no_summary` line exists.

    Returns (model, note). `note` is None when nothing surprising happened.
    """
    main = getattr(app, "model_id", None) or "local-model"
    want = (configured or "").strip()
    if not want:
        return main, None  # None = the main model, which is the old behaviour.
    loaded, remote = resident_models(app)
    if remote:
        # A remote backend loads nothing, so an id it does not serve costs one
        # 404 and no VRAM. Refusing here would be a different card's rule.
        return want, None
    if want in loaded:
        return want, None
    names = ", ".join(sorted(loaded)) or "(none)"
    return main, (
        f"tool-summary model {want!r} is not loaded, so the fold used "
        f"{main!r} instead. Loaded: {names}. Nothing was loaded to satisfy it."
    )
