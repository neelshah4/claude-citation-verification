# Changelog

All notable changes to this plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is calendar-based (`YYYY.M.D`), matching the skill's own dated version
string rather than imposing a semantic version it does not have.

## [2026.11.16] - 2026-11-16

### Changed
- Statuses now come from a script. `verify_citations.py` fetches the registry records (PubMed, Crossref, OpenAlex, arXiv, ClinicalTrials.gov, and others), compares each record with the citing text, and assigns the status; the `citation-verifier` agent and the `extras/citation-audit.js` workflow run it and review only the records it leaves open. Model-read verification remains as a labelled fallback (`verifier: manual`) when Python or the script cannot run.
- One 11-status vocabulary across skill, agent, and workflow: `OK`, `OK-REVIEWED`, `CORRECTED`, `NEEDS-REVIEW`, `WRONG-ID`, `MISMATCH`, `NOT-FOUND`, `RETRACTED`, `CONCERN`, `SUPERSEDED`, `UNVERIFIED`. A transport failure is `UNVERIFIED`, never `NOT-FOUND`.
- The workflow re-hashes the audit file in a second agent and compares it in code, shards `NEEDS-REVIEW` records to reviewers, and reads its log root and scripts directory from `CITATION_AUDIT_LOG_DIR` and `CITATION_SCRIPTS_DIR` (or the `logRoot` and `scriptsDir` arguments).
- `SKILL.md` notes that the PubMed connector's metadata call does not expose `CommentsCorrections`, so errata and retractions must be read from raw `efetch` XML.
- The OpenAlex title-search example in `references/history.md` no longer carries a contact address.

### Added
- `skills/citation-verification/scripts/verify_citations.py` (subcommands `audit`, `block`, `review`, `extract`) and `claim_packets.py` (`build`, `check`), standard library only.
- Claim-support tier: `references/claim-support.md` and `references/verdict-schema.json`.
- Eval bank in `skills/citation-verification/evals/`: 63 verifier cases (`cases.jsonl`, `grade.py`) and 33 claim-gate cases (`claim_cases.jsonl`, `grade_claims.py`), with a README.
- Optional Stop hook `hooks/citation-ledger-check.py`, with its README and a 31-test suite in `hooks/tests/`. The plugin does not register the hook.
- Environment variables `CITATION_AUDIT_MAILTO`, `NCBI_API_KEY`, `CITATION_HOOK_MODE`, `CITATION_AUDIT_LOG_DIR`, `CITATION_SCRIPTS_DIR`; documented in the README.

## [2026.9.24] - 2026-09-24

### Added
- Optional web fallback: when WebFetch or WebSearch is blocked (4xx/5xx, paywall, bot wall, unfollowable redirect, or content missing the target), retry once with the Parallel Search MCP if it is installed. Parallel excerpts are partial, so a value missing from one is unverified. No PHI, unpublished text, or credentials in a query; never used to pass a login or CAPTCHA gate.
- `citation-verifier` and `fabrication-auditor` agents granted the two Parallel tools, with the same fallback section. Bundled fabrication-audit synced to v2026.9.24.

## [2026.9.8] - 2026-09-08

### Changed
- `citation-verification/SKILL.md` body trimmed from 2,903 to 1,533 words: version notes, the dated PMID/retraction incident narrative, and worked examples moved to a new `skills/citation-verification/references/history.md`, with a one-line pointer left in SKILL.md. No trigger phrase, verification workflow, or output-contract behavior changed.
- Bundled `fabrication-audit` updated to its own 2026.9.8 release (see that plugin's changelog): SKILL.md trimmed from 3,257 to 1,744 words, with a new `skills/fabrication-audit/references/history.md` carrying the origin incidents, anti-pattern table, and case log.
- The feedback-loop section in both bundled skills has its body rewritten as a generic issue-driven prompt with no internal script or path names.
- Every internal user-machine path reference removed from SKILL.md, references, and README; the README's local-install command now points at a generic workflows directory rather than a literal path.

## [2026.8.29] - 2026-08-31

### Changed
- Version realigned to match the skill's own declared version (2026-08-29). The initial
  publication used a placeholder 1.0.0.
- README expanded substantially: worked examples with sample output, configuration,
  troubleshooting, limitations, and design rationale.
- Added the lookup chain, match criteria, the multi-agent re-verification rule, and worked audit output.


## [1.0.0] - 2026-08-31

### Added
- Initial public release as a Claude Code plugin.
- MIT license, plugin manifest, and installable marketplace entry.
- Bundled `fabrication-audit` v1.0.0 so the companion gate works on install.

### Changed
- OpenAlex and Semantic Scholar polite-pool contact replaced with a configurable
  placeholder. Set your own address for better rate limits.
- `extras/citation-audit.js` log directory is now `CITATION_AUDIT_LOG_DIR`, defaulting
  to `./logs/citation-audit/`.
2026-09-03: Sync from canonical: retraction and erratum screening now by CommentsCorrections linkage (RetractionIn / ErratumIn / ExpressionOfConcernIn), not publication type; PMID extraction pattern widened to 5 to 9 digits with the measured 184 to 328 example; bundled fabrication-audit updated.
