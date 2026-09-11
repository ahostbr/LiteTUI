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


def _family(model: str) -> str:
    """The leading alphabetic run of a model's LAST path segment, lowercased.

    `qwen/qwen3.8-27b` -> "qwen"; `minicpm5-2b` -> "minicpm". Deliberately crude:
    it exists only to break a tie between residents, so being approximately right
    costs a slightly odd pick and never a wrong refusal.
    """
    last = model.rsplit("/", 1)[-1].strip().lower()
    end = 0
    while end < len(last) and last[end].isalpha():
        end += 1
    return last[:end]


def substitute_main_model(
    resident: set[str],
    *,
    want: str | None,
    subagent_model: str | None,
    tool_summary_model: str | None,
    default_model: str | None,
) -> str | None:
    """Which resident model a MAIN seat should answer with, or None to refuse.

    🔴 RYAN'S RULE: "when a model is already loaded, USE THAT ONE." T594 refused
    whenever several were resident and none was the one asked for, on the
    reasoning that substituting is "picking one on the user's behalf". The
    hazard is real and the remedy was wrong: a tester's fresh thread refused its
    first prompt with TWO models sitting in VRAM (T642). Refuse now means one
    thing only — NOTHING is resident.

    ⚠️ THE HELPERS ARE EXCLUDED BY VALUE, NOT BY LOOKING SMALL. `subagent_model`
    and `tool_summary_model` (T538/T640) are configured for side calls, so a main
    seat answering as one of them would quietly hand the user a 2B where they
    expected a 27B. Nothing here inspects a name for size, because nothing in
    this process knows a size: `ModelRow` has none, LM Studio rows have no path,
    and the native listing reports CONTEXT LENGTHS, which are not sizes.

    THE TIE-BREAK, ruled by Sentinel (d1e7d2cd) and stated rather than measured:
      1. prefer a resident that is neither helper;
      2. among those, one whose family matches `default_model`'s;
      3. otherwise the first by name — deterministic, so a restarted child does
         not silently answer as somebody else.
    If step 1 empties the set, the helpers come back: the only resident being the
    subagent's model is still better than no answer at all.
    """
    if not resident:
        return None
    if want and want in resident:
        return want

    helpers = {h.strip() for h in (subagent_model, tool_summary_model) if h and h.strip()}
    candidates = resident - helpers or resident

    family = _family(default_model or "")
    same_family = [c for c in candidates if family and _family(c) == family]
    # `min`, not `sorted()[0]`: same answer, and it says "the first by name" once
    # rather than ordering a whole set to read one element off it.
    return min(same_family) if same_family else min(candidates)
