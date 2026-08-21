export const meta = {
  name: 'litetui-cleanup-review',
  description: 'Post-split: Uncle Bob/Linus/Dijkstra sweep disjoint partitions, suite gate, then a 4-reviewer panel with adversarial verification',
  phases: [
    { title: 'Sweep', detail: 'three cleaners, disjoint file partitions' },
    { title: 'Gate', detail: 'full suite must be green before review' },
    { title: 'Review', detail: 'Holmes, Carmack, Rams, Moriarty — read-only' },
    { title: 'Verify', detail: 'every finding faces two refuters' },
  ],
}

// args: { partitions: [[files...], [files...], [files...]], suite: "cmd string" }
// Partitions are DISJOINT by construction — that is the concurrency model.
// Sentinel builds them from the post-split module layout; this script never
// invents its own file list.
if (!args || !Array.isArray(args.partitions) || args.partitions.length !== 3) {
  throw new Error('args.partitions must be exactly three disjoint file lists (one per cleaner)')
}
const SUITE = (args && args.suite) || 'python -m pytest tests/ -q'

const CLEANERS = [
  ['polymathic-unclebob', 'Clean Code discipline: extract till you cannot, name till it reads, SOLID, zero duplication tolerance. Do NOT invent abstractions single-use code does not earn.'],
  ['polymathic-linus', 'Taste: structural elegance, kill special cases, minimal abstraction. If a change does not make the code OBVIOUSLY better, do not make it.'],
  ['polymathic-dijkstra', 'Simplicity as the prerequisite for reliability: eliminate complexity, make invariants explicit, prefer code whose correctness is visible.'],
]

const SWEEP_SCHEMA = {
  type: 'object',
  properties: {
    files_touched: { type: 'array', items: { type: 'string' } },
    changes: { type: 'array', items: { type: 'string' }, description: 'one line per meaningful change: what and why' },
    left_alone: { type: 'array', items: { type: 'string' }, description: 'smells seen but deliberately NOT fixed, with the reason' },
    suite_after: { type: 'string', description: 'the tail of the suite run YOU did after your edits' },
  },
  required: ['files_touched', 'changes', 'left_alone', 'suite_after'],
}

phase('Sweep')
const sweeps = await parallel(CLEANERS.map(([who, lens], i) => () =>
  agent(
    `Clean-code pass over EXACTLY these files in C:/Projects/LiteTUI — never touch any other: \n` +
    args.partitions[i].map(f => `  - ${f}`).join('\n') + '\n\n' +
    `Your lens: ${lens}\n\n` +
    'House laws that bind you: surgical changes only (every changed line traces to a real smell); ' +
    'match existing style; comments explain WHY; the ttyguard envelope sweep forbids raw subprocess ' +
    'calls in src/; the collector-integrity gate forbids module-level execution in collected tests; ' +
    'NEVER touch version.py or CHANGELOG.md. After your edits run the suite ' +
    `(${SUITE} — pipe through NOTHING, read the real exit) and fix what you broke. ` +
    'Report suite output honestly.',
    { label: `sweep:${who.replace('polymathic-', '')}`, phase: 'Sweep',
      schema: SWEEP_SCHEMA, agentType: who, effort: 'high' })
))
log(`sweeps done: ${sweeps.filter(Boolean).length}/3`)

// ── Gate: one suite run over the COMBINED result ─────────────────────────
phase('Gate')
const gate = await agent(
  `Run the LiteTUI suite from C:/Projects/LiteTUI: ${SUITE} — unpiped, real exit code. ` +
  'If anything fails, fix the MINIMAL cause (three cleaners just swept disjoint partitions; a failure ' +
  'here is almost certainly an interaction between two sweeps). Rerun until green or until the cause ' +
  'is genuinely ambiguous — then STOP and report exactly what fails and why, do not guess-fix. ' +
  'Return the final tail and pass count.',
  { label: 'suite-gate', phase: 'Gate', effort: 'medium',
    schema: { type: 'object', properties: {
      green: { type: 'boolean' }, passed: { type: 'number' },
      tail: { type: 'string' }, fixes: { type: 'array', items: { type: 'string' } } },
      required: ['green', 'passed', 'tail'] } })
if (!gate || !gate.green) {
  log('GATE RED — returning early; Sentinel owns the failure')
  return { sweeps, gate, reviews: null, confirmed: null }
}

// ── Review: four lenses, read-only ───────────────────────────────────────
const REVIEWERS = [
  ['polymathic-holmes', 'forensic: negative evidence, what nobody else noticed, the dog that did not bark'],
  ['polymathic-carmack', 'systems: did the plugin indirection cost real startup/latency/memory; where is the actual hot path'],
  ['polymathic-rams', 'less-but-better: does every module, plugin, knob and abstraction EARN its place'],
  ['polymathic-moriarty', 'adversarial: how does this plugin system get abused, broken, or rotted from inside'],
]

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      title: { type: 'string' }, file: { type: 'string' },
      claim: { type: 'string', description: 'the defect as a checkable statement' },
      evidence: { type: 'string', description: 'file:line or a command whose output shows it' },
      severity: { type: 'string' } },
      required: ['title', 'file', 'claim', 'evidence', 'severity'] } },
  },
  required: ['findings'],
}

phase('Review')
const reviews = await parallel(REVIEWERS.map(([who, lens]) => () =>
  agent(
    `Review C:/Projects/LiteTUI post-split, post-sweep. READ-ONLY — you edit nothing. Lens: ${lens}. ` +
    'Every finding must be a CHECKABLE claim with evidence (file:line, or a command to re-run) — ' +
    'a count can only be believed, a query can be re-run. Include what you looked for and did NOT find.',
    { label: `review:${who.replace('polymathic-', '')}`, phase: 'Review',
      schema: FINDINGS_SCHEMA, agentType: who, effort: 'high' })
))

const all = reviews.filter(Boolean).flatMap((r, i) =>
  r.findings.map(f => ({ ...f, reviewer: REVIEWERS[i][0] })))
log(`review: ${all.length} raw findings`)

// ── Verify: two refuters per finding, diverse lenses ─────────────────────
phase('Verify')
const VERDICT = { type: 'object', properties: {
  refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'] }

const judged = await parallel(all.map(f => () =>
  parallel([
    () => agent(
      `Try to REFUTE this code-review finding by reading the actual code (correctness lens). ` +
      `Default refuted=true if the evidence does not hold up:\n${JSON.stringify(f)}`,
      { label: `refute:${(f.file || '?').split('/').pop()}`, phase: 'Verify', schema: VERDICT, effort: 'medium' }),
    () => agent(
      `Try to REFUTE this finding on MATERIALITY: even if literally true, does fixing it make the ` +
      `product better, or is it churn? Default refuted=true when immaterial:\n${JSON.stringify(f)}`,
      { label: `refute-mat:${(f.file || '?').split('/').pop()}`, phase: 'Verify', schema: VERDICT, effort: 'medium' }),
  ]).then(vs => ({ ...f, verdicts: vs.filter(Boolean), survives: vs.filter(Boolean).every(v => !v.refuted) }))
))

const confirmed = judged.filter(Boolean).filter(f => f.survives)
log(`confirmed: ${confirmed.length}/${all.length} findings survive both refuters — Sentinel fixes ALL of them`)
return { sweeps, gate, confirmed, rejected: judged.filter(Boolean).filter(f => !f.survives) }
