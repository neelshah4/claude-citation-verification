---
name: citation-verification
description: Verify every academic citation and reference in real time. Triggers whenever Claude cites a paper, recommends a reference, quotes evidence, or produces output containing author-year citations, DOIs, PMIDs, or journal references — manuscripts, grants, abstracts, literature reviews, clinical consults with cited evidence, rebuttal letters, teaching materials, any response where a specific study is named. Scope is academic citations ONLY — for non-academic verifiable specifics (jurisdictions, fees, addresses, free-standing drug doses, distances, prices, sports/finance specifics), fabrication-audit handles those. The two skills compose. Runs as silent verification pass — no separate output unless verification fails or user requests audit. Never fabricate a citation. If unverifiable, mark [CITATION NEEDED]. Err heavily on triggering — any time a paper is mentioned by name, this skill should be active.
lastReviewed: 2026-06-04
---

# Citation Verification

Verify every academic reference in real time. Never let a fabricated, misattributed, or metadata-wrong
citation reach the user.

## Scope (added clarification)

This skill covers **academic citations only**:
- PMIDs, DOIs, paper titles, author names, journal references
- Author-year citations in manuscripts, grants, abstracts, literature reviews
- Specific studies cited as evidence in clinical consults
- Bibliographies and reference lists

This skill does **NOT** cover non-academic verifiable specifics. The `fabrication-audit` skill handles:
- Jurisdictions (which office/court/agency covers a place)
- Government fees, processing times, form versions
- Addresses, hours, drive times, distances
- Drug doses (when not tied to a specific cited paper)
- Sports specifics (cap hits, salaries, stats)
- Finance specifics (ETF expense ratios, prices, yields)
- Current officeholders, policy effective dates

The two skills compose. If a response contains both academic citations and operational specifics,
both skills run their respective audits. Neither replaces the other.

## Philosophy

AI-generated citations have a high error rate — wrong authors, wrong years, non-existent
papers, misattributed claims. This skill exists to ensure that every citation Claude produces
is verified against a real source before it appears in output. The cost of a single fabricated
reference in a submitted manuscript or grant is career-damaging. Verification is non-negotiable.

**CRITICAL LESSON (April 2026 incident):** PMIDs are the HIGHEST-RISK field for hallucination.
In a real manuscript workflow, 4 out of 12 PMIDs were fabricated — each was a plausible-looking
number that resolved to a completely unrelated paper (ophthalmology, rheumatology, veterinary
medicine, psychiatry). The citation text (authors, title, journal, year) was correct in all
cases, but the PMIDs were invented. A validation subagent then confabulated a report claiming
"all references verified" without actually querying PubMed. Both failures are addressed below.

## When to Trigger

**DEFAULT: ON for any output containing references.** This is a background verification
layer, not a standalone task.

### Always active for:
- Manuscripts, abstracts, grant narratives, specific aims pages
- Literature reviews, digests, article summaries
- Clinical consults where specific studies are cited as evidence
- Rebuttal letters referencing published work
- Teaching materials, board review content citing sources
- Emails or memos that reference specific papers
- Any response where a study is named (author, year, journal, or title)

### Skip for:
- General physiology explanations without specific study citations
- Casual conversation
- Code, data analysis, or technical tasks with no references
- When user explicitly says "don't worry about verifying references"
- **Non-academic specifics** (those go to `fabrication-audit`)

### Also trigger on explicit requests:
- "Verify these references," "check my citations," "audit my bibliography"
- "Are these real papers?" "Did I cite this correctly?"

## Core Principle

**Never generate a citation from memory alone.** Every reference must be verified through
search before inclusion. If verification fails, mark `[CITATION NEEDED]` and tell the user.

## Verification Workflow

### During Writing (Inline Verification)

When Claude needs to cite a paper during any writing task:

```
1. SEARCH   → PubMed MCP for the paper (title + first author); for non-PubMed-indexed work fall back to OpenAlex / Semantic Scholar, then generic web search
2. CONFIRM  → Verify the paper exists and metadata matches
3. PMID/DOI → Look up PMID via get_article_metadata — NEVER generate from memory
4. CHECK    → Confirm the claim being cited actually appears in that paper
5. FORMAT   → Use verified metadata for the citation
6. CITE     → Include in output with correct author, year, journal, PMID/DOI
```

