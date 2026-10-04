"""Fleet model/thinking floor (T1025).

2026-09-26: a Codex seat ran gpt-5.6-sol at medium thinking because the spawner
read a stale model list and substituted a weaker model. Ryan: "this MUST NEVER
happen again". So the floor lives in the spawn path, not in prose:

  check()            pre-launch: refuse a request below the floor
  check_available()  pre-launch: a model missing from the live list is refetched,
                     then refused by name -- never substituted
  verify_seat()      post-launch: read what the SEAT says it resolved (its presence
                     row) and fail on anything below the floor

Which seats a floor governs: every seat on a backend named like the floor
(codex governs every model it runs), plus every seat whose model starts with
one of the floor's match_prefixes ON ANY BACKEND -- the floor is about the
model's capability, and LiteTUI's custom backend can be OpenAI itself -- unless
the model starts with one of its exempt_prefixes (gpt-oss-: open weights run
locally, not the codex family).

"Below the floor" is index arithmetic on two ordered lists (low -> high). A model
or level that is NOT in its list is refused, never treated as above the floor.

The policy file is ~/.litesuite/fleet-policy.json, or $LITESUITE_FLEET_POLICY.
Missing -> DEFAULT_POLICY (a missing file must not remove the floor).
Unreadable or malformed -> PolicyError, and every spawn is refused.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

POLICY_ENV = "LITESUITE_FLEET_POLICY"

DEFAULT_POLICY: dict = {
    "version": 1,
    "floors": {
        "codex": {
            "match_prefixes": ["gpt-"],
            "exempt_prefixes": ["gpt-oss-"],
            "models": [
                "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
                "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra",
            ],
            "min_model": "gpt-6-sol",
            "thinking_levels": [
                "off", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
            ],
            "min_thinking_level": "high",
        }
    },
}


class PolicyError(Exception):
    """The policy file exists but cannot be used. Every spawn is refused."""


def policy_path() -> Path:
    override = os.environ.get(POLICY_ENV, "").strip()
    return Path(override) if override else Path.home() / ".litesuite" / "fleet-policy.json"


def _validate(policy: object, where: str) -> dict:
    def bad(why: str) -> PolicyError:
        return PolicyError(f"fleet policy {where} is malformed: {why}. Every spawn is refused until it is fixed.")

    if not isinstance(policy, dict) or not isinstance(policy.get("floors"), dict) or not policy["floors"]:
        raise bad("expected {\"floors\": {<backend>: {...}}}")
    for name, floor in policy["floors"].items():
        if not isinstance(floor, dict):
            raise bad(f"floor {name!r} is not an object")
        for list_key, min_key in (("models", "min_model"), ("thinking_levels", "min_thinking_level")):
            items = floor.get(list_key)
            if not isinstance(items, list) or not items or not all(isinstance(x, str) for x in items):
                raise bad(f"floor {name!r}.{list_key} must be a non-empty list of strings")
            if floor.get(min_key) not in items:
                raise bad(f"floor {name!r}.{min_key} {floor.get(min_key)!r} is not in {list_key}")
        for key in ("match_prefixes", "exempt_prefixes"):
            prefixes = floor.get(key, [])
            if not isinstance(prefixes, list) or not all(isinstance(x, str) for x in prefixes):
                raise bad(f"floor {name!r}.{key} must be a list of strings")
    return policy


def load(path: Path | None = None) -> tuple[dict, str]:
    """(policy, where-it-came-from). Raises PolicyError for an unusable file."""
    path = path or policy_path()
    if not path.exists():
        return DEFAULT_POLICY, f"built-in default ({path} absent)"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"fleet policy {path} is unreadable ({type(exc).__name__}: {exc}). "
                          "Every spawn is refused until it is fixed.") from exc
    return _validate(policy, str(path)), str(path)


def floor_for(policy: dict, backend: str | None, model: str | None) -> tuple[str, dict] | None:
    """The floor that governs this seat, or None when no floor applies. A backend
    named like the floor governs every model; otherwise a match_prefixes model
    is governed on ANY backend (custom can be OpenAI itself), unless it is one of
    the floor's exempt_prefixes (gpt-oss-120b on local is a local seat)."""
    backend = (backend or "").strip().lower()
    model = (model or "").strip().lower()

    def starts(key: str, floor: dict) -> bool:
        return any(model.startswith(p.lower()) for p in floor.get(key, []))

    for name, floor in policy["floors"].items():
        if backend == name.lower():
            return name, floor
        if model and starts("match_prefixes", floor) and not starts("exempt_prefixes", floor):
            return name, floor
    return None


