"""Evidence-driven goals and fixed-cadence loops.

The two commands are deliberately orthogonal.  A goal reacts to an explicit
plain-answer completion; a loop reacts to the existing scheduler tick.  Neither
uses worker idleness, and neither creates a second delivery or tool path.
"""
# Local wall time is the scheduler's public contract; jobs follow the user's
# clock rather than UTC, just like the existing cron module.
# ruff: noqa: DTZ005
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from litetui import paths, scheduler
from litetui.tool_policy import INTERACTIVE, SCHEDULED

GOAL_FILENAME = "goal.json"
_INTERVAL = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
_VERDICTS = frozenset({"met", "not_met", "blocked", "impossible"})


class GoalVerdictError(ValueError):
    """The evaluator did not return a verdict safe enough to act upon."""


@dataclass(eq=True)
class GoalState:
    objective: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "active"
    created: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    updated: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    evaluated_turns: int = 0
    max_turns: int = 0
    last_verdict: str = ""
    last_reason: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    no_progress_count: int = 0
    tool_profile: str = INTERACTIVE


@dataclass(frozen=True)
class GoalVerdict:
    verdict: str
    reason: str
    evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    next_instruction: str


def parse_interval(value: str) -> int:
    """Return a fixed cadence in minutes (m/h/d only, never ambiguous)."""
    match = _INTERVAL.fullmatch(value.strip())
    if not match:
        raise ValueError("interval must look like 15m, 2h, or 1d")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError("interval must be at least one minute")
    return amount * {"m": 1, "h": 60, "d": 1440}[match.group(2).lower()]


def _goal_path(convo_dir: Path) -> Path:
    return Path(convo_dir) / GOAL_FILENAME


