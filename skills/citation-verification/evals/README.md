# citation-verification eval bank

`cases.jsonl` holds 63 seeded-defect cases (43 `--ids` cases, 20 `--text` cases) for `scripts/verify_citations.py`. `grade.py` runs the script on each case and scores the returned status.

## Run

```
python3 evals/grade.py                    # all cases, script at ../scripts/verify_citations.py
python3 evals/grade.py --split train      # the split you may look at while tuning
python3 evals/grade.py --split test       # held-out split; run it to decide, not to tune
python3 evals/grade.py --script PATH --json --verbose
python3 evals/grade.py --batch            # one audit call per run instead of one per case
```

Run from this directory, or from the plugin root with the full path. Needs network (PubMed, Crossref, doi.org, arXiv, ClinicalTrials.gov). Each audit runs with a throwaway `--ledger` and `--raw-dir` in a temp directory, so the real ledger is never read or written. The default mode runs one `audit` call per case, about 40 calls, a few minutes at NCBI's polite rate.

Exit codes: 0 = `false_ok == 0`, `false_notfound == 0` and `accuracy >= 0.90`; 1 = graded and below the bar; 2 = script absent, cases unreadable, or nothing selected.

## Case format

```
{id, split, category, input, expect: {status_in: [...]}, provenance, simulate_transport_fail?}
```

`input` is one `--ids` entry. Text cases use `input: {"text": "..."}` and `expect: {"key": "PMID:123", status_in: [...]}`; the grader runs `audit --text FILE` and grades the record with that key (a missing record fails the case). Text lines are built mechanically from the efetch record of each paper. `simulate_transport_fail` (comma list of sources) is added to the run as `--simulate-transport-fail` for that case only. `provenance` records the live query that produced the identifier, and the raw API responses behind each label are kept outside the skill directory, one file per case id, in the build scratch folder.

Categories: `pmid_ok`, `doi_ok`, `pmid_bare`, `pmid_swapped`, `notfound`, `wrong_first_author`, `buried_author`, `retracted`, `erratum`, `concern`, `short_pmid`, `subtitle_split`, `arxiv_ok`, `ref_decoy`, `ref_positive`, `nct`, `transport_fail`.

Real identifiers came from live E-utilities, Crossref, arXiv and ClinicalTrials.gov queries. Synthetic defects were derived mechanically from those records (PMID paired with another paper's metadata, first author replaced or moved from the fourth slot, year shifted by 3, DOI last character changed, random PMID or NCT number) and each was confirmed live to behave as labelled. The registries change; a case whose live answer has changed (a new erratum on an "OK" paper, say) is a broken case, not a script failure. Re-check its provenance before blaming the script.

## Metrics

- `accuracy`: share of cases whose status is in `expect.status_in`.
- `false_ok`: cases whose expected set contains none of `OK`, `OK-REVIEWED`, `CORRECTED` but which came back as one of them. This is the unsafe error: a citation that should block or be reviewed passed as verified. One false OK fails the run regardless of accuracy.
- `false_notfound`: a simulated-transport case that returned `NOT-FOUND`. The spec forbids it; one fails the run.
- Category `known_limit` holds probes (same first-author surname and year, different paper). They print status and flags but never count toward accuracy, `false_ok` or `false_notfound`.

## Rules for using failures

Never paste a failing case, its identifier, its provenance string or its expected status into `SKILL.md`, the script's code comments or any prompt the script's author or the skill's agents read. Fix the class of defect that let the case through (a comparison rule, a screen, a missing endpoint), then re-run both splits. Tune on `train`; use `test` only to decide whether to ship. If a fix helps `train` and not `test`, it is overfit. A case that is wrong (the registry changed, the label was mis-derived) is repaired in `cases.jsonl` with a new provenance line, never worked around in the script.
