# Claim-support tier: reviewer procedure

The existence tier proves a cited paper is real. This tier asks the commoner question: does the paper say what the sentence claims? Typical failures are a wrong number, a reversed direction, adult data cited for children, and a subgroup or post hoc result presented as the primary finding.

## Who reads this

You are a fresh reviewer with no context. You see one file, `packets.json`. You do not see the manuscript, the author's reasoning, or any earlier verdict. Do not open other files, search the web, or use what you remember about a trial. Judge the claim against the packet text only.

Your output is a JSON array of verdicts and nothing else. Do not write out your reasoning. Give the verdict, the verbatim span, the four checks, and a one-sentence note naming the discrepancy.

## The packet

```json
{"key": "PMID:42307570", "claim": "<sentence under test>", "pmid": "42307570", "doi": "...",
 "title": "...", "publication_types": ["Randomized Controlled Trial"],
 "abstract": "IMPORTANCE: ... RESULTS: ...", "fulltext_excerpt": null, "sources": ["https://pubmed.ncbi.nlm.nih.gov/42307570/"]}
```

The evidence text is `title` + `abstract` + `fulltext_excerpt` (a capped Results excerpt, present only with `--fulltext` and an open-access PMC copy). A packet with `abstract: null` is not checkable; skip it.

## Limit you must respect

This is an abstract-only check. An abstract omits most subgroup tables, secondary outcomes, and exact counts. A claim whose element is absent from the text gets NOT-IN-ABSTRACT, which says nothing about whether the claim is true. Never use CONTRADICTED because a number is missing. Use it only when the text states something incompatible with the claim.

## The four checks

Record each as one value.

| Check | Values | Ask |
|---|---|---|
| `number` | match, mismatch, not-stated, n/a | Does each figure in the claim (effect size, CI, percentage, n, p) appear in the text for the same outcome? |
| `direction` | match, reversed, not-stated, n/a | Is the effect the same way round (reduced vs increased, benefit vs harm, difference vs no difference)? Is the claim stronger than the text (association worded as cause, non-significant worded as effect)? |
| `population` | match, different, not-stated, n/a | Does the studied population match the claim's? Adult vs pediatric is the commonest silent error. Also neonate vs child, ECMO vs non-ECMO, and one disease vs another. |
| `primary_vs_subgroup` | primary, secondary, subgroup, post-hoc, not-stated, n/a | Is the cited result the trial's primary outcome, or a secondary, subgroup, or post hoc result? Flag it when the claim reads as the headline finding. |

## Verdicts

- SUPPORTED: the text states every element of the claim, and all four checks are clean.
- PARTIAL: the core finding is in the text, but one element differs or is stronger than the source (population different, subgroup presented as primary, rounded or approximate number, causal wording).
- NOT-IN-ABSTRACT: an element of the claim is absent from the text. No span needed.
- CONTRADICTED: the text states the opposite direction or a different value for the same outcome and population. A span is required.

Population or primary-vs-subgroup problems alone give PARTIAL, not CONTRADICTED.

## Evidence span rules (the script enforces them)

1. Copy the span character for character from the packet text. Do not retype from memory, fix typos, or paraphrase. Whitespace, letter case, and dash style are forgiven.
2. Each fragment must be at least 20 characters and 4 words. Join two fragments from different sentences with ` ... `.
3. For SUPPORTED, the span must contain every number in the claim, excluding PMIDs, DOIs, bracketed reference numbers, and author-year citations. A claim that says "30-day mortality" needs a span containing 30. Quote the sentence holding the figures and a second fragment for other numbers (age cut-offs, follow-up).
4. Any span not found in the packet text is rejected as INVALID-SPAN, and the verdict is discarded whatever it said.
5. SUPPORTED, PARTIAL, and CONTRADICTED need a span. NOT-IN-ABSTRACT takes `""`.
6. A SUPPORTED verdict whose own checks show mismatch, reversed, or different is held as INCONSISTENT for re-review.

## Output

```json
[{"key": "PMID:...", "verdict": "SUPPORTED|PARTIAL|NOT-IN-ABSTRACT|CONTRADICTED",
  "evidence_span": "<verbatim text, or \"\" for NOT-IN-ABSTRACT>",
  "checks": {"number": "...", "direction": "...", "population": "...", "primary_vs_subgroup": "..."},
  "note": "<one sentence naming the discrepancy, or \"\">"}]
```

