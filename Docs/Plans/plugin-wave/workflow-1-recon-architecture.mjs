export const meta = {
  name: 'litetui-plugin-recon-architecture',
  description: 'Recon three plugin-system sources, then a 4-polymath architecture panel',
  phases: [
    { title: 'Recon', detail: 'deepseek-harness · LiteSuite panels · LiteTUI map' },
    { title: 'Architecture', detail: 'Johnson, Tesla, Gamma, Shannon — independent designs' },
    { title: 'Agreement', detail: 'where the four designs agree and collide' },
  ],
}

// ── Phase 1: three readers, three sources, one shape ──────────────────────
const RECON_SCHEMA = {
  type: 'object',
  properties: {
    source: { type: 'string' },
    mechanism: { type: 'string', description: 'how plugins/extensions/panels register, load, and die — concrete symbols and files' },
    contract: { type: 'string', description: 'what a unit IS: its manifest/interface, required exports, capabilities' },
    lifecycle: { type: 'string', description: 'discovery -> load -> activate -> teardown, with file:line pointers' },
    lessons: { type: 'array', items: { type: 'string' }, description: 'what worked, what rotted, what to copy or avoid' },
    pointers: { type: 'array', items: { type: 'string' }, description: 'file:line references worth re-reading' },
  },
  required: ['source', 'mechanism', 'contract', 'lifecycle', 'lessons', 'pointers'],
}

phase('Recon')
const [deepseek, panels, litetui] = await parallel([
  () => agent(
    'Recon the PLUGIN/EXTENSION architecture of deepseek-harness at E:/SAS/REPO_CLONES/deepseek-harness. ' +
    'It is ~1M lines of ts/js — do NOT read broadly. Find specifically: how extensions/plugins/tools ' +
    'register and load (search for plugin, extension, contribut, activate, registry, manifest), the ' +
    'contract a plugin implements, its lifecycle, and how core features dogfood the system. ' +
    'Return concrete file:line evidence, not impressions.',
    { label: 'recon:deepseek-harness', phase: 'Recon', schema: RECON_SCHEMA, effort: 'medium' }),
  () => agent(
    'Recon LiteSuite\'s PANEL SYSTEM as a plugin model. Read C:/Projects/docs/architecture/02-Panel-System.md ' +
    'first, then the registration source it points into (panel types, how a pane type is declared, mounted, ' +
    'torn down; the sidebar/canvas integration). This is the system Ryan named as the shape to match — ' +
    'capture its REGISTRATION contract precisely, with file:line pointers.',
    { label: 'recon:litesuite-panels', phase: 'Recon', schema: RECON_SCHEMA, effort: 'medium' }),
  () => agent(
    'Map LiteTUI for a plugin split. Repo C:/Projects/LiteTUI, runtime in src/ (app.py ~5.6k lines + 15 ' +
    'satellite modules). Produce: every seam that could become a plugin surface — the tool registry ' +
    '(_all_tools/_dispatch_for), command dispatch (_handle_command chain), palette provider, screens, ' +
    'themes, skills, MCP, prompts/, scheduler+calendar, studio/seat_guard — with line ranges, what each ' +
    'depends on from app.py (self.* attributes used), and which seams are already clean vs entangled. ' +
    'Also: the suite\'s guard tests that constrain refactors (ttyguard sweep, collector integrity, drift gates).',
    { label: 'recon:litetui-map', phase: 'Recon', schema: RECON_SCHEMA, effort: 'high' }),
])

const recon = { deepseek, panels, litetui }
const reconBrief = JSON.stringify(recon)
log('recon complete: 3 sources')

// ── Phase 2: four architects, same evidence, independent designs ─────────
const DESIGN_SCHEMA = {
  type: 'object',
  properties: {
    thesis: { type: 'string', description: 'the one-paragraph core idea of the plugin system' },
    plugin_definition: { type: 'string', description: 'what a LiteTUI plugin IS — manifest, entry point, capabilities it may declare (tools, commands, screens, palette rows, prompts, settings)' },
    registry: { type: 'string', description: 'discovery + registration + lifecycle, concretely (paths, load order, failure isolation)' },
    core_as_plugins: { type: 'string', description: 'which existing features become plugins on day one, and what stays core' },
    split_plan: { type: 'array', items: { type: 'string' }, description: 'ordered, verifiable steps to get from the current app.py to this design' },
    risks: { type: 'array', items: { type: 'string' } },
    rejected: { type: 'array', items: { type: 'string' }, description: 'alternatives considered and why rejected' },
  },
  required: ['thesis', 'plugin_definition', 'registry', 'core_as_plugins', 'split_plan', 'risks', 'rejected'],
}

const ARCHITECTS = [
  ['polymathic-johnson', 'frameworks are components plus patterns; design the white-box to black-box evolution'],
  ['polymathic-tesla', 'simulate the COMPLETE system mentally first — registry, lifecycle, failure modes — before proposing'],
  ['polymathic-gamma', 'refactor TO patterns from felt pain; apply the Rule of Three; remove any pattern a simpler thing beats'],
  ['polymathic-shannon', 'find the invariant minimal contract; strip everything that is not signal. CRITICAL: your single StructuredOutput call IS the deliverable — fill every field with your real, complete design. A prior run returned literal placeholder text ("test", "a", "b"), which is a hard failure: never call StructuredOutput with trial or placeholder values, and never call it more than once'],
]

phase('Architecture')
const designs = await parallel(ARCHITECTS.map(([who, lens]) => () =>
  agent(
    `Design LiteTUI's plugin system. Your lens: ${lens}.\n\n` +
    'Ryan\'s brief: split the ~5.6k-line app.py; make everything-that-can-be a plugin, the way ' +
    'LiteSuite\'s panel system works; the system must DOGFOOD itself (core features ship AS plugins — ' +
    'a plugin system that ships none of its plugins is a recorded failure mode here). Python 3.11, ' +
    'Textual 8, 688-test suite that must stay green through the split, one owner per fact.\n\n' +
    'Design INDEPENDENTLY from this recon evidence (three sources, file:line pointers included):\n' +
    reconBrief,
    { label: `design:${who.replace('polymathic-', '')}`, phase: 'Architecture',
      schema: DESIGN_SCHEMA, agentType: who, effort: 'high' })
))

const named = designs.map((d, i) => ({ who: ARCHITECTS[i][0], design: d }))
  .filter(x => x.design)
log(`architecture panel: ${named.length}/4 designs returned`)

// ── Phase 3: the agreement map (cross-item by construction: needs all 4) ──
phase('Agreement')
const agreement = await agent(
  'Four architects designed the same plugin system independently. Produce the AGREEMENT MAP: ' +
  '(1) decisions all four converge on — these are close to free, ' +
  '(2) genuine collisions with the strongest argument on each side, ' +
  '(3) ideas only one architect saw that deserve to survive, ' +
  '(4) questions none of them answered. Do NOT pick winners on collisions — Sentinel synthesizes.\n\n' +
  JSON.stringify(named),
  { label: 'agreement-map', phase: 'Agreement', effort: 'high',
    schema: {
      type: 'object',
      properties: {
        converged: { type: 'array', items: { type: 'string' } },
        collisions: { type: 'array', items: { type: 'string' } },
        singular_insights: { type: 'array', items: { type: 'string' } },
        open_questions: { type: 'array', items: { type: 'string' } },
      },
      required: ['converged', 'collisions', 'singular_insights', 'open_questions'],
    } })

return { recon, designs: named, agreement }
