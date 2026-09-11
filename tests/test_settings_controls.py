"""T544 — every Settings field must have a control, or the screen cannot save.

`_collect` refuses to write a partial Settings object: a field with no widget
goes on a `missing` list and the save raises "no control found for: ...". That
guard is right, and it means ADDING A FIELD TO settings.py WITHOUT A CONTROL
BREAKS SAVING FOR EVERY SETTING, not just the new one.

That is exactly what happened. Ryan's screenshot, 2026-09-08 23:3x:

    Cannot save — no control found for: lmstudio_graded_thinking_models,
    tool_auto_background_s, subagent_model — refusing to save a partial
    settings object

Three fields, from T517, T538 and T539-A, landed over three days and nobody
could save settings until all three were noticed at once. The guard reported
it faithfully the whole time; nothing ASKED the guard until a human opened the
dialog.

This gate asks. It reads the control declarations out of the source rather
than running Textual, so it costs nothing and cannot be defeated by a pane
that mounts lazily.

🔴 THE EXEMPT SET IS PARSED FROM `_collect`, NEVER RETYPED HERE. A test that
keeps its own copy of the implementation's list agrees with it right up until
someone edits one of them, and then both are precise and only one is right.
"""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from litetui.settings import Settings

SCREEN = Path(__file__).resolve().parents[1] / "src" / "litetui" / "settings_screen.py"
# T640 added `_model_pick_row`. THIS SET IS THE AUDIT'S ONLY EYES: a builder
# missing from it makes every field it builds read as UNCONTROLLED, and the
# audit then reports the T544 regression against working code. The `id=` scan
# below cannot cover the gap — a helper spells its id `f"f-{name}"`, an
# f-string, and that branch only sees a literal Constant.
ROW_HELPERS = {"_text_row", "_switch_row", "_select_row", "_model_pick_row"}


def _tree() -> ast.Module:
    return ast.parse(SCREEN.read_text(encoding="utf-8"))


def _exempt(tree: ast.Module) -> set[str]:
    """The names `_collect` deliberately skips, read out of its own `if name in (...)`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_collect":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Compare)
                    and isinstance(inner.left, ast.Name)
                    and inner.left.id == "name"
                    and inner.ops
                    and isinstance(inner.ops[0], ast.In)
                    and isinstance(inner.comparators[0], ast.Tuple)
                ):
                    return {
                        e.value
                        for e in inner.comparators[0].elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
    raise AssertionError("could not find the exempt tuple in _collect — did it move?")


def _controlled(tree: ast.Module) -> set[str]:
    """Field names that have a widget: a row helper, or a literal id="f-<name>"."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ROW_HELPERS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
        # Hand-built controls (Default model is a Select written out in full).
        for kw in getattr(node, "keywords", []):
            if (
                kw.arg == "id"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and kw.value.value.startswith("f-")
            ):
                found.add(kw.value.value[2:])
    return found


def test_every_settings_field_has_a_control_or_is_exempt():
    tree = _tree()
    controlled = _controlled(tree)
    exempt = _exempt(tree)
    missing = sorted(
        f.name for f in fields(Settings) if f.name not in controlled and f.name not in exempt
    )
    assert not missing, (
        "these Settings fields have no control, so the settings screen cannot save AT ALL: "
        + ", ".join(missing)
        + " — add a row in settings_screen.py, or add the name to _collect's exempt tuple "
          "with a comment saying which editor owns it instead."
    )


def test_the_three_fields_from_ryans_screenshot_are_controlled():
    """The regression itself, named. These are the fields the dialog reported."""
    controlled = _controlled(_tree())
    for name in ("lmstudio_graded_thinking_models", "tool_auto_background_s", "subagent_model"):
        assert name in controlled, f"{name} has no control — the T544 regression is back"


def test_exempt_names_are_real_settings_fields():
    """An exempt name that no longer exists is a stale exemption, and a stale
    exemption is how a REAL field gets silently skipped later if it is renamed
    into that slot."""
    known = {f.name for f in fields(Settings)}
    stale = sorted(n for n in _exempt(_tree()) if n not in known)
    assert not stale, f"exempt names that are not Settings fields any more: {', '.join(stale)}"
