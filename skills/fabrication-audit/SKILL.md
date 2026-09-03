---
name: fabrication-audit
description: >-
  Silent post-draft verification pass catching unverified specifics before they leave the response. Triggers on ANY substantive output containing factual claims about the world: numbers, dates, addresses, jurisdictions (which office/agency covers a place), distances, drive times, government fees, processing times, version numbers, current officeholders, drug doses, prices, statistics, sports stats/cap hits, ETF/stock specifics, salaries, antimicrobial susceptibility/resistance rates — any "the X is Y" / "X serves Y" pattern where Y is a specific retrievable value. Embedded sub-claims in longer responses get same treatment as direct questions. Triggers on nearly every substantive turn except greetings, pure code, pure math derivations, fictional creative writing. Does NOT trigger on academic citations (PMIDs/DOIs/journal refs) — citation-verification handles those exclusively. Runs silently, fixes draft before sending. Err very heavily on triggering. When in doubt: TRIGGER.
lastReviewed: 2026-06-04
---

# Fabrication audit — silent verification pass

Catch unverified operational specifics before they leave the response. This skill exists because LLMs reliably fabricate plausible-sounding specifics (addresses, fees, jurisdictions, drive times, drug doses, sports stats, finance numbers, surveillance percentages) inside longer analytical responses, even when the model "knows" to be careful in principle.

## Philosophy

The failure mode this skill prevents is **confident hallucination of a verifiable specific embedded in a longer response**. Real examples that have occurred:

- Stating "the Denver Field Office serves Wyoming" when the correct answer was the Cheyenne Field Office (jurisdiction error, plus a fabricated drive time)
- Stating "premium processing is $2,805" when search results in the same conversation had returned $2,965 (stale-recall over fresh context)
- Stating "the standard dose is 0.5 mg/kg" when a clinical dose was never confirmed against any source
- Stating "Auston Matthews's cap hit is $11.6M" from training data that may be stale
- Stating "QLD expense ratio is 0.95%" without verifying current value
- **Stating "B. fragilis resistance to cefoxitin is now 15–30% in US surveillance" when actual US data is ~3.5% for BFSS and up to ~15% for non-fragilis Bacteroides** (conflated international with US data; presented as didactic teaching but encoded a specific retrievable surveillance value)

Each of these has the same shape: **a confident "the X is Y" sentence where Y is specific and retrievable but unverified**. The skill's job is to catch the pattern and force verification, even when the sentence reads like established teaching rather than an operational specific.

## Scope: what this skill covers vs. doesn't

### COVERS (this skill)
- **Geographic / jurisdictional:** which office, court, or agency covers a city; which field office handles a case; which ZIP codes belong to a school district
- **Logistical:** addresses, hours, phone numbers, drive times, distances between specific locations, flight durations
- **Government / regulatory:** filing fees, processing times, current rule effective dates, form version numbers, current officeholders
- **Clinical specifics:** drug doses, weight-based dosing, lab thresholds, formulary specifics, brand-name drug prices
- **Antimicrobial surveillance specifics:** susceptibility or resistance percentages for any drug-organism pair, from any surveillance source (Snydman/Tufts Bacteroides series, CANWARD, CDC EIP, NHSN, MERINO, SENTRY, regional or hospital antibiograms). **These are not general medical knowledge** — they are time-varying, geography-specific, source-specific surveillance values. Any "drug X covers Y% of organism Z" or "resistance to drug X is now N%" claim falls here.
- **Financial:** ETF expense ratios, dividend yields, current stock prices, mortgage rates, exchange rates, cap rates, salary ranges
- **Sports:** cap hits, salaries, contract terms, current standings, recent transactions, player stats
- **Statistical/epidemiologic:** "X% of patients..." claims, registry mortality rates, prevalence figures, incidence rates, NNT/NNH, effect sizes with CI, hazard ratios, odds ratios

