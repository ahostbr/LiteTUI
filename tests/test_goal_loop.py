"""Mechanism tests for the lightweight /goal + /loop primitives."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import goal_loop as goal_mod
from litetui import scheduler
from litetui.goal_loop import (
    GoalRuntime,
    GoalState,
    GoalVerdictError,
    load_goal,
    parse_interval,
    parse_verdict,
    save_goal,
)
from litetui.plugins import PluginRegistry


def test_interval_parser_is_explicit_and_minute_based() -> None:
    assert parse_interval("15m") == 15
    assert parse_interval("2h") == 120
    assert parse_interval("1d") == 1440
    with pytest.raises(ValueError):
        parse_interval("0m")
    with pytest.raises(ValueError):
        parse_interval("whenever")


def test_goal_state_round_trips_atomically(tmp_path: Path) -> None:
    state = GoalState(objective="ship with proof", tool_profile="interactive")
    save_goal(tmp_path, state)
    assert load_goal(tmp_path) == state
    assert not list(tmp_path.glob(".goal-*.tmp"))


def test_met_verdict_requires_evidence_present_in_the_transcript() -> None:
    raw = json.dumps({
        "verdict": "met",
        "reason": "tests passed",
        "evidence": ["17 passed"],
        "missing_evidence": [],
        "next_instruction": "",
    })
    with pytest.raises(GoalVerdictError):
        parse_verdict(raw, "the worker merely says it is done")
    verdict = parse_verdict(raw, "pytest: 17 passed in 0.4s")
    assert verdict.verdict == "met"


class _Completion:
    def __init__(self, payload: dict) -> None:
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
        ]


class _FakeCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self.payload)


def _runtime_app(tmp_path: Path, payload: dict, transcript: str):
    completions = _FakeCompletions(payload)

    class App:
        def __init__(self) -> None:
            self.convo_dir = tmp_path
            self.convo_id = "convo-a"
            self.model_id = "qwen"
            self.conversation = [
                {"role": "user", "content": "[goal goal-test] do the work"},
                {"role": "assistant", "content": transcript},
            ]
            self.client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )
            self.settings = SimpleNamespace(
                compact_max_tokens=2048,
                tool_policy_profile="interactive",
            )
            self._pending_input: list[dict] = []
            self.notices: list[str] = []

        @property
        def workers(self):
            raise AssertionError("completion must never inspect persistent workers")

        async def _ensure_chat_ready(self):
            return None

        def _system(self, text: str):
            self.notices.append(text)

    return App(), completions


@pytest.mark.asyncio
async def test_not_met_queues_exactly_one_continuation(tmp_path: Path) -> None:
    payload = {
        "verdict": "not_met",
        "reason": "the test has not run",
        "evidence": [],
        "missing_evidence": ["test output"],
        "next_instruction": "run the focused test",
    }
    app, completions = _runtime_app(tmp_path, payload, "implementation written")
    save_goal(tmp_path, GoalState(objective="finish and test it", id="goal-test"))
    runtime = GoalRuntime(app)
    await runtime.on_plain_answer()
    await runtime.on_plain_answer()
    assert len(completions.calls) == 2
    assert completions.calls[0]["stream"] is False
    assert "tools" not in completions.calls[0]
    assert len(app._pending_input) == 1
    assert app._pending_input[0]["goal_continuation"] is True


@pytest.mark.asyncio
async def test_persistent_watcher_cannot_prevent_met_closure(tmp_path: Path) -> None:
    payload = {
        "verdict": "met",
        "reason": "the focused test passed",
        "evidence": ["17 passed"],
        "missing_evidence": [],
        "next_instruction": "",
    }
    app, _ = _runtime_app(tmp_path, payload, "pytest: 17 passed")
    save_goal(tmp_path, GoalState(objective="finish and test it", id="goal-test"))
    await GoalRuntime(app).on_plain_answer()
    assert load_goal(tmp_path).status == "met"
    assert app._pending_input == []


@pytest.mark.asyncio
async def test_malformed_verdict_pauses_visibly(tmp_path: Path) -> None:
    app, completions = _runtime_app(tmp_path, {}, "some output")
    completions.payload = {"verdict": "probably"}
    save_goal(tmp_path, GoalState(objective="finish safely", id="goal-test"))
    await GoalRuntime(app).on_plain_answer()
    assert load_goal(tmp_path).status == "paused"
    assert "paused" in app.notices[-1].lower()


@pytest.mark.asyncio
async def test_turn_that_started_before_goal_prompt_is_not_evaluated(
    tmp_path: Path,
) -> None:
    payload = {
        "verdict": "not_met",
        "reason": "continue",
        "evidence": [],
        "missing_evidence": ["proof"],
        "next_instruction": "work",
    }
    app, completions = _runtime_app(tmp_path, payload, "older turn finished")
    app.conversation[0]["content"] = "ordinary user turn without the goal marker"
    save_goal(tmp_path, GoalState(objective="new objective", id="goal-test"))
    await GoalRuntime(app).on_plain_answer()
    assert completions.calls == []
    assert app._pending_input == []
    assert load_goal(tmp_path).evaluated_turns == 0


@pytest.mark.asyncio
async def test_turn_finalizer_is_an_explicit_async_plugin_seam() -> None:
    registry = PluginRegistry()
    seen: list[str] = []

    async def finish():
        seen.append("plain-answer")

    registry.add_turn_finalizer("test", finish)
    await registry.finalize_turn()
    assert seen == ["plain-answer"]


def test_legacy_scheduler_json_still_loads(tmp_path: Path) -> None:
    (tmp_path / scheduler.JOBS_FILENAME).write_text(
        json.dumps([{"prompt": "legacy", "schedule": "@daily"}]),
        encoding="utf-8",
    )
    [job] = scheduler.load(tmp_path)
    assert job.kind == "cron"
    assert job.prompt == "legacy"


def test_loop_due_uses_the_existing_scheduler_tick() -> None:
    job = scheduler.Job.loop(
        prompt="check the build",
        interval_minutes=15,
        owner_convo_id="convo-a",
        now="2026-08-23T12:00:00",
    )
    due = scheduler.due(
        [job],
        __import__("datetime").datetime.fromisoformat("2026-08-23T12:15:00"),
    )
    assert due == [job]


def test_goal_and_loop_are_registered_as_first_party_commands(monkeypatch) -> None:
    from litetui import settings as settings_mod
    from litetui.settings import Settings

    monkeypatch.setattr(settings_mod, "load", lambda: Settings())
    app = app_mod.LiteTUI()
    assert app.plugins.commands["/goal"].owner == "goal-loop"
    assert app.plugins.commands["/loop"].owner == "goal-loop"
    assert len(app.plugins.turn_finalizers) == 1


def test_plain_answer_is_the_only_stream_site_that_finalizes() -> None:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.LiteTUI._stream)))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "finalize_turn"
    ]
    assert len(calls) == 1
    parents = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and calls[0] in list(ast.walk(node))
    ]
    assert any(
        isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "tool_acc"
        for node in parents
    )


def test_wrong_conversation_loop_pauses_without_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    job = scheduler.Job.loop(
        prompt="do not cross transcripts",
        interval_minutes=5,
        owner_convo_id="owner-a",
    )
    notices: list[str] = []
    delivered: list[str] = []
    app = SimpleNamespace(
        convo_id="other-b",
        jobs=[job],
        _system=notices.append,
        _chat_running=lambda: False,
        _handle_command=lambda command: delivered.append(command),
        _user_bubble=lambda *args, **kwargs: delivered.append("bubble"),
        _append=lambda message: delivered.append("append"),
        _stream=lambda: delivered.append("stream"),
    )
    monkeypatch.setattr(app_mod.paths, "ROOT", tmp_path)
    app_mod.LiteTUI._fire_job(app, job)
    assert job.enabled is False
    assert delivered == []
    assert "owner conversation" in notices[-1]
    [restored] = scheduler.load(tmp_path)
    assert restored.enabled is False


def test_due_loop_queues_behind_busy_owner_turn(tmp_path: Path, monkeypatch) -> None:
    job = scheduler.Job.loop(
        prompt="inspect after the current turn",
        interval_minutes=5,
        owner_convo_id="owner-a",
        now="2026-08-23T12:00:00",
    )
    pending: list[dict] = []
    app = SimpleNamespace(
        convo_id="owner-a",
        jobs=[job],
        _pending_input=pending,
        _chat_running=lambda: True,
        _user_bubble=lambda *args, **kwargs: None,
        # _fire_job reads the SET level since Ryan's ruling ("cron and loops run
        # at same set profile level"), so a double without settings no longer
        # models the app. Stated as `scheduled` to keep this test's subject --
        # that a due loop QUEUES behind a busy turn -- unchanged.
        settings=SimpleNamespace(tool_policy_profile="scheduled"),
    )
    monkeypatch.setattr(app_mod.paths, "ROOT", tmp_path)
    app_mod.LiteTUI._fire_job(app, job)
    assert job.run_count == 1
    assert len(pending) == 1
    assert pending[0]["tool_profile"] == "scheduled"
    assert pending[0]["content"] == job.prompt


def test_loop_command_creates_a_conversation_owned_scheduled_job(
    tmp_path: Path, monkeypatch
) -> None:
    notices: list[str] = []
    app = SimpleNamespace(
        convo_id="convo-a",
        convo_dir=tmp_path / "convo-a",
        jobs=[],
        _materialise_convo=lambda: None,
        _system=notices.append,
    )
    monkeypatch.setattr(goal_mod.paths, "ROOT", tmp_path)
    goal_mod.loop_command(app, "15m inspect the build")
    [job] = app.jobs
    assert job.kind == "loop"
    assert job.owner_convo_id == "convo-a"
    assert job.tool_profile == "scheduled"
    assert scheduler.load(tmp_path)[0].id == job.id


def test_goal_command_persists_then_starts_a_goal_owned_turn(tmp_path: Path) -> None:
    appended: list[dict] = []
    streamed: list[bool] = []
    app = SimpleNamespace(
        convo_id="convo-a",
        convo_dir=tmp_path,
        settings=SimpleNamespace(tool_policy_profile="interactive"),
        _pending_input=[],
        _materialise_convo=lambda: None,
        _chat_running=lambda: False,
        _user_bubble=lambda *args, **kwargs: None,
        _append=appended.append,
        _stream=lambda: streamed.append(True),
        _active_tool_profile="",
    )
    goal_mod.goal_command(app, "finish with evidence")
    state = load_goal(tmp_path)
    assert state is not None and state.status == "active"
    assert f"[goal {state.id}]" in appended[0]["content"]
    assert streamed == [True]
