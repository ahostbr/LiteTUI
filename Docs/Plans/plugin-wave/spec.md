# LiteTUI Plugin System — THE Spec (Sentinel's synthesis, 2026-08-21)

Synthesized from four independent architect designs (Johnson, Tesla, Gamma, Shannon —
Workflow run `wf_e1b5ef5c-65c`) over shared recon of deepseek-harness (Cordis), LiteSuite's
panel system, and LiteTUI's own seam map. Collision rulings are mine; every load-bearing
claim below was re-verified first-hand against the tree at `52a588f` (v0.20.0, 688 green,
app.py 5,914 lines) before being written down.

## 1. The shape (unanimous ground, adopted verbatim)

- **Framework calls the plugin.** One entry point per plugin; app.py never imports plugin
  specifics after migration.
- **No Cordis machinery** (DI resolver, fibers, event bus, vm sandbox), **no LiteSuite
  sandbox tier** (webview, permissions, rate limits), **no YAML/JSON manifests** — all
  built for scale/tenancy problems LiteTUI does not have. Deferral tripwire, named: the
  first plugin that cannot live in this repo (closed-source or user-authored outside git)
  triggers a manifest + MCP-shaped out-of-process tier — extend MCP's protocol shape,
  never invent a second mechanism (Johnson).
- **No teardown/hot-reload in v1**, but the API stays additive-friendly; Shannon's
  owner-stamped rows give `registry.unload(owner)` for test isolation without disposers.
- **The tool contract survives verbatim**: OpenAI spec dict + `run(args) -> str` +
  self-reported gate re-evaluated at use time. "Report own precondition, never silently
  vanish" (app.py:3020-3037) is preserved.
- **`_all_tools()` / `_dispatch_for()` keep their exact names and signatures** as thin
  registry facades. `test_tools_registered.py` passing UNEDITED is the load-bearing proof.
  The header count (`tools:{len(self._all_tools())}`, app.py:3544) stays derived for free.
- **Dogfood on day one.** Every capability ships AS a plugin in this pass. An empty plugin
  set fails the design (`litesuite-ships-no-pi-extensions` is the recorded failure mode).

## 2. Collision rulings (nine, decided)