### DOES NOT COVER (other skills handle)
- **Academic citations** (PMIDs, DOIs, paper titles, author names, journal references) → `citation-verification` handles these exclusively. Do not duplicate that work.
- **Concept explanations** ("photosynthesis converts light to chemical energy") → no specific value, no audit needed
- **Math derivations** ("CO = HR × SV; for HR 80, SV 70mL, CO is 5.6 L/min") → derivation from stated inputs is not a fabricated specific
- **Fictional creative writing** ("the lighthouse stood 200 feet tall") → fictional contexts don't have ground truth
- **Code generation** → syntax and logic, not factual claims about the world
- **User's own quoted values** — if the user states a fact ("my patient is 12kg"), that's the ground truth; don't audit it

## When to trigger

### Always trigger (default ON)
- Any response > 150 words containing one or more specific factual claims about the world
- Any direct question asking for a specific factual value (price, address, dose, distance, fee, time, name, susceptibility %)
- Any analytical response where operational specifics support recommendations (immigration timelines, clinical management plans, antimicrobial spectrum arguments, financial advice, travel planning, legal analysis)
- Any output that will be acted upon by the user (filing a form, paying a fee, going to an address, taking a dose, picking an antibiotic)
- **Any antimicrobial spectrum-of-coverage argument.** "Drug X covers organism Y but not Z" is a verifiable claim built on surveillance data; the underlying percentages must be checked even if the qualitative claim is well-known.

### Skip
- Pure greetings, acknowledgments, casual one-liners
- Outputs already inside the body of `citation-verification` (academic specifics)
- Pure conceptual explanations with no specific quantitative claims
- Code review, debugging, syntax help
- Math-only derivations from user-supplied values
- Fiction, poetry, creative writing where ground truth doesn't apply
- When the user explicitly says "I don't need verification, just brainstorm"

### Borderline cases — TRIGGER unless clearly excluded
- Mixed conceptual + specific responses → trigger (the specifics still need audit)
- Recommendations involving specific products / brands / drugs → trigger
- "What's the consensus on X" where X has a quantitative answer → trigger
- Comparative answers ("A is bigger/faster/more common than B") with specific magnitudes → trigger
- **Didactic-cadence statements that encode a specific number.** Sentences phrased as established teaching ("X is now associated with Y% resistance," "the standard threshold is Z") are exactly the failure mode this skill catches. Teaching cadence is not exemption from verification — it's a flag for it.

When in doubt: TRIGGER. The marginal cost of an extra audit pass is far smaller than the cost of one fabricated specific reaching the user.

## Procedure (the audit pass)

This skill runs **after drafting the response, before sending**. It is a critic-loop pass over the draft, not a replacement for the initial generation.

### Step 1: Scan the draft for specific factual claims

Walk the draft sentence-by-sentence. Mark every sentence matching the pattern:

> **"the X is Y"** or **"X serves Y"** or **"Y is the [office / fee / dose / distance / date / officeholder / version / susceptibility / resistance rate]"** or **"X costs Y"** or **"X takes Y [time]"** or **"X covers Y"** or **"resistance to X is now N%"** or **"X% of isolates are susceptible to Y"**

…where Y is a specific retrievable value (number, proper noun, address, date, percentage).

Examples that match the pattern:
- "The Cheyenne Field Office handles your case" → CHECK (jurisdiction)
- "Premium processing is $2,965" → CHECK (fee)
- "It's about a 4-hour drive" → CHECK (drive time)
- "Standard heparin loading dose is 75–100 units/kg" → CHECK (dose)
- "QLD has an expense ratio of 0.95%" → CHECK (ETF specific)
- "Matthews's cap hit through 2031 is $13.25M" → CHECK (sports specific)
- **"B. fragilis resistance to cefoxitin is now 15–30%"** → CHECK (surveillance specific)
- **"Pip-tazo covers >95% of Bacteroides"** → CHECK (surveillance specific, even though qualitatively correct)
- **"MRSA rates in US ICUs are around 50%"** → CHECK (surveillance specific)

