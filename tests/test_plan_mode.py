"""Plan mode — T558 part 1. Ryan, 2026-09-10 03:2x:

    "we need to add a plan mode to litetui ... basically tells the model when
    running in litetui to use its plan w quizmaster skill and askuser tool"

🔴 THE INSTRUCTION IS A PROMPT SECTION, NOT AN APPENDED MESSAGE, and that is
the whole design. A message appended when the mode is entered is still sitting
in the conversation after the mode is left, telling the model to refuse to build
long after the user asked it to — and nothing on screen would say why. A gated
section composes out, so LEAVING THE MODE ACTUALLY LEAVES IT. The arms below
prove the leaving, not just the entering: the composition must return
byte-for-byte to what it was.

⬜ THE SKILL IS NOT INSTALLED OR COPIED, and the card's premise that it needed to
be was FALSE when measured. `skills.DEFAULT_EXTRA_ROOTS` already globs
`~/.claude/plugins/cache/liteharness/liteharness/*/skills`, so
`ls-plan-w-quizmaster` is discovered there and already rides in the names-only
index every turn. Copying it to a literal path would duplicate a working
mechanism AND reintroduce the staleness the glob exists to prevent — that
module's own docblock records two path constants rotting exactly that way on
2026-08-20. Plan mode therefore only has to TELL the model to load it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import paths  # noqa: E402
from litetui import plugins as plugins_mod  # noqa: E402
from litetui import skills as skills_mod  # noqa: E402


def make_app(plan: bool = False):
    a = m.LiteTUI(plan_mode=plan)
    a.convo_dir = None
    a.skills = []
    return a


# ── the mode changes the prompt, and leaving it changes it back ────────────

def test_plan_mode_adds_the_instruction_and_leaving_removes_it():
    off = make_app(plan=False)
    baseline = off._system_prompt_text()

    on = make_app(plan=True)
    assert on._system_prompt_text() != baseline, (
        "plan mode changed nothing — the section is not reaching the prompt, "
        "and every other arm in this file would pass against a dead feature"
    )

    # And back: not merely "different again", but IDENTICAL to before. An
    # instruction that half-leaves is the failure this design exists to avoid.
    on._plan_mode = False
    assert on._system_prompt_text() == baseline


def test_the_instruction_is_the_file_on_disk():
    a = make_app(plan=True)
    text = a._system_prompt_text()
    authored = paths.PLAN_PROMPT_FILE.read_text(encoding="utf-8").strip()
    assert authored in text
    # The three things Ryan named, so a rewrite of the prompt file cannot
    # quietly drop one of them.
    assert "ls-plan-w-quizmaster" in authored
    assert "ask_user_question" in authored


def test_the_section_sits_between_the_base_prompt_and_the_store():
    """Order is model behaviour. PLAN must land after BASE and before MEMORY."""
    order = plugins_mod.PROMPT_ORDER
    assert order["BASE"] < order["PLAN"] < order["MEMORY"]
    # And the slot table's own rule: no two sections share an integer.
    assert len(set(order.values())) == len(order)


def test_a_missing_prompt_file_disables_the_section_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(paths, "PLAN_PROMPT_FILE", Path("no-such-plan-file.md"))
    a = make_app(plan=True)
    # The mode is on and the file is gone: the composition must still build.
    # (The startup notice says so out loud; this arm is that it does not raise.)
    assert isinstance(a._system_prompt_text(), str)


# ── the toggle ─────────────────────────────────────────────────────────────

def test_the_toggle_flips_the_mode_and_rewrites_the_live_system_message():
    a = make_app(plan=False)
    a._system = lambda *args, **kwargs: None
    a._update_header = lambda: None
    edits: list[tuple[int, str]] = []
    a._edit = lambda i, why: edits.append((i, why))
    a.conversation = [{"role": "system", "content": a._system_prompt_text()}]
    before = a.conversation[0]["content"]

    a.action_toggle_plan_mode()
    assert a._plan_mode is True
    # The message already in the conversation is REWRITTEN, not appended to.
    assert a.conversation[0]["content"] != before
    assert len(a.conversation) == 1
    assert edits == [(0, "plan mode toggled")]

    a.action_toggle_plan_mode()
    assert a._plan_mode is False
    assert a.conversation[0]["content"] == before


def test_the_key_is_bound_at_app_level():
    keys = {b.key: b.action for b in m.LiteTUI.BINDINGS if hasattr(b, "key")}
    assert keys.get("ctrl+p") == "toggle_plan_mode"


# ── the mode is visible ────────────────────────────────────────────────────

def test_the_footer_names_the_mode_only_while_it_is_on():
    a = make_app(plan=True)
    assert "plan" in a.ctx_label_text.plain

    a._plan_mode = False
    assert "plan" not in a.ctx_label_text.plain


# ── what the card asked for that was already true ──────────────────────────

def test_the_quizmaster_skill_is_already_discoverable_without_being_installed():
    """The card said LiteTUI's catalog does NOT carry ls-plan-w-quizmaster and
    that plan mode would have to copy or link it in. Measured otherwise: the
    plugin-cache glob is a DEFAULT root, so it is found without any install
    step. This arm is what keeps someone from "fixing" that by adding one.
    """
    roots = skills_mod.DEFAULT_EXTRA_ROOTS
    globbed_plugin_roots = [r for r in roots if "plugins" in r and "*" in r]
    assert globbed_plugin_roots, (
        "no globbed plugin-cache root — either the marketplace library is no "
        "longer mounted (plan mode then names a skill the model cannot load), "
        "or a literal version path replaced the glob and will go stale on the "
        "next plugin update, quietly"
    )
    assert any("liteharness" in r for r in globbed_plugin_roots), (
        "the liteharness plugin library is where ls-plan-w-quizmaster lives"
    )
