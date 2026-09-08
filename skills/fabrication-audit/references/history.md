# fabrication-audit — history, rationale, and worked examples

Moved out of SKILL.md on 2026-09-08 per the audit rubric: version notes, dated rationale, and narrative examples that do not change model behavior. SKILL.md keeps a one-line pointer to this file.

## Origin incidents (Philosophy)

The skill exists because these confident fabrications actually occurred:

- Stating "the Denver Field Office serves Wyoming" when the correct answer was the Cheyenne Field Office (jurisdiction error, plus a fabricated drive time)
- Stating "premium processing is $2,805" when search results in the same conversation had returned $2,965 (stale-recall over fresh context)
- Stating "the standard dose is 0.5 mg/kg" when a clinical dose was never confirmed against any source
- Stating "Auston Matthews's cap hit is $11.6M" from training data that may be stale
- Stating "QLD expense ratio is 0.95%" without verifying current value
- Stating "B. fragilis resistance to cefoxitin is now 15-30% in US surveillance" when actual US data is ~3.5% for BFSS and up to ~15% for non-fragilis Bacteroides (conflated international with US data; presented as didactic teaching but encoded a specific retrievable surveillance value)

## Anti-patterns table (worked examples)

| Pattern | Example | Fix |
|---|---|---|
| Wrong jurisdiction | "Denver field office serves Cheyenne" | Search "USCIS field office Cheyenne" -> Cheyenne Field Office |
| Stale fee | "Premium processing is $2,805" (after fee increased to $2,965) | Use search-returned current value |
| Confabulated distance | "About a 4-hour drive" without checking | Search -> 3h 50m or remove the detail |
| Hallucinated officeholder | "The current ELSO president is Dr. X" (stale or invented) | Search ELSO leadership page |
| Fabricated drug dose | "Standard dose is 0.5 mg/kg" (not from any source) | Defer to clinical guidelines or `icu-clinical-consult` skill |
| Stale sports specific | "Matthews's cap hit is $11.6M" (changed after extension) | Search current cap hit |
| Stale ETF specific | "QLD expense ratio is 0.95%" without checking | Search fund's current prospectus |
| Confabulated form version | "Use Form I-485 edition 04/01/24" without verification | Search USCIS forms page for current edition date |
| Confabulated surveillance percentage | "B. fragilis cefoxitin resistance is 15-30% in US data" (actual US data ~3.5% for BFSS, up to 15% for non-fragilis) | Search PubMed for current Snydman/Tufts series or recent regional surveillance; cite the specific paper and geography |
| Conflated geography | "US cefoxitin resistance has reached 65%" (that number is from Japanese SSI surveillance, not US) | Match the geography of the data to the geography of the claim; if no US data available, qualify explicitly |
| Didactic-cadence fabrication | "Cefoxitin's anaerobic coverage has eroded, resistance is now N%" delivered in teaching voice | Teaching cadence does not exempt the underlying number from verification; treat the cadence as a flag, not a permit |

## Reference: the user's NEVER EVENT rules

This skill operationalizes three NEVER EVENT rules from the user's preferences and memory:

1. Citations rule (PMIDs/DOIs) — handled by `citation-verification`
2. Retrievable specifics rule — handled by THIS skill
3. Pre-flight pass rule — operationalized as Step 1-5 of the procedure in SKILL.md

If this skill catches a fabrication, that's a successful save. If a fabrication slips through, the skill failed and the description/procedure should be updated. Treat each catch and each miss as data.

## Lessons learned (case log)

| Date | Failure | Pattern | Update applied |
|---|---|---|---|
| 2026-05 | Claimed "B. fragilis cefoxitin resistance is 15-30% in US surveillance"; actually ~3.5% for BFSS, up to 15% for non-fragilis. Conflated Japanese SSI data (65%) with US data. | Didactic-cadence claim encoding a specific surveillance percentage; geography conflation | Added antimicrobial surveillance as explicit COVERS category; added didactic-cadence anti-pattern; added Step 5 geography cross-check |

## Versions

- 2026-08-29 — Added the lint-enforced `## Feedback loop` contract block (capture via an internal tracking script); no behavioral change.
- 2026-06-04 — Best-practices pass (Anthropic "how we use skills"): added `lastReviewed`; added a Versions section. No trigger phrases, no Step 1-5 procedure, no scope boundary, and no output contract changed. The "Lessons learned (case log)" and all incident-grounded anti-pattern content are unchanged.
- 2026-09-08 — Word-budget pass per the audit rubric: moved this file's contents out of SKILL.md, deduplicated repeated "when in doubt: TRIGGER" and "err on the side of triggering" restatements, removed the "What this skill does NOT do" section (each bullet duplicated an earlier stated rule), lowercased one mid-sentence ALL-CAPS emphasis. No trigger, scope, classification, procedure, or output-format rule was removed or weakened.
