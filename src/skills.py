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

import glob
import os
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

    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        key, val = k.strip(), v.strip()

        # ── YAML block scalars: `key: >`, `>-`, `>+`, `|`, `|-`, `|+` ────
        #
        # The value is the INDENTED BLOCK THAT FOLLOWS, not the indicator.
        # Reading the indicator as the value gave 10 of 77 skills a
        # description of ">" or "|" — listed in the index and impossible to
        # choose from. Still no YAML dependency: this handles the one shape
        # that actually appears, and anything else falls through unchanged.
        if val in (">", ">-", ">+", "|", "|-", "|+"):
            fold = val.startswith(">")
            base = len(line) - len(line.lstrip())
            chunk: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= base:
                    break          # dedented: the block ended
                chunk.append(nxt.strip())
                i += 1
            while chunk and not chunk[-1]:
                chunk.pop()
            if fold:
                # Folded: blank lines are paragraph breaks, the rest joins.
                out, para = [], []
                for c in chunk:
                    if c:
                        para.append(c)
                    elif para:
                        out.append(" ".join(para))
                        para = []
                if para:
                    out.append(" ".join(para))
                fm[key] = "\n".join(out)
            else:
                fm[key] = "\n".join(chunk)
            continue

        fm[key] = val.strip('"').strip("'")
    return fm, body


class Skill:
    __slots__ = ("name", "description", "path", "source")

    def __init__(self, name: str, description: str, path: Path,
                 source: str = "local"):
        self.name = name
        self.description = description
        self.path = path
        #: Which root this came from. Shown by /skills and used to
        #: disambiguate a name that exists in two libraries.
        self.source = source


#: Extra skill libraries, scanned in order after the repo's own `skills/`.
#:
#: These are DIRECTORIES OF SKILLS (each holding `<name>/SKILL.md`), not repo
#: roots. `~` expands, and a `*` glob picks the NEWEST match — the plugin cache
#: is versioned, so a literal path here would silently go stale on the next
#: plugin update and the library would just get quieter. Two path constants
#: rotted exactly that way in this repo on 2026-08-20.
DEFAULT_EXTRA_ROOTS: tuple[str, ...] = (
    "~/.claude/skills",
    "~/.claude/plugins/cache/liteharness/liteharness/*/skills",
)


def _label_for(d: Path) -> str:
    """A short name for where a skill came from, for /skills and collisions."""
    parts = [p for p in d.parts if p not in ("/", "\\")]
    if "plugins" in parts:
        i = parts.index("plugins")
        for seg in parts[i + 1:]:
            if seg not in ("cache", "marketplaces"):
                return seg
    for seg in reversed(parts[:-1]):
        if seg not in ("skills",):
            return seg.lstrip(".") or seg
    return d.name


def resolve_roots(patterns) -> list[Path]:
    """Expand `~` and globs to existing directories, in order, deduped.

    A glob resolves to its NEWEST match (mtime), so a versioned plugin path
    follows the plugin instead of pinning one release.

    A pattern that matches nothing is skipped SILENTLY here and reported by
    /skills — an unreadable library and an unconfigured one look identical
    from the index, and only one of them is a mistake.
    """
    out: list[Path] = []
    for raw in patterns or ():
        pat = str(raw).strip()
        if not pat:
            continue
        pat = os.path.expanduser(pat)
        if any(ch in pat for ch in "*?["):
            hits = [Path(p) for p in glob.glob(pat) if Path(p).is_dir()]
            hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            cand = hits[:1]
        else:
            cand = [Path(pat)] if Path(pat).is_dir() else []
        for c in cand:
            c = c.resolve()
            if c not in out:
                out.append(c)
    return out


def unresolved_roots(patterns) -> list[str]:
    """Patterns that matched no directory. The counterpart to resolve_roots's
    deliberate silence — a typo'd library path must be visible SOMEWHERE."""
    bad: list[str] = []
    for raw in patterns or ():
        pat = str(raw).strip()
        if not pat:
            continue
        expanded = os.path.expanduser(pat)
        if any(ch in expanded for ch in "*?["):
            if not [p for p in glob.glob(expanded) if Path(p).is_dir()]:
                bad.append(pat)
        elif not Path(expanded).is_dir():
            bad.append(pat)
    return bad


def discover_dir(d: Path, source: str) -> list[Skill]:
    """Every `<name>/SKILL.md` directly under `d`."""
    if not d.is_dir():
        return []
    out: list[Skill] = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        f = sub / "SKILL.md"
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out.append(Skill(sub.name, f"[unreadable: {e.__class__.__name__}]", f, source))
            continue
        fm, _ = _parse_frontmatter(text)
        name = (fm.get("name") or sub.name).strip() or sub.name
        desc = (fm.get("description") or "").strip()
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[: MAX_DESC_CHARS - 1].rstrip() + "\u2026"
        out.append(Skill(name, desc, f, source))
    return out


def discover_all(root: Path, extra=None) -> list[Skill]:
    """The repo's own skills, then every extra library, deduped by NAME.

    🔴 A COLLISION IS RENAMED, NEVER DROPPED. `ls-skill-author` exists in both
    ~/.claude/skills and the liteharness catalog; keeping the first and
    discarding the second would make a real skill unreachable with nothing in
    the index to say so. The later one becomes `<name>@<source>` and stays
    loadable. Dropping is how a library gets quietly smaller than it looks.
    """
    got = discover(root)
    for d in resolve_roots(DEFAULT_EXTRA_ROOTS if extra is None else extra):
        got += discover_dir(d, _label_for(d))

    seen: dict[str, Skill] = {}
    out: list[Skill] = []
    for s in got:
        if s.name in seen:
            s.name = f"{s.name}@{s.source}"
        if s.name in seen:      # still colliding: two libraries, same label
            continue
        seen[s.name] = s
        out.append(s)
    return sorted(out, key=lambda s: s.name.lower())


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