| # | Collision | Ruling | Why |
|---|---|---|---|
| 1 | Ordering: topo-sort over `requires` (Johnson, Shannon) vs literal list (Tesla, Gamma) | **Literal `PLUGIN_LOAD_ORDER` list** | 17 known plugins, ordering already documented in `__init__`'s comments; auditable in one screen; zero machinery. `requires` is additive later. |
| 2 | Discovery: dir scan vs explicit list | **The same list** | One greppable "what ships"; deterministic across OSes; collapses two collisions into one committed artifact. |
| 3 | Layout: `src/plugins/` package vs flat `src/` | **`src/plugins/` package** | 3-1; Gamma's flat-file discipline is unenforced by its own admission. Ttyguard glob widens to `rglob` in the SAME commit the directory is born, proven by a negative control (plant a violating file, watch the gate fail, revert). |
| 4 | App exposure: strict facade (Johnson) vs app handle (Tesla, Gamma, Shannon) | **App handle for first-party** — `handler(app, name, arg)` and `make_runner(app)` closures (the proven studio idiom) | 3-1; command bodies move VERBATIM (lowest transcription risk); Rule of Three — one impure hook exists. **Johnson's boundary is kept structurally: MCP entries never receive the app** — the dynamic provider is `(specs_fn, dispatch_fn)`, arg-only, exactly today's privilege. |
| 5 | Settings fields: distributed registration (Johnson, Shannon) vs central dataclass (Tesla, Gamma) | **Central stays** | Single owner of a fact many plugins read; the deferred-reload list stays ONE auditable list at its current home; a forgettable per-field flag is a silent-bug generator (Johnson's own risk list). SettingsScreen UI + apply-mapping move to a plugin. v2 candidate, named. |
| 6 | Screens as a registry kind | **No registry — direct pushes** | One screen family (scheduler) = Rule of Three fails. Screen classes still MOVE to their owning plugin modules (file relocation); grep `from app import <Screen>` refs first (Tesla). |
| 7 | Boot criticality | **`critical=True` on `core_tools` only** (Tesla) | The suite's own EXPECTED comment: bash/read/write/web_fetch missing = "the harness itself is broken". Booting a chat harness that cannot read a file is a lying boot. Everything else: per-plugin isolated, loud, non-fatal — Shannon's coupled pair holds (fail-open is correct ONLY while ttyguard, the delivery funnel, and ROOT stay permanently core; move that boundary and the boot policy must be revisited with it). |
| 8 | Registry residency | **Per-instance: `self.plugins = PluginRegistry()` in `__init__`** (Tesla) | Tests build multiple `LiteTUI()` per process; a module-global registry leaks skills/mcp/seat across them. Imports are per-process (Python caches); registration is cheap appends. |
| 9 | Capability-kind surface | **Five kinds**: tools (+ dynamic provider), commands (palette label optional + `palette_row` escape hatch), prompt sections, themes, monitors | Each maps to a REAL seam in app.py today (3-source tool merge · command chain + palette double-authoring · 4-section hand-stitched prompt · on_mount theme block · two monitor starts). No screens kind (ruling 6), no settings-fields kind (ruling 5). Tesla's escape hatch is required: 2 of 15 palette rows are not slash-commands (New scheduled job, Toggle agent tools) — a pure derivation silently drops them. |

## 3. The contract

`src/plugins/__init__.py` — the substrate (stays core; bootstrap paradox):

```python
@dataclass(frozen=True)
class PluginManifest:
    id: str                      # kebab-case, unique
    critical: bool = False       # True aborts boot on register failure (core_tools only)
    register: Callable[[PluginContext], None] | None = None   # pure, cheap, __init__
    activate: Callable[[LiteTUI], None] | None = None         # side-effects, on_mount

PLUGIN = PluginManifest(...)     # one module-level constant per plugin module

PLUGIN_LOAD_ORDER: tuple[str, ...] = (   # THE discovery + THE order, one artifact
    "plugins.core_tools",        # critical — bash/read/write/web_fetch
    "plugins.view_image", "plugins.pccontrol", "plugins.chrome",
    "plugins.ask_user_question", "plugins.studio",
    "plugins.skills_plugin", "plugins.harness_plugin", "plugins.mcp_plugin",
    "plugins.themes_plugin", "plugins.scheduler_plugin", "plugins.settings_ui",
    "plugins.convo", "plugins.model_switch", "plugins.help_plugin",
    "plugins.mark_plugin", "plugins.misc",
)   # order preserves today's _all_tools append order exactly (MCP last) — Gamma:
    # no test is KNOWN order-sensitive; keep the ordering until that is verified, not assumed.
```

`PluginContext` (NOT named ToolContext — `src/tool_context.py` owns that name for
output-masking policy, a different concept):

```python
class PluginContext:
    app: LiteTUI                                          # first-party trust tier only
    def tool(spec, run, gate=None): ...                   # run: (args)->str; make closures over ctx.app as needed
    def dynamic_tools(specs_fn, dispatch_fn): ...         # MCP shape — NO app access, arg-only (trust boundary)
    def command(tokens, handler, palette=None, help=""): ...  # handler(app, name, arg); collision on a token RAISES, unconditionally
    def palette_row(title, help, run): ...                # escape hatch for non-command rows
    def prompt_section(order, render, enabled=None): ...  # order from the PROMPT_ORDER table; duplicate order RAISES
    def theme(theme_obj): ...
    def monitor(start_fn): ...                            # invoked at activate-time by the host; semantics = today's run_worker
```

`PluginRegistry` — per-instance, owner-stamped rows (Shannon): every `register_*` lands
tagged with the calling plugin's id (the loader closes ctx over it); duplicate tool or
command names across owners are a load-time error, never last-write-wins;
`unload(owner)` sweeps all tables for tests/disable.

**Loader rules (Tesla's, all adopted):**
- Register loop in `__init__` AFTER settings/client/skills-discovery/mcp.load()/seat
  construction — **skills discovery and `mcp.load()` stay host-owned literal lines**,
  never inside a plugin's register body (per-plugin isolation would swallow a genuinely
  broken checkout into a silent half-boot).
- Non-critical failures: collect into `self.plugin_failures[id]`, log loud, continue.
- Activate loop in `on_mount`, same order — monitors and theme-apply move here.
- `settings.plugins_disabled: list[str]` (mirrors `mcp_disabled_servers`) skips
  non-critical plugins at the register loop; applies at next boot.
- `/plugins` command lists every id: active / disabled / failed:<msg>.

**Per-call context inventory** (scouted first-hand — the fields the six `_dispatch_for`
closures actually need): `app.skills`, `app.seat`, `app.model_id`, `app.model_type`,
`app.mcp`, the `_tool_view_image` bound method (its staging list `_pending_tool_images`
is agent-loop infrastructure and stays core), and the app handle for
ask_user_question's screen pushes (whose `set_app()` module-global dies in the same
commit that migrates it — all four architects refuse to defer this).

## 4. Stays core, permanently (the coupled pair binds this list to the boot policy)

