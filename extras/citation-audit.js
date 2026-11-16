export const meta = {
  name: 'citation-audit',
  description: 'Deterministic citation-audit sweep — one citation-verifier agent runs verify_citations.py (the script fetches registry records and assigns every status), a second agent re-hashes the audit file and re-renders the block so the workflow can compare them in code, NEEDS-REVIEW records are sharded to reviewers whose decisions one agent applies serially, claim-support packets go to fresh reviewers and are gated by claim_packets.py check, and the citation-audit block is returned. Proposes fixes to the improvement log when the goal is not met.',
  whenToUse: 'Bulk citation verification on a manuscript / reference list / digest (>5 identifiers). Pass args {citations: [...ids]} or {text} (or {textPath}); embedded callers pass {embedded: true} and get no claim tier unless they pass {claims: true}.',
  phases: [
    { title: 'Verify' },
    { title: 'Review' },
    { title: 'Claims' },
    { title: 'Synthesize' },
    { title: 'Introspect' },
  ],
}

// ---------------------------------------------------------------------------
// Contracts carried verbatim into every spawned agent (hard rule).
// ---------------------------------------------------------------------------
const NO_FAB =
  'HARD RULE: Never fabricate or pattern-match a citation identifier ' +
  '(PMID/DOI/PMC/arXiv/NCT/URL) from training memory. Every identifier you report must come ' +
  'from a real tool call this run. Statuses come from verify_citations.py (OK, OK-REVIEWED, CORRECTED, ' +
  'NEEDS-REVIEW, WRONG-ID, MISMATCH, NOT-FOUND, RETRACTED, CONCERN, SUPERSEDED, UNVERIFIED); you never ' +
  'assign or upgrade one by assertion. A correct author/title/year with a fabricated PMID is worse than ' +
  'no PMID. Verify before claiming; state what you checked vs. inferred. Distinguish a genuine ' +
  '"not found" (NOT-FOUND) from a tool timeout/unreachable error (UNVERIFIED, reason transport), which ' +
  'is never a negative finding. Run the exact commands you are given and paste their output as given.'

// Tolerate args delivered as a JSON string (Workflow-tool / scheduled-task callers sometimes
// stringify), and never crash on absent args.
const IN = typeof args === 'string' ? (() => { try { return JSON.parse(args) } catch (e) { return {} } })() : (args || {})
const RUN = String(IN.runId || IN.asof || 'adhoc')
const SAFE = RUN.replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 80) || 'adhoc'
const EMBEDDED = !!IN.embedded // suppress introspection writeback when called by a parent workflow
const UPSTREAM = String(IN.upstreamSkill || 'workflow').replace(/[^A-Za-z0-9._-]+/g, '_')
// Claim tier: on for direct callers, off for embedded callers unless they ask for it; claims:false always wins.
const RUN_CLAIMS = IN.claims === false ? false : (EMBEDDED ? IN.claims === true : true)
const ENV = (typeof process !== 'undefined' && process.env) ? process.env : {}
const clean = (v) => String(v).replace(/["`$\\]/g, '')
const SCRIPTS = IN.scriptsDir ? clean(IN.scriptsDir).replace(/\/+$/, '') : (ENV.CITATION_SCRIPTS_DIR ? clean(ENV.CITATION_SCRIPTS_DIR).replace(/\/+$/, '') : '$HOME/.claude/skills/citation-verification/scripts')
const VERIFY = 'python3 ' + SCRIPTS + '/verify_citations.py'
const PACKETS = 'python3 ' + SCRIPTS + '/claim_packets.py'
const CLAIM_DOC = SCRIPTS + '/../references/claim-support.md'
const ROOT_SH = IN.logRoot ? clean(IN.logRoot) : (ENV.CITATION_AUDIT_LOG_DIR ? clean(ENV.CITATION_AUDIT_LOG_DIR).replace(/\/+$/, '') : './logs/citation-audit')
const LEDGER_ARG = IN.ledger ? ' --ledger ' + shq(String(IN.ledger)) : ''

const chunk = (arr, n) => {
  const out = []
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n))
  return out
}
function shq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" } // POSIX single-quote
const oneLine = (s, n) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n || 600)

// Pure-JS SHA-256 over the UTF-8 bytes of a string, so the workflow can compare the file an agent
// wrote against the content the workflow meant it to write. Returns null when the string cannot be encoded.
const sha256Hex = (str) => {
  let ascii
  try { ascii = unescape(encodeURIComponent(str)) } catch (e) { return null }
  const rightRotate = (v, a) => (v >>> a) | (v << (32 - a))
  const maxWord = Math.pow(2, 32)
  const words = []
  const bitLen = ascii.length * 8
  let hash = []
  const k = []
  let primeCounter = 0
  const isComposite = {}
  for (let candidate = 2; primeCounter < 64; candidate++) {
    if (!isComposite[candidate]) {
      for (let i = 0; i < 313; i += candidate) isComposite[i] = candidate
      hash[primeCounter] = (Math.pow(candidate, 0.5) * maxWord) | 0
      k[primeCounter++] = (Math.pow(candidate, 1 / 3) * maxWord) | 0
    }
  }
  ascii += '\x80'
  while ((ascii.length % 64) - 56) ascii += '\x00'
  for (let i = 0; i < ascii.length; i++) {
    const j = ascii.charCodeAt(i)
    words[i >> 2] |= j << (((3 - i) % 4) * 8)
  }
  words[words.length] = (bitLen / maxWord) | 0
  words[words.length] = bitLen
  for (let j = 0; j < words.length;) {
    const w = words.slice(j, (j += 16))
    const oldHash = hash
    hash = hash.slice(0, 8)
    for (let i = 0; i < 64; i++) {
      const w15 = w[i - 15]
      const w2 = w[i - 2]
      const a = hash[0]
      const e = hash[4]
      const temp1 = hash[7] + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25)) + ((e & hash[5]) ^ (~e & hash[6])) + k[i] +
        (w[i] = i < 16 ? w[i] : (w[i - 16] + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3)) + w[i - 7] + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))) | 0)
      const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22)) + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]))
      hash = [(temp1 + temp2) | 0].concat(hash)
      hash[4] = (hash[4] + temp1) | 0
    }
    for (let i = 0; i < 8; i++) hash[i] = (hash[i] + oldHash[i]) | 0
  }
  let result = ''
  for (let i = 0; i < 8; i++) for (let j = 3; j + 1; j--) { const b = (hash[i] >> (j * 8)) & 255; result += (b < 16 ? '0' : '') + b.toString(16) }
  return result
}
const hex64 = (s) => { const m = String(s || '').toLowerCase().match(/[0-9a-f]{64}/); return m ? m[0] : null }

