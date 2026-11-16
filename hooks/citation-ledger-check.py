#!/usr/bin/env python3
"""citation-ledger-check: Stop hook that flags PMID / DOI / NCT identifiers in the final
assistant message that have no fresh, passing row in the citation verification ledger.

The ledger is the JSONL file that verify_citations.py appends to (one row per identifier per
audit). This hook is the enforcement backstop for "clinical-citation-audit MUST fire before an
identifier reaches the user": it cannot verify anything itself, it only checks that the
verifier already did.

Modes (env CITATION_HOOK_MODE): warn (default) | block | off. Unknown values behave as warn.
FAILS OPEN: any error, bad input, or missing file exits 0 with no output. It never blocks a
turn on its own malfunction, and `stop_hook_active` is honoured first so block mode cannot loop.

Env overrides (all optional):
  CITATION_HOOK_MODE          warn | block | off
  CITATION_LEDGER_PATH        ledger file (default ~/.claude/data/citation-ledger.jsonl)
  CITATION_LEDGER_TTL_DAYS    freshness window in days (default 30)
  CITATION_HOOK_LOG_PATH      warn/block log (default ~/.claude/logs/citation-ledger-check.log)
  CITATION_VERIFIER_PATH      verify_citations.py to import extract_identifiers from
                              (default ~/.claude/skills/clinical-citation-audit/scripts/verify_citations.py)
"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
DEFAULT_LEDGER = os.path.join(HOME, ".claude", "data", "citation-ledger.jsonl")
DEFAULT_LOG = os.path.join(HOME, ".claude", "logs", "citation-ledger-check.log")
DEFAULT_VERIFIER = os.path.join(HOME, ".claude", "skills", "clinical-citation-audit",
                                "scripts", "verify_citations.py")
PASSING = {"OK", "OK-REVIEWED", "CORRECTED", "SUPERSEDED"}
KEY_TYPES = ("PMID:", "DOI:", "NCT:")
MAX_MESSAGE_CHARS = 400_000

# ---------------------------------------------------------------- exemptions
FENCE_RE = re.compile(r"```[ \t]*citation-audit\b.*?(?:```|\Z)", re.S | re.I)
BRACKET_RE = re.compile(r"\[\s*(?:CITATION\s+NEEDED|RETRACTED|WRONG\s+ID)[^\]]*\]", re.I)
# A sentence that itself reports an identifier as failing exempts the identifiers in it.
FAIL_RE = re.compile(
    r"\bnot\s+found\b|\bnot\s+(?:a\s+)?valid\b|\bdoes(?:\s+not|n['’]t)\s+(?:exist|resolve)\b|"
    r"\bno\s+such\b|\bnon-?existent\b|\bcould\s+not\s+be\s+(?:verified|found|confirmed|resolved)\b|"
    r"\bunable\s+to\s+(?:verify|confirm|find)\b|\bunverif(?:ied|iable)\b|\bretracted\b|"
    r"\bwrong\s+(?:id|identifier|pmid|doi)\b|\bfabricated\b|\binvalid\b|\bfailed\s+verification\b",
    re.I)
SENT_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
ANCHOR_WORD_RE = re.compile(
    r"\b(?:journal|trials?|stud(?:y|ies)|patients?|cohorts?|randomi[sz]ed|ICU|PICU|"
    r"pediatric|paediatric|guidelines?)\b", re.I)

# ---------------------------------------------------------------- fallback extraction
_PMID_RES = (
    re.compile(r"\b(?:PMID|PubMed\s*ID)\s*[:#]?\s*(\d{5,9})(?!\d)", re.I),
    re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})(?!\d)", re.I),
    re.compile(r"ncbi\.nlm\.nih\.gov/pubmed/(\d{5,9})(?!\d)", re.I),
)
_DOI_RE = re.compile(r"(?<![0-9A-Za-z])10\.\d{4,9}/[^\s<>\"'`]+")
_NCT_RE = re.compile(r"(?<![0-9A-Za-z])NCT\d{8}(?!\d)", re.I)
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _clean_doi(raw):
    d = raw
    while d:
        last = d[-1]
        if last in ".,;:!?*_'\"":
            d = d[:-1]
        elif last in _CLOSERS and d.count(last) > d.count(_CLOSERS[last]):
            d = d[:-1]
        else:
            break
    return d.lower()


def fallback_keys(text):
    """Regex fallback with the same PMID / DOI / NCT patterns as the verifier. Ordered, unique."""
    found = []
    for rx in _PMID_RES:
        for m in rx.finditer(text):
            found.append((m.start(), "PMID:" + m.group(1)))
    for m in _DOI_RE.finditer(text):
        d = _clean_doi(m.group(0))
        if re.match(r"10\.\d{4,9}/.+", d):
            found.append((m.start(), "DOI:" + d))
    for m in _NCT_RE.finditer(text):
        found.append((m.start(), "NCT:" + m.group(0).upper()))
    found.sort(key=lambda t: t[0])
    out, seen = [], set()
    for _, k in found:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _import_extractor():
    path = os.environ.get("CITATION_VERIFIER_PATH") or DEFAULT_VERIFIER
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_citation_verifier_ext", path)
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(mod)
        fn = getattr(mod, "extract_identifiers", None)
        return fn if callable(fn) else None
    except BaseException:
        return None


def extract_keys(text):
    """Keys for PMID / DOI / NCT identifiers in `text`. Uses the verifier's extract_identifiers
    when importable, else the regex fallback. Any failure in the import branch falls back."""
    fn = _import_extractor()
    if fn is not None:
        try:
            hits = fn(text)
            keys, seen = [], set()
            for h in hits:
                k = h.get("key") if isinstance(h, dict) else None
                if isinstance(k, str) and k.startswith(KEY_TYPES) and k not in seen:
                    seen.add(k)
                    keys.append(k)
            return keys
        except BaseException:
            pass
    return fallback_keys(text)


# ---------------------------------------------------------------- text preparation
def prepare(text):
    """Drop exempt regions: audit blocks, [CITATION NEEDED]-style brackets, and any sentence
    that itself reports a failure. Returns the text the identifiers are read from."""
    t = FENCE_RE.sub(" ", text)
    t = BRACKET_RE.sub(" ", t)
    kept = [s for s in SENT_SPLIT_RE.split(t) if s and not FAIL_RE.search(s)]
    return "\n".join(kept)


def has_anchor(text, keys):
    if any(k.startswith(("PMID:", "NCT:")) for k in keys):
        return True
    return bool(ANCHOR_WORD_RE.search(text)) and any(k.startswith("DOI:") for k in keys)


# ---------------------------------------------------------------- ledger
def _parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def ttl_days():
    try:
        v = float(os.environ.get("CITATION_LEDGER_TTL_DAYS", "30"))
        return v if v >= 0 else 30.0
    except ValueError:
        return 30.0


def newest_rows(path, keys):
    """{key: (checked_at|None, file_index, row)} for the newest row of each wanted key.
    Malformed lines are skipped. Cheap substring pre-filter keeps a 20k-row ledger fast."""
    wanted = {k.lower(): k for k in keys}
    best = {}
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return best
    with fh:
        for idx, line in enumerate(fh):
            low = line.lower()
            if not any(w in low for w in wanted):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            k = row.get("key")
            if not isinstance(k, str) or k.lower() not in wanted:
                continue
            canon = wanted[k.lower()]
            ts = _parse_ts(row.get("checked_at"))
            cur = best.get(canon)
            rank = (ts or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), idx)
            if cur is None or rank >= cur[0]:
                best[canon] = (rank, ts, row)
    return {k: (v[1], v[2]) for k, v in best.items()}


def classify(keys, ledger_path, now=None):
    """Return [(key, reason)] for identifiers without a fresh passing row."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = newest_rows(ledger_path, keys)
    limit = datetime.timedelta(days=ttl_days())
    bad = []
    for k in keys:
        hit = rows.get(k)
        if hit is None:
            bad.append((k, "no_row"))
            continue
        ts, row = hit
        status = row.get("status")
        if ts is None or now - ts > limit:
            bad.append((k, "stale"))
        elif status not in PASSING:
            bad.append((k, "status:%s" % status))
    return bad


