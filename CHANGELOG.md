# Changelog

All notable changes to this plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