// ---------------------------------------------------------------------------
// Input normalisation: legacy {id,type,raw,topic} objects, bare identifier strings (what
// embedding workflows pass), or the script's own {type,id,cited,claim,context}.
// ---------------------------------------------------------------------------
const entryFromString = (raw) => {
  const s = String(raw).trim()
  let m
  if ((m = s.match(/^(PMID|DOI|PMC|ARXIV|NCT|ISRCTN|EUDRACT|URL)\s*[:\s]\s*(\S.*)$/i))) return { type: m[1].toUpperCase(), id: m[2].trim() }
  if (/^10\.\d{4,9}\//.test(s)) return { type: 'DOI', id: s }
  if (/^PMC\d+$/i.test(s)) return { type: 'PMC', id: s }
  if (/^NCT\d{8}$/i.test(s)) return { type: 'NCT', id: s }
  if (/^\d{5,9}$/.test(s)) return { type: 'PMID', id: s }
  if (/^arxiv\.org\/abs\//i.test(s) || /^\d{4}\.\d{4,5}(v\d+)?$/.test(s)) return { type: 'ARXIV', id: s }
  if (/^https?:\/\//i.test(s)) return { type: 'URL', id: s }
  return { type: 'REF', cited: { title: s } }
}
const hasMetaIn = (c) => typeof c === 'object' && c !== null && !!((c.cited && Object.keys(c.cited).length) || c.claim || c.context)
const entryFromObject = (c, keepTopic) => {
  const type = String(c.type || '').toUpperCase() || (c.id ? entryFromString(c.id).type : 'REF')
  const e = { type }
  if (type === 'REF') {
    e.cited = Object.assign({}, c.cited || {})
    if (!e.cited.title) e.cited.title = String(c.raw || c.id || '')
  } else {
    e.id = String(c.id == null ? c.raw : c.id).trim()
    if (c.cited && Object.keys(c.cited).length) e.cited = c.cited
  }
  const ctx = c.context || (keepTopic ? c.topic : null)
  if (ctx) e.context = String(ctx)
  if (c.claim) e.claim = String(c.claim)
  return e
}
const toEntries = (list, keepTopic) => {
  const seen = new Set()
  const out = []
  const supplied = new Map() // id string written to the ids file -> the string the caller supplied
  let dropped = 0
  for (const c of list) {
    if (c == null || c === '') continue
    const e = typeof c === 'string' ? entryFromString(c) : entryFromObject(c, keepTopic)
    const sup = typeof c === 'string' ? c.trim() : String(c.id == null ? (c.raw == null ? '' : c.raw) : c.id)
    const rk = e.id || ('ref-' + out.length)
    if (!supplied.has(rk)) supplied.set(rk, sup)
    // An exact duplicate that carries no per-occurrence metadata is a caller artefact, not a reference-list finding.
    const bare = !e.cited && !e.claim && (typeof c === 'string' || !c.claim)
    const dk = e.type + '|' + String(e.id || (e.cited && e.cited.title) || '').toLowerCase()
    if (bare && seen.has(dk)) { dropped++; continue }
    if (bare) seen.add(dk)
    out.push(e)
  }
  return { entries: out, dropped, supplied }
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    runDir: { type: 'string', description: 'absolute run directory (cd DIR && pwd -P)' },
    auditPath: { type: 'string', description: 'absolute path of audit.json' },
    sha256: { type: 'string', description: 'shasum -a 256 of audit.json, 64 hex chars' },
    inputSha256: { type: 'string', description: 'shasum -a 256 of the ids/text file the audit read' },
    exitCode: { type: 'number', description: 'exit code of the LAST audit run (0 pass, 1 blocking records, 2 error)' },
    repairRounds: { type: 'number', description: 'times the audit command was re-run because errors[] listed transport failures (0-2)' },
    summaryJson: { type: 'string', description: 'the stdout of the summary command, verbatim, one JSON line' },
    error: { type: 'string', description: 'set only when you could not run the commands, e.g. no_shell_tool' },
    reason: { type: 'string', description: 'one sentence saying why, when error is set' },
  },
  required: [],
}

const TRUST_SCHEMA = {
  type: 'object',
  properties: {
    sha256: { type: 'string', description: 'shasum -a 256 of audit.json, 64 hex chars' },
    block: { type: 'string', description: 'stdout of verify_citations.py block, verbatim, including the code fence' },
    summaryJson: { type: 'string', description: 'stdout of the compact summary command, verbatim' },
  },
  required: ['sha256', 'block', 'summaryJson'],
}

const DECISION_ENUM = ['confirm', 'reject', 'superseded', 'unresolved']
const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    decisions: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: { type: 'string' },
          decision: { type: 'string', enum: DECISION_ENUM },
          note: { type: 'string', description: 'one or two sentences naming the evidence' },
          evidence: { type: 'string', description: 'the resolved-record fields and/or the tool result that decided it' },
          supersededBy: { type: 'string', description: 'newer edition; required when decision is superseded' },
        },
        required: ['key', 'decision', 'note', 'evidence'],
      },
    },
  },
  required: ['decisions'],
}

const APPLY_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: { key: { type: 'string' }, exitCode: { type: 'number' }, message: { type: 'string' } },
        required: ['key', 'exitCode'],
      },
    },
    sha256: { type: 'string', description: 'shasum -a 256 of audit.json AFTER the last review command' },
  },
  required: ['results', 'sha256'],
}

const PACKET_BUILD_SCHEMA = {
  type: 'object',
  properties: {
    packetsPath: { type: 'string' },
    exitCode: { type: 'number' },
    keys: { type: 'array', items: { type: 'string' }, description: 'key of every packet in packets.json (the packets array)' },
    notCheckable: { type: 'array', items: { type: 'string' }, description: 'keys in not_checkable (no abstract)' },
    skippedCount: { type: 'number' },
  },
  required: ['packetsPath', 'keys'],
}