Textual App shell + agent loop (`_stream`/`_compact`) · the plugin substrate itself ·
`ttyguard.py` (a NEGATIVE capability — enforcement can't be contributable) · the
queued-delivery funnel (`_pending_input`/`_user_bubble`/flush at
`on_worker_state_changed`) — inbox and cron plugins CALL it, never own it · ROOT
resolution + data stores (plugins receive paths, never compute ROOT) · conversation
persistence + `self.client` · Settings dataclass + load/save · seat CONSTRUCTION
(the harness TOOL is a plugin; the seat object's existence before monitors is a core
ordering guarantee).

## 5. Resolved open questions

1. **Disable × requires**: moot in v1 (no `requires`); revisit with topo-sort if ever added.
2. **Resumed convo naming a now-absent tool**: history rendering is data replay and never
   consults the registry; a NEW call to an unknown name already resolves to None and errors
   in-band today. Verified during P2 with the existing unknown-tool path.
3. **Mid-session disable**: applies at next boot; SettingsScreen says so (existing
   deferred-apply doctrine, same list, same home).
4. **Prompt-order governance**: one `PROMPT_ORDER` constants table in the substrate
   (BASE=0 core, MEMORY=10 core, TOOLS=20 core, SKILLS_INDEX=30 skills plugin); duplicate
   order raises; the canonical order is documented AT the table. Composition output proven
   byte-identical by a characterization snapshot BEFORE the old path is deleted (Shannon:
   prompt order is model behavior, not code shape).
5. **Monitor crash mid-session**: unchanged-by-design in v1 — same `run_worker` semantics
   as today; the registry changes who STARTS monitors, not how they live or die.
6. **Key bindings**: core in v1. Screens carry their own BINDINGS and move with their
   classes. A bindings kind is a named deferral, pain-gated.
7. **Same-commit atomicity under multi-agent dispatch**: not applicable — Ryan ruled the
   split is Sentinel's own hands, single seat, sequential commits. The atomic pairs
   (glob-widen + dir-creation; gate-rewrite + first command move) are single commits.
8. **Re-accretion guard for app.py**: `test_plugin_dogfood.py` asserts, permanently:
   ≥3 plugin-sourced entries per kind (tools, commands, prompt sections) AND app.py's
   source never imports any module named in PLUGIN_LOAD_ORDER. The road back to monolith
   is gated, not eyeballed.
9. **Per-tier context contract**: first-party = closures over `ctx.app` (inventory in §3);
   MCP tier = args-only via `dynamic_tools` — privilege identical to today, structurally.
10. **Per-instance cost**: imports are process-cached; registration is O(kinds) appends;
    discovery is a tuple. If suite wall-time regresses at P1, measure then — don't
    pre-optimize.

**Test convention**: migrated plugins keep their existing feature suites; a NEW plugin
ships its own `tests/test_plugin_<id>.py`. Every new test file must be classifiable by
the collector-integrity contract (pytest-style vs script-style, conftest.collect_ignore
exact).

## 6. Execution phases (mine; suite green after every commit)

- **P0 — instruments first**: widen `test_ttyguard.py` glob → `rglob` + negative control
  (plant a violating file under src/plugins/, watch it FAIL, revert); substrate module +
  its unit tests; collector updates. *The gate is proven live before anything can hide
  behind it.*
- **P1 — loader wired at zero**: register+activate loops into `__init__`/`on_mount` over
  an empty PLUGIN_LOAD_ORDER. Proves clean no-op. 688 unchanged.
- **P2 — tools, one commit each, bridge shrinking**: core_tools (critical) with
  facade-with-bridge; then view_image, pccontrol, chrome, ask_user_question (+set_app
  death), studio. `test_tools_registered.py` unedited green at every step.
- **P3 — prompt sections**: characterization snapshot of `_system_prompt_text()` FIRST;
  compose() over sections (base/memory/tools core, skills via plugin); byte-identical.
- **P4 — mcp (`dynamic_tools`) + harness** (tool + activate moves `_inbox_monitor`).
- **P5 — themes** (+ activate applies saved theme), then DELETE the bridge — registry is
  the sole source, ordering preserved.
- **P6 — commands**: registry-first lookup + legacy-chain fallback + duplicate-token
  assertion; first migration commit ALSO rewrites the palette drift gate to read the
  registry, mutation-tested (rename a command, watch it fail, revert). Then plugin by
  plugin: misc, convo, model_switch, mark, help, settings_ui, scheduler-commands. Chain
  ends empty; fallback deleted.
- **P7 — palette derived**: `LiteTUICommands` reads the registry (labels + 2 escape-hatch
  rows). Title-set test passes unedited.
- **P8 — screens relocate**: grep `from app import <Screen>` refs first; Calendar/Day/Job
  → scheduler module, Help → help, Picker → model_switch; ConfirmStop stays core.
- **P9 — observability**: `/plugins`, `settings.plugins_disabled`, boot-failure surfacing;
  positive tests + negative controls (unknown id inert; disabled plugin's tool absent and
  its command unknown).
- **P10 — permanent gates + sweep**: `test_plugin_dogfood.py` (counts + re-accretion
  guard); dead-code deletion (grep-before-delete); line-count report; full suite +
  `run_all.py` script half; version 0.21.0 + CHANGELOG (1.0.0 is Ryan's call).

Then **Workflow 2** (`workflow-2-cleanup-review.mjs`) with three disjoint partitions of
the post-split tree; every confirmed finding gets fixed — nothing deferred.
