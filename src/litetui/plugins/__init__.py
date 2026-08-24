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

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from litetui.tool_policy import MCP_UNKNOWN_POLICY, ToolPolicy

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
    "litetui.plugins.core_tools",
    "litetui.plugins.view_image",
    "litetui.plugins.pccontrol",
    "litetui.plugins.chrome",
    "litetui.plugins.ask_user_question",
    "litetui.plugins.studio",
    "litetui.plugins.skills_plugin",
    "litetui.plugins.harness_plugin",
    "litetui.plugins.mcp_plugin",
    "litetui.plugins.themes_plugin",
    "litetui.plugins.mark_plugin",
    "litetui.plugins.misc",
    "litetui.plugins.convo",
    "litetui.plugins.model_switch",
    "litetui.plugins.help_plugin",
    "litetui.plugins.settings_ui",
    "litetui.plugins.scheduler_plugin",
    "litetui.plugins.goal_loop_plugin",
    "litetui.plugins.glassbox_plugin",
)

# Module-level criticality, for failures that happen BEFORE a manifest exists
# (a syntax error, a missing dependency at import). The manifest's own
# critical flag can only speak once the module has imported.
CRITICAL_MODULES: frozenset[str] = frozenset({"litetui.plugins.core_tools"})


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
    policy: ToolPolicy = MCP_UNKNOWN_POLICY


@dataclass(frozen=True)
class DynamicTools:
    owner: str
    specs_fn: Callable[[], list[dict]]
    dispatch_fn: Callable[[str], Callable | None]
    policy: ToolPolicy = MCP_UNKNOWN_POLICY


@dataclass(frozen=True)
class Observer:
    """A plugin WATCHING the host, rather than offering something to the user.

    The other four tables all answer "what can the user reach". This one
    answers "who is listening", and it is the only table whose entries the
    host calls rather than the user. The glass-box brain is the first
    consumer: it contributes no tool and no command, it only needs to know
    when the host thought, called, wrote, or finished.
    """

    owner: str
    handler: Callable[[dict], None]


@dataclass(frozen=True)
class TurnFinalizer:
    """A plugin reacting to an explicit plain-answer completion."""

    owner: str
    handler: Callable[[], Any]


#: Palette groups, in display order (Ryan, 2026-08-22).
#: Named for what the user is doing, not for the subsystem that owns it.
#: `app` is last so Quit sits at the very bottom, away from anything frequent.
PALETTE_GROUPS: tuple[str, ...] = (
    "convo", "backend", "tools", "automation", "screen", "app",
)

#: Display headings for those groups.
PALETTE_GROUP_LABELS: dict[str, str] = {
    "convo": "Convo",
    "backend": "Backend",
    "tools": "Tools",
    "automation": "Automation",
    "screen": "Screen",
    "app": "App",
}


def palette_sort_key(group: str, order: int, title: str) -> tuple:
    """Sort key for one palette row.

    An unknown group sorts to the END rather than raising: a third-party
    plugin inventing its own group should land at the bottom of the list, not
    take down the palette. Ties break on title so the order is total and the
    list can never shuffle between runs.
    """
    try:
        rank = PALETTE_GROUPS.index(group)
    except ValueError:
        rank = len(PALETTE_GROUPS)
    return (rank, order, title.lower())


@dataclass(frozen=True)
class CommandEntry:
    owner: str
    tokens: tuple[str, ...]
    handler: Callable[[Any, str, str], None]   # handler(app, name, arg)
    palette: str | None = None
    help: str = ""
    # Where this sits in the palette. Declared HERE, at the point of
    # registration, rather than in a table in the provider -- a hand-authored
    # second table is the drift class the derived palette already replaced,
    # and it would silently drop any command nobody remembered to add.
    group: str = "app"
    order: int = 500