This loop runs for EVERY citation, every time. No exceptions.

### PMID and DOI Verification (MANDATORY — added after April 2026 incident)

**PMIDs MUST be verified by tool call.** This is the single highest-risk failure mode in
AI-generated references. The pattern is: title, authors, journal, and year are all correct
(retrieved from training data), but the PMID is a hallucinated number that resolves to a
completely unrelated paper.

**REQUIRED WORKFLOW for every PMID:**
1. Search PubMed MCP by title + first author to find the paper
2. Extract the PMID from the search result (NOT from memory)
3. Call `get_article_metadata` with that PMID to confirm the returned title/authors match
4. If the MCP returns a different paper → the PMID is wrong → search by title instead
5. If no PMID can be confirmed after 2 attempts → omit PMID and include DOI only, or mark `[PMID NEEDED]`

**NEVER DO:**
- Generate a PMID from memory or training data — they are almost always wrong
- Claim you "verified" a PMID without an actual `get_article_metadata` tool call
- Trust a subagent's verification report unless it includes tool call evidence (see Multi-Agent section)
- Assume a plausible-looking 8-digit number is correct because the rest of the citation is right

**FOR DOIs:**
- If a DOI is included, verify it resolves to the correct paper via web search or PubMed
- DOIs are slightly more reliable than PMIDs but still warrant verification for new/recent papers

**FOR arXiv IDs:**
- Verify by fetching the canonical resolver `https://arxiv.org/abs/<id>` (must return 200) and confirm title + first author match the citation. A 404 → the ID is wrong → search by title (OpenAlex/Semantic Scholar) and correct or mark `[CITATION NEEDED]`.
- If the preprint was later published, prefer the published DOI/PMID but keep the arXiv ID only if it resolves.

**RETRACTION CHECK (every citation, all identifier types):**
- A citation can resolve with perfectly valid metadata yet be **retracted** — citing it is a hard error, not a pass. When the PubMed record's publication type includes "Retracted Publication" / a retraction notice (or a Retraction Watch / publisher retraction banner surfaces on the DOI page), do NOT silently pass it. Flag inline: `[RETRACTED — <topic>; verify before citing]` and surface to the user. Resolution alone is necessary but not sufficient.

### Metadata That Must Match

For a citation to pass verification, these must be confirmed:

| Field | Tolerance |
|-------|-----------|
| **Title** | Minor capitalization differences OK; substantive word changes = wrong paper |
| **First author** | Must match exactly |
| **Year** | ±1 year allowed (preprint → publication lag) |
| **Journal/Conference** | Must match (abbreviated or full name both acceptable) |
| **PMID** | MUST be verified via PubMed MCP tool call — NEVER from memory |
| **DOI** | If provided, must resolve to the correct paper |
| **Non-PubMed source (OpenAlex / Semantic Scholar)** | Used only for non-PubMed-indexed refs; confirms existence + metadata (title + first author + year). DOI/PMID lookups are exact; title-search hits require first-author+year match — do not accept a top-hit on title alone. |

### Claim Verification

When citing a paper for a specific claim (e.g., "mortality was 30%"), the claim
must be traceable to that paper. Steps:

1. Search for the paper's abstract or full text
2. Confirm the specific finding appears
3. If the claim is a secondary finding or subgroup result, note this context
4. If the claim cannot be confirmed from available text, write: "Author et al.
   reported [claim] (exact finding not confirmed from abstract; verify in full text)"

## Verification by Context

### Manuscripts and Grants
- Every reference in the bibliography must be verifiable
- Run verification during drafting, not as a post-hoc step
- Flag any reference that fails verification with `[CITATION NEEDED]`
- For literature review sections: verify at least title + first author + year + journal
  for every cited paper
- **Every PMID must be confirmed via PubMed MCP before the document is delivered**

### Clinical Consults
- When citing evidence to support a clinical recommendation, verify the paper exists
  and the finding is accurately represented
