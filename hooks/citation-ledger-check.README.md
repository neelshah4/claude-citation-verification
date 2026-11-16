# citation-ledger-check

Stop hook. After each assistant turn it reads the final message, extracts PMID, DOI and NCT identifiers, and checks each against the citation ledger that `verify_citations.py` appends to. An identifier is covered when its newest ledger row is within the TTL and its status is `OK`, `OK-REVIEWED`, `CORRECTED` or `SUPERSEDED`. Anything else is reported. It cannot verify a citation; it only shows that the verifier ran.

This hook is optional. The plugin does not register it, so installing the plugin never adds a Stop hook. To use it, copy `citation-ledger-check.py` to a stable location (for example a `hooks/` folder in your Claude config directory) and add one entry to the `Stop` hooks in `settings.json`.

## settings.json entry

Add one object to the `Stop` hooks array, next to any entries already there (all use `type: command`):

```json
{
  "type": "command",
  "command": "python3 /absolute/path/to/citation-ledger-check.py"
}
```

Write the absolute path of the copy.

## Modes

Set with `CITATION_HOOK_MODE`.

| Mode | Behaviour |
|---|---|
| `warn` (default; any unrecognised value) | Appends one JSON line per uncovered identifier to the log and prints `{"systemMessage": "..."}`. Exit 0. The turn is never blocked. |
| `block` | Same log line, then prints `{"decision":"block","reason":"..."}`. Claude is told to run `verify_citations.py audit`, replace non-OK items with `[CITATION NEEDED]`, and restate. Exit 0. |
| `off` | Exits 0 immediately, before reading stdin. No log, no output. |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CITATION_HOOK_MODE` | `warn` | Mode above. |
| `CITATION_LEDGER_PATH` | `.claude/data/citation-ledger.jsonl` under the home directory (the default of `verify_citations.py --ledger`) | Ledger file (rows: `key`, `status`, `checked_at`, ...). |
| `CITATION_LEDGER_TTL_DAYS` | `30` | A row older than this is stale and does not cover. |
| `CITATION_HOOK_LOG_PATH` | `.claude/logs/citation-ledger-check.log` under the home directory | One JSON line per uncovered identifier: `ts`, `mode`, `key`, `reason` (`no_row`, `stale`, `status:<S>`), `session_id`. |
| `CITATION_VERIFIER_PATH` | `.claude/skills/clinical-citation-audit/scripts/verify_citations.py` under the home directory | File to import `extract_identifiers` from. That directory does not exist on a plugin install, so the default import fails and the built-in regex fallback runs. Set this variable to the plugin's `skills/citation-verification/scripts/verify_citations.py` to use the verifier's own extractor; setting it is enough, no code change is needed, and leaving it unset still works. Only keys starting `PMID:`, `DOI:` or `NCT:` are kept. |

## What counts

- Fires only when the message has a clinical anchor: a PMID or NCT id, or a DOI together with one of journal, trial, study, patients, cohort, randomized, ICU, PICU, pediatric, guideline. A bare DOI in non-clinical text is silent.
- Exempt: a fenced `citation-audit` block, `[CITATION NEEDED ...]`, `[RETRACTED ...]` and `[WRONG ID ...]` brackets, and any sentence that itself reports a failure (not found, does not exist, could not be verified, unverified, retracted, invalid, wrong ID, fabricated).
- Skipped without output: `stop_hook_active`, subagents (`agent_id` / `agent_type`), `plan` and `bypassPermissions` permission modes, malformed stdin, empty message, any internal error.

## Flip warn to block

1. Read the log (`CITATION_HOOK_LOG_PATH`, by default under `.claude/logs/` in the home directory) over a week of real use; confirm the flagged keys are real gaps and not extraction noise.
2. Set the mode where the hook runs. Either prefix the command in settings.json:
   `"command": "CITATION_HOOK_MODE=block CITATION_VERIFIER_PATH=/absolute/path/to/verify_citations.py python3 /absolute/path/to/citation-ledger-check.py"`
   or export it under `env` in settings.json.
3. To revert, set `warn` or `off`. `stop_hook_active` stops a block from looping: the turn after a block always passes.

## Tests

`cd hooks/tests && python3 -m unittest test_citation_ledger_check` (stdlib only, no network, temporary directories; never touches the default ledger or log).