def _clean(value: str | None) -> str | None:
    """Trimmed, and blank -> None: the same normalisation resolveSpawn applies."""
    return value.strip() or None if isinstance(value, str) else value


def _add_model_hint(name: str, floor: dict, path: Path | None) -> str:
    """Sentinel af053094: a new model is ONE EDIT to the policy file, never a code
    change -- a refusal that reads like a wall gets routed around."""
    path = path or policy_path()
    target = str(path) if path.exists() else f"the built-in default; create {path} to override"
    return (f"To allow it, add it to floors.{name}.models in {target}. That list is ordered "
            f"low->high, so insert it by rank (the floor is min_model {floor['min_model']})")


def below_floor(name: str, floor: dict, model: str | None, thinking_level: str | None,
                path: Path | None = None) -> str | None:
    """Why (model, thinking_level) is below `floor`, or None when it meets it."""
    need = (f"{name} seats need model >= {floor['min_model']} and thinking_level >= "
            f"{floor['min_thinking_level']} (models low->high: {', '.join(floor['models'])}; "
            f"levels low->high: {', '.join(floor['thinking_levels'])})")
    models, levels = floor["models"], floor["thinking_levels"]
    problems = []
    if model not in models:
        problems.append(f"model {model!r} is not in the {name} list, and an unlisted model is refused"
                        + (f". Nothing was substituted. {_add_model_hint(name, floor, path)}" if model else ""))
    elif models.index(model) < models.index(floor["min_model"]):
        problems.append(f"model {model!r} is below {floor['min_model']}")
    if thinking_level not in levels:
        problems.append(f"thinking_level {thinking_level!r} is not a listed level (unset/default is refused)")
    elif levels.index(thinking_level) < levels.index(floor["min_thinking_level"]):
        problems.append(f"thinking_level {thinking_level!r} is below {floor['min_thinking_level']}")
    if not problems:
        return None
    return f"FLEET FLOOR: {'; '.join(problems)}. {need}."


def check(backend: str | None, model: str | None, thinking_level: str | None,
          path: Path | None = None) -> str | None:
    """Pre-launch gate. None = allowed; otherwise the refusal text."""
    model, thinking_level = _clean(model), _clean(thinking_level)
    try:
        policy, where = load(path)
    except PolicyError as exc:
        return f"SPAWN REFUSED: {exc}"
    governed = floor_for(policy, backend, model)
    if governed is None:
        return None
    why = below_floor(*governed, model, thinking_level, path)
    return f"SPAWN REFUSED: {why} Policy: {where}. Nothing was spawned." if why else None


def gate(backend: str | None, model: str | None, thinking_level: str | None,
         path: Path | None = None, cache: Path | None = None,
         refetch: Callable[[], set[str]] | None = None) -> str | None:
    """The whole pre-launch gate: the floor, then (for a governed seat) the model list."""
    model = _clean(model)
    why = check(backend, model, thinking_level, path)
    if why is not None:
        return why
    if floor_for(load(path)[0], backend, model) is None:
        return None
    return check_available(model, cache, refetch or refetch_codex_models)


# ── item 4: the model list ──────────────────────────────────────────────────────

def codex_models_cache() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".codex") / "models_cache.json"


def _slugs(catalog: object) -> set[str]:
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    return {m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)}


def refetch_codex_models() -> set[str]:
    """`codex debug models` refreshes the catalog (and rewrites models_cache.json)."""
    out = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True,
                         encoding="utf-8", timeout=90)
    if out.returncode != 0:
        raise RuntimeError(f"codex debug models exited {out.returncode}: {out.stderr.strip()[:200]}")
    return _slugs(json.loads(out.stdout))


