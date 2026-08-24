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
    """The global Claude plugin cache, overridable by an explicit environment."""
    configured = os.environ.get("CLAUDE_SKILL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "plugins" / "cache"


def compile_prompt(
    authored: str,
    *,
    root: Path,
    skill_dir: Path | None = None,
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
    unresolved = sorted(
        {match.group(1) or match.group(2) for match in _UNRESOLVED.finditer(compiled)}
    )
    if unresolved:
        names = ", ".join(unresolved)
        raise PromptPlaceholderError(f"unresolved system-prompt placeholder(s): {names}")
    return compiled


def compile_prompt_file(
    path: Path,
    *,
    root: Path,
    skill_dir: Path | None = None,
) -> str:
    authored = Path(path).read_text(encoding="utf-8")
    return compile_prompt(authored, root=root, skill_dir=skill_dir)
