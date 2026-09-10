"""System-prompt composition — the characterization gate for the P3 refactor.

Prompt-section order is MODEL BEHAVIOR, not code shape: the concatenation
decides what the model reads every turn. The reference below is the
pre-refactor fold, copied verbatim; the app's composition must match it
byte-for-byte under every gate state. If the registry refactor reorders,
double-strips, or drops a section under any combination, this fails.
"""
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import paths  # noqa: E402
from litetui import plugins as plugins_mod
from litetui import skills as skills_mod  # noqa: E402


def _reference(a) -> str:
    # The pre-refactor _system_prompt_text, verbatim. The CONTRACT.
    base = ""
    if paths.SYSTEM_PROMPT_FILE.exists():
        authored = paths.SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        configured = os.environ.get("CLAUDE_SKILL_DIR", "").strip()
        skill_dir = (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".claude" / "plugins" / "cache"
        )
        # Intentionally independent from prompt_compiler.compile_prompt. This
        # is the reference side of the characterization gate, not a second
        # invocation of the implementation it is meant to check.
        base = authored.replace("<root>", str(paths.ROOT)).replace(
            "${CLAUDE_SKILL_DIR}", str(skill_dir)
        )
        unresolved = re.findall(
            r"<[A-Za-z_][A-Za-z0-9_]*>|\$\{[A-Za-z_][A-Za-z0-9_]*\}",
            base,
        )
        if unresolved:
            raise ValueError(f"reference found unresolved placeholders: {unresolved}")
        base = base.strip()
    # T558: plan mode rides slot 5, between BASE and MEMORY. Built here
    # INDEPENDENTLY of the app, like every other arm of this reference.
    if getattr(a, "_plan_mode", False) and paths.PLAN_PROMPT_FILE.exists():
        base = (
            base + "\n" + paths.PLAN_PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n"
        ).strip()
    if a.convo_dir is not None:
        base = (base + m.memory_prompt(a.convo_id, a.convo_dir)).strip()
    if a.tools_enabled:
        # Was m.TOOLS_PROMPT, a constant in app.py, until the prompt text moved
        # to prompts/tools.md (2026-08-22). The reference still builds the fold
        # INDEPENDENTLY of the app -- it just reads the same source of truth.
        base = (
            base + "\n" + paths.TOOLS_PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n"
        ).strip()
        # 2026-09-10: deferred tools (tool_search plugin) ride between the tools
        # prompt and the skills index as a names-only list — slot 25.
        deferred = a.plugins.deferred_specs()
        if deferred:
            base = (base + plugins_mod.deferred_tools_block(deferred)).strip()
        if a.skills:
            base = (base + skills_mod.index_block(a.skills)).strip()
    else:
        # T073: tools OFF no longer means NO tools section. The schemas are
        # still advertised to the model (so it takes the structured path and
        # never types tool markup as prose), so the prompt must say they are
        # refused and who can turn them on — shipping silence here is the
        # "offered with no instructions" state this file's fold exists to
        # notice. The skills index stays off with the tools, as before.
        base = (base + m.TOOLS_DISABLED_PROMPT).strip()
    return base


def _fake_skills():
    # index_block only reads name/description; build the real dataclass so a
    # field rename breaks THIS test rather than silently diverging.
    return [
        skills_mod.Skill(
            name="probe-skill",
            description="a probe",
            path=Path("probe/SKILL.md"),
            source="probe",
        )
    ]


@pytest.mark.parametrize("tools_on", [True, False])
@pytest.mark.parametrize("with_skills", [True, False])
@pytest.mark.parametrize("with_store", [True, False])
@pytest.mark.parametrize("plan_on", [True, False])
def test_composition_matches_the_reference_fold(
    tools_on, with_skills, with_store, plan_on, tmp_path
):
    a = m.LiteTUI(plan_mode=plan_on)
    a.tools_enabled = tools_on
    a.skills = _fake_skills() if with_skills else []
    if with_store:
        a.convo_dir = tmp_path  # memory_prompt renders against an empty store
        a.convo_id = "probe-convo"
    else:
        a.convo_dir = None
    assert a._system_prompt_text() == _reference(a)


def test_the_gate_can_fail():
    # An equality gate whose operands could never differ proves nothing:
    # perturb one side's input and demand a mismatch.
    a = m.LiteTUI()
    a.tools_enabled = True
    a.skills = _fake_skills()
    a.convo_dir = None
    real = a._system_prompt_text()
    a.skills = []
    assert a._system_prompt_text() != real, (
        "removing the skills index changed nothing — the gate is comparing "
        "a constant to itself"
    )


def test_unknown_placeholder_fails_the_composed_prompt_gate(tmp_path, monkeypatch):
    authored = tmp_path / "systemprompt.md"
    authored.write_text("bad=${UNKNOWN_PROMPT_PATH}", encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_PROMPT_FILE", authored)
    with pytest.raises(ValueError, match="UNKNOWN_PROMPT_PATH"):
        m.LiteTUI()
