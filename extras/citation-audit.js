export const meta = {
  name: 'citation-audit',
  description: 'Parallel, self-repairing citation-audit sweep — shards identifiers, verifies each via the citation-verifier agent, repairs transient failures (<=2 rounds), emits the canonical clinical-citation-audit block, and proposes fixes to the improvement log when the goal is not met.',
  whenToUse: 'Bulk citation verification on a manuscript / reference list / digest (>5 identifiers). Reused as shared infrastructure by pubmed-surveillance and (later) evidence-debate, teaching-deck, grant-reviewer.',
  phases: [
    { title: 'Extract' },
    { title: 'Verify' },
    { title: 'Repair' },
    { title: 'Synthesize' },
    { title: 'Introspect' },
  ],
}

// ---------------------------------------------------------------------------
// Contracts carried verbatim into every spawned agent (CLAUDE.md hard rule).
// ---------------------------------------------------------------------------
const NO_FAB =
  'HARD RULE (verbatim, non-negotiable): Never fabricate or pattern-match a citation identifier ' +
  '(PMID/DOI/PMC/arXiv/NCT/URL) from training memory. Every identifier you mark VERIFIED must come ' +
  'from a real tool call this run. Unverifiable after 2 attempts -> mark UNVERIFIABLE. A correct ' +
  'author/title/year with a fabricated PMID is worse than no PMID. Verify before claiming; state ' +
  'what you checked vs. inferred. Distinguish a genuine "not found" (-> UNVERIFIABLE) from a tool ' +
  'timeout/unreachable error (-> classification "transient-error", so the repair loop retries it).'

// Tolerate args delivered as a JSON string (Workflow-tool / scheduled-task callers sometimes
// stringify), and never crash on absent args.
const IN = typeof args === 'string' ? (() => { try { return JSON.parse(args) } catch (e) { return {} } })() : (args || {})
const SHARD_SIZE = IN.shardSize || 8
const RUN = IN.runId || IN.asof || 'adhoc'
const EMBEDDED = !!IN.embedded // suppress introspection writeback when called by a parent workflow

const chunk = (arr, n) => {
  const out = []
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n))
  return out
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------
const EXTRACT_SCHEMA = {
  type: 'object',
  properties: {
    citations: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          raw: { type: 'string' },
          id: { type: 'string' },
          type: { type: 'string', enum: ['PMID', 'DOI', 'PMC', 'arXiv', 'NCT', 'URL', 'REF'] },
          topic: { type: 'string', description: 'short citation context, e.g. "dexmedetomidine in pediatric ARDS"' },
        },
        required: ['id', 'type'],
      },
    },
  },
  required: ['citations'],
}

const SHARD_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          raw: { type: 'string' },
          id: { type: 'string' },
          type: { type: 'string', enum: ['PMID', 'DOI', 'PMC', 'arXiv', 'NCT', 'URL', 'REF'] },
          topic: { type: 'string' },
          status: { type: 'string', enum: ['VERIFIED', 'FLAGGED', 'UNVERIFIABLE'] },
          classification: { type: 'string', enum: ['clean', 'transient-error', 'ambiguous-mismatch'] },
          canonicalTitle: { type: 'string' },
          sourceEvidence: { type: 'string', description: 'the actual tool call + result that confirmed/refuted it' },
          attempts: { type: 'number' },
          error: { type: 'string' },
        },
        required: ['id', 'type', 'status', 'classification'],
      },
    },
  },
  required: ['verdicts'],
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------
const extractPrompt = (text) =>
  'EXTRACT-ONLY MODE. From the text below, extract every citation identifier (PMID, DOI, PMC, arXiv, ' +
  'NCT, deeper-than-root URL) and every "Author et al., Journal Year" reference lacking an identifier. ' +
  'Deduplicate; count each unique identifier once. Do NOT verify yet. For each, give a short topic. ' +
  'Return JSON per schema.\n' + NO_FAB + '\n\n---TEXT---\n' + text