const CHECK_ENUM = ['SUPPORTED', 'PARTIAL', 'NOT-IN-ABSTRACT', 'CONTRADICTED']
const CLAIM_REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: { type: 'string' },
          verdict: { type: 'string', enum: CHECK_ENUM },
          evidence_span: { type: 'string' },
          checks: {
            type: 'object',
            properties: {
              number: { type: 'string' }, direction: { type: 'string' }, population: { type: 'string' }, primary_vs_subgroup: { type: 'string' },
            },
            required: ['number', 'direction', 'population', 'primary_vs_subgroup'],
          },
          note: { type: 'string' },
        },
        required: ['key', 'verdict', 'evidence_span', 'checks', 'note'],
      },
    },
  },
  required: ['verdicts'],
}

const CLAIM_CHECK_SCHEMA = {
  type: 'object',
  properties: {
    verdictsSha256: { type: 'string', description: 'shasum -a 256 of verdicts.json as written' },
    exitCode: { type: 'number', description: 'exit code of claim_packets.py check (0 no blocking row, 1 blocking row)' },
    claimAuditJson: { type: 'string', description: 'the full contents of claim_audit.json, verbatim' },
  },
  required: ['verdictsSha256', 'exitCode', 'claimAuditJson'],
}

// ---------------------------------------------------------------------------
// Prompts. Every shell command a prompt asks for is built here, so an agent runs a command the
// workflow wrote, never one it composed.
// ---------------------------------------------------------------------------
// Prints the audit summary as one JSON line. 'compact' drops the NEEDS-REVIEW detail.
const SUMMARY_PY =
  "import json, sys\n" +
  "a = json.load(open(sys.argv[1], encoding='utf-8'))\n" +
  "compact = len(sys.argv) > 2 and sys.argv[2] == 'compact'\n" +
  "BLOCKING = {'NEEDS-REVIEW','WRONG-ID','MISMATCH','NOT-FOUND','RETRACTED','CONCERN','UNVERIFIED'}\n" +
  "recs = a.get('records', [])\n" +
  "def detail(r):\n" +
  "    x = r.get('resolved') or {}\n" +
  "    return {'key': r.get('key'), 'type': r.get('type'), 'status': r.get('status'), 'reason': r.get('reason'), 'note': r.get('note'), 'context': r.get('context'), 'claim': r.get('claim'), 'cited': r.get('cited') or {}, 'candidates': (r.get('candidates') or [])[:5], 'flags': r.get('flags') or [], 'resolved': {k: x.get(k) for k in ('title', 'first_author', 'year', 'journal', 'pmid', 'doi', 'has_abstract')}}\n" +
  "out = {'audit_id': a.get('audit_id'), 'schema_version': a.get('schema_version'), 'upstream_skill': a.get('upstream_skill'), 'total': a.get('total'), 'verified': a.get('verified'), 'counts': a.get('counts'), 'audit_pass': a.get('audit_pass'), 'blocking': [{'key': r['key'], 'status': r['status'], 'raw': r.get('raw')} for r in recs if r.get('status') in BLOCKING], 'needs_review_keys': [r['key'] for r in recs if r.get('status') == 'NEEDS-REVIEW'], 'author_year_only': [r['key'] for r in recs if 'author-year-only' in (r.get('flags') or [])], 'existence_only': [r['key'] for r in recs if 'existence-only' in (r.get('flags') or [])], 'errors': a.get('errors') or [], 'duplicate_findings': a.get('duplicate_findings') or []}\n" +
  "if not compact:\n" +
  "    out['needs_review'] = [detail(r) for r in recs if r.get('status') == 'NEEDS-REVIEW']\n" +
  "print(json.dumps(out, ensure_ascii=False))\n"
const summaryCmd = (auditSh, compact) => "python3 - " + auditSh + (compact ? ' compact' : '') + " <<'CA_SUMMARY_PY'\n" + SUMMARY_PY + "CA_SUMMARY_PY"

const EOFTAG = 'CITATION_AUDIT_EOF_9c41'
const writeFileCmd = (pathSh, content) => 'cat > ' + pathSh + " <<'" + EOFTAG + "'\n" + content.replace(/\n$/, '') + '\n' + EOFTAG

const verifyPrompt = (p) => {
  const dirSh = '"' + ROOT_SH + '/' + SAFE + '"'
  const f = (n) => '"' + ROOT_SH + '/' + SAFE + '/' + n + '"'
  const inputFlag = p.mode === 'ids' ? '--ids ' + f('ids.json') : '--text ' + (p.textPath ? shq(p.textPath) : f('input.txt'))
  const auditCmd = VERIFY + ' audit ' + inputFlag + ' --out ' + f('audit.json') + ' --raw-dir ' + f('raw') + ' --upstream-skill ' + shq(UPSTREAM) + LEDGER_ARG + (p.acceptBare ? ' --accept-bare' : '')
  const steps = []
  steps.push('1. Create the run directory:\n   mkdir -p ' + dirSh)
  if (p.writeCmd) steps.push('2. Write the input file EXACTLY as given (do not edit, wrap, re-order, or re-format it; the workflow compares its hash):\n' + p.writeCmd(f))
  else steps.push('2. The input file already exists at ' + p.textPath + '; do not copy or edit it.')
  steps.push('3. Hash the input file:\n   shasum -a 256 ' + (p.mode === 'ids' ? f('ids.json') : (p.textPath ? shq(p.textPath) : f('input.txt'))) + '\n   Report the 64-hex digest as inputSha256.')
  steps.push('4. Run the audit (repairRounds = 0 on this first run):\n   ' + auditCmd + '; echo "exit=$?"\n   Exit 0 means audit_pass true; exit 1 means the audit ran and found blocking records, which is a RESULT and not a failure; exit 2 is a usage or internal error (report it, do not retry blindly).')
  steps.push('5. Print the summary:\n   ' + summaryCmd(f('audit.json'), false) + '\n   Read the "errors" array. If it lists transport failures (a registry timed out or was unreachable), re-run the SAME audit command from step 4 once more (the ledger makes it cheap), then print the summary again. Do this at most 2 times in total; each re-run adds 1 to repairRounds. Never retry anything that is not in errors[]. Do not alter audit.json by hand.')
  steps.push('6. Final state (after the last audit run): shasum -a 256 ' + f('audit.json') + '\n   Return runDir as the absolute path from: cd ' + dirSh + ' && pwd -P\n   Return auditPath as that directory plus /audit.json, sha256 as the digest, exitCode as the exit code of the LAST audit run, and summaryJson as the stdout of the LAST summary command, pasted verbatim as one string.')
  return 'VERIFY STEP ONLY. You need a shell (Bash) tool for every step below. If you have none, run nothing and return only {"error":"no_shell_tool","reason":"<one sentence>"}. Run the deterministic verifier; do NOT do the review, claim-check, web-search, or cleaning steps of your usual procedure (the workflow runs those as separate steps). Do not run the review subcommand. You assign no status: the script does.\n\n' +
    steps.join('\n\n') + '\n\n' + NO_FAB + '\nTEST_MODE=true (skip the optimization web-scan).'
}

