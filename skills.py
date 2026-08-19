"""Skills — `skills/<name>/SKILL.md`, discovered from the repo root.

Same shape Claude Code uses: a directory per skill, a `SKILL.md` with YAML
frontmatter carrying `name` and `description`. The INDEX (name + one line) goes
into the system prompt; the BODY is loaded on demand by the `skill` tool.

That split is the whole design. Inlining every body would spend the context
window on instructions the model does not need this turn, and the index is what
lets it know the skill exists at all — a capability with no pointer is
indistinguishable from an absent one.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR_NAME = "skills"
MAX_SKILL_BYTES = 60_000
#: How much of a description survives into the index. A skill that writes an
#: essay here would otherwise crowd out every other skill's pointer.
MAX_DESC_CHARS = 200


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter, body). Tolerates a missing or malformed block.

    Deliberately not a YAML dependency: the frontmatter this reads is flat
    `key: value`, and a parser that can fail is a parser that can make a valid
    skill invisible. Unknown/complex keys are ignored rather than fatal.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end]
    body = text[end + 4 :].lstrip("\n")
    fm: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body


class Skill:
    __slots__ = ("name", "description", "path")

    def __init__(self, name: str, description: str, path: Path):
        self.name = name
        self.description = description
        self.path = path


def discover(root: Path) -> list[Skill]:
    """Every `skills/*/SKILL.md` under root, sorted by name.

    A directory without a SKILL.md is skipped silently — that is a scratch
    folder, not a broken skill. An UNREADABLE SKILL.md is NOT skipped silently;
    it becomes a skill whose description says so, because a skill that vanishes
    on a decode error looks exactly like one that was never written.
    """
    base = root / SKILLS_DIR_NAME
    if not base.is_dir():
        return []
    out: list[Skill] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out.append(Skill(d.name, f"[unreadable: {e.__class__.__name__}]", f))
            continue
        fm, _ = _parse_frontmatter(text)
        name = (fm.get("name") or d.name).strip() or d.name
        desc = (fm.get("description") or "").strip()
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[: MAX_DESC_CHARS - 1].rstrip() + "…"
        out.append(Skill(name, desc, f))
    return out


def index_block(skills: list[Skill]) -> str:
    """The pointer list that rides in the system prompt. Empty when none."""
    if not skills:
        return ""
    lines = [
        "",
        "## Skills",
        "",
        f"{len(skills)} skill(s) are available. These are INDEX LINES ONLY — call the",
        "`skill` tool with the name to load the full instructions before acting on one.",
        "",
    ]
    for s in skills:
        lines.append(f"- `{s.name}` — {s.description}" if s.description else f"- `{s.name}`")
    return "\n".join(lines) + "\n"


def load(skills: list[Skill], name: str) -> str:
    """Full SKILL.md body for `name`, or a message naming what IS available.

    An unknown name returns the candidate list rather than a bare error: the
    model's next move after a miss is always "what else is there?", and making
    it guess again is the expensive path.
    """
    want = (name or "").strip().lower()
    if not want:
        return "[error] skill: a name is required"
    for s in skills:
        if s.name.lower() == want or s.path.parent.name.lower() == want:
            try:
                text = s.path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return f"[error] skill {s.name!r} could not be read: {e}"
            if len(text) > MAX_SKILL_BYTES:
                text = text[:MAX_SKILL_BYTES] + "\n\n[truncated]"
            return text
    have = ", ".join(s.name for s in skills) or "(none)"
    return f"[error] no skill named {name!r}. Available: {have}"


SKILL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Load the full instructions for a named skill. The system prompt lists "
            "only each skill's name and one-line description; call this to read the "
            "actual procedure before following it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the Skills list"}
            },
            "required": ["name"],
        },
    },
}
