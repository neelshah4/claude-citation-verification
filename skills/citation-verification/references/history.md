# Citation-verification: history and worked examples

Moved out of SKILL.md on 2026-09-08 (word-count trim per the internal audit rubric). Design rationale and dated incident narrative live here; SKILL.md keeps only the resulting rule.

## The April 2026 PMID incident (origin of the mandatory PMID workflow)

In a real manuscript workflow, 4 out of 12 PMIDs were fabricated — each a plausible-looking number that resolved to a completely unrelated paper (ophthalmology, rheumatology, veterinary medicine, psychiatry). The citation text (authors, title, journal, year) was correct in all cases; only the PMIDs were invented. A validation subagent then confabulated a report claiming "all references verified" without actually querying PubMed. This is the incident that produced the mandatory tool-call-evidence and spot-check rules now in SKILL.md's Multi-Agent and PMID sections.

## Retraction screening: publication-type vs linkage (measured 2026-09-01)

A publication-type screen (asking whether the cited record is *itself* a retraction/erratum notice) reported 0 errata across a corpus where linkage screening (parsing `CommentsCorrections` for `RefType=RetractionIn`/`ErratumIn`/`ExpressionOfConcernIn`) found 11 errata on 10 records, 8 of them previously unmarked — one of which had the FACTT trial's fluid arms reversed. This is why SKILL.md specifies linkage screening, not publication type.

## PMID extraction pattern: why `[0-9]{5,9}`, not `[0-9]{7,8}`

PMIDs are not all 7-8 digits. A narrower pattern silently drops shorter identifiers rather than failing, so an audit can cover an incomplete set while reporting a complete-looking number. Measured 2026-09-01: a reported `184/184` became `328/328` on the same corpus once the pattern was widened from `[0-9]{7,8}` to `[0-9]{5,9}`. General lesson: state the extraction pattern and the screen used alongside any "N/N verified" result, since the bare count hides both how N was gathered and what the gathering could not see.

## Non-PubMed fallback API endpoints (detail behind Verification Workflow step 1 / "Paper not found")

- OpenAlex: `https://api.openalex.org/works/doi:<doi>` for an exact DOI lookup, or
  `?filter=title.search:<title>&mailto=your-email@example.com` for a title search.
- Semantic Scholar: `https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>?fields=title,year,authors,externalIds`.
  `externalIds.PubMed` can recover a missing PMID. The DOI endpoint is reliable; the search endpoint is
  rate-limited and worth one retry before falling through to web search / Google Scholar.

## Prompt template for verification subagents

Restates the "tool call evidence required" guardrail in imperative form for dispatch prompts:

```
For EVERY PMID you include, you MUST:
1. Call get_article_metadata with that PMID
2. Confirm the returned title matches the intended paper
3. Include the tool call result in your output
If you cannot call the PubMed MCP, mark every PMID as [PMID UNVERIFIED].
Do NOT generate PMIDs from memory under any circumstances.
```

## Versions

- 2026-09-08 — Trimmed to meet the skill's word-count target per the internal audit rubric. Moved inline incident narrative, dated rationale, and worked examples here. No trigger phrase, verification workflow, or output-contract behavior changed.
- 2026-08-29 — Added the lint-enforced `## Feedback loop` contract block (capture via an internal tracking script); no behavioral change.
- 2026-06-04 — Best-practices pass (Anthropic "how we use skills"): added `lastReviewed`; added Versions section; added clinical-citation-audit composition cross-ref. No trigger phrases, no verification workflow, and no output contract changed. The April 2026 PMID-incident content is unchanged.