const trustPrompt = (auditAbs, tag) =>
  'INDEPENDENT TRUST CHECK (' + tag + '). You did not produce this audit. Run these two commands against the same file, and a third that summarises it, then return the outputs verbatim. Do not interpret, edit, repair, or re-run the audit.\n\n' +
  '1. shasum -a 256 ' + shq(auditAbs) + '\n   Return the 64-hex digest as sha256.\n\n' +
  '2. ' + VERIFY + ' block --audit ' + shq(auditAbs) + '\n   Return its complete stdout, including the opening and closing ``` fence, as block.\n\n' +
  '3. ' + summaryCmd(shq(auditAbs), true) + '\n   Return its stdout, verbatim as one string, as summaryJson.\n\n' + NO_FAB

const reviewPrompt = (shard, auditAbs) =>
  'REVIEW STEP, DECISIONS-ONLY. For each NEEDS-REVIEW record below, compare the resolved record with the citing sentence and return a decision. Do NOT run the review subcommand and do NOT edit audit.json: several reviewers read it at once, and one agent applies every decision afterwards. You need no shell and no script: the resolved metadata, the cited metadata and the citing sentence for every record are supplied below, and your tools for the guideline, edition and URL checks are the web tools. (For reference only, the audit file is ' + auditAbs + '; do not rely on reading it.)\n\n' +
  'Decision per record:\n' +
  '- confirm: the resolved record is the work the sentence intends (it becomes OK-REVIEWED).\n' +
  '- reject: the identifier resolves to a different work than the sentence describes (it becomes WRONG-ID).\n' +
  '- superseded: a named guideline whose edition is out of date (give supersededBy; it becomes SUPERSEDED).\n' +
  '- unresolved: the evidence does not decide it. Use this rather than guessing; a record left open stays blocking.\n' +
  'The note names the evidence: which resolved fields match or differ and what in the sentence decided it. A decision with no named evidence is invalid.\n\n' +
  'By record shape:\n' +
  '- Bare identifier (cited is empty, context present): confirm only when the resolved title and topic fit the sentence or topic in context. With no context or claim at all, nothing can be compared: return unresolved with the note "no citing sentence supplied".\n' +
  '- Author-year-only match (reason author-year-only): a swapped identifier sharing first author and year would pass, so compare the resolved title and journal with the full reference text in context; confirm only when they fit.\n' +
  '- Grey-band title (reason mentions title similarity): read both titles; confirm a same-work subtitle, translation or updated title; reject a different study.\n' +
  '- Guideline candidates: check PubMed first (the society\'s own publication by body and year), the issuing body\'s site second (WebFetch; retry once with mcp__parallel-search__web_fetch on a 4xx/5xx or bot wall), then the edition check (search for a newer edition; use superseded when one exists).\n' +
  '- URL content match: fetch the page and confirm it carries the content the sentence attributes to it; a 200 status alone confirms nothing.\n' +
  'Return one decision per key, no more and no fewer.\n\n' + NO_FAB + '\nTEST_MODE=true.\n\n---RECORDS---\n' + JSON.stringify(shard, null, 1)

const applyPrompt = (cmds, auditAbs) =>
  'APPLY REVIEW DECISIONS. Run the commands below one at a time, in order, in separate Bash calls (they edit one shared file, so never run them in parallel or chain them). Do not change a flag, note, or key, and do not run any other review command. After each, record its exit code (0 = applied) and the one-line message it printed. If a command exits non-zero, record it and continue with the next.\n\n' +
  cmds.map((c, i) => (i + 1) + '. ' + c).join('\n') + '\n\n' +
  'When all are done, run: shasum -a 256 ' + shq(auditAbs) + '\nReturn one result per command (key, exitCode, message) and the digest as sha256.\n\n' + NO_FAB

const packetBuildPrompt = (auditAbs, packetsAbs) =>
  'CLAIM PACKETS, BUILD ONLY. Run:\n' +
  PACKETS + ' build --audit ' + shq(auditAbs) + ' --raw-dir ' + shq(packetsAbs.replace(/\/packets\.json$/, '') + '/raw-claims') + (IN.fulltext ? ' --fulltext' : '') + ' --out ' + shq(packetsAbs) + '; echo "exit=$?"\n' +
  'Then print the packet keys with:\n' +
  "python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps({'keys':[p['key'] for p in d.get('packets',[])],'not_checkable':[(x['key'] if isinstance(x,dict) else x) for x in d.get('not_checkable',[])],'skipped':len(d.get('skipped',[]))}))\" " + shq(packetsAbs) + '\n' +
  'Return packetsPath (' + packetsAbs + '), exitCode, keys, notCheckable, and skippedCount from that output. Do not read the packets or judge any claim.\n\n' + NO_FAB

const claimReviewPrompt = (keys, packetsAbs) =>
  'CLAIM-SUPPORT REVIEW. You are a fresh reviewer with no context. Read ' + CLAIM_DOC + ' first and follow it exactly; it defines the four checks, the verdicts, the evidence-span rules and the output. You may open that file and the packets below and nothing else: do not open the manuscript or any other file, do not search the web, and do not use what you remember about a trial.\n\n' +
  'Print your packets (this prints only your keys):\n' +
  "python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); ks=set(sys.argv[2:]); print(json.dumps([p for p in d['packets'] if p['key'] in ks], indent=1, ensure_ascii=False))\" " + shq(packetsAbs) + ' ' + keys.map(shq).join(' ') + '\n\n' +
  'Copy every evidence_span character for character from the packet text (title + abstract + fulltext_excerpt); the script rejects a span it cannot find. Return one verdict per packet, same key, and nothing else. Use NOT-IN-ABSTRACT with an empty span when an element of the claim is absent; never use CONTRADICTED because a number is missing.\n\n' + NO_FAB

