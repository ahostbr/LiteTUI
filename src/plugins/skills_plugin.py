"""Skills — the `skill` tool plus the system-prompt index section.

Discovery stays a host-owned line in __init__ (per-plugin isolation would
swallow a genuinely broken checkout into a silent half-boot); this module
owns the two ways discovered skills reach the model: the loader tool and
the index that rides the system prompt.
"""
import skills as skills_mod
import paths
from plugins import PROMPT_ORDER, PluginManifest


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
    app._system(_skills_report(app, base))


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

    # A folder without a SKILL.md is a scratch folder, not an error —
    # but a MISNAMED one looks identical, so name what was passed over.
    if base.is_dir():
        loaded = {s.path.parent.name for s in app.skills}
        skipped = [d.name for d in sorted(base.iterdir())
                   if d.is_dir() and d.name not in loaded]
        if skipped:
            lines.append("")
            lines.append(f"  {len(skipped)} local folder(s) skipped — no SKILL.md inside:")
            for d in skipped:
                lines.append(f"    {d}/")

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
        help="List discovered skills and their sources (/skills)",
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