- Acceptable shorthand: "A 2023 PCCM study by Shah et al." — but author, year, and
  journal must be confirmed
- For well-known landmark trials (ARDSNet, PROSEVA, TTM2, etc.), verify the citation
  is attributed to the correct trial and correct outcome
- **Drug doses cited within a clinical consult**: if tied to a cited paper, this skill
  verifies the citation; if free-standing, `fabrication-audit` handles dose verification

### Literature Digests and Summaries
- Every article link and title must be confirmed via search
- Titles must match the actual publication exactly (check capitalization, subtitles)
- Journal abbreviations must follow Medline style

### Rebuttal Letters
- When citing new references in a rebuttal, apply full verification
- When re-citing papers already in the manuscript, confirm they are in the bibliography
  and the cited finding is correct

## Handling Verification Failures

### Paper not found
1. Try alternate search queries (different keyword combinations, DOI if available)
2. Try PubMed MCP first; then, for non-PubMed-indexed work (CS / AI / informatics / engineering / preprint), **OpenAlex** (`https://api.openalex.org/works/doi:<doi>` exact, or `?filter=title.search:<title>&mailto=your-email@example.com`) and **Semantic Scholar** (`https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>?fields=title,year,authors,externalIds` — `externalIds.PubMed` can recover a missing PMID; DOI endpoint is the reliable S2 path, search endpoint is rate-limited, retry once); then web search / Google Scholar. On any **title-search** hit, confirm first author + year before accepting — relevance ranking can surface a same-title decoy (observed: a 2017 paper's title returned a 2025 work as top hit). These fallbacks widen recall best-effort, not guaranteed; non-resolution still falls through to `[CITATION NEEDED]`.
3. If still not found after 2-3 search attempts: mark `[CITATION NEEDED]` and notify user
4. Never guess or approximate — if it can't be found, it doesn't get cited

### PMID not found or mismatched
1. Search PubMed by title + first author
2. If the paper is found but under a different PMID → use the correct PMID from search results
3. If the paper is found but has no PMID (e.g., preprint, non-indexed journal) → omit PMID, use DOI
4. If the PMID resolves to a different paper → the PMID is WRONG — discard it entirely
5. NEVER keep a PMID that resolves to the wrong paper

### Metadata mismatch
1. Confirm you found the correct paper (not a similarly titled one)
2. Check for preprint vs. published version discrepancies
3. Use the most current/accurate version's metadata
4. Note discrepancies to user if significant (e.g., different author order)

### Claim not confirmed
1. Check if the claim appears in the abstract
2. If not in abstract, note: "Finding cited from [Author et al., Year]; not confirmed
   from abstract — verify against full text"
3. Never fabricate page numbers or section references

## Execution mode — dispatch the verifier agent (preferred) vs. inline (fallback)

**If the `Agent`/`Task` tool is available, dispatch the `citation-verifier` subagent** (in `this plugin's `agents/` directory` for Claude Code, `.claude/agents/` for Cowork) to run the verification workflow in its own context, and require it to return the actual tool-call evidence for every VERIFIED identifier. The parent then spot-checks ≥30% of returned PMIDs by an independent `get_article_metadata` call (per the guardrails below); if any spot-check fails, re-verify all. **If the tool is unavailable, run the verification workflow inline** in this context, unchanged. Either way the workflow above is authoritative; the agent is built directly from it.

## Multi-Agent Citation Workflows (MANDATORY — added after April 2026 incident)

When citation tasks are delegated to subagents (research agents, validation agents, etc.):

### The Confabulation Problem
Subagents can and do confabulate verification reports. A subagent that claims "all 12
references verified" without tool call evidence has verified nothing. This is the exact
failure mode that produced 4 wrong PMIDs in a real manuscript.

### Required Guardrails
1. **Tool call evidence required:** Any subagent performing citation verification MUST
   include in its output the actual PubMed MCP tool calls it made (search queries,
   `get_article_metadata` calls, returned PMIDs). A narrative-only report is NOT verification.
2. **Parent agent spot-check:** After receiving a subagent's verification report, the parent
   agent MUST re-verify at least 30% of PMIDs by calling `get_article_metadata` directly.
   If ANY PMID fails spot-check, re-verify ALL PMIDs before delivering to user.
3. **No transitive trust:** Do not trust a subagent's claim that it "searched PubMed" or
   "confirmed via MCP." Verify independently. The subagent may have confabulated the entire
   search process.
4. **Final gate:** Before any document with PMIDs is delivered to the user, the delivering
   agent must have direct tool-call evidence for every PMID in the document. No exceptions.

### Prompt Template for Verification Subagents
When spawning a citation verification subagent, include this instruction:
```
For EVERY PMID you include, you MUST:
1. Call get_article_metadata with that PMID
2. Confirm the returned title matches the intended paper
3. Include the tool call result in your output
If you cannot call the PubMed MCP, mark every PMID as [PMID UNVERIFIED].
Do NOT generate PMIDs from memory under any circumstances.
```

## Output Format (When Verification Audit Is Requested)

If the user explicitly asks to verify or audit citations, produce:

```
CITATION VERIFICATION REPORT

Verified: [X] / [Total]
Flagged:  [Y] (metadata issues or unconfirmed claims)
Failed:   [Z] (not found)

| # | Citation | Status | PMID Verified | Issue (if any) |
|---|----------|--------|---------------|----------------|
| 1 | Shah et al., PCCM 2024 | ✓ Verified | ✓ via MCP | — |
| 2 | Jones et al., CCM 2023 | ⚠ Flagged | ✗ Wrong paper | PMID resolves to unrelated article |
| 3 | Smith et al., JAMA 2025 | ✗ Not found | — | No matching paper after 3 searches |
```

## Integration with Other Skills

- **fabrication-audit**: handles non-academic verifiable specifics (fees, jurisdictions,
  addresses, doses-not-tied-to-citation, sports/finance specifics). The two skills compose;
  scopes don't overlap.
- **clinical-citation-audit**: the pre+post-flight GATE that wraps this skill for the six
  clinical-writing skills (manuscript-reviewer, scientific-writing, literature-review,
  clinical-reports, clinical-evidence-debate, clinical-teaching-deck). That skill enforces; this skill provides the
  verification methodology it runs. When invoked from the clinical pipeline, defer block
  formatting to clinical-citation-audit's audit summary.
- **manuscript-reviewer**: When Agent 6 (Tables/Figures) or Agent 5 (Literature) flags
  citation concerns, this skill provides the verification methodology
- **review-response**: When new references are added during revision, verify each one
- **literature-review**: Every paper included in a systematic or narrative review must
  pass verification
- **icu-clinical-consult**: When citing evidence for clinical recommendations, verify
  the cited study exists and the finding is accurate. Free-standing doses (no paper cited)
  go to `fabrication-audit`.
- **interesting-articles-formatting**: Verify every article title and journal name
  before formatting into the digest
- **writing-anti-ai**: Vague attributions ("Experts believe...") flagged by anti-AI
  skill should be replaced with verified specific citations

## What This Skill Does NOT Do

- It does not require the user to request verification — it is always on
- It does not fabricate "verification" of papers it cannot actually confirm
- It does not override the user's citation choices — it flags problems, user decides
- It does not verify references the user provides verbatim (assumes user has the paper) —
  but will flag obvious metadata errors if noticed
- It does NOT trust subagent verification reports at face value — see Multi-Agent section
- **It does NOT cover non-academic verifiable specifics** — those go to `fabrication-audit`

## Self-improvement
Found a missed edge case, a wrong-shaped output, or a rule that misfires?
Open an issue on this plugin's repository with the input and the output you
expected. Do not edit this skill mid-run.

## Versions

- 2026-08-29 — Added the `## Self-improvement` section.
- **2026-06-04** — Best-practices pass (Anthropic "how we use skills"): added
  `lastReviewed`; added Versions section; added clinical-citation-audit composition cross-
  ref. No trigger phrases, no verification workflow, and no output contract changed. The
  April 2026 PMID-incident content is unchanged.
