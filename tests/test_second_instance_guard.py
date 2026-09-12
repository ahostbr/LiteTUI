"""T690 — a second LiteTUI may SHARE a model, but loading a different one is
a VRAM decision the human makes.

Ryan's ruling (a-62edbbe0, 2026-09-12): *"no both should be able to use the
model at the same time ... llama.cpp supports parallel and lmstudio does for
exactly this ... THE HUMAN MUST BE WARNED THAT LOADING different models in
different instances WILL CAUSE MULTIPLE MODELS IN VRAM! ... they go through a
warning modal every time ... EVERY TIME a model would be swapped or loaded."*

🔴 SHARING IS THE DESIGN, NOT THE DEFECT. An earlier reading of the same
measurement treated two instances on one server as a collision to prevent and
proposed an ownership field; that was overruled. Nothing here serialises
anything: same-model use raises no modal at all, and the gate fires only when a
load would put a SECOND set of weights on the card.

⚠️ THE REGISTRY IS PATCHED IN EVERY ARM. `harness.AGENTS_DIR` is
`~/.liteharness/agents`, live, with Ryan's own seats in it — an arm that read it
would pass or fail depending on which windows happened to be open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui import harness, router_record, second_instance

SELF_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registry we control, plus a liveness oracle we control."""
    d = tmp_path / "agents"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(harness, "AGENTS_DIR", d)
    alive: set[int] = set()
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: pid in alive)

    def add(agent_id: str, *, cli: str = "litetui", pid: int, name: str, live: bool) -> None:
        (d / f"{agent_id}.json").write_text(
            json.dumps({"agent_id": agent_id, "cli": cli, "session_pid": pid, "name": name}),
            encoding="utf-8",
        )
        if live:
            alive.add(pid)

    add.dir = d  # type: ignore[attr-defined]
    return add


def test_no_other_instance_when_the_registry_is_empty(registry) -> None:
    assert harness.other_live_litetui(SELF_ID) is None


def test_a_live_sibling_is_found_by_name(registry) -> None:
    registry("22222222-2222-2222-2222-222222222222", pid=4242, name="OpenBolt", live=True)
    assert harness.other_live_litetui(SELF_ID) == "OpenBolt"


def test_a_DEAD_row_is_not_a_sibling(registry) -> None:
    """A crash leaves its row behind. Counting it would raise the modal on
    every load forever, which is the fastest way to teach someone to click
    through a warning without reading it."""
    registry("22222222-2222-2222-2222-222222222222", pid=4242, name="Ghost", live=False)
    assert harness.other_live_litetui(SELF_ID) is None


def test_OUR_OWN_row_is_never_a_sibling(registry) -> None:
    """We are always in the registry, and we are always alive."""
    registry(SELF_ID, pid=999, name="Me", live=True)
    assert harness.other_live_litetui(SELF_ID) is None


def test_another_CLI_is_not_a_sibling(registry) -> None:
    """Claude Code and Codex seats share this registry and load no models."""
    registry("33333333-3333-3333-3333-333333333333",
             cli="claude-code", pid=777, name="Sentinel", live=True)
    assert harness.other_live_litetui(SELF_ID) is None


def test_a_headless_litetui_child_COUNTS(registry) -> None:
    """LiteSuite's rpc children register as `litetui` and load models like any
    other instance. Excluding them because they have no window would miss the
    case most likely to surprise the human — a model loaded by something they
    are not looking at."""
    registry("44444444-4444-4444-4444-444444444444", pid=8080, name="litetui-rpc", live=True)
    assert harness.other_live_litetui(SELF_ID) == "litetui-rpc"


def test_an_unreadable_row_is_skipped_not_fatal(registry) -> None:
    """This runs on the way into a model load; a corrupt file must not take the
    load down with it."""
    registry("22222222-2222-2222-2222-222222222222", pid=4242, name="OpenBolt", live=True)
    (registry.dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert harness.other_live_litetui(SELF_ID) == "OpenBolt"


class TestTheRule:
    """`needs_vram_confirmation` — three inputs, and every combination stated."""

    def test_alone_never_prompts_even_for_a_swap(self) -> None:
        """One instance swapping models replaces its OWN weights. There is no
        second set, so there is nothing to warn about — and a prompt here is
        the noise that teaches someone to stop reading prompts."""
        assert second_instance.needs_vram_confirmation(
            sibling=None, already_loaded=False) is False
        assert second_instance.needs_vram_confirmation(
            sibling=None, already_loaded=True) is False

    def test_the_SAME_model_never_prompts_and_that_is_the_design(self) -> None:
        """Ryan: *"both should be able to use the model at the same time"*.
        Two instances on one loaded model cost one set of weights. Prompting
        would be asking permission for the thing he asked for."""
        assert second_instance.needs_vram_confirmation(
            sibling="OpenBolt", already_loaded=True) is False

    def test_a_DIFFERENT_model_beside_a_sibling_is_the_one_case_that_prompts(self) -> None:
        assert second_instance.needs_vram_confirmation(
            sibling="OpenBolt", already_loaded=False) is True


class TestWhatItSays:
    def test_the_hot_load_warning_names_the_sibling_the_model_and_the_cost(self) -> None:
        t = second_instance.warning_text("OpenBolt", "qwen/qwen3-8b")
        assert "OpenBolt" in t and "qwen/qwen3-8b" in t
        assert "VRAM" in t and "OOM" in t

    def test_the_RESTART_warning_is_a_different_sentence_not_a_louder_one(self) -> None:
        """A hot load costs VRAM; a preset regen costs the other instance its
        turn. Saying "may use more VRAM" when the real cost is "the other
        window's answer stops" is the wrong warning delivered convincingly."""
        t = second_instance.warning_text("OpenBolt", "qwen/x", needs_restart=True)
        assert "RESTART" in t.upper()
        assert "interrupts OpenBolt" in t
        assert "OOM" not in t, "the restart cost is not an OOM risk; do not cite one"

    def test_a_headless_child_REFUSES_and_says_where_to_do_it(self) -> None:
        """It must not assume yes. An rpc child has no keyboard, and approving
        on the human's behalf reaches the exact outcome the modal prevents, by
        the one path that cannot show it."""
        t = second_instance.refusal_text("OpenBolt", "qwen/x")
        assert t.startswith("refused:")
        assert "OpenBolt" in t and "qwen/x" in t
        assert "LiteTUI window" in t
