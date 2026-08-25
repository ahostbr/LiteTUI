"""Denying a tool in the approval modal ENDS THE TURN.

Ryan's ruling, 2026-08-24. He was asked whether Deny should REPLACE the
existing behaviour (the loop carries on with an honest refusal the model can
react to) or SUPPLEMENT it, with the cost of replacing stated: there is then no
way to refuse one call and let the model try a different approach in the same
turn. He chose replace.

🔴 THE BUG THIS FILE ALSO LOCKS, WHICH IS NOT PART OF THAT RULING.

`_stream`'s tool loop breaks at the top when `_stop_requested` is set, and then
falls through to the bottom of the function — which prints

    [stopped — reached N tool iterations in one turn — raise it in /settings]

So ANY early break explained itself with a cap that was never reached, and sent
the user to change a setting that had nothing to do with it. That was already
reachable before this change (press Escape while a tool is executing rather
than while the model is streaming); denying a tool would have made it the
common case. Nothing in the suite mentioned that message, so nothing caught it.

THE DISCRIMINATOR. The fake model calls a tool on EVERY round, forever, and
`tool_iterations` is 3. The two behaviours are therefore far apart:

    deny stops the turn   ->  1 attempt, "[stopped — you denied ...]"
    deny does not         ->  3 attempts, "reached 3 tool iterations"

and `test_approving_runs_to_the_iteration_cap` is the control that PROVES this
harness can reach the cap. Without it, "no cap message" in the deny test could
mean the loop stopped correctly OR that the harness never got there.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings
from litetui.tool_approval import ALWAYS, DENIED, ONCE
from litetui.tool_policy import SHELL_POLICY

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-denystop-"))


# ── fakes: a model that calls the same tool every round, forever ───────────

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, **kw):
        self.choices = [type("C", (), {"delta": _Delta(**kw)})()]
        self.usage = None


class _ToolCallStream:
    def __init__(self, n: int):
        self._chunks = [
            _Chunk(tool_calls=[_TC(0, id=f"c{n}", name="probe")]),
            _Chunk(tool_calls=[_TC(0, arguments='{"command": "git status"}')]),
        ]

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


def _app(answer, iterations: int = 3):
    """A LiteTUI whose one tool needs confirmation, answered with `answer`."""
    a = app_mod.LiteTUI()
    # Same reason as test_tool_approval: this file is ABOUT the deny path,
    # which only exists under a confirm profile. T084's default never asks.
    a.settings.tool_policy_profile = app_mod.tool_policy.INTERACTIVE
    # BOTH, because `_active_tool_profile` is stamped from settings at
    # CONSTRUCTION and `_execute_tool` reads that, not the settings field.
    # Setting only the settings value leaves the door on the old profile.
    a._active_tool_profile = app_mod.tool_policy.INTERACTIVE
    a.settings = Settings(
        tools_enabled=True,
        tool_iterations=iterations,
        autocompact_enabled=False,
        wake_after_compact=False,
        clear_screen_after_compact=False,
    )
    a.tools_enabled = True
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.said = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))

    a.ran = []
    a.prompted = []
    a.plugins.add_tool(
        "test",
        {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda args: a.ran.append(args) or "ran",
        policy=SHELL_POLICY,
    )

    async def confirm(screen):
        a.prompted.append(screen)
        return answer

    a.push_screen_wait = confirm

    rounds = {"n": 0}

    async def create(**kw):
        rounds["n"] += 1
        return _ToolCallStream(rounds["n"])

    a._create = create
    return a


async def _settle(a, pilot, extra: int = 10):
    for _ in range(200):
        await pilot.pause()
        if not a._chat_running():
            break
    for _ in range(extra):
        await pilot.pause()


async def _turn(a, pilot):
    a.client.chat.completions.create = a._create
    a._append({"role": "user", "content": "please run the probe"})
    a._stream()
    await _settle(a, pilot)


# ── the ruling ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_denying_a_tool_ends_the_turn():
    a = _app(DENIED)
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)

    assert len(a.prompted) == 1, f"asked {len(a.prompted)} times, expected once"
    assert a.ran == [], "the denied tool executed"
    assert a._stop_requested is True, "the turn was not stopped"
    assert a._stop_reason and "probe" in a._stop_reason, a._stop_reason


@pytest.mark.asyncio
async def test_the_stop_says_WHY_and_never_blames_the_iteration_cap():
    """The lie. A denial must not be reported as an exhausted iteration cap."""
    a = _app(DENIED)
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)

    said = " | ".join(a.said)
    assert "you denied probe" in said, said
    assert "reached" not in said, f"a denial was reported as the cap: {said}"
    assert "/settings" not in said, f"sent the user to raise a limit: {said}"


@pytest.mark.asyncio
async def test_the_refusal_is_still_recorded_for_the_transcript():
    """Stopping the turn must not swallow WHY it stopped. The tool result is
    still appended, so the next turn's context says the user refused."""
    a = _app(DENIED)
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)

    tool_msgs = [m for m in a.conversation if m.get("role") == "tool"]
    assert tool_msgs, "the refusal was never recorded"
    body = str(tool_msgs[-1].get("content", ""))
    assert "denied by user" in body, body
    assert "probe" in body, body
    assert "{" not in body, f"a raw placeholder reached the transcript: {body}"


@pytest.mark.asyncio
async def test_a_denied_turn_counts_as_abandoned():
    """Same reasoning as the Esc path: a turn the user killed must not be
    resumed by the post-compaction wake ping."""
    a = _app(DENIED)
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)
    assert a._turn_abandoned is True


# ── the controls ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approving_runs_to_the_iteration_cap():
    """THE CONTROL THAT MAKES THE DENY TESTS MEAN ANYTHING.

    Identical harness, opposite answer. It proves the fake model really does
    keep calling tools and that this loop really does reach the cap message —
    so the absence of that message above is the stop working, not the harness
    falling short of it.
    """
    a = _app(ONCE, iterations=3)
    async with a.run_test(size=(100, 35)) as pilot:
        await _turn(a, pilot)

    said = " | ".join(a.said)
    assert len(a.ran) == 3, f"ran {len(a.ran)} times, expected the full cap"
    assert len(a.prompted) == 3, "'once' stopped asking"
    assert "reached 3 tool iterations" in said, said
    assert a._stop_requested is False, "approving stopped the turn"


@pytest.mark.asyncio
async def test_always_allow_runs_to_the_cap_and_asks_only_once():
    """The other side of T073: 'always' silences the prompt without stopping
    anything. If deny-stops-turn had been implemented on the modal's dismissal
    rather than on the DENIAL, this would stop too."""
    a = _app(ALWAYS, iterations=3)
    a.settings.tool_always_allow = []
    saved = []
    a_save = app_mod.settings_mod.save
    app_mod.settings_mod.save = lambda s, root=None: saved.append(s)
    try:
        async with a.run_test(size=(100, 35)) as pilot:
            await _turn(a, pilot)
    finally:
        app_mod.settings_mod.save = a_save

    assert len(a.prompted) == 1, f"asked {len(a.prompted)} times despite 'always'"
    assert len(a.ran) == 3, f"ran {len(a.ran)} times"
    assert a._stop_requested is False, "'always allow' stopped the turn"
    assert a.settings.tool_always_allow == ["probe:process_execution"]
