# Changelog

All notable changes to this plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is calendar-based (`YYYY.M.D`), matching the skill's own dated version
string rather than imposing a semantic version it does not have.

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