Examples that do NOT match the pattern:
- "Premium processing is faster than regular processing" → no specific Y, no check needed
- "PEEP increases PVR at high values" → physiology concept, no specific Y
- "He's a fast skater" → no quantitative claim
- "Pip-tazo has broader coverage than cefoxitin" → qualitative claim, no specific magnitude

### Step 2: Classify each marked claim

For each marked claim, classify the source:

| Class | Definition | Action |
|---|---|---|
| **A. VERIFIED THIS TURN** | Tool call this turn returned this exact value | Pass. Use the tool-returned value verbatim |
| **B. VERIFIED PRIOR TURN** | Tool call earlier in this same conversation returned this value | Pass. **Cross-check the value matches the source — do not paraphrase numbers from prior turns** |
| **C. STATED BY USER** | User provided this fact in their message | Pass. User's facts are ground truth |
| **D. DERIVATION** | Computed from A/B/C using explicit reasoning shown | Pass. Verify the math |
| **E. UNVERIFIED** | Comes from training-data recall with no tool-call provenance | Fail. Take action below |

### Step 3: For class E (unverified), take action

Two attempts, then mark.

**Attempt 1:** Run a tool call to verify the specific value. Use `web_search` for most facts, `web_fetch` for known authoritative URLs, `PubMed:search_articles` for surveillance/epidemiologic claims, specialized tools when available.

**Attempt 2:** If the first search didn't return a confident answer, retry with different terms. For pricing/policy: search the official source (USCIS.gov, IRS.gov, etc.). For clinical: search guideline documents. For antimicrobial surveillance: search PubMed for the most recent published surveillance from the relevant geography. For sports/finance: search the official statistic source (NHL Cap Friendly, Yahoo Finance, fund prospectus).

**If both attempts fail:**
- Rewrite the sentence to remove the specific value (replace with a hedged formulation: "premium processing is faster than regular processing" instead of a dollar amount; "cefoxitin's anaerobic coverage is variable" instead of a percentage)
- OR mark inline as `[UNVERIFIED]` so the user knows
- OR if the specific is load-bearing, surface the failure to the user explicitly: "I couldn't verify [X] in two searches; consider checking [authoritative source] directly"

**Never:** Leave a class E claim in the response without either verification, removal, or `[UNVERIFIED]` marking.

### Step 4: Cross-check verified-earlier values

The most insidious failure mode is this: a search returned `$2,965` earlier in the conversation, but the model writes `$2,805` later because that's the value in its training data.

**Defensive rule:** For any value that has been verified earlier in the conversation, before writing the value, scan the prior tool outputs for that specific. If the tool output value differs from what you're about to write, USE THE TOOL OUTPUT VALUE. Training-data recall does not override fresh search results in the same conversation.

This is a discrete, mechanical check. Do it explicitly. Don't trust your own memory over your own search results.

### Step 5: Geography / source cross-check (for surveillance specifics)

For any antimicrobial susceptibility, resistance rate, prevalence figure, or mortality rate, confirm the **geography and source** of the data match the geography and source of the claim.

| Failure mode | Example |
|---|---|
| US claim, international source | "US B. fragilis cefoxitin resistance is 65%" — that number comes from Japanese SSI surveillance, not US data |
| Recent claim, old source | "Current MRSA rates are X%" citing a 2010 paper |
| ICU claim, ward data | "ICU Pseudomonas resistance is Y%" using outpatient antibiogram |
| Pediatric claim, adult source | "Peds B. fragilis susceptibility is Z%" using adult registry |

**Rule:** Geography, era, population, and care setting must match between the data source and the claim. If they don't, either find a matching source or qualify the claim explicitly ("in Japanese SSI surveillance" not "in US data").

## Prioritization for long drafts

If the draft has > 10 specific factual claims:

1. Verify load-bearing claims first (claims that drive recommendations or actions)
2. Verify high-volatility specifics next (anything that changes month-to-month or year-to-year: prices, fees, processing times, current officeholders, antimicrobial surveillance data)
3. Verify low-volatility specifics last (historical dates, established geographic facts)
4. If running out of audit budget, mark remaining unverified specifics with `[UNVERIFIED]` rather than skipping the audit

