---
name: fabrication-audit
description: >-
  Silent post-draft verification pass catching unverified specifics before they leave the response. Triggers on ANY substantive output containing factual claims about the world: numbers, dates, addresses, jurisdictions (which office/agency covers a place), distances, drive times, government fees, processing times, version numbers, current officeholders, drug doses, prices, statistics, sports stats/cap hits, ETF/stock specifics, salaries, antimicrobial susceptibility/resistance rates — any "the X is Y" / "X serves Y" pattern where Y is a specific retrievable value. Embedded sub-claims in longer responses get same treatment as direct questions. Triggers on nearly every substantive turn except greetings, pure code, pure math derivations, fictional creative writing. Does NOT trigger on academic citations (PMIDs/DOIs/journal refs) — citation-verification handles those exclusively. Runs silently, fixes draft before sending. Err very heavily on triggering. When in doubt: TRIGGER.
lastReviewed: 2026-09-08
---
# Fabrication audit — silent verification pass

Catch unverified operational specifics before they leave the response: LLMs reliably fabricate plausible-sounding specifics (addresses, fees, jurisdictions, drive times, drug doses, sports stats, finance numbers, surveillance percentages) inside longer analytical responses. The failure mode is a confident "the X is Y" sentence where Y is specific and retrievable but unverified, even when it reads like established teaching. Origin incidents are in `references/history.md`.

## Scope: what this skill covers vs. doesn't

### COVERS (this skill)
- **Geographic / jurisdictional:** which office, court, or agency covers a city; which field office handles a case; which ZIP codes belong to a district
- **Logistical:** addresses, hours, phone numbers, drive times, distances, flight durations
- **Government / regulatory:** filing fees, processing times, rule effective dates, form version numbers, current officeholders
- **Clinical specifics:** drug doses, weight-based dosing, lab thresholds, formulary specifics, brand-name drug prices
- **Antimicrobial surveillance specifics:** susceptibility or resistance percentages for any drug-organism pair, from any surveillance source (Snydman/Tufts, CANWARD, CDC EIP, NHSN, MERINO, SENTRY, regional/hospital antibiograms). These are time-varying, geography- and source-specific, not general medical knowledge; any "drug X covers Y% of organism Z" or "resistance to X is now N%" claim falls here.
- **Financial:** ETF expense ratios, dividend yields, stock prices, mortgage rates, exchange rates, cap rates, salary ranges
- **Sports:** cap hits, salaries, contract terms, standings, recent transactions, player stats
- **Statistical/epidemiologic:** "X% of patients..." claims, registry mortality rates, prevalence, incidence, NNT/NNH, effect sizes with CI, hazard/odds ratios

### DOES NOT COVER (other skills handle)
- **Academic citations** (PMIDs, DOIs, paper titles, author names, journal references) → `citation-verification` handles these exclusively; identifier verification is never this skill's job
- **Concept explanations** ("photosynthesis converts light to chemical energy") → no specific value, no audit needed
- **Math derivations** from stated inputs → not a fabricated specific
- **Fictional creative writing** → fictional contexts don't have ground truth
- **Code generation** → syntax and logic, not factual claims about the world
- **User's own quoted values** — the user's stated facts are ground truth, don't audit them

## When to trigger

### Always trigger (default ON)
- Any response > 150 words with one or more specific factual claims about the world
- Any direct question asking for a specific factual value (price, address, dose, distance, fee, time, name, susceptibility %)
- Any analytical response where operational specifics support recommendations (immigration timelines, clinical plans, antimicrobial spectrum arguments, financial advice, travel planning, legal analysis)
- Any output the user will act on (filing a form, paying a fee, going to an address, taking a dose, picking an antibiotic)
- Any antimicrobial spectrum-of-coverage argument: the surveillance percentages behind "drug X covers Y but not Z" must be checked even if the qualitative claim is well-known

### Skip
- Pure greetings, acknowledgments, casual one-liners
- Outputs already inside the body of `citation-verification` (academic specifics)
- Pure conceptual explanations with no specific quantitative claims
- Code review, debugging, syntax help
- Math-only derivations from user-supplied values
- Fiction, poetry, creative writing where ground truth doesn't apply
- When the user explicitly says "I don't need verification, just brainstorm"

### Borderline cases — trigger unless clearly excluded
- Mixed conceptual + specific responses (the specifics still need audit)
- Recommendations involving specific products / brands / drugs
- "What's the consensus on X" where X has a quantitative answer
- Comparative answers with specific magnitudes ("A is bigger/faster than B")
- Didactic-cadence statements that encode a specific number ("X is now associated with Y% resistance"): teaching cadence is a flag for verification, not an exemption from it

When in doubt: trigger. The marginal cost of an extra audit pass is far smaller than the cost of one fabricated specific reaching the user.

## Procedure (the audit pass)

This runs after drafting the response, before sending: a critic-loop pass over the draft, not a replacement for the initial generation.

### Step 1: Scan the draft for specific factual claims

Walk the draft sentence-by-sentence. Mark every sentence matching "the X is Y" / "X serves Y" / "X costs Y" / "X takes Y [time]" / "X covers Y" / "resistance to X is now N%," where Y is a specific retrievable value (number, proper noun, address, date, percentage). Match: "Premium processing is $2,965." Non-match: "Premium processing is faster than regular processing" (no specific Y).

### Step 2: Classify each marked claim

