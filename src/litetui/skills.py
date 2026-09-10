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

import json
import tempfile
import time
from litetui import tool_schemas

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
    """Every `<name>/SKILL.md` directly under `d`.

    A directory without a SKILL.md is skipped silently — that is a scratch
    folder, not a broken skill. An UNREADABLE SKILL.md is NOT skipped silently;
    it becomes a skill whose description says so, because a skill that vanishes
    on a decode error looks exactly like one that was never written.
    """
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
    """The repo's own `skills/*/SKILL.md`, as the "local" library.

    A thin delegation: the walk, the frontmatter parse, and the
    unreadable-skill doctrine live ONCE, in discover_dir — this function
    and it used to be structurally identical bodies kept in sync by hand.
    """
    return discover_dir(root / SKILLS_DIR_NAME, "local")


#: The discovered index, cached beside the skills it describes.
INDEX_CACHE_NAME = "index.json"


def cache_path(root: Path) -> Path:
    """Where the index cache lives: <root>/skills/index.json."""
    return root / SKILLS_DIR_NAME / INDEX_CACHE_NAME


def write_cache(root: Path, skills: list[Skill]) -> Path | None:
    """Persist the index. Best-effort: a cache that cannot be written must
    never stop the app from having skills -- discovery already succeeded."""
    p = cache_path(root)
    payload = {
        "generated": time.time(),
        "count": len(skills),
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "path": str(s.path),
                "source": s.source,
            }
            for s in skills
        ],
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a torn cache is worse than no cache, because a
        # half-written one still parses often enough to be believed.
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".idx-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, p)
        return p
    except OSError:
        return None


def read_cache(root: Path) -> tuple[list[Skill], float] | None:
    """Load the cached index, or None when there is nothing usable.

    An entry whose SKILL.md has since been deleted is DROPPED rather than
    returned: load() would fail on it later with a file error, which reads as
    a broken skill instead of a stale cache. The count difference is what
    /skills reports, so a shrinking library is visible rather than silent.
    """
    p = cache_path(root)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = raw.get("skills")
    if not isinstance(rows, list):
        return None
    out: list[Skill] = []
    for r in rows:
        try:
            sp = Path(r["path"])
        except (KeyError, TypeError):
            continue
        if not sp.is_file():
            continue
        out.append(Skill(str(r.get("name") or ""), str(r.get("description") or ""),
                         sp, str(r.get("source") or "local")))
    if not out:
        return None
    generated = raw.get("generated")
    return out, float(generated) if isinstance(generated, (int, float)) else 0.0


def index_block(skills: list[Skill]) -> str:
    """The NAMES-ONLY index that rides in the system prompt. Empty when none.

    Names only (Ryan 2026-09-10 12:5x: "send index only of the skills for sure if
    its that much thats nutz"): with the plugin libraries mounted, the old
    name-plus-description form was ~19,400 chars ≈ 4,800 tokens on EVERY turn.
    Descriptions come back on demand through `skill` with `find`.
    """
    if not skills:
        return ""
    by_source: dict[str, list[str]] = {}
    for s in skills:
        by_source.setdefault(s.source, []).append(s.name)
    lines = [
        "",
        "## Skills",
        "",
        f"{len(skills)} skill(s) are available — NAMES ONLY. Call `skill` with `find: <keywords>`",
        "to see what the matching skills do, and `skill` with `name` to load one's full",
        "instructions before acting on it.",
        "",
    ]
    for source, names in by_source.items():
        lines.append(f"- {source}: {', '.join(names)}")
    return "\n".join(lines) + "\n"


def find(skills: list[Skill], query: str) -> str:
    """Skills whose name or description carries the query's words, ranked by
    how many words hit — the descriptions the index no longer carries."""
    words = "".join(c if c.isalnum() else " " for c in (query or "").lower()).split()
    if not words:
        return "[error] skill find: keywords are required"
    scored = []
    for s in skills:
        hay = (s.name + " " + s.description).lower()
        n = sum(1 for w in words if w in hay)
        if n:
            scored.append((-n, s.name, s))
    scored.sort(key=lambda t: (t[0], t[1]))
    if not scored:
        return f"no skill matches {query!r}. Names: " + (", ".join(s.name for s in skills) or "(none)")
    return "\n".join(f"- `{s.name}` ({s.source}) — {s.description}" for _, _, s in scored[:12])


def load(skills: list[Skill], name: str) -> str:
    """Full SKILL.md body for `name`, or a message naming what IS available.

    An unknown name returns the candidate list rather than a bare error: the
    model's next move after a miss is always "what else is there?", and making
    it guess again is the expensive path.
    """
    # A leading slash is how every skill is WRITTEN and spoken -- /ls-mark,
    # /library, /arch -- so it is what a user and a model both type. Without
    # this, "/ls-mark" failed against a list that visibly contained "ls-mark",
    # which reads as the skill being missing rather than the name being
    # mispunctuated. The command surface and the tool surface both land here.
    want = (name or "").strip().lower().lstrip("/")
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


SKILL_TOOL_SPEC = tool_schemas.load("skill")
