"""Skills — the `skill` tool plus the system-prompt index section.

Discovery stays a host-owned line in __init__ (per-plugin isolation would
swallow a genuinely broken checkout into a silent half-boot); this module
owns the two ways discovered skills reach the model: the loader tool and
the index that rides the system prompt.
"""
import skills as skills_mod
from plugins import PROMPT_ORDER, PluginManifest


def _register(ctx) -> None:
    app = ctx.app
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