const claimCheckPrompt = (verdictsContent, runDirAbs) => {
  const vp = shq(runDirAbs + '/verdicts.json')
  return 'CLAIM CHECK. 1) Write this file EXACTLY as given (do not edit, wrap, re-order or re-format it; the workflow compares its hash):\n' +
    writeFileCmd(vp, verdictsContent) + '\n\n' +
    '2) shasum -a 256 ' + vp + '   (return as verdictsSha256)\n\n' +
    '3) ' + PACKETS + ' check --packets ' + shq(runDirAbs + '/packets.json') + ' --verdicts ' + vp + ' --out ' + shq(runDirAbs + '/claim_audit.json') + '; echo "exit=$?"\n' +
    '   Exit 0 means no blocking row; exit 1 means at least one CONTRADICTED or INVALID-SPAN row, which is a RESULT, not a failure. Return the exit code as exitCode.\n\n' +
    '4) cat ' + shq(runDirAbs + '/claim_audit.json') + '\n   Return its full contents, verbatim, as claimAuditJson. Do not judge, edit, or re-run any verdict.\n\n' + NO_FAB
}

const introspectPrompt = (pmPath, summary) =>
  'INTROSPECTION / SELF-IMPROVEMENT step for the citation-audit workflow. The run did NOT fully meet ' +
  'its goal (a trust check disagreed, some identifiers stayed unresolved due to transport errors, a review or claim step failed, or a systematic failure occurred). ' +
  'Resolve the working directory first (pwd) and use absolute paths. Do two things, then stop:\n' +
  '1. Write a post-mortem to ' + pmPath + ' with: what failed, ' +
  'the most likely cause (e.g. PubMed/Crossref unreachable, hash mismatch from an agent mis-copying input, schema drift, a specific identifier class), ' +
  'and a concrete proposed fix. Be specific and brief (AJRCCM voice, no hedging). The audit files are in ' + pmPath.replace(/\.md$/, '') + '/.\n' +
  '2. Append ONE row to ' + ROOT_SH + '/citation-audit-workflow.md ' +
  '(create the file with a header if missing) in the format: ' +
  '`| ' + RUN + ' | <trigger> | <proposed change> | <rationale> |`. PROPOSE ONLY — never edit the ' +
  'workflow script, any skill, or any agent definition.\n' + NO_FAB +
  '\n\n---RUN SUMMARY---\n' + JSON.stringify(summary, null, 2)

// ---------------------------------------------------------------------------
// Trust check: a second agent that did not run the audit re-hashes the file and re-renders the block.
// The comparison happens here, in code. The block is canonical only when everything agrees.
// ---------------------------------------------------------------------------
const parseJson = (s) => { try { return JSON.parse(String(s)) } catch (e) { return null } }
const blockFacts = (block) => {
  const v = String(block || '').match(/verified: (\d+) of (\d+) citations/)
  const p = String(block || '').match(/audit_pass: (true|false) · audit_id: (\S+)/)
  return { verified: v ? Number(v[1]) : null, total: v ? Number(v[2]) : null, auditPass: p ? p[1] === 'true' : null, auditId: p ? p[2] : null, hasFence: /^```/.test(String(block || '').trim()) && /```$/.test(String(block || '').trim()) }
}
const sameSummary = (a, b) =>
  !!a && !!b && a.audit_id === b.audit_id && a.total === b.total && a.verified === b.verified && a.audit_pass === b.audit_pass &&
  JSON.stringify(a.counts) === JSON.stringify(b.counts)

async function trustCheck(auditAbs, claimedSha, claimedSummary, tag) {
  const reasons = []
  let t = null
  try {
    t = await agent(trustPrompt(auditAbs, tag), { label: 'trust:' + tag, phase: tag === 'verify' ? 'Verify' : 'Review', agentType: 'general-purpose', model: 'sonnet', effort: 'low', schema: TRUST_SCHEMA })
  } catch (e) { reasons.push('trust agent threw: ' + (e && e.message)) }
  if (!t) { reasons.push('trust agent returned nothing'); return { ok: false, reasons, block: null, summary: null, sha: null } }
  const sha2 = hex64(t.sha256)
  const sha1 = hex64(claimedSha)
  if (!sha2) reasons.push('trust agent returned no valid sha256')
  else if (!sha1) reasons.push('first agent returned no valid sha256')
  else if (sha1 !== sha2) reasons.push('sha256 disagreement on audit.json: ' + sha1 + ' vs ' + sha2)
  const sum2 = parseJson(t.summaryJson)
  if (!sum2) reasons.push('trust summary is not valid JSON')
  const bf = blockFacts(t.block)
  if (!bf.hasFence || bf.total == null || bf.auditPass == null) reasons.push('block output is missing its fence or its verified / audit_pass lines')
  if (sum2 && bf.total != null && (bf.total !== sum2.total || bf.verified !== sum2.verified || bf.auditPass !== sum2.audit_pass || bf.auditId !== sum2.audit_id)) reasons.push('block lines disagree with the file summary (total ' + bf.total + '/' + sum2.total + ', verified ' + bf.verified + '/' + sum2.verified + ')')
  if (claimedSummary && sum2 && !sameSummary(claimedSummary, sum2)) reasons.push('first agent summary disagrees with trust summary (audit_id, counts, or audit_pass)')
  return { ok: reasons.length === 0, reasons, block: t.block, summary: sum2, sha: sha2 }
}