def save_goal(convo_dir: Path, state: GoalState) -> None:
    """Atomically replace the small derived goal state beside its transcript."""
    convo_dir = Path(convo_dir)
    convo_dir.mkdir(parents=True, exist_ok=True)
    path = _goal_path(convo_dir)
    payload = json.dumps(asdict(state), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(convo_dir), prefix=".goal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_goal(convo_dir: Path | None) -> GoalState | None:
    if convo_dir is None:
        return None
    try:
        raw = json.loads(_goal_path(Path(convo_dir)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("objective"), str):
        return None
    known = {
        key: value
        for key, value in raw.items()
        if key in GoalState.__dataclass_fields__
    }
    try:
        return GoalState(**known)
    except TypeError:
        return None


def _json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise GoalVerdictError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GoalVerdictError("verdict must be one JSON object")
    return value


def parse_verdict(raw: str, transcript: str) -> GoalVerdict:
    value = _json_object(raw)
    verdict = value.get("verdict")
    reason = value.get("reason")
    evidence = value.get("evidence", [])
    missing = value.get("missing_evidence", [])
    instruction = value.get("next_instruction", "")
    if verdict not in _VERDICTS:
        raise GoalVerdictError(f"unknown verdict: {verdict!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise GoalVerdictError("verdict needs a non-empty reason")
    if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
        raise GoalVerdictError("evidence must be a list of transcript quotes")
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise GoalVerdictError("missing_evidence must be a list of strings")
    if not isinstance(instruction, str):
        raise GoalVerdictError("next_instruction must be a string")
    clean_evidence = tuple(x.strip() for x in evidence if x.strip())
    if verdict == "met":
        if not clean_evidence:
            raise GoalVerdictError("met requires quoted transcript evidence")
        absent = [quote for quote in clean_evidence if quote not in transcript]
        if absent:
            raise GoalVerdictError("met cited evidence absent from the transcript")
    if verdict == "not_met" and not instruction.strip():
        raise GoalVerdictError("not_met requires a next_instruction")
    return GoalVerdict(
        verdict=verdict,
        reason=reason.strip(),
        evidence=clean_evidence,
        missing_evidence=tuple(x.strip() for x in missing if x.strip()),
        next_instruction=instruction.strip(),
    )


def evidence_transcript(conversation: list[dict]) -> str:
    """A text-only evidence packet; images become labels, tool results remain."""
    rows: list[str] = []
    for message in conversation:
        role = str(message.get("role", "unknown"))
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    parts.append("[image attached]")
            content = " ".join(parts)
        rows.append(f"[{role}] {content or ''}")
    # Preserve the newest evidence if an exceptionally long conversation would
    # otherwise turn the evaluator into a second compaction mechanism.
    return "\n".join(rows)[-240_000:]


def evaluator_messages(state: GoalState, transcript: str) -> list[dict]:
    schema = (
        '{"verdict":"met|not_met|blocked|impossible","reason":"...",'
        '"evidence":["exact quote from transcript"],'
        '"missing_evidence":["..."],"next_instruction":"..."}'
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a stateless completion judge. You have no tools. Judge only "
                "the supplied transcript. Never accept the worker's confidence as "
                "proof. A met verdict requires exact quoted evidence present in the "
                "transcript. "
                "Return only one JSON object matching: " + schema
            ),
        },
        {
            "role": "user",
            "content": f"GOAL:\n{state.objective}\n\nTRANSCRIPT:\n{transcript}",
        },
    ]


class GoalRuntime:
    """Plugin-owned post-turn evaluator. It never inspects worker state."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._evaluating = False

    async def on_plain_answer(self) -> None:
        if self._evaluating:
            return
        state = load_goal(getattr(self.app, "convo_dir", None))
        if state is None or state.status != "active":
            return
        self._evaluating = True
        transcript = evidence_transcript(list(getattr(self.app, "conversation", [])))
        if f"[goal {state.id}]" not in transcript:
            # /goal may have been queued while an older turn was running. That
            # older plain answer is not goal-owned and must not be judged.
            self._evaluating = False
            return
        try:
            await self.app._ensure_chat_ready()
            response = await self.app.client.chat.completions.create(
                model=self.app.model_id or "local-model",
                messages=evaluator_messages(state, transcript),
                stream=False,
                max_tokens=getattr(self.app.settings, "compact_max_tokens", 2048),
                extra_body={"reasoning_effort": "low"},
            )
            raw = response.choices[0].message.content or ""
            verdict = parse_verdict(raw, transcript)
        except Exception as exc:  # noqa: BLE001 - any unsafe verdict pauses
            state.status = "paused"
            state.last_verdict = "invalid"
            state.last_reason = f"{type(exc).__name__}: {exc}"
            state.updated = datetime.now().isoformat(timespec="seconds")
            save_goal(self.app.convo_dir, state)
            self.app._system(
                "/goal paused: evaluator returned no safe verdict "
                f"({state.last_reason})"
            )
            return
        finally:
            self._evaluating = False

        previous_reason = state.last_reason
        state.evaluated_turns += 1
        state.last_verdict = verdict.verdict
        state.last_reason = verdict.reason
        state.missing_evidence = list(verdict.missing_evidence)
        state.updated = datetime.now().isoformat(timespec="seconds")
        state.no_progress_count = (
            state.no_progress_count + 1 if verdict.reason == previous_reason else 0
        )

        if verdict.verdict == "met":
            state.status = "met"
            save_goal(self.app.convo_dir, state)
            self.app._system(f"/goal met: {verdict.reason}")
            return
        if verdict.verdict in {"blocked", "impossible"}:
            state.status = verdict.verdict
            save_goal(self.app.convo_dir, state)
            self.app._system(f"/goal {verdict.verdict}: {verdict.reason}")
            return
        if state.max_turns and state.evaluated_turns >= state.max_turns:
            state.status = "paused"
            save_goal(self.app.convo_dir, state)
            self.app._system("/goal paused: turn budget exhausted")
            return
        if state.no_progress_count >= 2:
            state.status = "paused"
            save_goal(self.app.convo_dir, state)
            self.app._system("/goal paused: evaluator reported no progress three times")
            return

        save_goal(self.app.convo_dir, state)
        if any(item.get("goal_continuation") for item in self.app._pending_input):
            return
        continuation = (
            "[goal continuation]\n"
            f"Objective: {state.objective}\n"
            f"Evaluator: {verdict.reason}\n"
            f"Next: {verdict.next_instruction}\n"
            "Continue the work and produce transcript evidence for completion."
        )
        self.app._pending_input.append({
            "content": continuation,
            "text": continuation,
            "tool_profile": state.tool_profile,
            "goal_continuation": True,
        })


def _deliver_goal_turn(app: Any, state: GoalState, instruction: str) -> None:
    text = f"[goal {state.id}]\nObjective: {state.objective}\n{instruction}"
    if app._chat_running():
        app._user_bubble(text, False, queued=True)
        app._pending_input.append({
            "content": text,
            "text": text,
            "tool_profile": state.tool_profile,
            "goal_continuation": True,
        })
        return
    app._user_bubble(text, False)
    app._append({"role": "user", "content": text})
    app._active_tool_profile = state.tool_profile
    app._stream()


def goal_command(app: Any, arg: str) -> None:
    arg = arg.strip()
    state = load_goal(getattr(app, "convo_dir", None))
    if not arg or arg.lower() in {"status", "show"}:
        if state is None:
            app._system("/goal: none active. Use /goal <objective>.")
        else:
            app._system(
                f"/goal {state.status} · {state.evaluated_turns} evaluation(s)\n"
                f"  {state.objective}\n  {state.last_reason or 'not evaluated yet'}"
            )
        return
    verb = arg.lower()
    if verb in {"clear", "remove", "rm"}:
        path = _goal_path(app.convo_dir)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        app._system("/goal cleared")
        return
    if verb == "pause":
        if state is None:
            app._system("/goal: none to pause")
            return
        state.status = "paused"
        save_goal(app.convo_dir, state)
        app._system("/goal paused")
        return
    if verb == "resume":
        if state is None:
            app._system("/goal: none to resume")
            return
        state.status = "active"
        state.updated = datetime.now().isoformat(timespec="seconds")
        save_goal(app.convo_dir, state)
        _deliver_goal_turn(app, state, "Resume from the existing transcript evidence.")
        return

    app._materialise_convo()
    state = GoalState(
        objective=arg,
        tool_profile=getattr(app.settings, "tool_policy_profile", INTERACTIVE),
    )
    save_goal(app.convo_dir, state)
    _deliver_goal_turn(
        app,
        state,
        "Work autonomously until the transcript contains verifiable completion "
        "evidence.",
    )


def _loop_jobs(app: Any) -> list[scheduler.Job]:
    return [job for job in app.jobs if getattr(job, "kind", "cron") == "loop"]


def loop_command(app: Any, arg: str) -> None:
    arg = arg.strip()
    if not arg or arg.lower() in {"list", "ls", "status"}:
        jobs = _loop_jobs(app)
        if not jobs:
            app._system("/loop: none. Use /loop <15m|2h|1d> <prompt>.")
            return
        lines = [f"{len(jobs)} loop(s):"]
        for job in jobs:
            state = "on" if job.enabled else "paused"
            lines.append(
                f"  {job.id} {state} every {job.interval_minutes}m · {job.prompt}"
            )
        app._system("\n".join(lines))
        return
    verb, _, rest = arg.partition(" ")
    if verb.lower() in {"clear", "rm", "remove", "pause", "resume"}:
        token = rest.strip()
        hits = [job for job in _loop_jobs(app) if not token or job.id.startswith(token)]
        if len(hits) != 1:
            app._system(f"/loop: expected one matching loop, found {len(hits)}")
            return
        job = hits[0]
        if verb.lower() in {"clear", "rm", "remove"}:
            app.jobs.remove(job)
            scheduler.save(app.jobs, paths.ROOT)
            app._system(f"/loop removed {job.id}")
        else:
            job.enabled = verb.lower() == "resume"
            if job.enabled:
                job.next_run_at = (
                    datetime.now() + timedelta(minutes=job.interval_minutes)
                ).isoformat(timespec="seconds")
            scheduler.save(app.jobs, paths.ROOT)
            app._system(f"/loop {job.id} {'resumed' if job.enabled else 'paused'}")
        return
    interval_token, _, prompt = arg.partition(" ")
    try:
        minutes = parse_interval(interval_token)
    except ValueError as exc:
        app._system(f"/loop: {exc}")
        return
    if not prompt.strip():
        app._system("/loop: a prompt is required after the interval")
        return
    app._materialise_convo()
    job = scheduler.Job.loop(
        prompt=prompt.strip(),
        interval_minutes=minutes,
        owner_convo_id=app.convo_id,
        tool_profile=SCHEDULED,
    )
    app.jobs.append(job)
    scheduler.save(app.jobs, paths.ROOT)
    app._system(f"/loop added {job.id} · every {minutes}m\n  {job.prompt}")