const verifyPrompt = (shard, repair) =>
  (repair ? 'REPAIR PASS. ' : '') +
  'Verify each of these citation identifiers per your Citation Verifier procedure. TEST_MODE=true ' +
  '(skip the optimization web-scan). For each identifier return one verdict object: status ' +
  '(VERIFIED only with cited tool evidence; FLAGGED on metadata mismatch you can correct; ' +
  'UNVERIFIABLE only after a genuine not-found across <=2 attempts), and classification = "clean" ' +
  '(resolved cleanly, verified or genuinely-not-found), "transient-error" (a verification tool ' +
  'timed out / was unreachable -> NOT a real negative), or "ambiguous-mismatch" (title/author ' +
  'partially matched, needs another look). Echo back raw/id/type/topic. Put the actual tool call + ' +
  'result in sourceEvidence for every VERIFIED row. Return JSON per schema.\n' + NO_FAB +
  '\n\n---IDENTIFIERS---\n' + JSON.stringify(shard, null, 2)

const LOG_DIR = process.env.CITATION_AUDIT_LOG_DIR || './logs/citation-audit/'

const introspectPrompt = (summary) =>
  'INTROSPECTION / SELF-IMPROVEMENT step for the citation-audit workflow. The run did NOT fully meet ' +
  'its goal (some identifiers stayed unresolved due to tool errors, or a systematic failure occurred). ' +
  'Do two things, then stop:\n' +
  '1. Write a post-mortem to ' + LOG_DIR + RUN + '.md with: what failed, ' +
  'the most likely cause (e.g. PubMed/Crossref unreachable, schema drift, a specific identifier class), ' +
  'and a concrete proposed fix. Be specific and brief (AJRCCM voice, no hedging).\n' +
  '2. Append ONE row to ' + LOG_DIR + 'citation-audit-workflow.md ' +
  '(create the file with a header if missing) in the format: ' +
  '`| ' + RUN + ' | <trigger> | <proposed change> | <rationale> |`. PROPOSE ONLY — never edit the ' +
  'workflow script, any skill, or any agent definition.\n' + NO_FAB +
  '\n\n---RUN SUMMARY---\n' + JSON.stringify(summary, null, 2)

// ---------------------------------------------------------------------------
// Render the canonical audit block (deterministic — done in JS, not by an agent)
// matches clinical-citation-audit/SKILL.md output schema.
// ---------------------------------------------------------------------------
const renderAuditBlock = (verdicts) => {
  const total = verdicts.length
  const verified = verdicts.filter((v) => v.status === 'VERIFIED')
  const unresolved = verdicts.filter((v) => v.status !== 'VERIFIED')
  const toolErrors = verdicts.filter((v) => v.classification === 'transient-error')
  const lines = []
  lines.push('```')
  lines.push('citation-audit')
  lines.push('verified: ' + verified.length + ' of ' + total + ' citation identifiers')
  lines.push(
    'unverified (replace with [CITATION NEEDED]): ' +
      (unresolved.length ? unresolved.map((v) => v.id + (v.topic ? ' (' + v.topic + ')' : '')).join(', ') : 'none')
  )
  lines.push(
    'errors (verification tool itself failed): ' +
      (toolErrors.length ? toolErrors.map((v) => v.id + ' — ' + (v.error || 'tool error')).join('; ') : 'none')
  )
  lines.push('```')
  return {
    block: lines.join('\n'),
    json: {
      kind: 'CitationAudit',
      upstream_skill: IN.upstreamSkill || 'workflow',
      total,
      verified: verified.length,
      unverified: unresolved.map((v) => v.id),
      errors: toolErrors.map((v) => ({ identifier: v.id, tool_failure: v.error || 'tool error' })),
      audit_pass: toolErrors.length === 0 && unresolved.filter((v) => v.classification !== 'transient-error').length === 0,
    },
  }
}

// ===========================================================================
// Run
// ===========================================================================
phase('Extract')
let citations = IN.citations || null
if (!citations) {
  if (!IN.text) throw new Error('citation-audit: provide args.citations[] or args.text')
  // Extraction is mechanical (scan text, dedup identifiers) — sonnet at medium.
  // It must not MISS an identifier, so it does not drop to low.
  const ex = await agent(extractPrompt(IN.text), { label: 'extract', model: 'sonnet', effort: 'medium', schema: EXTRACT_SCHEMA })
  citations = ex.citations
}
if (!citations.length) {
  return { auditBlock: '```\ncitation-audit\nverified: 0 of 0 citation identifiers\n```', verified: 0, unverified: [], errors: 0 }
}
log(citations.length + ' unique identifiers across ' + chunk(citations, SHARD_SIZE).length + ' shards')

