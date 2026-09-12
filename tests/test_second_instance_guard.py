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

from litetui import harness, llm_backend, router_record, second_instance

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


# ── the chokepoint ───────────────────────────────────────────────────────────


class _Gate:
    """Records what it was asked about and answers a scripted verdict."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.asked: list[str] = []

    async def __call__(self, model: str) -> bool:
        self.asked.append(model)
        return self.allow


def _llama(monkeypatch, gate):
    from litetui import settings as st

    cfg = st.Settings()
    cfg.backend = "llamacpp"
    b = llm_backend.LlamaCppBackend(cfg)
    b.vram_gate = gate
    # Nothing may reach a real server; the gate is what these arms measure.
    monkeypatch.setattr(b, "_load_sync", lambda key: None)
    monkeypatch.setattr(b, "_apply_sync", lambda key, c: None)
    return b


@pytest.mark.asyncio
async def test_a_DIRECT_backend_load_is_gated(monkeypatch) -> None:
    """🔴 THE ONE THE FIRST CUT MISSED. `plugins/model_switch.py:216` calls
    `backend.load(target)` for `/model <name>`, the picker AND the rpc
    `set_model` — the most common swap in the app. Gating the pre-turn path in
    app.py left every one of them uncovered."""
    gate = _Gate()
    b = _llama(monkeypatch, gate)
    await b.load("qwen/a")
    assert gate.asked == ["qwen/a"]


@pytest.mark.asyncio
async def test_apply_load_settings_is_gated_because_a_RELOAD_IS_A_LOAD(monkeypatch) -> None:
    """`model_switch.py:835/854`. A ctx change puts the weights back with a new
    window — the first cut called that a settings change, and it was wrong."""
    gate = _Gate()
    b = _llama(monkeypatch, gate)
    await b.apply_load_settings("qwen/a", {"ctx": 8192})
    assert gate.asked == ["qwen/a"]


@pytest.mark.asyncio
async def test_a_refused_load_RAISES_and_does_not_touch_the_server(monkeypatch) -> None:
    """Cancel must stop the load, not merely colour it. The refusal is a
    BackendError subclass, so every existing caller already renders it as plain
    words instead of a traceback."""
    gate = _Gate(allow=False)
    b = _llama(monkeypatch, gate)
    loaded: list[str] = []
    monkeypatch.setattr(b, "_load_sync", lambda key: loaded.append(key))

    with pytest.raises(llm_backend.VramRefused):
        await b.load("qwen/a")
    assert loaded == [], "the server was touched after a refusal"
    assert isinstance(llm_backend.VramRefused("x"), llm_backend.BackendError)


@pytest.mark.asyncio
async def test_one_user_action_asks_ONCE_even_though_the_entries_nest(monkeypatch) -> None:
    """`LlamaCppBackend.load(ctx=...)` delegates to `apply_load_settings`, and
    LM Studio's `apply_load_settings` delegates to `load`. Guarding both without
    reentrancy would ask the same question twice for one click — which is how a
    modal stops being read."""
    gate = _Gate()
    b = _llama(monkeypatch, gate)
    await b.load("qwen/a", ctx=8192)
    assert gate.asked == ["qwen/a"], f"asked {len(gate.asked)} times for one load"


@pytest.mark.asyncio
async def test_no_gate_installed_means_no_prompt_and_no_crash(monkeypatch) -> None:
    """Every existing test, and any embedder, constructs a backend without a
    gate. That must behave exactly as it did before this card."""
    b = _llama(monkeypatch, None)
    await b.load("qwen/a")
    await b.apply_load_settings("qwen/a", {"ctx": 4096})


def test_the_FACTORY_stamps_the_gate_so_a_backend_switch_cannot_drop_it(monkeypatch) -> None:
    """🔴 FOUR ASSIGNMENT SITES, THREE OF THEM IN A PLUGIN. `app.backend =
    make_backend(...)` happens at boot and three more times in
    `plugins/model_switch.py`. Installing the hook at the App's own assignment
    would mean a `/backend` switch silently dropped it — the same "enforced at
    the callers" mistake, one layer up."""
    from litetui import settings as st

    gate = _Gate()
    monkeypatch.setattr(llm_backend, "_DEFAULT_VRAM_GATE", gate)
    cfg = st.Settings()
    cfg.backend = "llamacpp"
    assert llm_backend.make_backend(cfg).vram_gate is gate
    cfg.backend = "lmstudio"
    assert llm_backend.make_backend(cfg).vram_gate is gate


def test_EVERY_public_load_entry_point_GOES_THROUGH_THE_GUARD() -> None:
    """🔴 DERIVED FROM THE SOURCE, NOT FROM A LIST I WROTE.

    The defect this fixes was a gate applied to the call sites I happened to
    grep — and my grep was `src/litetui/*.py`, which does not include
    `plugins/`. A list cannot contain the entry point somebody adds next month,
    so this reads the class bodies and insists each one either takes the guard
    or delegates to something that does.
    """
    import inspect

    checked = []
    for cls in (llm_backend.LlamaCppBackend, llm_backend.LMStudioBackend):
        for name in ("load", "apply_load_settings"):
            src = inspect.getsource(getattr(cls, name))
            guarded = "vram_guard" in src
            # Delegation counts ONLY when the body does nothing else that
            # loads. LM Studio's `apply_load_settings` is pure delegation; a
            # function that BOTH delegates and has its own to_thread path is
            # covered on one branch and open on the other.
            #
            # ⚠️ THIS CLAUSE USED TO BE `delegates = "await self.load(" in src`
            # ALONE, AND IT MADE THIS ARM BLIND TO ITS OWN SUBJECT. Deleting
            # the guard from `LlamaCppBackend.load` left this GREEN, because
            # that function still delegates on its `ctx is not None` branch
            # while its other branch went straight to `_load_sync`. Two sibling
            # arms went red and this one — the one that exists to catch a
            # future entry point — did not. An escape hatch that is not scoped
            # to the whole body is an escape hatch for the whole body.
            delegates = ("await self.load(" in src or "await self.apply_load_settings(" in src)
            own_load = ("to_thread" in src or "_load_sync" in src or "_apply_sync" in src)
            assert guarded or (delegates and not own_load), (
                f"{cls.__name__}.{name} loads weights without passing a VRAM gate "
                f"— add `async with self.vram_guard(key):` or delegate to one that does"
            )
            checked.append(f"{cls.__name__}.{name}")

    # The validity gate: an empty sweep would satisfy the loop above.
    assert len(checked) == 4, checked
