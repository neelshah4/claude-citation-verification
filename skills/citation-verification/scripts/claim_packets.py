#!/usr/bin/env python3
"""claim_packets.py - claim-support tier for clinical-citation-audit.

The existence tier proves a cited paper is real. This tier checks that the paper says
what the sentence claims. Two subcommands:

  claim_packets.py build --audit audit.json [--fulltext] [--raw-dir DIR] --out packets.json
  claim_packets.py check --packets packets.json --verdicts verdicts.json --out claim_audit.json

`build` assembles one packet per verified record that carries a checkable assertion
(claim sentence + abstract re-fetched from PubMed by PMID). A fresh reviewer agent judges
each packet under references/claim-support.md and writes verdicts.json. `check` is the
deterministic gate: a verdict whose quoted evidence is not literally in the fetched text is
rejected, whatever the reviewer concluded. Models never assign the final status.

Exit codes. build: 0 ok, 2 usage/error. check: 0 claim_pass (no blocking row and every checkable packet judged), 1 otherwise,
2 usage/error.

Standard library only (Python 3.12). Contact email only from env CITATION_AUDIT_MAILTO;
NCBI key only from env NCBI_API_KEY.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

SCHEMA_VERSION = 1
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
VERIFIED_STATUSES = {"OK", "OK-REVIEWED", "CORRECTED", "SUPERSEDED"}
VERDICTS = {"SUPPORTED", "PARTIAL", "NOT-IN-ABSTRACT", "CONTRADICTED"}
SPAN_REQUIRED = {"SUPPORTED", "PARTIAL", "CONTRADICTED"}
BLOCKING = {"CONTRADICTED", "INVALID-SPAN"}
CHECK_VALUES = {
    "number": {"match", "mismatch", "not-stated", "n/a"},
    "direction": {"match", "reversed", "not-stated", "n/a"},
    "population": {"match", "different", "not-stated", "n/a"},
    "primary_vs_subgroup": {"primary", "secondary", "subgroup", "post-hoc", "not-stated", "n/a"},
}
BAD_CHECK_VALUES = {"mismatch", "reversed", "different"}
MIN_SPAN_CHARS = 20
MIN_SPAN_WORDS = 4
FULLTEXT_CAP = 8000
_last_call = [0.0]


# ----------------------------------------------------------------------------- network

def _user_agent() -> str:
    return "citation-audit-claim-packets/1.0 (claim-support tier; stdlib urllib)"


def _eutils(endpoint: str, params: dict, timeout: float = 20.0, attempts: int = 2) -> bytes:
    p = dict(params)
    p["tool"] = "citation-audit"
    mailto = os.environ.get("CITATION_AUDIT_MAILTO")
    if mailto:
        p["email"] = mailto
    key = os.environ.get("NCBI_API_KEY")
    if key:
        p["api_key"] = key
    gap = 0.11 if key else 0.36
    url = EUTILS + endpoint + "?" + urllib.parse.urlencode(p)
    last_err: Exception | None = None
    for i in range(attempts):
        wait = gap - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # noqa: PERF203
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"E-utilities {endpoint} failed after {attempts} attempts: {last_err}")


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def fetch_pubmed(pmids: list[str], raw_dir: str | None = None) -> dict[str, dict]:
    """Return {pmid: {title, abstract|None, doi, pmc, publication_types}} via raw efetch XML."""
    out: dict[str, dict] = {}
    for i in range(0, len(pmids), 100):
        batch = pmids[i:i + 100]
        raw = _eutils("efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
        if raw_dir:
            os.makedirs(raw_dir, exist_ok=True)
            with open(os.path.join(raw_dir, f"efetch-{batch[0]}-{len(batch)}.xml"), "wb") as fh:
                fh.write(raw)
        root = ET.fromstring(raw)
        for art in root.findall(".//PubmedArticle"):
            pmid = _text(art.find("./MedlineCitation/PMID"))
            parts = []
            for ab in art.findall("./MedlineCitation/Article/Abstract/AbstractText"):
                body = _text(ab)
                if not body:
                    continue
                label = ab.get("Label")
                parts.append(f"{label.upper()}: {body}" if label else body)
            ids = {a.get("IdType"): _text(a) for a in art.findall("./PubmedData/ArticleIdList/ArticleId")}
            out[pmid] = {
                "title": _text(art.find("./MedlineCitation/Article/ArticleTitle")),
                "abstract": " ".join(parts) if parts else None,
                "doi": ids.get("doi") or None,
                "pmc": ids.get("pmc") or None,
                "publication_types": [_text(t) for t in art.findall(
                    "./MedlineCitation/Article/PublicationTypeList/PublicationType")],
            }
    return out


def pmid_from_doi(doi: str) -> str | None:
    raw = _eutils("esearch.fcgi", {"db": "pubmed", "term": f"{doi}[aid]", "retmode": "json"})
    ids = json.loads(raw).get("esearchresult", {}).get("idlist", [])
    return ids[0] if len(ids) == 1 else None


def fetch_pmc_excerpt(pmc: str) -> str | None:
    """Open-access PMC body text (Results first), capped; None when no body is served."""
    try:
        raw = _eutils("efetch.fcgi", {"db": "pmc", "id": pmc.replace("PMC", ""), "retmode": "xml"})
        root = ET.fromstring(raw)
    except (RuntimeError, ET.ParseError):
        return None
    body = root.find(".//body")
    if body is None:
        return None
    for el in body.iter():  # block elements end with a space so headings do not run into text
        if el.tag in ("title", "p", "label", "caption", "td", "th"):
            el.tail = " " + (el.tail or "")
    chunks = []
    for sec in body.iter("sec"):
        title = _text(sec.find("title")).lower()
        if "result" in title:
            chunks.append(_text(sec))
    text = " ".join(chunks) if chunks else _text(body)
    text = text.strip()
    return text[:FULLTEXT_CAP] if text else None


# ------------------------------------------------------------------ text and numbers

_ID_STRIP = [
    re.compile(r"\bPMID:?\s*\d{4,9}\b", re.I),
    re.compile(r"\bPMC\d+\b", re.I),
    re.compile(r"\bNCT\d{8}\b", re.I),
    re.compile(r"\bISRCTN\d+\b", re.I),
    re.compile(r"\b10\.\d{4,9}/\S+", re.I),
    re.compile(r"\barXiv:\s*\d{4}\.\d{4,5}(v\d+)?", re.I),
    re.compile(r"https?://\S+", re.I),
    re.compile(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]"),                    # [12], [3-5]
    re.compile(r"\b(?:et al\.?|and colleagues)[,;]?\s*\(?\s*(?:19|20)\d{2}[a-z]?\)?", re.I),
    re.compile(r"\(\s*[A-Z][A-Za-z\-' ]+(?:,\s*[A-Za-z .]+)?,?\s*(?:19|20)\d{2}[a-z]?\s*\)"),
]


def strip_identifiers(text: str) -> str:
    for rx in _ID_STRIP:
        text = rx.sub(" ", text)
    return text


_NUM_RX = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d*\.\d+|\d+)")


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = (s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", "-").replace("−", "-"))
    return re.sub(r"\s+", " ", s).strip().casefold()


def numbers_in(text: str) -> set[Decimal]:
    """Normalised numeric values: 30% == 30.0 == 30; .5 == 0.5; 1,000 == 1000."""
    vals: set[Decimal] = set()
    for m in _NUM_RX.finditer(norm_text(text)):
        tok = m.group(1).replace(",", "")
        try:
            vals.add(Decimal(tok).normalize())
        except InvalidOperation:
            continue
    return vals


def claim_numbers(claim: str) -> set[Decimal]:
    return numbers_in(strip_identifiers(claim))


_COMPARATORS = re.compile(
    r"\b(reduc\w*|increas\w*|decreas\w*|lower\w*|higher|greater|fewer|more|less|improv\w*|worse\w*|"
    r"superior\w*|inferior\w*|non-?inferior\w*|no (?:significant )?(?:difference|benefit|effect|association)|"
    r"did not|failed to|associated with|odds|hazard|risk ratio|relative risk|mortality|survival|"
    r"significant\w*|versus|vs\.?|compared (?:with|to)|than)\b", re.I)
_STAT = re.compile(r"\b(OR|HR|RR|aOR|aHR|CI|IQR|p\s*[<=>]|NNT|SMD|MD)\b|\bp-?value\b|%|\bpercent\b", re.I)
_POPULATION = re.compile(
    r"\b(child\w*|pediatric\w*|paediatric\w*|infant\w*|neonat\w*|newborn\w*|adolescen\w*|preterm|"
    r"adult\w*|elderly|older|women|men|pregnan\w*)\b", re.I)


def has_checkable_assertion(claim: str) -> tuple[bool, str]:
    """True when the claim sentence carries a number, stat, comparator or population word."""
    body = strip_identifiers(claim or "")
    if claim_numbers(claim or ""):
        return True, "number"
    if _STAT.search(body):
        return True, "statistic"
    if _COMPARATORS.search(body):
        return True, "comparator"
    if _POPULATION.search(body):
        return True, "population"
    return False, "no number, statistic, comparator or population word"


# ------------------------------------------------------------------------------- build

def cmd_build(args: argparse.Namespace) -> int:
    with open(args.audit, encoding="utf-8") as fh:
        audit = json.load(fh)
    records = audit.get("records", [])
    skipped, not_checkable, work = [], [], []
    for rec in records:
        key = rec.get("key", "?")
        if rec.get("status") not in VERIFIED_STATUSES:
            skipped.append({"key": key, "reason": f"status {rec.get('status')} is not a verified status"})
            continue
        claim = (rec.get("claim") or rec.get("context") or "").strip()
        if not claim:
            skipped.append({"key": key, "reason": "no claim or context sentence"})
            continue
        ok, why = has_checkable_assertion(claim)
        if not ok:
            skipped.append({"key": key, "reason": f"no checkable assertion ({why})"})
            continue
        res = rec.get("resolved") or {}
        pmid = res.get("pmid") or (rec.get("id") if rec.get("type") == "PMID" else None)
        doi = res.get("doi") or (rec.get("id") if rec.get("type") == "DOI" else None)
        if not pmid and doi:
            try:
                pmid = pmid_from_doi(doi)
            except RuntimeError as e:
                skipped.append({"key": key, "reason": f"PMID lookup failed: {e}"})
                continue
        work.append({"key": key, "claim": claim, "pmid": str(pmid) if pmid else None, "doi": doi,
                     "pmc": res.get("pmc"), "assertion": why})
    pmids = sorted({w["pmid"] for w in work if w["pmid"]})
    fetched = fetch_pubmed(pmids, args.raw_dir) if pmids else {}
    packets = []
    for w in work:
        rec = fetched.get(w["pmid"]) if w["pmid"] else None
        abstract = rec["abstract"] if rec else None
        pmc = w["pmc"] or (rec or {}).get("pmc")
        excerpt = None
        if args.fulltext and pmc:
            excerpt = fetch_pmc_excerpt(pmc)
        sources = []
        if w["pmid"]:
            sources.append(f"https://pubmed.ncbi.nlm.nih.gov/{w['pmid']}/")
        if excerpt and pmc:
            sources.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/")
        packets.append({
            "key": w["key"], "claim": w["claim"], "pmid": w["pmid"],
            "doi": w["doi"] or (rec or {}).get("doi"),
            "title": (rec or {}).get("title"),
            "publication_types": (rec or {}).get("publication_types", []),
            "abstract": abstract, "fulltext_excerpt": excerpt, "sources": sources,
            "assertion_type": w["assertion"],
        })
        if abstract is None and excerpt is None:
            not_checkable.append({"key": w["key"], "reason": "no abstract available from PubMed"
                                  if w["pmid"] else "no PMID resolved"})
    out = {"kind": "ClaimPackets", "schema_version": SCHEMA_VERSION,
           "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "audit_id": audit.get("audit_id"), "fulltext": bool(args.fulltext),
           "packets": packets, "skipped": skipped, "not_checkable": not_checkable}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"packets: {len(packets)} · skipped (no checkable assertion or unverified): {len(skipped)} "
          f"· not checkable (no abstract): {len(not_checkable)} -> {args.out}")
    for s in skipped:
        print(f"  skipped {s['key']}: {s['reason']}")
    for n in not_checkable:
        print(f"  not checkable {n['key']}: {n['reason']}")
    return 0


# ------------------------------------------------------------------------------- check

def packet_text(p: dict) -> str:
    return "\n".join(x for x in (p.get("title"), p.get("abstract"), p.get("fulltext_excerpt")) if x)


def check_span(span: str, text: str) -> tuple[bool, str]:
    """Each fragment (split on '...' or an ellipsis) must be a verbatim substring."""
    hay = norm_text(text)
    frags = [f for f in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\])\s*", span or "") if f.strip()]
    if not frags:
        return False, "empty evidence_span"
    for f in frags:
        n = norm_text(f)
        if len(n) < MIN_SPAN_CHARS or len(n.split()) < MIN_SPAN_WORDS:
            return False, f"fragment too short to be evidence: {f.strip()[:40]!r}"
        if n not in hay:
            return False, f"not found in fetched text: {f.strip()[:60]!r}"
    return True, "ok"


def validate_verdict(v: dict) -> str | None:
    if not isinstance(v, dict) or not v.get("key"):
        return "verdict is not an object with a key"
    if v.get("verdict") not in VERDICTS:
        return f"verdict {v.get('verdict')!r} not in {sorted(VERDICTS)}"
    checks = v.get("checks")
    if not isinstance(checks, dict):
        return "checks object missing"
    for name, allowed in CHECK_VALUES.items():
        if checks.get(name) not in allowed:
            return f"checks.{name}={checks.get(name)!r} not in {sorted(allowed)}"
    if not isinstance(v.get("evidence_span", ""), str):
        return "evidence_span must be a string"
    return None


def cmd_check(args: argparse.Namespace) -> int:
    with open(args.packets, encoding="utf-8") as fh:
        pk = json.load(fh)
    with open(args.verdicts, encoding="utf-8") as fh:
        vs = json.load(fh)
    if isinstance(vs, dict):
        vs = vs.get("verdicts", [])
    packets = {p["key"]: p for p in pk.get("packets", [])}
    seen: set[str] = set()
    rows, not_checked, unmatched = [], [], []
    for v in vs:
        key = v.get("key") if isinstance(v, dict) else None
        if key not in packets:
            unmatched.append({"key": key, "reason": "no packet with this key"})
            continue
        if key in seen:
            not_checked.append({"key": key, "reason": "duplicate verdict ignored"})
            continue
        seen.add(key)
        p = packets[key]
        text = packet_text(p)
        if not (p.get("abstract") or p.get("fulltext_excerpt")):
            not_checked.append({"key": key, "reason": "not checkable: no abstract fetched"})
            continue
        bad = validate_verdict(v)
        if bad:
            not_checked.append({"key": key, "reason": f"malformed verdict: {bad}"})
            continue
        final, gate = v["verdict"], []
        span = v.get("evidence_span", "") or ""
        if final in SPAN_REQUIRED:
            ok, why = check_span(span, text)
            if not ok:
                final, gate = "INVALID-SPAN", [f"SPAN-NOT-VERBATIM: {why}"]
            elif final == "SUPPORTED":
                missing = sorted(claim_numbers(p["claim"]) - numbers_in(span))
                if missing:
                    final = "INVALID-SPAN"
                    gate = ["NUMBER-NOT-IN-SPAN: " + ", ".join(format(m, "f") for m in missing)]
        if final == "SUPPORTED":
            flagged = [k for k, val in v["checks"].items() if val in BAD_CHECK_VALUES]
            if flagged:
                final = "INCONSISTENT"
                gate = [f"SUPPORTED with failing checks: {', '.join(flagged)}; re-review"]
        rows.append({"key": key, "verdict": final, "reviewer_verdict": v["verdict"],
                     "evidence_span": span, "checks": v["checks"], "note": v.get("note", ""),
                     "gate": gate, "claim": p["claim"], "pmid": p.get("pmid")})
    for key, p in packets.items():
        if key in seen:
            continue
        if not (p.get("abstract") or p.get("fulltext_excerpt")):
            not_checked.append({"key": key, "reason": "not checkable: no abstract fetched"})
        else:
            not_checked.append({"key": key, "reason": "no reviewer verdict returned"})
    for n in pk.get("not_checkable", []):
        if not any(x["key"] == n["key"] for x in not_checked):
            not_checked.append({"key": n["key"], "reason": f"not checkable: {n['reason']}"})
    judged = {r["key"] for r in rows}
    unjudged = [k for k, p in packets.items()
                if (p.get("abstract") or p.get("fulltext_excerpt")) and k not in judged]
    claim_complete = not unjudged
    blocking = [r for r in rows if r["verdict"] in BLOCKING]
    advisory = [r for r in rows if r["verdict"] not in BLOCKING]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    body = json.dumps(rows, sort_keys=True).encode()
    out = {"kind": "ClaimAudit", "schema_version": SCHEMA_VERSION,
           "audit_id": pk.get("audit_id"), "packets_sha256": hashlib.sha256(
               json.dumps(pk, sort_keys=True).encode()).hexdigest(),
           "rows_sha256": hashlib.sha256(body).hexdigest(),
           "limit": "abstract-only unless fulltext_excerpt present; absence from an abstract is not contradiction",
           "counts": counts, "claim_complete": claim_complete, "unjudged": unjudged,
           "claim_pass": (not blocking) and claim_complete, "blocking": blocking, "advisory": advisory,
           "not_checked": not_checked, "unmatched_verdicts": unmatched}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    head = f"claim_pass: {str(out['claim_pass']).lower()}"
    if not claim_complete:
        head += f" · incomplete: {len(unjudged)} packets without a verdict"
    print(f"{head} · blocking: {len(blocking)} · advisory: {len(advisory)} · "
          f"not checked: {len(not_checked)} · counts: {counts}")
    for r in blocking:
        print(f"  BLOCK {r['key']}: {r['verdict']} {'; '.join(r['gate']) or r['note']}")
    for n in not_checked:
        print(f"  not checked {n['key']}: {n['reason']}")
    return 0 if out["claim_pass"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="assemble claim packets from audit.json")
    b.add_argument("--audit", required=True)
    b.add_argument("--fulltext", action="store_true", help="add PMC open-access excerpt when a PMC id exists")
    b.add_argument("--raw-dir", help="save raw efetch XML here")
    b.add_argument("--out", required=True)
    c = sub.add_parser("check", help="gate reviewer verdicts against packet text")
    c.add_argument("--packets", required=True)
    c.add_argument("--verdicts", required=True)
    c.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        return cmd_build(args) if args.cmd == "build" else cmd_check(args)
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
