"""The plugin substrate: manifest, context, registry, loader.

Stays core forever — a loader cannot be its own plugin (bootstrap paradox),
and the registry is the one place "one owner per fact" is ENFORCED rather
than hoped for: every row is stamped with the plugin id that wrote it, and
a second owner claiming the same tool name, command token, or prompt-order
slot is a load-time error, never a silent overwrite. Today's single if/elif
chain structurally cannot shadow a command; a registry can, so the registry
must refuse.

The registry is built PER LiteTUI INSTANCE, never at module level: the test
suite constructs many apps inside one process, and a module-global registry
populated by instance-bound plugins would leak skills/mcp/seat state from
one test's app into the next.

Trust tiers: first-party plugins receive ctx.app and may close over it.
Dynamic providers (MCP) register (specs_fn, dispatch_fn) pairs that never
see the app — the same argument-only privilege MCP has today. Unifying the
registries must not widen MCP's reach.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

# System-prompt section slots. The composition order is MODEL BEHAVIOR, not
# code shape — it decides what the model reads every turn, so the canonical
# order lives here, once, and a plugin claiming an occupied slot is refused.
# Gaps are deliberate: future sections pick an unused integer between their
# neighbours.
PROMPT_ORDER = {
    "BASE": 0,          # prompts/systemprompt.md (host)
    "MEMORY": 10,       # the store block (host)
    "TOOLS": 20,        # TOOLS_PROMPT, gated on tools_enabled (host)
    "SKILLS_INDEX": 30, # skills index, gated on tools_enabled + skills (skills plugin)
}

# THE discovery list and THE load order, one committed artifact. Filesystem
# order carries no semantics anywhere in this system — only this tuple does.
# The sequence preserves _all_tools()'s historical append order (MCP last):
# no test is KNOWN to be order-sensitive, but that is an unverified
# assumption, so the ordering is kept until someone proves it free.
PLUGIN_LOAD_ORDER: tuple[str, ...] = (
    "plugins.core_tools",
    "plugins.view_image",
    "plugins.pccontrol",
    "plugins.chrome",
    "plugins.ask_user_question",
    "plugins.studio",
    "plugins.skills_plugin",
    "plugins.harness_plugin",
    "plugins.mcp_plugin",
    "plugins.themes_plugin",
    "plugins.mark_plugin",
    "plugins.misc",
    "plugins.convo",
    "plugins.model_switch",
    "plugins.help_plugin",
    "plugins.settings_ui",
    "plugins.scheduler_plugin",
)

# Module-level criticality, for failures that happen BEFORE a manifest exists
# (a syntax error, a missing dependency at import). The manifest's own
# critical flag can only speak once the module has imported.
CRITICAL_MODULES: frozenset[str] = frozenset({"plugins.core_tools"})


@dataclass(frozen=True)
class PluginManifest:
    """What a plugin module exports as its module-level ``PLUGIN`` constant."""

    id: str
    critical: bool = False   # True: a register() failure aborts boot (core_tools only —
                             # a chat harness that cannot read a file is a lying boot).
    register: Callable[["PluginContext"], None] | None = None  # pure, cheap; __init__
    activate: Callable[[Any], None] | None = None              # side-effects; on_mount


@dataclass(frozen=True)
class ToolEntry:
    owner: str
    name: str
    spec: dict
    run: Callable[[dict], str]
    gate: Callable[[], bool] | None = None


@dataclass(frozen=True)
class DynamicTools:
    owner: str
    specs_fn: Callable[[], list[dict]]
    dispatch_fn: Callable[[str], Callable | None]


@dataclass(frozen=True)
class CommandEntry:
    owner: str
    tokens: tuple[str, ...]
    handler: Callable[[Any, str, str], None]   # handler(app, name, arg)
    palette: str | None = None
    help: str = ""


@dataclass(frozen=True)
class PaletteRow:
    owner: str
    title: str
    help: str
    run: Callable[[], Any]


@dataclass(frozen=True)
class PromptSection:
    owner: str
    order: int
    render: Callable[[], str]
    enabled: Callable[[], bool] | None = None


class PluginRegistry:
    """Owner-stamped capability tables. One instance per app, always."""

    def __init__(self) -> None:
        self.tools: list[ToolEntry] = []
        self.dynamic: list[DynamicTools] = []
        self.commands: dict[str, CommandEntry] = {}   # every alias token maps here
        self.palette_rows: list[PaletteRow] = []
        self.prompt_sections: list[PromptSection] = []
        self.status: dict[str, str] = {}              # id -> active|disabled|failed: ...
        self._tool_by_name: dict[str, ToolEntry] = {}

    # ── registration (called via PluginContext, owner pre-bound) ──────────

    def add_tool(self, owner: str, spec: dict, run, gate=None) -> None:
        name = spec["function"]["name"]
        prior = self._tool_by_name.get(name)
        if prior is not None:
            raise ValueError(
                f"tool {name!r}: {owner!r} collides with {prior.owner!r} — "
                "one owner per fact; a silently shadowed tool is worse than a crash"
            )
        entry = ToolEntry(owner, name, spec, run, gate)
        self.tools.append(entry)
        self._tool_by_name[name] = entry

    def add_dynamic(self, owner: str, specs_fn, dispatch_fn) -> None:
        self.dynamic.append(DynamicTools(owner, specs_fn, dispatch_fn))

    def add_command(self, owner: str, tokens, handler, palette=None, help="") -> None:
        toks = tuple(t.lower() for t in tokens)
        for t in toks:
            prior = self.commands.get(t)
            if prior is not None:
                raise ValueError(
                    f"command {t!r}: {owner!r} collides with {prior.owner!r} — "
                    "a shadowed command was impossible in the if/elif era; "
                    "the registry must not make it possible now"
                )
        entry = CommandEntry(owner, toks, handler, palette, help)
        for t in toks:
            self.commands[t] = entry

    def add_palette_row(self, owner: str, title: str, help: str, run) -> None:
        self.palette_rows.append(PaletteRow(owner, title, help, run))

    def add_prompt_section(self, owner: str, order: int, render, enabled=None) -> None:
        for s in self.prompt_sections:
            if s.order == order:
                raise ValueError(
                    f"prompt-section order {order}: {owner!r} collides with "
                    f"{s.owner!r} — the slot table is PROMPT_ORDER; pick a free integer"
                )
        self.prompt_sections.append(PromptSection(owner, order, render, enabled))

    # ── consumption (the host's side) ─────────────────────────────────────

    def tool_specs(self) -> list[dict]:
        """Gated static specs, then every dynamic provider's, in registration
        order — the same shape _all_tools() has always returned."""
        specs = [e.spec for e in self.tools if e.gate is None or e.gate()]
        for d in self.dynamic:
            specs.extend(d.specs_fn())
        return specs

    def dispatch_for(self, name: str):
        """Static first, then dynamic providers. Gates deliberately NOT
        consulted here — dispatch has never been gated, only the offer is."""
        entry = self._tool_by_name.get(name)
        if entry is not None:
            return entry.run
        for d in self.dynamic:
            fn = d.dispatch_fn(name)
            if fn is not None:
                return fn
        return None

    def sections_sorted(self) -> list[PromptSection]:
        return sorted(self.prompt_sections, key=lambda s: s.order)

    def compose_prompt(self) -> str:
        """The exact historical fold: each enabled section glued with
        (base + text).strip(), in slot order. The glue is behavior — the
        model reads this every turn — so it lives in ONE place."""
        base = ""
        for s in self.sections_sorted():
            if s.enabled is None or s.enabled():
                base = (base + s.render()).strip()
        return base

    def unload(self, owner: str) -> None:
        """Sweep every table for one owner's rows — whole-plugin disable and
        per-test isolation without per-call disposer machinery."""
        self.tools = [e for e in self.tools if e.owner != owner]
        self._tool_by_name = {n: e for n, e in self._tool_by_name.items() if e.owner != owner}
        self.dynamic = [d for d in self.dynamic if d.owner != owner]
        self.commands = {t: e for t, e in self.commands.items() if e.owner != owner}
        self.palette_rows = [r for r in self.palette_rows if r.owner != owner]
        self.prompt_sections = [s for s in self.prompt_sections if s.owner != owner]


class PluginContext:
    """A plugin's entire host surface. The loader binds one per plugin, with
    the owner id closed over — an author never passes (and can never fake)
    their own identity."""

    def __init__(self, app: Any, registry: PluginRegistry, owner: str) -> None:
        self.app = app
        self._reg = registry
        self._owner = owner

    def tool(self, spec: dict, run, gate=None) -> None:
        self._reg.add_tool(self._owner, spec, run, gate)

    def dynamic_tools(self, specs_fn, dispatch_fn) -> None:
        self._reg.add_dynamic(self._owner, specs_fn, dispatch_fn)

    def command(self, tokens, handler, palette=None, help="") -> None:
        self._reg.add_command(self._owner, tokens, handler, palette, help)

    def palette_row(self, title: str, help: str, run) -> None:
        self._reg.add_palette_row(self._owner, title, help, run)

    def prompt_section(self, order: int, render, enabled=None) -> None:
        self._reg.add_prompt_section(self._owner, order, render, enabled)


def register_plugins(
    app: Any,
    registry: PluginRegistry,
    order: tuple[str, ...] = None,
    disabled: frozenset[str] | set[str] = frozenset(),
) -> list[PluginManifest]:
    """Import each module in load order and run its register() hook.

    Non-critical failures are collected loud-and-non-fatal (a daily-driver
    TUI missing one capability beats one that refuses to boot); a CRITICAL
    failure re-raises, because booting without the floor capability is a
    broken checkout pretending to work. Critical plugins ignore `disabled`
    for the same reason. This fail-open policy is only correct while
    everything safety-relevant (ttyguard, the delivery funnel, ROOT) stays
    core — move that boundary and revisit this policy WITH it.
    """
    if order is None:
        order = PLUGIN_LOAD_ORDER
    manifests: list[PluginManifest] = []
    for mod_name in order:
        try:
            manifest = import_module(mod_name).PLUGIN
        except Exception as e:  # noqa: BLE001
            # No manifest yet, so the module NAME keys the failure and
            # CRITICAL_MODULES speaks for the criticality the manifest
            # could not declare.
            if mod_name in CRITICAL_MODULES:
                raise
            registry.status[mod_name] = f"failed: {type(e).__name__}: {e}"
            continue
        if not manifest.critical and manifest.id in disabled:
            registry.status[manifest.id] = "disabled"
            continue
        try:
            if manifest.register is not None:
                manifest.register(PluginContext(app, registry, manifest.id))
        except Exception as e:  # noqa: BLE001 — isolation is the point
            if manifest.critical:
                raise
            registry.status[manifest.id] = f"failed: {type(e).__name__}: {e}"
            continue
        registry.status[manifest.id] = "active"
        manifests.append(manifest)
    return manifests


def activate_plugins(app: Any, registry: PluginRegistry, manifests: list[PluginManifest]) -> None:
    """Run activate() hooks in the same order — on_mount's side of the split."""
    for manifest in manifests:
        if manifest.activate is None:
            continue
        try:
            manifest.activate(app)
        except Exception as e:  # noqa: BLE001
            if manifest.critical:
                raise
            registry.status[manifest.id] = f"failed at activate: {type(e).__name__}: {e}"
