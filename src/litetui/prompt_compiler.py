"""Compile authored system-prompt placeholders into concrete local paths."""
from __future__ import annotations

import os
import re
from pathlib import Path


class PromptPlaceholderError(ValueError):
    """An authored prompt contains a placeholder the host cannot resolve."""


_UNRESOLVED = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_]*)>|\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
)


def claude_skill_dir() -> Path:
    """The plugin skills directory that is ACTUALLY SCANNED, env-overridable.

    T582: this used to answer with the cache ROOT, two levels above any skill.
    The cache is versioned -- 1.0.15 and 1.0.16 sat side by side on 2026-09-10 --
    and discovery resolves that glob to its NEWEST match, so naming the root in
    the system prompt described a directory the loader never reads and invited
    the reader to go hunting through the versions by hand.

    Delegated to `skills.resolve_roots` rather than re-globbed here: "newest
    installed wins" is a rule that must have exactly one definition, and a
    second copy of it would agree today and drift later. Imported inside the
    function to keep this module free of a package-level cycle.
    """
    configured = os.environ.get("CLAUDE_SKILL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    cache = Path.home() / ".claude" / "plugins" / "cache"
    from litetui import skills as _skills

    for root in _skills.resolve_roots(_skills.DEFAULT_EXTRA_ROOTS):
        if cache in root.parents:
            return root
    return cache


def compile_prompt(
    authored: str,
    *,
    root: Path,
    skill_dir: Path | None = None,
    user_name: str = "",
) -> str:
    """Resolve known placeholders and reject every unknown one.

    Replacement is intentionally explicit rather than environment-wide. A
    misspelled name must stop prompt construction instead of reaching the model
    as plausible-looking instructions.
    """
    values = {
        "root": str(Path(root)),
        "CLAUDE_SKILL_DIR": str(skill_dir or claude_skill_dir()),
    }
    compiled = authored.replace("<root>", values["root"])
    compiled = compiled.replace("${CLAUDE_SKILL_DIR}", values["CLAUDE_SKILL_DIR"])
    # The name is data, not prompt syntax. Check authored placeholders before
    # substituting it so names such as "<Ada>" or "${HOME}" stay literal.
    without_name_slot = compiled.replace("${USER_NAME_CLAUSE}", "")
    unresolved = sorted(
        {match.group(1) or match.group(2) for match in _UNRESOLVED.finditer(without_name_slot)}
    )
    if unresolved:
        names = ", ".join(unresolved)
        raise PromptPlaceholderError(f"unresolved system-prompt placeholder(s): {names}")
    name = user_name.strip()
    clause = f" Your user's name is {name}." if name else ""
    return compiled.replace("${USER_NAME_CLAUSE}", clause)


def compile_prompt_file(
    path: Path,
    *,
    root: Path,
    skill_dir: Path | None = None,
    user_name: str = "",
) -> str:
    authored = Path(path).read_text(encoding="utf-8")
    return compile_prompt(
        authored, root=root, skill_dir=skill_dir, user_name=user_name
    )