// ===========================================================================
// Run
// ===========================================================================
phase('Verify')
let entries = null
let mode = 'ids'
let droppedDup = 0
let acceptBare = false
let suppliedById = new Map()
const textInline = typeof IN.text === 'string' && IN.text.length ? IN.text : null
const textPath = typeof IN.textPath === 'string' && IN.textPath.length ? IN.textPath : null
if (textInline || textPath) mode = 'text'
else if (Array.isArray(IN.citations)) {
  // Legacy callers (strings, or {id,type,raw,topic}) carry no cited metadata, claim or context. A topic is a label, not a
  // citing sentence, so it is not sent to the script as context; that lets the script's --accept-bare flag apply.
  const anyMeta = IN.citations.some(hasMetaIn)
  acceptBare = !anyMeta && IN.bareIdPolicy !== 'leave-open'
  const norm = toEntries(IN.citations, !acceptBare)
  entries = norm.entries
  droppedDup = norm.dropped
  suppliedById = norm.supplied
} else throw new Error('citation-audit: provide args.citations[], args.text, or args.textPath')

if (mode === 'ids' && !entries.length) {
  return { auditBlock: '```\ncitation-audit\nverified: 0 of 0 citations (identifiers + named guideline references)\n```', verified: 0, unverified: [], unverifiedKeys: [], errors: 0, goalMet: true, repairRounds: 0, introspectionPath: null,
    auditJson: { kind: 'CitationAudit', upstream_skill: UPSTREAM, schema_version: 2, total: 0, verified: 0, unverified: [], errors: [], audit_pass: true, counts: {}, needs_review: [], audit_path: null, claim_audit: null } }
}
if (IN.shardSize) log('args.shardSize accepted for compatibility and ignored: the script batches existence checks, so they are not sharded across agents')
if (droppedDup) log(droppedDup + ' exact duplicate identifier(s) with no per-occurrence metadata were dropped before the audit')

// Input content the workflow wants on disk, and its hash.
let idsContent = null
let textContent = null
let writeCmd = null
let expectedInputSha = null
if (mode === 'ids') {
  idsContent = JSON.stringify(entries, null, 1) + '\n'
  expectedInputSha = sha256Hex(idsContent)
  writeCmd = (f) => writeFileCmd(f('ids.json'), idsContent)
} else if (textInline) {
  textContent = textInline.endsWith('\n') ? textInline : textInline + '\n'
  expectedInputSha = sha256Hex(textContent)
  writeCmd = (f) => writeFileCmd(f('input.txt'), textContent)
}
log(mode === 'ids' ? entries.length + ' identifiers; one verifier agent, existence checks batched by the script' : 'text input; the script extracts the identifiers')

const verifyArgs = { mode, textPath, writeCmd, acceptBare }
const dispatchVerify = async () => {
  try { return await agent(verifyPrompt(verifyArgs), { label: 'verify', phase: 'Verify', agentType: 'general-purpose', model: 'sonnet', effort: 'medium', schema: VERIFY_SCHEMA }) } catch (e) { log('verify agent threw: ' + (e && e.message)); return null }
}
let V = await dispatchVerify()
if (!V) { log('verify agent returned nothing; re-dispatching once (the ledger makes the re-run cheap)'); V = await dispatchVerify() }
if (!V) throw new Error('citation-audit: the verify agent returned nothing after 2 dispatches; no audit exists, so nothing can be reported as verified')

// Fail fast and legibly when the verifier never ran (no shell tool, or no input hash): skip the trust agent, which has nothing to check.
const noRunReason = V.error || /no[_ ]shell/i.test(String(V.reason || ''))
  ? oneLine((V.error || 'no_shell_tool') + (V.reason ? ': ' + V.reason : ''), 300)
  : (!hex64(V.inputSha256) ? 'verify agent returned no valid input sha256' : null)
if (noRunReason) {
  phase('Synthesize')
  log('VERIFIER DID NOT RUN: ' + noRunReason)
  const dnrFailures = ['VERIFIER DID NOT RUN: ' + noRunReason]
  let dnrIntro = null
  if (!EMBEDDED) {
    phase('Introspect')
    const root = typeof V.runDir === 'string' && V.runDir.charAt(0) === '/' ? V.runDir.replace(/\/+$/, '') : (ROOT_SH + '/' + SAFE)
    dnrIntro = root + '.md'
    await agent(introspectPrompt(dnrIntro, { runId: RUN, failures: dnrFailures, note: 'the verify agent could not run its shell commands; check that the dispatched agent definition has the Bash tool' }), { label: 'introspect', phase: 'Introspect', model: 'opus', effort: 'medium' })
  }
  return {
    auditBlock: '```\nVERIFIER DID NOT RUN: ' + noRunReason + '\naudit_pass: false\n```',
    auditJson: { kind: 'CitationAudit', schema_version: 2, upstream_skill: IN.upstreamSkill || 'workflow', total: 0, verified: 0, counts: null, unverified: [], unverifiedKeys: [], needs_review: [], errors: [], duplicate_findings: [], audit_pass: false, claim_audit: null, audit_path: null, trust: { ok: false, reasons: dnrFailures } },
    verified: 0, unverified: [], unverifiedKeys: [], errors: 0, goalMet: false, repairRounds: 0, introspectionPath: dnrIntro,
  }
}

const failures = [] // reasons the goal is not met; any entry makes goalMet false
const absOr = (p, fallback) => (typeof p === 'string' && p.charAt(0) === '/' ? p : fallback)
const runDirAbs = absOr(V.runDir, null) || (absOr(V.auditPath, '') ? V.auditPath.replace(/\/audit\.json$/, '') : null)
const auditAbs = absOr(V.auditPath, runDirAbs ? runDirAbs + '/audit.json' : null)
if (!auditAbs) throw new Error('citation-audit: the verify agent returned no absolute audit path')
const repairRounds = Math.max(0, Math.min(2, Math.floor(Number(V.repairRounds) || 0)))
const vSummary = parseJson(V.summaryJson)
if (!vSummary) failures.push('verify summary is not valid JSON')

// Input integrity: the agent wrote the ids/text file by copying it, so compare hashes in code.
const inputFail = []
if (expectedInputSha) {
  const got = hex64(V.inputSha256)
  if (!got) inputFail.push('verify agent returned no input sha256')
  else if (got !== expectedInputSha) inputFail.push('input file hash mismatch: the agent altered or truncated the file it audited (expected ' + expectedInputSha + ', got ' + got + ')')
}

