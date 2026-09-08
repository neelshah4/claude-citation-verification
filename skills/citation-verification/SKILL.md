---
name: citation-verification
description: Verify every academic citation and reference in real time. Triggers whenever Claude cites a paper, recommends a reference, quotes evidence, or produces output containing author-year citations, DOIs, PMIDs, or journal references — manuscripts, grants, abstracts, literature reviews, clinical consults with cited evidence, rebuttal letters, teaching materials, any response where a specific study is named. Scope is academic citations ONLY — for non-academic verifiable specifics (jurisdictions, fees, addresses, free-standing drug doses, distances, prices, sports/finance specifics), fabrication-audit handles those. The two skills compose. Runs as silent verification pass — no separate output unless verification fails or user requests audit. Never fabricate a citation. If unverifiable, mark [CITATION NEEDED]. Err heavily on triggering — any time a paper is mentioned by name, this skill should be active.
lastReviewed: 2026-09-08
---
# Citation Verification

Never let a fabricated, misattributed, or metadata-wrong citation reach the user.

## Scope

Covers **academic citations only**: PMIDs, DOIs, PMC IDs, arXiv IDs, NCT numbers, paper titles, author
names, journal references, author-year citations, and bibliographies.

Does **NOT** cover non-academic verifiable specifics (jurisdictions, fees, addresses, drug doses not tied
to a cited paper, sports/finance specifics, officeholders, policy dates) — `fabrication-audit` handles
those; see Integration below.

## When to Trigger

**DEFAULT: ON for any output containing references.** This is a background verification layer, not a
standalone task.

Always active for: manuscripts, abstracts, grant narratives, aims pages; literature reviews, digests,
article summaries; clinical consults citing specific studies as evidence; rebuttal letters; teaching or
board-review content citing sources; emails/memos referencing specific papers; any response naming a study
(author, year, journal, or title).

Skip for: general physiology explanations with no specific study cited; casual conversation; code/data
tasks with no references; user says "don't worry about verifying references"; non-academic specifics
(route to `fabrication-audit`).

Also trigger on explicit requests: "verify these references," "check my citations," "audit my
bibliography," "are these real papers?," "did I cite this correctly?"

## Core Principle

Never generate a citation from memory alone; every reference is verified through search before inclusion.
Failure after the attempt limits below → `[CITATION NEEDED]`, told to the user.

## Verification Workflow

### During Writing (Inline Verification)

1. SEARCH → PubMed MCP (title + first author); non-PubMed-indexed work falls back to OpenAlex / Semantic
   Scholar, then web search.
2. CONFIRM → the paper exists and metadata matches.
3. PMID/DOI → look up via `get_article_metadata`, never from memory.
4. CHECK → the cited claim actually appears in that paper.
5. FORMAT/CITE → use the verified metadata (author, year, journal, PMID/DOI) in the output.

This loop runs for every citation.

### PMID Verification

PMIDs are the highest-risk hallucination field: other fields can be correct while the PMID resolves to an
unrelated paper (`references/history.md`). Required workflow for every PMID:

1. Search PubMed MCP by title + first author.
2. Extract the PMID from the result, not from memory.
3. Call `get_article_metadata` with that PMID to confirm the returned title/authors match.
4. A different returned paper means the PMID is wrong; search by title instead.
5. No PMID confirmed after 2 attempts → omit it and use the DOI only, or mark `[PMID NEEDED]`.

Never generate a PMID from memory, and never claim one is "verified" without the tool call.

### DOIs

Verify a DOI resolves to the correct paper via web search or PubMed; more reliable than PMIDs but still
warrants a check for new or recent papers.

### arXiv IDs

Fetch `https://arxiv.org/abs/<id>` (must return 200) and confirm title + first author match. A 404 means
the ID is wrong: search by title (OpenAlex/Semantic Scholar) and correct it, or mark `[CITATION NEEDED]`.
If later published, prefer the published DOI/PMID and keep the arXiv ID only if it still resolves.

### Retraction / Erratum Check

A citation can resolve with valid metadata yet be retracted or corrected; citing it is a hard error. Screen
by parsing `CommentsCorrections` for `RefType=RetractionIn`, `ErratumIn`, or `ExpressionOfConcernIn`, not by
publication type (misses corrections on an otherwise-normal record, `references/history.md`). A publisher
or Retraction Watch banner is supplementary, never primary. Flag inline:
`[RETRACTED — <topic>; verify before citing]` or `[ERRATUM — <what changed>; verify before citing]`.

### Identifier Extraction

Use `[0-9]{5,9}` for PMIDs, not `[0-9]{7,8}`, which silently drops shorter IDs (`references/history.md`).
State the extraction pattern alongside any verification count so the denominator can be audited.

### Metadata That Must Match

| Field | Tolerance |
|-------|-----------|
| **Title** | Minor capitalization differences OK; substantive word changes = wrong paper |
| **First author** | Must match exactly |
| **Year** | ±1 year allowed (preprint → publication lag) |
| **Journal/Conference** | Must match (abbreviated or full name both acceptable) |
| **PMID** | Verified via PubMed MCP tool call, never from memory |
| **DOI** | If provided, must resolve to the correct paper |
| **Non-PubMed source (OpenAlex / Semantic Scholar)** | Non-indexed refs only; DOI/PMID lookups are exact, a title-search hit needs first-author+year match |