One verdict per packet, same `key`. Return the array only.

## What blocks

Only CONTRADICTED and INVALID-SPAN block as rows. `claim_pass` is true only when no row blocks and `claim_complete` is true (every packet with an abstract got a valid verdict); a missing or malformed verdict prints `incomplete: N packets without a verdict` and exits 1. SUPPORTED, PARTIAL, NOT-IN-ABSTRACT, and INCONSISTENT are advisory and appear in the audit for the author to read. Packets with no abstract and packets with no verdict are listed under `not_checked`, never as supported. A model never decides the pass; `check` does.

## Worked examples

Each abstract below was fetched from PubMed in this build (efetch, 2026-11-16). The claims are written for the example.

### 1. SUPPORTED (PMID 42307570)

Claim: "In infants aged 6 months or younger with bronchiolitis on high-flow nasal cannula, prone positioning did not significantly reduce escalation of care (adjusted OR 0.66, 95% CI 0.40-1.07) [PMID 42307570]."

```json
{"key": "PMID:42307570", "verdict": "SUPPORTED",
 "evidence_span": "Infants aged 6 months or younger admitted for 24 hours or less with a diagnosis of acute bronchiolitis ... with no significant difference between the prone position (33/220 [15.0%]) and supine position (47/226 [20.8%]) groups (adjusted odds ratio, 0.66 [95% CI, 0.40-1.07]; P = .09)",
 "checks": {"number": "match", "direction": "match", "population": "match", "primary_vs_subgroup": "primary"}, "note": ""}
```

The span carries 6, 0.66, 0.40, 1.07, and 95, so the number gate passes. The abstract names escalation of care as the primary outcome.

### 2. CONTRADICTED, direction (PMID 42143375)

Claim: "In PROSEVA, higher baseline respiratory system elastance predicted a greater mortality benefit from prone positioning (interaction OR 0.94, 90% CrI 0.74-1.20) (PMID: 42143375)."

```json
{"key": "PMID:42143375", "verdict": "CONTRADICTED",
 "evidence_span": "The treatment effect of prone positioning on mortality did not meaningfully vary with baseline Ers",
 "checks": {"number": "match", "direction": "reversed", "population": "match", "primary_vs_subgroup": "post-hoc"},
 "note": "Numbers match, but the abstract reports no variation of the proning effect by elastance; the paper is a post hoc analysis of PROSEVA."}
```

Every figure is correct and the claim is still wrong: it reads an interaction interval spanning 1 as a gradient of benefit. The number check alone would pass this sentence.

### 3. PARTIAL, population mismatch (PMID 41324946)

Claim: "In children supported on VA-ECMO for cardiogenic shock, levosimendan did not shorten time to weaning (sHR 1.02, 95% CI 0.74-1.39) [PMID 41324946]."

```json
{"key": "PMID:41324946", "verdict": "PARTIAL",
 "evidence_span": "205 adult patients with acute cardiogenic shock who had started VA-ECMO in the preceding 48 hours were enrolled",
 "checks": {"number": "match", "direction": "match", "population": "different", "primary_vs_subgroup": "primary"},
 "note": "Adult trial (median age 58) cited for a pediatric claim; the sHR and CI are the trial's own."}
```

The numbers and direction are right, so only a population check catches this. The fix belongs to the author: restate the claim for adults, or cite pediatric evidence.

## Orchestrator steps

1. Run the existence audit first. Only records in a verified status get packets.
2. `claim_packets.py build --audit audit.json [--fulltext] --out packets.json`. It prints skipped records (no checkable assertion) and records with no abstract.
3. Spawn one fresh reviewer (Opus) with this file and `packets.json` only. Split packets across reviewers by key if there are more than about 25; do not split a packet.
4. `claim_packets.py check --packets packets.json --verdicts verdicts.json --out claim_audit.json`. Exit 0 means `claim_pass` (no blocking row and every checkable packet judged); exit 1 means a CONTRADICTED or INVALID-SPAN row, or packets still without a verdict.
5. For a blocking row, correct the claim sentence in the draft (or cite a better paper), then rebuild. Do not edit the verdict. For INVALID-SPAN, send the packet back to a fresh reviewer once; a second INVALID-SPAN on the same key goes to the author.
6. Report the counts and every advisory row with `population: different` or `primary_vs_subgroup` other than `primary`.