let T = await trustCheck(auditAbs, V.sha256, vSummary, 'verify')
if (T.ok && mode === 'ids' && T.summary.total !== entries.length) inputFail.push('audit total ' + T.summary.total + ' differs from the ' + entries.length + ' identifiers submitted')
// Any integrity failure makes the file untrusted: no review decision is written to it and no claim packet is built from it.
if (inputFail.length) T = Object.assign({}, T, { ok: false, reasons: T.reasons.concat(inputFail) })
if (!T.ok) failures.push(...T.reasons.map((r) => 'trust(verify): ' + r))
log('verify: ' + (T.summary ? T.summary.verified + '/' + T.summary.total + ' verified, ' + (T.summary.errors || []).length + ' transport error(s), ' + repairRounds + ' repair round(s)' : 'no trusted summary') + (T.ok ? '' : ' | TRUST CHECK FAILED'))

// ---------------------------------------------------------------------------
phase('Review')
let decisionsAll = []
let applyFailed = []
if (!T.ok) {
  log('Review skipped: the audit file is not trusted, so no decision is written to it.')
} else {
  const sumByKey = new Map((vSummary && vSummary.needs_review ? vSummary.needs_review : []).map((r) => [r.key, r]))
  const trustedKeys = T.summary.needs_review_keys || []
  const missingDetail = trustedKeys.filter((k) => !sumByKey.has(k))
  if (missingDetail.length) failures.push('needs-review detail missing from the first agent summary for: ' + missingDetail.join(', '))
  const queue = trustedKeys.filter((k) => sumByKey.has(k)).map((k) => sumByKey.get(k))
  if (!queue.length) log('no NEEDS-REVIEW records')
  else {
    const REVIEW_SHARD = IN.reviewShardSize || Math.max(8, Math.ceil(queue.length / 40))
    const shards = chunk(queue, REVIEW_SHARD)
    log(queue.length + ' NEEDS-REVIEW record(s) across ' + shards.length + ' review shard(s)')
    const raw = await parallel(shards.map((sh, i) => () =>
      agent(reviewPrompt(sh, auditAbs), { label: 'review:shard' + (i + 1), phase: 'Review', agentType: 'citation-verifier', model: 'sonnet', effort: 'medium', schema: REVIEW_SCHEMA })
    ))
    // Reconcile by index against the dispatch roster; a dead or partial shard leaves its keys open, loudly.
    const valid = []
    shards.forEach((sh, i) => {
      const r = raw[i]
      const want = new Set(sh.map((x) => x.key))
      const got = new Map()
      if (r && Array.isArray(r.decisions)) {
        for (const d of r.decisions) {
          if (!d || !want.has(d.key) || got.has(d.key)) continue
          const needsSup = d.decision === 'superseded' && !oneLine(d.supersededBy)
          const noEvidence = d.decision !== 'unresolved' && (!oneLine(d.note) || !oneLine(d.evidence))
          if (DECISION_ENUM.indexOf(d.decision) === -1 || needsSup || noEvidence) { got.set(d.key, { key: d.key, decision: 'unresolved', note: 'invalid decision from reviewer: ' + oneLine(d.decision) + (needsSup ? ' (no supersededBy)' : '') + (noEvidence ? ' (no evidence named)' : ''), evidence: '' }); continue }
          got.set(d.key, d)
        }
      } else failures.push('review shard ' + (i + 1) + ' returned nothing; its ' + sh.length + ' record(s) stay NEEDS-REVIEW')
      sh.forEach((x) => {
        if (got.has(x.key)) valid.push(got.get(x.key))
        else { valid.push({ key: x.key, decision: 'unresolved', note: 'no decision returned', evidence: '' }) }
      })
    })
    decisionsAll = valid
    const toApply = valid.filter((d) => d.decision !== 'unresolved')
    log('review decisions: ' + toApply.length + ' to apply, ' + (valid.length - toApply.length) + ' left open')
    if (toApply.length) {
      const cmds = toApply.map((d) => VERIFY + ' review --audit ' + shq(auditAbs) + ' --key ' + shq(d.key) + ' --decision ' + d.decision +
        ' --note ' + shq(oneLine(d.note + ' [evidence: ' + d.evidence + ']', 700)) + (d.decision === 'superseded' ? ' --superseded-by ' + shq(oneLine(d.supersededBy, 300)) : '') + LEDGER_ARG)
      let A = null
      try { A = await agent(applyPrompt(cmds, auditAbs), { label: 'apply-review', phase: 'Review', agentType: 'general-purpose', model: 'sonnet', effort: 'low', schema: APPLY_SCHEMA }) } catch (e) { log('apply agent threw: ' + (e && e.message)) }
      if (!A) {
        failures.push('apply-review agent returned nothing; decisions may or may not have been written')
        applyFailed = toApply.map((d) => d.key)
      } else {
        const byKey = new Map((A.results || []).map((r) => [r.key, r]))
        applyFailed = toApply.filter((d) => !byKey.has(d.key) || byKey.get(d.key).exitCode !== 0).map((d) => d.key)
        if (applyFailed.length) failures.push('review command failed or unreported for: ' + applyFailed.join(', '))
      }
      // The file changed, so the trust check runs again against the post-review hash.
      const T2 = await trustCheck(auditAbs, A ? A.sha256 : null, null, 'review')
      if (!T2.ok) failures.push(...T2.reasons.map((r) => 'trust(review): ' + r))
      else {
        const stillOpen = toApply.filter((d) => applyFailed.indexOf(d.key) === -1 && (T2.summary.needs_review_keys || []).indexOf(d.key) !== -1)
        if (stillOpen.length) failures.push('applied decisions did not clear NEEDS-REVIEW for: ' + stillOpen.map((d) => d.key).join(', '))
      }
      T = T2
    }
  }
}