# ---------------------------------------------------------------- output
def write_log(bad, mode, session_id):
    path = os.environ.get("CITATION_HOOK_LOG_PATH") or DEFAULT_LOG
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as fh:
            for k, reason in bad:
                fh.write(json.dumps({"ts": ts, "mode": mode, "key": k, "reason": reason,
                                     "session_id": session_id or None},
                                    ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def main():
    mode = (os.environ.get("CITATION_HOOK_MODE") or "warn").strip().lower()
    if mode == "off":
        return 0
    if mode not in ("warn", "block"):
        mode = "warn"
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    if data.get("stop_hook_active"):
        return 0
    if data.get("agent_id") or data.get("agent_type"):
        return 0
    if data.get("permission_mode", "default") in ("plan", "bypassPermissions"):
        return 0
    msg = data.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return 0
    msg = msg[:MAX_MESSAGE_CHARS]

    text = prepare(msg)
    keys = extract_keys(text)
    if not keys or not has_anchor(text, keys):
        return 0
    ledger = os.environ.get("CITATION_LEDGER_PATH") or DEFAULT_LEDGER
    bad = classify(keys, ledger)
    if not bad:
        return 0

    write_log(bad, mode, data.get("session_id"))
    names = ", ".join(k for k, _ in bad[:10]) + (" (+%d more)" % (len(bad) - 10) if len(bad) > 10 else "")
    if mode == "block":
        emit({"decision": "block", "reason": (
            "These identifiers in your reply have no fresh passing row in the citation ledger: %s. "
            "Run verify_citations.py audit on the draft (clinical-citation-audit), replace anything "
            "that is not OK with [CITATION NEEDED], then restate the reply." % names)})
    else:
        emit({"systemMessage": (
            "citation-ledger-check (warn): %d identifier(s) in the last reply have no fresh passing "
            "ledger row: %s. Run verify_citations.py audit before relying on them." % (len(bad), names))})
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit:
        raise
    except BaseException:
        rc = 0
    sys.exit(rc)