## Output format

The skill runs **silently by default**. Three possible outcomes:

### Outcome 1: All clean
- Send the response with no separate output. The audit happened, nothing flagged.

### Outcome 2: Issues silently fixed
- Quietly fix the draft (replace stale value with verified value, remove unverifiable specific, add `[UNVERIFIED]` marker)
- Send the corrected response with no separate audit report

### Outcome 3: Systematic problems
- If 3+ specifics couldn't be verified, add a brief end-of-response note: **"Verification note: [list of marked items and why they couldn't be verified]"**
- This signals to the user that the response has known gaps to spot-check

## Anti-patterns this skill prevents

A non-exhaustive list of failure modes that should NEVER appear in output:

| Pattern | Example | Fix |
|---|---|---|
| Wrong jurisdiction | "Denver field office serves Cheyenne" | Search "USCIS field office Cheyenne" → Cheyenne Field Office |
| Stale fee | "Premium processing is $2,805" (after fee increased to $2,965) | Use search-returned current value |
| Confabulated distance | "About a 4-hour drive" without checking | Search → 3h 50m or remove the detail |
| Hallucinated officeholder | "The current ELSO president is Dr. X" (stale or invented) | Search ELSO leadership page |
| Fabricated drug dose | "Standard dose is 0.5 mg/kg" (not from any source) | Defer to clinical guidelines or `icu-clinical-consult` skill |
| Stale sports specific | "Matthews's cap hit is $11.6M" (changed after extension) | Search current cap hit |
| Stale ETF specific | "QLD expense ratio is 0.95%" without checking | Search fund's current prospectus |
| Confabulated form version | "Use Form I-485 edition 04/01/24" without verification | Search USCIS forms page for current edition date |
| **Confabulated surveillance percentage** | **"B. fragilis cefoxitin resistance is 15–30% in US data"** (actual US data ~3.5% for BFSS, up to 15% for non-fragilis) | **Search PubMed for current Snydman/Tufts series or recent regional surveillance; cite the specific paper and geography** |
| **Conflated geography** | **"US cefoxitin resistance has reached 65%"** (that number is from Japanese SSI surveillance, not US) | **Match the geography of the data to the geography of the claim; if no US data available, qualify explicitly** |
| **Didactic-cadence fabrication** | **"Cefoxitin's anaerobic coverage has eroded — resistance is now N%"** delivered in teaching voice | **Teaching cadence does not exempt the underlying number from verification. Treat the cadence as a flag, not a permit.** |

## Execution mode — dispatch the auditor agent (preferred) vs. inline (fallback)

**If the `Agent`/`Task` tool is available, dispatch the `fabrication-auditor` subagent** (in `this plugin's `agents/` directory` for Claude Code, `.claude/agents/` for Cowork) to run the Step 1–5 procedure in its own context, and require it to return tool-call evidence for every verified specific. The parent then spot-checks ≥30% of the auditor's verified specifics by independent verification (per the guardrails below). **If the tool is unavailable, run the Step 1–5 procedure inline** in this context, unchanged. Either way the procedure and scope above are authoritative; the agent is built directly from them.

## Multi-agent / subagent guardrails

When this skill runs in a multi-agent context (research subagent + parent, or orchestrated workflow):

1. **Subagent verification reports must include tool-call evidence.** A subagent that says "I verified the fee is $2,965" without showing the search tool call has verified nothing.
2. **Parent agent spot-checks** at least 30% of subagent-verified specifics by running independent verification.
3. **No transitive trust** — don't trust a subagent's claim that it searched. Verify independently for high-stakes specifics.
4. **Final gate:** Before any document with operational specifics is delivered, the delivering agent must have direct tool-call evidence (this turn or prior turns in this conversation) for every load-bearing specific.