def check_available(model: str, cache: Path | None = None,
                    refetch: Callable[[], set[str]] = refetch_codex_models) -> str | None:
    """None if `model` is in the codex list; else refetch once, then refuse by name."""
    try:
        cached = _slugs(json.loads((cache or codex_models_cache()).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        cached = set()
    if model in cached:
        return None
    try:
        fresh = refetch()
    except Exception as exc:  # noqa: BLE001 -- any failure to refetch is a refusal
        return (f"SPAWN REFUSED: model {model!r} is not in the cached codex list and the refetch "
                f"failed ({exc}). Nothing was substituted and nothing was spawned.")
    if model in fresh:
        return None
    return (f"SPAWN REFUSED: model {model!r} is not in the codex model list even after a refetch "
            f"(available: {', '.join(sorted(fresh)) or 'none'}). Nothing was substituted and "
            "nothing was spawned.")


# ── T0088: the codex CLI's own record of what it ran ───────────────────────────

def codex_sessions_dir() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def _codex_first_turn(rollout: Path) -> tuple[dict | None, dict | None]:
    """(session_meta payload, first turn_context payload) of a rollout. session_meta
    carries id/cwd/timestamp; the effective `model` and `effort` are on turn_context,
    which exists only once the seat has taken a turn."""
    meta = turn = None
    try:
        with open(rollout, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "session_meta" and meta is None:
                    meta = rec.get("payload") or {}
                elif rec.get("type") == "turn_context":
                    turn = rec.get("payload") or {}
                    break
    except OSError:
        pass
    return meta, turn


def _same_dir(a: object, b: object) -> bool:
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    return bool(a) and bool(b) and norm(a) == norm(b)


def verify_codex_rollout(cwd: str, since: float, expect_model: str | None, expect_effort: str | None,
                         path: Path | None = None, sessions: Path | None = None, wait: float = 90,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> str | None:
    """DORMANT (T0088): no spawn path calls this. T0088-A enables it once a codex seat can
    be tied to its own rollout; until then the codex CLI stays refused outright.

    Post-launch check of a codex CLI seat: correlated, not identity-proven.
    Finds the rollouts opened in `cwd` since `since` (epoch seconds), reads the model
    and effort of their first turn, and fails on a mismatch with what the spawn asked
    for or on anything below the codex floor. No turn within `wait`, or more than one
    candidate rollout (which one is ours?), is refused. None means exactly one
    rollout matched and it ran what was asked; it does NOT prove that rollout is this
    seat's. Attribution is cwd + time only (a codex process does not hold its rollout
    open, and the pty daemon returns no pid), so a compliant rollout from another seat
    in the same cwd, while this seat wrote none or has not yet, is a false pass. Only a
    spawner-controlled marker that lands in the rollout would close that; it needs a
    live codex to validate. Launch with the first prompt on the command line so a turn
    exists to read."""
    try:
        policy, where = load(path)
    except PolicyError as exc:
        return f"SEAT FAILED FLOOR: {exc}"
    root = sessions or codex_sessions_dir()
    deadline = clock() + wait
    while True:
        seen: list[tuple[str, str, str | None, str | None]] = []
        for rollout in root.rglob("rollout-*.jsonl") if root.exists() else ():
            try:
                if rollout.stat().st_mtime < since - 5:
                    continue
            except OSError:
                continue
            meta, turn = _codex_first_turn(rollout)
            if not meta or not turn or not _same_dir(meta.get("cwd"), cwd):
                continue
            seen.append((rollout.name, meta.get("id") or rollout.name, turn.get("model"), turn.get("effort")))
        if len(seen) > 1:
            return (f"SEAT FAILED FLOOR: {len(seen)} codex rollouts opened in {cwd} since the spawn "
                    f"({', '.join(s[1] for s in seen)}); which one is this seat's cannot be told. Refused.")
        if seen:
            for name, sid, model, effort in seen:
                if ((expect_model and not _same(model, expect_model))
                        or (expect_effort and not _same(effort, expect_effort))):
                    return (f"SEAT FAILED MISMATCH: codex session {sid} was asked for model={expect_model or '<any>'} "
                            f"thinking_level={expect_effort or '<any>'} and ran model={model} effort={effort}. "
                            f"Policy: {where}.")
                governed = floor_for(policy, "codex", model)
                why = below_floor(*governed, model, effort, path) if governed else None
                if why:
                    return f"SEAT FAILED FLOOR: codex session {sid} ran model={model} effort={effort}. {why} Policy: {where}."
            return None
        if clock() >= deadline:
            return (f"SEAT FAILED FLOOR: no codex rollout in {cwd} recorded a turn within {wait:g}s, so the "
                    "model and effort it ran cannot be read. Unverified is refused.")
        sleep(2)


# ── item 2: what the seat actually resolved ────────────────────────────────────

def _same(a: object, b: object) -> bool:
    return str(a).strip().casefold() == str(b).strip().casefold()


def verify_seat(agent_id: str, root: Path, wait: float = 90, path: Path | None = None,
                backend: str | None = None, allow_silent: bool = False,
                expect_model: str | None = None, expect_thinking: str | None = None,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> str | None:
    """Poll the seat's presence row until it reports its effective model (and,
    for a governed model, its thinking_level); None = at or above the floor.
    `backend` is the one the spawner launched: under a governed backend a model
    the floor does not list (o4-mini, codex-mini-latest) is refused, not waved
    through as ungoverned.

    `allow_silent` is for a request the pre-gate called ungoverned (a local seat
    with no model loaded reports "unknown" forever): a seat that never reports a
    model passes. It never excuses a REPORTED governed model: the pin can turn a
    local request into codex gpt-5.6-sol/medium, and that seat is refused.

    `expect_model` / `expect_thinking` are what the spawn ASKED for. Card T1025 item
    (2): the spawn fails on MISMATCH, not only below the floor -- the T1004 pin can
    override --model, and a gpt-6-astra/xhigh request that became gpt-6-sol/high,
    or a gpt-6-sol request pinned to o4-mini, is not the seat that was asked for.
    Compared trimmed and case-folded."""
    try:
        policy, where = load(path)
    except PolicyError as exc:
        return f"SEAT FAILED FLOOR: {exc}"
    row_path = root / "agents" / f"{agent_id}.json"
    deadline = clock() + wait
    row: dict = {}
    while True:
        try:
            row = json.loads(row_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            row = {}
        model = row.get("model")
        if model and model != "unknown":
            level = row.get("thinking_level")
            if ((expect_model and not _same(model, expect_model))
                    or (expect_thinking and "thinking_level" in row and not _same(level, expect_thinking))):
                return (f"SEAT FAILED MISMATCH: {agent_id} was asked for model={expect_model or '<any>'} "
                        f"thinking_level={expect_thinking or '<any>'} and resolved model={model} "
                        f"thinking_level={row.get('thinking_level', '<absent>')}. A pinned default can "
                        "override --model (T1004), so the spawn fails on any mismatch, not only below "
                        f"the floor. Policy: {where}.")
            # T0095: governed by the spawner's backend OR the backend the seat reports
            # (T1027), and it must clear every floor that applies: a reported backend
            # can add a floor (local request pinned to codex/o4-mini) but never remove one.
            governed = [g for g in (floor_for(policy, backend, model),
                                    floor_for(policy, row.get("backend"), model)) if g]
            if not governed and not expect_thinking:
                return None
            if "thinking_level" in row:
                whys = [w for g in governed if (w := below_floor(*g, model, row["thinking_level"], path))]
                return (f"SEAT FAILED FLOOR: {agent_id} resolved model={model} "
                        f"thinking_level={row['thinking_level']}. {whys[0]} Policy: {where}.") if whys else None
        if clock() >= deadline:
            if allow_silent and not (model and model != "unknown"):
                return None
            return (f"SEAT FAILED FLOOR: {agent_id} did not report its effective model and "
                    f"thinking_level within {wait:g}s (row: model={row.get('model')!r}, "
                    f"thinking_level={row.get('thinking_level', '<absent>')!r}). Unverified is refused.")
        sleep(2)
