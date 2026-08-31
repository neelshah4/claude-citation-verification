# citation-verification

Checks that every citation identifier in a document actually exists, and replaces the ones that do not with `[CITATION NEEDED]`.

## The problem it solves

Language models generate citation identifiers that look correct and are not. A PMID has the right number of digits. A DOI has the right prefix shape. Both resolve to nothing, or worse, to a different paper.

A correct author, title, and year attached to a fabricated PMID is more damaging than no identifier at all, because it survives a casual check. Reviewers and editors do the non-casual check.

## What it verifies

PMIDs, DOIs, PMC IDs, arXiv IDs, and NCT numbers, each against a live lookup:

1. **PubMed** for indexed biomedical work
2. **Crossref** for DOIs
3. **OpenAlex** and **Semantic Scholar** for CS, AI, informatics, engineering, and preprints
4. Web search as a last resort

On a title-search hit it confirms first author and year before accepting, because relevance ranking will happily return a same-title decoy from a different year.

Anything unresolved after two attempts becomes `[CITATION NEEDED]`. It does not guess, and it does not soften a failure into a hedge.

## Install

```
/plugin marketplace add neelshah4/claude-plugins
/plugin install citation-verification@neel-plugins
```

Or directly:

```
/plugin install neelshah4/claude-citation-verification
```

## Use

```
Verify every citation in draft.md
```

It also fires as a final gate after any deliverable that names papers. Run it last, after the prose is settled, so you verify what you are actually shipping.

## Configure

The OpenAlex and Semantic Scholar lookups use the polite-pool convention, which asks for a contact email. Replace `your-email@example.com` in `skills/citation-verification/SKILL.md` and `agents/citation-verifier.md` with yours to get better rate limits. It works without this.

A PubMed MCP server, if you have one connected, is tried before the HTTP fallbacks.

## What it does not check

Doses, thresholds, percentages, guideline names, fees, prices — every retrievable value that is not an academic identifier. That is `fabrication-audit`, which ships bundled here and runs alongside. The two together cover the claim surface; either alone leaves a gap.

It also does not judge whether a citation *supports* the sentence it is attached to. A real paper cited for a claim it does not make will pass this gate.

## Requirements

- Web access.
- Claude Code with the Agent tool for the batched subagent path. Without it, verification runs inline and is slower.
- `extras/citation-audit.js` is a deterministic workflow version. Copy it to `~/.claude/workflows/` by hand if you want the scripted path. Set `CITATION_AUDIT_LOG_DIR` to control where run logs are written.

## Bundled dependency

`skills/fabrication-audit` ships inside this plugin at v1.0.0. Canonical copy: [claude-fabrication-audit](https://github.com/neelshah4/claude-fabrication-audit).

## Version

1.0.0. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. Author: Neel Shah, MD, MSc.