This mirrors the citation-verification skill's multi-agent guardrails for a reason: the same confabulation pattern that produces fake PMIDs also produces fake fees, distances, addresses, and surveillance percentages.

## Integration with other skills

- **citation-verification** — handles PMIDs, DOIs, journal references, paper titles. Do not duplicate. If a response contains both academic citations and operational specifics, both skills run; their scopes don't overlap.
- **prompt-optimizer** — runs at the front of the workflow (input). This skill runs at the back (output). They compose; neither replaces the other.
- **icu-clinical-consult** — handles complex clinical reasoning. When a clinical response includes specific doses, susceptibility percentages, or other numeric clinical claims, this skill audits them; the consult skill provides the clinical framing. icu-clinical-consult's Phase 3 (Citation & Numeric-Claim Validation) explicitly delegates numeric-claim verification to this skill.
- **writing-anti-ai** — handles voice and AI tells. This skill handles factual specifics. Both pass simultaneously over the draft.
- **clinical-data-scientist** — when analyzing data, this skill audits any specific quantitative claims about external benchmarks. Internal data analyses (computed from user-provided datasets) are derivations and don't need this audit.

## What this skill does NOT do

- It does not slow down responses with visible verification steps. It runs silently.
- It does not require the user to request verification. It is always on.
- It does not fabricate verification of values it cannot confirm. It marks `[UNVERIFIED]` and surfaces the gap.
- It does not override user-provided facts. The user's stated values are ground truth.
- It does not duplicate citation-verification. PMIDs/DOIs/journal references are out of scope.

## Reference: the user's NEVER EVENT rules

This skill operationalizes three NEVER EVENT rules from the user's preferences and memory:

1. **Citations rule (PMIDs/DOIs)** — handled by `citation-verification`
2. **Retrievable specifics rule** — handled by THIS skill
3. **Pre-flight pass rule** — operationalized as Step 1–5 of the procedure above

If this skill catches a fabrication, that's a successful save. If a fabrication slips through, the skill failed and the description/procedure should be updated. Treat each catch and each miss as data.

## Lessons learned (case log)

This section records specific past failures and what the skill update should catch next time. Add a row whenever a new failure mode is identified.

| Date | Failure | Pattern | Update applied |
|---|---|---|---|
| 2026-05 | Claimed "B. fragilis cefoxitin resistance is 15–30% in US surveillance" — actually ~3.5% for BFSS, up to 15% for non-fragilis. Conflated Japanese SSI data (65%) with US data. | Didactic-cadence claim encoding a specific surveillance percentage; geography conflation | Added antimicrobial surveillance as explicit COVERS category; added didactic-cadence anti-pattern; added Step 5 geography cross-check |

## Triggering reminder

Default: ON for nearly every substantive turn. The cost of an extra audit pass is trivial. The cost of one fabricated specific reaching a high-stakes context (immigration filing, clinical decision, antibiotic selection, financial transaction, legal advice) is high. Err on the side of triggering.

**Didactic cadence is the trap.** When a sentence sounds like established teaching — confident, declarative, no hedge — that is exactly when the specific number embedded inside it most needs verification. Teaching voice is not exemption; it is the strongest possible trigger.

When in doubt: TRIGGER.

## Self-improvement
Found a missed edge case, a wrong-shaped output, or a rule that misfires?
Open an issue on this plugin's repository with the input and the output you
expected. Do not edit this skill mid-run.
Per-run case facts stay in this skill's own case log / memory store; only
*skill-file changes* go to the observation log.

## Versions

- 2026-08-29 — Added the lint-enforced `## Self-improvement` contract block (capture via `skill-observation-add.sh`); no behavioral change.
- **2026-06-04** — Best-practices pass (Anthropic "how we use skills"): added
  `lastReviewed`; added this Versions section. No trigger phrases, no Step 1-5 procedure,
  no scope boundary, and no output contract changed. The "Lessons learned (case log)" and
  all incident-grounded anti-pattern content are unchanged.
