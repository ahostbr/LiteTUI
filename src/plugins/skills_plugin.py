"""Skills — the `skill` tool plus the system-prompt index section.

Discovery stays a host-owned line in __init__ (per-plugin isolation would
swallow a genuinely broken checkout into a silent half-boot); this module
owns the two ways discovered skills reach the model: the loader tool and
the index that rides the system prompt.
"""
import skills as skills_mod
from picker import PickerScreen
import paths
from plugins import PROMPT_ORDER, PluginManifest


#: Picker row id for "show me the old text report". Not a skill name, and
#: deliberately unmatchable as one.
_REPORT_ROW = "\u2500report\u2500"


def _cmd_skills(app, name: str, arg: str) -> None:
    # Discovery is silent by design: a directory with no SKILL.md is a
    # scratch folder, not an error. That makes a MISNAMED or MISPLACED
    # skill look exactly like one that was never written — so say what
    # was found, where it was looked for, and what the model can see.
    base = paths.ROOT / skills_mod.SKILLS_DIR_NAME
    if arg:
        body = skills_mod.load(app.skills, arg)
        # Show what the MODEL would receive, not a summary of it.
        app._system(f"[skill {arg!r} — {len(body):,} chars as the model sees it]\n\n{body}")
        return
    if not app.settings.skills_enabled:
        app._system(
            "Skills are OFF in /settings, so none were discovered and the "
            "`skill` tool is not offered to the model."
        )
        return
    if not app.skills:
        # Nothing to pick. The report is the useful answer here: it says where
        # it looked, which is the whole question when the list is empty.
        app._system(_skills_report(app, base))
        return

    # A picker, not eighty lines of transcript. The full report stays one
    # keystroke away as the picker's own first row, so nothing is lost --
    # it is just no longer the default way to answer "what skills do I have".
    skipped = _skipped_lines(app, base)
    if skipped:
        # A folder that produced no skill is the one thing a picker cannot
        # show, because it has no row. Say it out loud, briefly.
        app._system("\n".join(x for x in skipped if x.strip()))

    rows = [(_REPORT_ROW, "Full report  —  roots, libraries, token cost, skipped folders")]
    for s in sorted(app.skills, key=lambda s: s.name.lower()):
        desc = (s.description or "(no description)").replace("\n", " ")
        rows.append((s.name, f"{s.name}  [{s.source}]  —  {desc[:110]}"))

    def _picked(choice: str | None) -> None:
        if not choice:
            return
        if choice == _REPORT_ROW:
            app._system(_skills_report(app, base))
            return
        body = skills_mod.load(app.skills, choice)
        app._system(f"[skill {choice!r} — {len(body):,} chars as the model sees it]\n\n{body}")

    app.push_screen(
        PickerScreen(
            f"Skills ({len(app.skills)})",
            rows,
            hint="↑↓ move · Enter or click to read one · Esc to cancel",
        ),
        _picked,
    )


def _skipped_lines(app, base) -> list[str]:
    """Local folders that produced no skill, each with the REAL reason.

    This used to report every one of them as "no SKILL.md inside" — inferred
    from `name not in loaded` and never checked. A folder created after the app
    booted has a SKILL.md and is simply not loaded yet, so the message named a
    cause that was not merely unverified but false, and sent the reader off to
    write a file that was already there.

    Discovery happens once at startup, which is the actual answer in that case,
    and it is the one thing the old message could never say.
    """
    if not base.is_dir():
        return []
    loaded = {s.path.parent.name for s in app.skills}
    unloaded = [d for d in sorted(base.iterdir()) if d.is_dir() and d.name not in loaded]
    if not unloaded:
        return []

    missing = [d for d in unloaded if not (d / "SKILL.md").is_file()]
    present = [d for d in unloaded if (d / "SKILL.md").is_file()]

    lines: list[str] = [""]
    if present:
        lines.append(
            f"  {len(present)} local folder(s) have a SKILL.md but are NOT loaded — "
            "skills are discovered at startup, so restart to pick them up:"
        )
        lines.extend(f"    {d.name}/" for d in present)
    if missing:
        if present:
            lines.append("")
        lines.append(f"  {len(missing)} local folder(s) skipped — no SKILL.md inside:")
        lines.extend(f"    {d.name}/" for d in missing)
    return lines


def _skills_report(app, base) -> str:
    """The full /skills listing: sources, roots, per-library groups, and
    what got passed over — everything _cmd_skills shows once gating clears."""
    roots = skills_mod.resolve_roots(app.settings.skill_roots)
    missing = skills_mod.unresolved_roots(app.settings.skill_roots)
    block = skills_mod.index_block(app.skills)

    by_source: dict[str, list] = {}
    for s in app.skills:
        by_source.setdefault(s.source, []).append(s)

    lines = [
        f"{len(app.skills)} skill(s) from {len(by_source)} librar"
        f"{'y' if len(by_source) == 1 else 'ies'} — "
        f"{len(block):,} chars (~{len(block) // 4:,} tokens) in the system prompt"
    ]
    lines.append("")
    lines.append(f"  [local] {base}" + ("" if base.is_dir() else "  (does not exist)"))
    for d in roots:
        lines.append(f"  [{skills_mod._label_for(d)}] {d}")
    if missing:
        # A library that resolved to nothing is silent everywhere else.
        lines.append("")
        lines.append(f"  {len(missing)} configured root(s) matched NOTHING:")
        for m in missing:
            lines.append(f"    {m}")

    for src, group in by_source.items():
        lines.append("")
        lines.append(f"  ── {src} ({len(group)}) " + "─" * max(0, 46 - len(src)))
        for s in group:
            lines.append(f"  {s.name}  —  {s.description or '(no description)'}")

    lines.extend(_skipped_lines(app, base))

    lines.append("")
    lines.append(
        "  the model sees only name + description; it calls the `skill` "
        "tool to read a body. /skills <name> shows what it would get."
    )
    return "\n".join(lines)


def _register(ctx) -> None:
    app = ctx.app
    ctx.command(
        ("/skills", "/skill"), _cmd_skills,
        palette="Skills",
        help="Extra abilities it can use, and where they came from.",
        group="tools",
        order=20,
    )
    ctx.tool(
        skills_mod.SKILL_TOOL_SPEC,
        lambda args: skills_mod.load(app.skills, args.get("name", "")),
        gate=lambda: bool(app.skills),
    )
    # Pointers only; bodies load through the `skill` tool. Gated on
    # tools_enabled because without that tool the index would advertise
    # something the model has no way to open.
    ctx.prompt_section(
        PROMPT_ORDER["SKILLS_INDEX"],
        lambda: skills_mod.index_block(app.skills),
        enabled=lambda: app.tools_enabled and bool(app.skills),
    )


PLUGIN = PluginManifest(id="skills", register=_register)
