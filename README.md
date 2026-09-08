# citation-verification

Checks that every citation identifier in a document actually exists, and replaces the ones that do not with `[CITATION NEEDED]`.

---

## Contents

- [The problem](#the-problem)
- [What it verifies](#what-it-verifies)
- [The lookup chain](#the-lookup-chain)
- [Install](#install)
- [Worked examples](#worked-examples)
- [The match criteria](#the-match-criteria)
- [Multi-agent workflows](#multi-agent-workflows)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [What it does not check](#what-it-does-not-check)
- [Design notes](#design-notes)

---

## The problem

Language models generate citation identifiers that look correct and are not. A PMID has the right number of digits. A DOI has the right prefix shape. An arXiv ID has the right year-month stem. All three resolve to nothing, or worse, to a different paper than the one named.

**A correct author, title, and year attached to a fabricated PMID is more damaging than no identifier at all**, because it survives a casual check. It reaches a reviewer, an editor, or a study section — and those checks are not casual.

This skill exists because of a specific incident: a batch of plausible PMIDs that had never been looked up, shipped in a draft. Everything downstream of that is designed on the assumption that it will happen again unless something mechanical stops it.

---

## What it verifies

| Type | Verified by |
|---|---|
| **PMID** | PubMed lookup; title and first author must match |
| **DOI** | Crossref resolution; metadata compared against the citing text |
| **PMC ID** | PMC resolution |
| **arXiv ID** | Canonical resolver `arxiv.org/abs/<id>` must return 200, title and first author must match |
| **NCT number** | ClinicalTrials.gov registry |

Root domains for journals and known brands are treated as safe. **Deeper URL paths are not** — they must be search-verified, because a plausible-looking article path is exactly the thing a model invents.

---

## The lookup chain

Tried in order, stopping at the first confident match:

1. **PubMed MCP**, if you have one connected
2. **PubMed** over HTTP for indexed biomedical work
3. **Crossref** for DOIs
4. **OpenAlex** for CS, AI, informatics, engineering, preprints — anything outside PubMed's index
5. **Semantic Scholar** — the DOI endpoint is reliable; the search endpoint is rate-limited and retried once. Its `externalIds.PubMed` field can recover a missing PMID
6. **Web search / Google Scholar** as a last resort

After two to three failed attempts, the identifier is marked `[CITATION NEEDED]` and you are told. It does not guess, and it does not soften a failure into a hedge like "possibly Smith et al."

**On title searches specifically:** any title-search hit must have first author *and* year confirmed before acceptance. Relevance ranking will happily return a same-title paper from a different year as the top hit. This has been observed in practice — a 2017 paper's title returning a 2025 work first.

---

## Install

```
/plugin marketplace add neelshah4/claude-plugins
/plugin install citation-verification@neel-plugins
```

Or directly:

```
/plugin install neelshah4/claude-citation-verification
```

---

## Worked examples

### Auditing a draft

```
Verify every citation in ~/manuscripts/pards_review.docx
```

```
CITATION AUDIT — 34 identifiers found

VERIFIED (31)
  PMID 32222134   ✓ Matches: Fan et al., AJRCCM 2020
  DOI 10.1007/s00134-020-06022-5  ✓ Matches
  NCT02282657     ✓ Matches: EOLIA
  ...

FAILED (3)
  PMID 34556789   ✗ Resolves to a 2021 nephrology paper. The text
                    cites it for a 2019 ECMO outcome. Not the same
                    work. → [CITATION NEEDED]

  DOI 10.1164/rccm.202104-0987OC
                  ✗ Does not resolve. Crossref returns nothing.
                    Prefix is real (ATS journals); the suffix is not.
                    → [CITATION NEEDED]

  arXiv 2401.09876
                  ✗ Resolver returns 404. → [CITATION NEEDED]

ACTION: 3 replacements written into the draft. Supply the correct
identifiers or remove the claims they support.
```

### As a silent gate

You do not have to ask. It fires whenever output names a specific study, and runs as a silent pass — no separate report unless something fails or you request an audit. In normal use you see nothing, which is the point.

### Recovering a missing PMID

```
This paper has a DOI but no PMID — can you find it?
```

The Semantic Scholar DOI endpoint exposes `externalIds.PubMed`, which recovers the PMID without a title search and without the decoy risk that comes with one.

---

## The match criteria

An identifier is accepted only when the retrieved record agrees with the citing text:

| Field | Requirement |
|---|---|
| **First author** | Must match |
| **Year** | Must match |
| **Title** | Must match, allowing for subtitle and punctuation variation |
| **Journal** | Must match; abbreviated or full name both acceptable |

Author-year shorthand in prose is acceptable — "a 2023 PCCM study by Shah et al." — but author, year, and journal still have to be right. Vagueness is not a way around verification.

---

## Multi-agent workflows

This is the part most citation tooling misses.

**When Claude dispatches subagents that produce citations, every subagent's citations must be re-verified by the gate.** A subagent's report is a lead, not a finding. The dispatching agent has no way to know whether a cited paper was retrieved or recalled, and a confident sub-report reads identically either way.

The skill mandates that verification runs **after** synthesis, on the merged output, not inside each subagent where it can be skipped or reported as done without being done. This rule exists because the failure it prevents has happened.

If you build multi-agent workflows that cite anything, wire this in as the final gate and give it the last word.

---

## Configuration

**Set your contact email.** OpenAlex and Semantic Scholar use the polite-pool convention — supplying a contact address gets you better rate limits. Replace `your-email@example.com` in two files:

```bash
skills/citation-verification/SKILL.md
agents/citation-verifier.md
```

It works without this, just with tighter limits.

**PubMed MCP.** If connected, it is tried before HTTP fallbacks. Not required.

**The deterministic workflow.** `extras/citation-audit.js` runs the audit as a scripted workflow with a self-improvement introspection step. Claude Code does not load `workflows/` from a plugin, so copy it manually:

```bash
cp extras/citation-audit.js <your Claude Code workflows directory>/
export CITATION_AUDIT_LOG_DIR=~/citation-logs/
```

Without the variable it writes to `./logs/citation-audit/`.

---

## Troubleshooting

**It marked a real paper `[CITATION NEEDED]`.** Usually a very recent paper not yet indexed, or a book chapter with no DOI. Supply the identifier directly and it will verify against it rather than searching.

**Verification is slow.** It is doing real network lookups, serially where rate limits demand it. A 40-reference manuscript takes a few minutes. Run it once, at the end.

**Rate-limited by Semantic Scholar.** The search endpoint is limited; the DOI endpoint is not. Set your polite-pool email, and prefer DOIs over title searches.

**It verified a paper that does not say what I cited it for.** Expected. See the next section.

**It re-verifies things I already checked.** By design across sessions — it cannot know what you verified last week. Within a session, user-supplied identifiers are trusted and not re-checked.

---

## What it does not check

**Non-citation specifics.** Doses, thresholds, resistance percentages, guideline names, fees, prices. That is [fabrication-audit](https://github.com/neelshah4/claude-fabrication-audit), which ships bundled here and runs alongside. Different lookups, different acceptance rules. The two together cover the claim surface; either alone leaves half of it unchecked.

**Whether the citation supports the claim.** This is the important limitation. A real paper cited for something it does not say will pass every check here. The identifier is verified; the argument is not. Nothing automated substitutes for having read the paper.

**Whether the citation is appropriate.** It cannot tell you that you cited a case report where a trial exists, or missed the seminal reference.

---

## Design notes

**Why failure is loud.** Every softer option — a hedge, a "possibly", a silent drop — leaves an unverified claim in the document looking like a verified one. `[CITATION NEEDED]` is visible in the draft and impossible to miss at submission.

**Why two attempts, not ten.** Retrying a fabricated identifier does not make it real. Two to three attempts distinguish a network problem from a nonexistent paper; beyond that you are burning time on a paper that does not exist.

**Why root domains pass but deep paths do not.** `nejm.org` is safe to assert. `nejm.org/doi/full/10.1056/NEJMoa2118687` is a specific claim about a specific article, and that is precisely the shape a model fabricates.

**Why it runs last.** Verification on a draft you are still cutting wastes lookups on sentences that will not survive. Settle the prose, then verify what you are actually shipping.

---

## Bundled dependency

`skills/fabrication-audit` ships inside this plugin. Canonical copy: [claude-fabrication-audit](https://github.com/neelshah4/claude-fabrication-audit).

## Requirements

- Web access.
- Claude Code with the Agent tool for the batched subagent path; without it verification runs inline and is slower.

## Version

`2026.8.29`, matching the skill's latest declared version date. Calendar versioning, because the skill is date-versioned. See [CHANGELOG.md](CHANGELOG.md).

## Contributing

Issues and pull requests welcome. New resolvers are the most useful contribution — the current chain is biomedical-first and thinner outside PubMed's index.

## License

MIT. Author: Neel Shah, MD, MSc.