### Claim Verification

When citing a paper for a specific claim (e.g., "mortality was 30%"): search the abstract or full text,
confirm the finding appears, note if it is a secondary/subgroup result, and if unconfirmable write "Author
et al. reported [claim] (exact finding not confirmed from abstract; verify in full text)."

## Verification by Context

- **Manuscripts/grants**: verify every bibliography reference during drafting, not post-hoc; flag failures
  with `[CITATION NEEDED]`; literature review sections need at least title + first author + year + journal.
- **Clinical consults**: verify the paper and finding; landmark trials (ARDSNet, PROSEVA, TTM2) must be
  attributed to the correct trial and outcome; a dose tied to a cited paper is verified here, free-standing
  goes to `fabrication-audit`.
- **Literature digests**: confirm every link and title exactly (capitalization, subtitles); Medline-style
  journal abbreviations.
- **Rebuttal letters**: full verification for new references; re-cited papers must be confirmed in the
  bibliography with the finding intact.

## Handling Verification Failures

**Paper not found**: try alternate search queries, then the step-1 fallback order (PubMed MCP → OpenAlex →
Semantic Scholar → web search); confirm first author + year on any title-search hit, since relevance
ranking can surface a same-title decoy; still not found after 2-3 attempts → `[CITATION NEEDED]`.

**PMID not found or mismatched**: use the correct PMID if found under a different one; omit and use the DOI
if the paper has none; discard entirely if it resolves to a different paper.

**Metadata mismatch**: confirm the correct paper, not a similarly titled one; check preprint-vs-published
discrepancies; use the most current metadata; note significant discrepancies (e.g., author order).

**Claim not confirmed**: if absent from the abstract, note "not confirmed from abstract — verify against
full text"; never fabricate page numbers or section references.

## Execution Mode: Dispatch vs. Inline

If the `Agent`/`Task` tool is available, dispatch the `citation-verifier` subagent (`this plugin's `agents/` directory` for
Claude Code, `.claude/agents/` for Cowork), requiring tool-call evidence for every VERIFIED identifier and
spot-checking at least 30% independently (see Multi-Agent guardrails below). If the tool is unavailable, run the workflow
inline, unchanged.

## Multi-Agent Citation Workflows

Subagents can confabulate verification reports: a claim of "all 12 references verified" with no tool-call
evidence has verified nothing (`references/history.md`). Guardrails: (1) any subagent verifying citations
must include its actual PubMed MCP calls (queries, `get_article_metadata` results) — a narrative-only
report is not verification; (2) the parent spot-checks at least 30% of PMIDs directly, and re-verifies all
on any failure; (3) no transitive trust — never accept a subagent's claim that it "searched PubMed" without
independent verification; (4) before delivery, the delivering agent must hold direct tool-call evidence for
every PMID in the document.

The dispatch-prompt template for verification subagents (restates guardrail 1) is in
`references/history.md`.

## Output Format (Audit Requested)

```
CITATION VERIFICATION REPORT

Verified: [X] / [Total]
Flagged:  [Y] (metadata issues or unconfirmed claims)
Failed:   [Z] (not found)

| # | Citation | Status | PMID Verified | Issue (if any) |
|---|----------|--------|---------------|----------------|
| 1 | Shah et al., PCCM 2024 | ✓ Verified | ✓ via MCP | — |
| 2 | Jones et al., CCM 2023 | ⚠ Flagged | ✗ Wrong paper | PMID resolves to unrelated article |
```

## Integration with Other Skills

- **fabrication-audit**: owns non-academic verifiable specifics (scope split above); the two skills compose
  without overlap.
- **clinical-citation-audit**: the pre+post-flight gate wrapping this skill for the six clinical-writing
  skills (manuscript-reviewer, scientific-writing, literature-review, clinical-reports,
  clinical-evidence-debate, clinical-teaching-deck); that skill enforces, this skill supplies the
  methodology, and defers block formatting to its audit summary when invoked from the pipeline.
- **manuscript-reviewer**, **review-response**, **literature-review**, **icu-clinical-consult**,
  **interesting-articles-formatting**: each hands its citation-bearing content through this skill's
  workflow (icu-clinical-consult routes free-standing doses to `fabrication-audit` instead).
- **writing-anti-ai**: vague attributions it flags ("Experts believe...") should be replaced with verified
  specific citations.

## What This Skill Does NOT Do

- It does not override the user's citation choices; it flags problems and the user decides.
- It does not verify references the user provides verbatim, though it flags obvious metadata errors if
  noticed.

## Feedback loop
Found a missed edge case, a wrong-shaped output, or a rule that misfires?
Open an issue on this plugin's repository with the input and the output you
expected. Do not edit this skill mid-run.

## Versions

See `references/history.md` for version history and the incidents behind each rule.