phase('Verify')
const verifyRaw = (
  await parallel(
    chunk(citations, SHARD_SIZE).map((sh, i) => () =>
      // Tiering agrees with citation-verifier.md (sonnet / medium). Verification here is
      // TOOL-GROUNDED — the verdict comes from a live PubMed/Crossref call, not from model
      // recall — so the tier governs how much reasoning wraps the lookup, never whether the
      // lookup happens. The no-transitive-trust + tool-evidence contract is unchanged.
      agent(verifyPrompt(sh, false), {
        label: 'verify:shard' + (i + 1),
        phase: 'Verify',
        agentType: 'citation-verifier',
        model: 'sonnet',
        effort: 'medium',
        schema: SHARD_SCHEMA,
      })
    )
  )
)
  .filter(Boolean)
  .flatMap((r) => r.verdicts)
// Reconcile: every input citation MUST have a verdict. A dropped shard (null) becomes a
// transient-error verdict so the repair loop retries it — never silently pass an unaudited citation.
const verifyById = new Map(verifyRaw.map((v) => [v.id, v]))
let verdicts = citations.map(
  (c) =>
    verifyById.get(c.id) || {
      raw: c.raw,
      id: c.id,
      type: c.type,
      topic: c.topic,
      status: 'UNVERIFIABLE',
      classification: 'transient-error',
      error: 'verify agent returned no result',
      attempts: 0,
    }
)

// ---- Self-improvement #1: bounded critic-repair loop (<=2 rounds) ----
phase('Repair')
let rounds = 0
const needsRepair = (vs) => vs.filter((v) => v.classification === 'transient-error' || v.classification === 'ambiguous-mismatch')
let pending = needsRepair(verdicts)
while (pending.length && rounds < 2) {
  rounds++
  log('repair round ' + rounds + ': re-verifying ' + pending.length + ' transient/ambiguous')
  const repaired = (
    await parallel(
      chunk(pending.map((v) => ({ raw: v.raw, id: v.id, type: v.type, topic: v.topic })), SHARD_SIZE).map((sh, i) => () =>
        agent(verifyPrompt(sh, true), {
          label: 'repair:r' + rounds + 's' + (i + 1),
          phase: 'Repair',
          agentType: 'citation-verifier',
          model: 'sonnet',
          effort: 'medium',
          schema: SHARD_SCHEMA,
        })
      )
    )
  )
    .filter(Boolean)
    .flatMap((r) => r.verdicts)
  const byId = new Map(repaired.map((v) => [v.id, v]))
  verdicts = verdicts.map((v) => byId.get(v.id) || v)
  pending = needsRepair(verdicts)
}

phase('Synthesize')
const audit = renderAuditBlock(verdicts)
const unresolved = verdicts.filter((v) => v.status !== 'VERIFIED')
const toolErrors = verdicts.filter((v) => v.classification === 'transient-error')
const goalMet = toolErrors.length === 0
log('verified ' + audit.json.verified + '/' + audit.json.total + ' | unresolved ' + unresolved.length + ' | tool-errors ' + toolErrors.length)

// ---- Self-improvement #2: introspection report (propose, never self-edit) ----
let introspectionPath = null
if (!goalMet && !EMBEDDED) {
  phase('Introspect')
  introspectionPath = (process.env.CITATION_AUDIT_LOG_DIR || './logs/citation-audit/') + RUN + '.md'
  await agent(
    introspectPrompt({ runId: RUN, total: audit.json.total, verified: audit.json.verified, unresolved: unresolved.map((v) => v.id), toolErrors: toolErrors.map((v) => ({ id: v.id, error: v.error })) }),
    // Root-cause diagnosis + a proposed fix — judgment, not extraction, and it feeds the
    // self-improvement log. opus at medium: the write path matters more than thinking volume.
    { label: 'introspect', phase: 'Introspect', model: 'opus', effort: 'medium' }
  )
}

return {
  auditBlock: audit.block,
  auditJson: audit.json,
  verified: audit.json.verified,
  unverified: audit.json.unverified,
  errors: toolErrors.length,
  goalMet,
  repairRounds: rounds,
  introspectionPath,
}