@dataclass(frozen=True)
class PaletteRow:
    owner: str
    title: str
    help: str
    run: Callable[[], Any]
    group: str = "app"
    order: int = 500
    # What to show as the trailing command tag. A command-backed row derives
    # this from the dispatcher and can never disagree with it; a row like
    # "Scheduled jobs" runs a SUBcommand ("/cron list") that no single token
    # names, so it states its own. Declared here at registration for the same
    # reason group and order are -- never in a table somewhere else.
    tag: str = ""


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
        self.observers: list[Observer] = []           # watchers; see emit()
        self.turn_finalizers: list[TurnFinalizer] = []
        self.status: dict[str, str] = {}              # id -> active|disabled|failed: ...
        self._tool_by_name: dict[str, ToolEntry] = {}
        #: The human's per-tool denylist, as a LIVE PROVIDER rather than a set.
        #: Unchecking a tool must take effect on the next request, not the next
        #: restart, and a snapshot taken at construction cannot do that.
        #:
        #: It is consulted INSIDE `tool_specs()` rather than passed in by the
        #: caller on purpose: a parameter is something a new call site can
        #: forget, and forgetting it would silently offer a tool the user
        #: switched off. Defaults to "nothing disabled", so every bare registry
        #: — which is what the tests build — behaves exactly as before.
        self.tools_disabled: Callable[[], frozenset[str]] = frozenset

    # ── registration (called via PluginContext, owner pre-bound) ──────────

    def add_tool(self, owner: str, spec: dict, run, gate=None,
                 policy: ToolPolicy | None = None) -> None:
        name = spec["function"]["name"]
        prior = self._tool_by_name.get(name)
        if prior is not None:
            raise ValueError(
                f"tool {name!r}: {owner!r} collides with {prior.owner!r} — "
                "one owner per fact; a silently shadowed tool is worse than a crash"
            )
        entry = ToolEntry(owner, name, spec, run, gate, policy or MCP_UNKNOWN_POLICY)
        self.tools.append(entry)
        self._tool_by_name[name] = entry

    def add_dynamic(self, owner: str, specs_fn, dispatch_fn,
                    policy: ToolPolicy | None = None) -> None:
        self.dynamic.append(
            DynamicTools(owner, specs_fn, dispatch_fn, policy or MCP_UNKNOWN_POLICY)
        )

    def add_command(self, owner: str, tokens, handler, palette=None, help="",
                    group="app", order=500) -> None:
        toks = tuple(t.lower() for t in tokens)
        for t in toks:
            prior = self.commands.get(t)
            if prior is not None:
                raise ValueError(
                    f"command {t!r}: {owner!r} collides with {prior.owner!r} — "
                    "a shadowed command was impossible in the if/elif era; "
                    "the registry must not make it possible now"
                )
        entry = CommandEntry(owner, toks, handler, palette, help, group, order)
        for t in toks:
            self.commands[t] = entry

    def add_palette_row(self, owner: str, title: str, help: str, run,
                        group="app", order=500, tag="") -> None:
        self.palette_rows.append(PaletteRow(owner, title, help, run, group, order, tag))

    def add_observer(self, owner: str, handler) -> None:
        """No collision check, deliberately. The other tables refuse a second
        owner because a shadowed tool or command is a SILENT loss of function.
        Watching is not exclusive: any number of plugins may observe the same
        event and none of them consumes it, so there is nothing to shadow."""
        self.observers.append(Observer(owner, handler))

    def add_turn_finalizer(self, owner: str, handler) -> None:
        self.turn_finalizers.append(TurnFinalizer(owner, handler))

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
        order — the same shape _all_tools() has always returned.

        MINUS anything the user switched off in the tool list. Withholding the
        schema is the primary mechanism: `prompts/tools.md` deliberately does
        NOT enumerate tools ("an inventory written here drifts the moment a
        tool is added, and the schemas cannot"), so the schemas ARE the model's
        only inventory and a withheld tool is one it never learns exists.

        ⚠️ Withholding is not sufficient on its own and is not the only guard.
        `dispatch_for` does not consult gates, so a name remembered from
        earlier in the SAME conversation still reaches the host — see the
        refusal at `_execute_tool`'s authorization door. Two mechanisms,
        because this one cannot cover that case.

        The denylist is name-based and applies to DYNAMIC specs too. MCP tools
        are normally governed by `mcp_disabled_servers` at the server level and
        the tool list does not offer per-MCP checkboxes — but a name that lands
        in the denylist must be honoured wherever it came from. A denylist that
        silently ignores half its entries is worse than not having one.
        """
        off = self.tools_disabled()
        specs = [
            e.spec for e in self.tools
            if (e.gate is None or e.gate()) and e.name not in off
        ]
        for d in self.dynamic:
            specs.extend(
                s for s in d.specs_fn() if s["function"]["name"] not in off
            )
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

    def policy_for(self, name: str) -> ToolPolicy | None:
        """Policy metadata for one callable tool, static or dynamic.

        Dynamic providers carry a conservative provider-wide policy because
        LiteTUI cannot infer the effects of arbitrary MCP tools from their
        names or prose descriptions.  Undeclared third-party tools receive the
        same fail-safe policy during registration, so this never returns a
        permission-free callable.
        """
        entry = self._tool_by_name.get(name)
        if entry is not None:
            return entry.policy
        for d in self.dynamic:
            if d.dispatch_fn(name) is not None:
                return d.policy
        return None

    def emit(self, event: dict) -> None:
        """Hand one host event to every observer. Called from the STREAM LOOP,
        so two properties are load bearing:

        TELEMETRY MAY FAIL; IT MAY NOT BE LOAD BEARING. A raising observer is
        swallowed and the remaining observers still run. If a broken telemetry
        plugin could take the turn down, installing the brain would make the
        app less reliable than not having it — and the symptom would surface as
        a broken conversation with nothing pointing at the observer.

        The failure is swallowed, not hidden: it is recorded on the plugin's
        status row, which /plugins prints. A plugin that is silently failing
        every event has somewhere to say so.

        The list is copied before iteration so an observer that unloads its own
        plugin mid-event cannot mutate the list being walked.
        """
        for obs in list(self.observers):
            try:
                obs.handler(event)
            except Exception as e:                      # noqa: BLE001 — see above
                self.status[obs.owner] = f"observer failed: {type(e).__name__}: {e}"

    async def finalize_turn(self) -> None:
        """Run completion hooks only when the host names a plain answer.

        This is intentionally not driven by worker counts or idleness: a
        persistent inbox monitor never becomes idle and is irrelevant to
        whether the transcript contains completion evidence.
        """
        for finalizer in list(self.turn_finalizers):
            try:
                result = finalizer.handler()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # noqa: BLE001 — isolate optional plugins
                self.status[finalizer.owner] = (
                    f"turn finalizer failed: {type(e).__name__}: {e}"
                )

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
        self.observers = [o for o in self.observers if o.owner != owner]
        self.turn_finalizers = [f for f in self.turn_finalizers if f.owner != owner]


class PluginContext:
    """A plugin's entire host surface. The loader binds one per plugin, with
    the owner id closed over — an author never passes (and can never fake)
    their own identity."""

    def __init__(self, app: Any, registry: PluginRegistry, owner: str) -> None:
        self.app = app
        self._reg = registry
        self._owner = owner

    def tool(self, spec: dict, run, gate=None,
             policy: ToolPolicy | None = None) -> None:
        self._reg.add_tool(self._owner, spec, run, gate, policy)

    def dynamic_tools(self, specs_fn, dispatch_fn,
                      policy: ToolPolicy | None = None) -> None:
        self._reg.add_dynamic(self._owner, specs_fn, dispatch_fn, policy)

    def command(self, tokens, handler, palette=None, help="",
                group="app", order=500) -> None:
        self._reg.add_command(self._owner, tokens, handler, palette, help, group, order)

    def palette_row(self, title: str, help: str, run, group="app", order=500,
                    tag="") -> None:
        self._reg.add_palette_row(self._owner, title, help, run, group, order, tag)

    def prompt_section(self, order: int, render, enabled=None) -> None:
        self._reg.add_prompt_section(self._owner, order, render, enabled)

    def observe(self, handler) -> None:
        """Watch host events: handler({channel, intensity, label, ...}).

        The only registration that gives nothing to the user. Your handler runs
        INSIDE the stream loop — keep it cheap and non-blocking. Raising is
        survivable (emit swallows it and records the failure on your status
        row) but it means you saw nothing, so do not rely on it."""
        self._reg.add_observer(self._owner, handler)

    def turn_finalizer(self, handler) -> None:
        """React to a persisted plain answer; never to ambient idleness."""
        self._reg.add_turn_finalizer(self._owner, handler)


def status_command(app, name: str, arg: str) -> None:
    """/plugins — every plugin with its true status. Host-registered, so it
    survives any plugin being disabled (a status readout that dies with a
    disabled plugin reports nothing exactly when it matters). The status
    dict is insertion-ordered, which IS the load order; keys are manifest
    ids, or the module name when the module never imported."""
    lines = [f"{len(app.plugins.status)} plugin(s), load order:"]
    for pid, status in app.plugins.status.items():
        lines.append(f"  {status:<9} {pid}")
    app.system_message(chr(10).join(lines))


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