| Class | Definition | Action |
|---|---|---|
| **A. VERIFIED THIS TURN** | Tool call this turn returned this exact value | Pass; use the tool-returned value verbatim |
| **B. VERIFIED PRIOR TURN** | Tool call earlier in this conversation returned this value | Pass; cross-check against the source, don't paraphrase |
| **C. STATED BY USER** | User provided this fact | Pass; user's facts are ground truth |
| **D. DERIVATION** | Computed from A/B/C with explicit reasoning shown | Pass; verify the math |
| **E. UNVERIFIED** | Training-data recall, no tool-call provenance | Fail; take action below |

### Step 3: For class E (unverified), take action

Two attempts, then mark. **Attempt 1:** verify via tool call (`web_search`, `web_fetch` for known authoritative URLs, `PubMed:search_articles` for surveillance/epidemiologic claims, or a specialized tool). **Attempt 2:** if not confident, retry against the official source: USCIS.gov/IRS.gov for pricing/policy, guidelines for clinical, PubMed for recent surveillance from the relevant geography, or the official statistic source for sports/finance.

**If both attempts fail**, rewrite the sentence to remove the value, mark it `[UNVERIFIED]`, or, if load-bearing, say so: "I couldn't verify [X] in two searches; consider checking [source] directly." Never leave a class E claim without one of these three.

### Step 4: Cross-check verified-earlier values

The most insidious failure mode: a search returned $2,965 earlier, but the model writes $2,805 later from training-data recall. Before writing any earlier-verified value, scan the prior tool outputs; if they differ from what you're about to write, use the tool output. Training-data recall never overrides fresh search results from the same conversation.

### Step 5: Geography / source cross-check (for surveillance specifics)

For any antimicrobial susceptibility, resistance rate, prevalence, or mortality figure, confirm the data's geography and source match the claim's.

| Failure mode | Example |
|---|---|
| US claim, international source | "US B. fragilis cefoxitin resistance is 65%" is actually Japanese SSI surveillance |
| Recent claim, old source | "Current MRSA rates are X%" citing a 2010 paper |
| ICU claim, ward data | "ICU Pseudomonas resistance is Y%" using an outpatient antibiogram |
| Pediatric claim, adult source | "Peds B. fragilis susceptibility is Z%" using an adult registry |

Geography, era, population, and care setting must match; if not, find a matching source or qualify the claim explicitly ("in Japanese SSI surveillance," not "in US data").

## Prioritization for long drafts

If the draft has > 10 specific claims, verify in order: load-bearing claims, then high-volatility specifics (prices, fees, processing times, officeholders, surveillance data), then low-volatility ones (historical dates, established geography) last. If budget runs out, mark the rest `[UNVERIFIED]` rather than skipping.

## Output format

The skill runs silently by default. Three outcomes:

1. **All clean** — send the response with no separate output.
2. **Issues silently fixed** — quietly fix the draft (replace stale value with verified value, remove unverifiable specific, add `[UNVERIFIED]` marker) and send it with no separate audit report.
3. **Systematic problems** — if 3+ specifics couldn't be verified, add a brief end-of-response note: "Verification note: [list of marked items and why they couldn't be verified]," signaling known gaps to spot-check.

Anti-pattern worked examples (jurisdiction, fee, dose, surveillance-percentage failures and their fixes) are in `references/history.md`.

## Execution mode and multi-agent guardrails

Preferred: dispatch the `fabrication-auditor` subagent (`this plugin's `agents/` directory` for Claude Code, `.claude/agents/` for Cowork) to run Step 1-5 in its own context, if the `Agent`/`Task` tool is available. If the tool is unavailable, run Step 1-5 inline.

Guardrails for any multi-agent context (research subagent + parent, or orchestrated workflow):

1. Subagent verification reports must include tool-call evidence; a claim of "I verified the fee is $2,965" with no shown search call has verified nothing.
2. The parent spot-checks at least 30% of subagent-verified specifics independently. No transitive trust.
3. Before delivery, the delivering agent must hold direct tool-call evidence (this turn or a prior turn) for every load-bearing specific.

Same rationale as citation-verification's guardrails: the confabulation pattern behind fake PMIDs also produces fake fees, distances, addresses, and surveillance percentages.

## Integration with other skills

- **citation-verification** — owns PMIDs, DOIs, journal references, paper titles; this skill never verifies identifiers. Both run when a response has both.
- **prompt-optimizer** — front of the workflow (input); this skill is the back (output). They compose.
- **icu-clinical-consult** — owns clinical reasoning; this skill audits any doses, susceptibility percentages, or other numeric claims it produces. Its Phase 3 delegates numeric-claim verification here.
- **writing-anti-ai** — owns voice and AI tells; this skill owns factual specifics. Both pass over the draft.
- **clinical-data-scientist** — this skill audits quantitative claims about external benchmarks; internal analyses derived from user-supplied data don't need it.

This skill is canonical and bundled inside `icu-clinical-consult`, `citation-verification`, and `grant-review`; sync any edit here to those three copies.

## Feedback loop
Found a missed edge case, a wrong-shaped output, or a rule that misfires?
Open an issue on this plugin's repository with the input and the output you
expected. Do not edit this skill mid-run.
Per-run case facts stay in this skill's own case log / memory store; only
*skill-file changes* go to the observation log.

## Versions

See `references/history.md` for the full version history and dated rationale.