// ---------------------------------------------------------------------------
phase('Claims')
let claimAudit = null
let claimsRan = false
if (!RUN_CLAIMS) log('claim tier off (' + (EMBEDDED ? 'embedded caller, args.claims not true' : 'args.claims === false') + ')')
else if (!T.ok) log('claim tier skipped: the audit file is not trusted')
else {
  const packetsAbs = (runDirAbs || auditAbs.replace(/\/audit\.json$/, '')) + '/packets.json'
  const runAbs = packetsAbs.replace(/\/packets\.json$/, '')
  let P = null
  try { P = await agent(packetBuildPrompt(auditAbs, packetsAbs), { label: 'claims:build', phase: 'Claims', agentType: 'general-purpose', model: 'sonnet', effort: 'low', schema: PACKET_BUILD_SCHEMA }) } catch (e) { log('packet build threw: ' + (e && e.message)) }
  if (!P) failures.push('claim packet build returned nothing')
  else if (!P.keys || !P.keys.length) log('no claim packets (no verified record carries a checkable assertion, or none has an abstract)' + ((P.notCheckable || []).length ? '; not checkable: ' + P.notCheckable.join(', ') : ''))
  else {
    claimsRan = true
    const CLAIM_SHARD = IN.claimShardSize || 8
    const kshards = chunk(P.keys, CLAIM_SHARD)
    log(P.keys.length + ' claim packet(s) across ' + kshards.length + ' fresh reviewer(s)')
    const rawC = await parallel(kshards.map((ks, i) => () =>
      agent(claimReviewPrompt(ks, packetsAbs), { label: 'claims:review' + (i + 1), phase: 'Claims', agentType: 'general-purpose', model: 'opus', effort: 'medium', schema: CLAIM_REVIEW_SCHEMA })
    ))
    const verdicts = []
    kshards.forEach((ks, i) => {
      const r = rawC[i]
      if (!r || !Array.isArray(r.verdicts)) { failures.push('claim review shard ' + (i + 1) + ' returned nothing; its packets are not checked'); return }
      const want = new Set(ks)
      const seen = new Set()
      for (const v of r.verdicts) { if (v && want.has(v.key) && !seen.has(v.key)) { seen.add(v.key); verdicts.push(v) } }
    })
    if (!verdicts.length) failures.push('no claim verdicts returned; claim tier not checked')
    else {
      const vContent = JSON.stringify(verdicts, null, 1) + '\n'
      const vSha = sha256Hex(vContent)
      let C = null
      try { C = await agent(claimCheckPrompt(vContent, runAbs), { label: 'claims:check', phase: 'Claims', agentType: 'general-purpose', model: 'sonnet', effort: 'low', schema: CLAIM_CHECK_SCHEMA }) } catch (e) { log('claim check threw: ' + (e && e.message)) }
      if (!C) failures.push('claim check agent returned nothing')
      else {
        if (vSha && hex64(C.verdictsSha256) !== vSha) failures.push('verdicts.json hash mismatch: the agent altered the verdicts it wrote (expected ' + vSha + ', got ' + hex64(C.verdictsSha256) + ')')
        claimAudit = parseJson(C.claimAuditJson)
        if (!claimAudit) failures.push('claim_audit.json is not valid JSON')
        else claimAudit.exit_code = C.exitCode
      }
    }
  }
}

// ---------------------------------------------------------------------------
phase('Synthesize')
const S = T.summary // the independent agent's reading of the final audit.json, or null when no trusted file exists
const trusted = T.ok && !!S
const blocking = S && Array.isArray(S.blocking) ? S.blocking : []
const unverifiedKeys = blocking.map((b) => b.key)
// Legacy callers compare `unverified` with the identifier strings they supplied; map each key back through the record's raw id.
const unverifiedIds = blocking.map((b) => (suppliedById.get(b.raw) || b.raw || b.key))
const errList = S && Array.isArray(S.errors) ? S.errors : []
if (errList.length) failures.push(errList.length + ' transport error(s) remain after ' + repairRounds + ' repair round(s): ' + errList.map((e) => (e.key || '?') + '/' + (e.source || '?')).join(', '))
const goalMet = trusted && failures.length === 0

const auditBlock = trusted
  ? T.block
  : '```\ncitation-audit\nTRUST CHECK FAILED: ' + (failures[0] || 'no trusted audit file') + '\naudit_pass: false\n```'
const auditJson = {
  kind: 'CitationAudit',
  schema_version: S ? S.schema_version : 2,
  upstream_skill: IN.upstreamSkill || 'workflow',
  audit_id: S ? S.audit_id : null,
  total: S ? S.total : 0,
  verified: S ? S.verified : 0,
  counts: S ? S.counts : null,
  unverified: unverifiedIds,
  unverifiedKeys,
  existence_only: S ? (S.existence_only || []) : [],
  needs_review: S ? (S.needs_review_keys || []) : [],
  author_year_only: S ? (S.author_year_only || []) : [],
  errors: errList.map((e) => ({ identifier: e.key, tool_failure: (e.source && String(e.error || '').indexOf(e.source) !== 0 ? e.source + ': ' : '') + (e.error || 'tool error'), key: e.key, source: e.source, error: e.error })),
  duplicate_findings: S ? (S.duplicate_findings || []) : [],
  audit_pass: trusted ? !!S.audit_pass : false,
  claim_pass: claimAudit ? (claimAudit.claim_pass === undefined ? null : claimAudit.claim_pass) : null,
  audit_path: auditAbs,
  audit_sha256: T.sha,
  review: decisionsAll.map((d) => ({ key: d.key, decision: d.decision, note: d.note })),
  claim_audit: claimAudit,
  trust: { ok: T.ok, reasons: T.reasons },
}
log('verified ' + auditJson.verified + '/' + auditJson.total + ' | blocking ' + unverifiedKeys.length + ' | transport errors ' + errList.length + ' | goalMet ' + goalMet + (claimsRan ? ' | claims run' : ''))

// ---- Self-improvement: introspection report (propose, never self-edit) ----
let introspectionPath = null
if (!goalMet && !EMBEDDED) {
  phase('Introspect')
  introspectionPath = (runDirAbs || auditAbs.replace(/\/audit\.json$/, '')).replace(/\/+$/, '') + '.md'
  await agent(
    introspectPrompt(introspectionPath, { runId: RUN, total: auditJson.total, verified: auditJson.verified, failures, unresolved: unverifiedKeys, transportErrors: auditJson.errors, repairRounds, trustOk: T.ok }),
    // Root-cause diagnosis + a proposed fix — judgment, not extraction, and it feeds the
    // self-improvement log. opus at medium: the write path matters more than thinking volume.
    { label: 'introspect', phase: 'Introspect', model: 'opus', effort: 'medium' }
  )
}

return {
  auditBlock,
  auditJson,
  verified: auditJson.verified,
  unverified: auditJson.unverified,
  unverifiedKeys,
  errors: errList.length,
  goalMet,
  repairRounds,
  introspectionPath,
}
