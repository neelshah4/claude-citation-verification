#!/usr/bin/env python3
"""verify_citations.py: deterministic citation verifier (clinical-citation-audit v2).

A script, not a model, fetches registry records and assigns the status.
Standard library only (Python 3.12).

Subcommands
  audit    verify every identifier in a text or an ids file; write audit.json
  block    print the fenced citation-audit block from an audit.json
  review   record a reviewer decision on a NEEDS-REVIEW record
  extract  extraction only (JSON to stdout)

Environment (all optional)
  CITATION_AUDIT_MAILTO  contact address for the polite API pools
  NCBI_API_KEY           raises the NCBI limit from 3 to 10 requests per second

Importable: extract_identifiers(text) -> list[dict]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCHEMA_VERSION = 2
TOOL_NAME = "citation-audit"
ALL_SOURCES = ("pubmed", "crossref", "doiorg", "openalex", "arxiv", "ctgov", "pmc", "url", "isrctn", "eudract")
STATUSES = ("OK", "OK-REVIEWED", "CORRECTED", "NEEDS-REVIEW", "WRONG-ID", "MISMATCH", "NOT-FOUND",
            "RETRACTED", "CONCERN", "SUPERSEDED", "UNVERIFIED")
BLOCKING = {"NEEDS-REVIEW", "WRONG-ID", "MISMATCH", "NOT-FOUND", "RETRACTED", "CONCERN", "UNVERIFIED"}
VERIFIED = {"OK", "OK-REVIEWED", "CORRECTED", "SUPERSEDED"}
# worst first, used when one identifier is cited in several places
SEVERITY = ["RETRACTED", "CONCERN", "WRONG-ID", "MISMATCH", "NEEDS-REVIEW", "UNVERIFIED", "NOT-FOUND",
            "CORRECTED", "OK"]
LEDGER_REUSE_TYPES = {"PMID", "DOI", "PMC", "ARXIV", "NCT"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home() -> str:
    return os.path.expanduser("~")


def default_ledger() -> str:
    return os.path.join(home(), ".claude", "data", "citation-ledger.jsonl")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


# ----------------------------------------------------------------------------
# Normalization and matching
# ----------------------------------------------------------------------------

def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str) -> str:
    s = strip_diacritics(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def jaccard(a: str, b: str) -> float:
    ta, tb = set(norm_text(a).split()), set(norm_text(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(cited: str, variants: list[str]) -> float:
    return max((jaccard(cited, v) for v in variants if v), default=0.0)


PARTICLES = {"von", "van", "de", "der", "den", "del", "della", "di", "da", "dos", "du", "la", "le", "el",
             "al", "bin", "ibn", "ter", "ten", "op", "zu", "zur", "st"}


def _is_initials(tok: str) -> bool:
    t = tok.replace(".", "")
    return 1 <= len(t) <= 3 and t.isalpha() and t.upper() == t


def cited_surname_tokens(cited: str) -> set[str]:
    s = re.sub(r"(?i)\bet\s+al\.?|\band\s+others\b", " ", cited or "")
    raw = [t for t in re.split(r"[\s,]+", s.strip()) if t]
    keep = [t for t in raw if not _is_initials(t)] or raw
    toks = set(norm_text(" ".join(keep)).split()) - PARTICLES
    return toks or set(norm_text(" ".join(keep)).split())


def record_surname_tokens(family: str) -> set[str]:
    toks = set(norm_text(family).split()) - PARTICLES
    return toks or set(norm_text(family).split())


def author_match(cited: str, families: list[str]) -> tuple[str, int | None]:
    """Return ('first'|'later'|'none'|'unknown', index)."""
    if not families:
        return "unknown", None
    ct = cited_surname_tokens(cited)
    if not ct:
        return "unknown", None
    for i, fam in enumerate(families):
        rt = record_surname_tokens(fam)
        if rt and (ct <= rt or rt <= ct):
            return ("first" if i == 0 else "later"), i
    return "none", None


_JSTOP = {"of", "the", "and", "for", "in", "de", "la", "et", "und"}


def journal_tokens(s: str) -> list[str]:
    return [t for t in norm_text(s).split() if t not in _JSTOP]


def _tok_eq(x: str, y: str) -> bool:
    return x == y or y.startswith(x) or x.startswith(y)


def _contig(a: list[str], b: list[str]) -> bool:
    if not a or len(a) > len(b):
        return False
    for i in range(len(b) - len(a) + 1):
        if all(_tok_eq(a[j], b[i + j]) for j in range(len(a))):
            return True
    return False


def journal_match(cited: str, variants: list[str]) -> bool | None:
    vs = [journal_tokens(v) for v in variants if v]
    ct = journal_tokens(cited)
    if not vs or not ct:
        return None
    for v in vs:
        if ct == v or _contig(ct, v) or _contig(v, ct):
            return True
    return False


def to_int_year(v) -> int | None:
    if v is None:
        return None
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(v))
    return int(m.group(1)) if m else None


def norm_doi(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"(?i)^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", s)
    s = urllib.parse.unquote(s)
    return clean_trailing(s).lower()


def clean_trailing(s: str) -> str:
    prev = None
    while s and s != prev:
        prev = s
        while s and s[-1] in ".,;:'\"*_>`":
            s = s[:-1]
        for close, opn in ((")", "("), ("]", "["), ("}", "{")):
            if s.endswith(close) and s.count(close) > s.count(opn):
                s = s[:-1]
    return s


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------

PATTERN_DOC = {
    "PMID": r"PMIDs?[:# ]\d{5,9} | PubMed ID \d{5,9} | pubmed.ncbi.nlm.nih.gov/\d{5,9} | ncbi.nlm.nih.gov/pubmed/\d{5,9}",
    "DOI": r"10\.\d{4,9}/\S+ (bare, doi:, doi.org/)",
    "PMC": r"PMC\d+",
    "ARXIV": r"arXiv:NNNN.NNNNN | arxiv.org/abs/... | old-style archive/NNNNNNN",
    "NCT": r"NCT\d{8}",
    "ISRCTN": r"ISRCTN\d{8}",
    "EUDRACT": r"\d{4}-\d{6}-\d{2}",
    "URL": r"https?://... with a path, not an identifier form",
    "GUIDELINE": "issuing body / guideline acronym / trial-group name near a 4-digit year; "
                 "guideline|consensus|statement|recommendation(s)|position paper near a year",
}

RE_PMID_LABEL = re.compile(
    r"(?i)(?<![A-Za-z])PMIDs?\s*[:#]?\s*((?<!\d)\d{5,9}(?!\d)(?:\s*(?:,|;|/|&|\band\b)\s*(?<!\d)\d{5,9}(?!\d))*)")
RE_PUBMED_ID = re.compile(r"(?i)\bPubMed\s*(?:ID|identifier)s?\s*[:#]?\s*((?<!\d)\d{5,9}(?!\d))")
RE_PMID_URL = re.compile(r"(?i)(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/((?<!\d)\d{5,9}(?!\d))")
RE_DOI = re.compile(r"(?<![\d.])(10\.\d{4,9}/[^\s<>\"'`\[\]{}|\\^]+)")
RE_PMC = re.compile(r"(?<![A-Za-z0-9])(PMC\d{4,})(?!\d)")
RE_ARXIV_NEW = re.compile(r"(?i)arxiv\s*:\s*(\d{4}\.\d{4,5})(v\d+)?(?!\d)")
RE_ARXIV_URL = re.compile(r"(?i)arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})(v\d+)?")
RE_ARXIV_OLD = re.compile(r"(?i)arxiv\s*:\s*([a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})(v\d+)?")
RE_NCT = re.compile(r"(?<![A-Z0-9])(NCT\d{8})(?!\d)")
RE_ISRCTN = re.compile(r"(?<![A-Z0-9])(ISRCTN\d{8})(?!\d)")
RE_EUDRACT = re.compile(r"(?<![\d-])(\d{4}-\d{6}-\d{2})(?![\d-])")
RE_URL = re.compile(r"https?://[^\s<>\"'`]+")

GUIDELINE_BODIES = [
    "Surviving Sepsis Campaign", "SSC", "SCCM", "ATS", "ERS", "ESICM", "ESPNIC", "PALICC", "PALISI", "ELSO", "AHA",
    "ACC", "ESC", "IDSA", "WHO", "NICE", "KDIGO", "ARDSNet", "ARDS Network", "CDC", "AAP", "ACOG", "ASA", "ASPEN",
    "ESPEN", "AAN", "ERC", "ILCOR", "ACCP", "NHLBI", "AABB", "ISTH", "WSES", "Brain Trauma Foundation",
    "Neurocritical Care Society", "Pediatric Sepsis Definition Taskforce", "Sepsis-3", "Berlin Definition",
]
_BODY_ALT = "|".join(sorted((re.escape(b) for b in GUIDELINE_BODIES), key=len, reverse=True))
RE_GL_BODY = re.compile(r"\b(" + _BODY_ALT + r")\b((?:[\s,/&\-]+[\w\-]+){0,4}?)[\s,(\[]+((?:19|20)\d{2})(?!\d)")
RE_GL_WORD = re.compile(
    r"((?:[A-Z][\w\-/&]*[ ,]+){0,4}?)"
    r"((?i:guidelines?|consensus(?: statement| conference)?|statements?|recommendations?|position paper))"
    r"((?:[\s,:(\[]+[\w\-]+){0,4}?[\s,(\[]+(?:19|20)\d{2})(?!\d)")
RE_GL_TRIAL = re.compile(
    r"\b([A-Z][A-Z0-9\-]{3,14})\s+(?:trial|study|investigators|group|network|collaborative)\b"
    r"((?:[\s,:(\[]+[\w\-]+){0,3}?[\s,(\[]+(?:19|20)\d{2})(?!\d)")
TRIAL_DENY = {"COVID-19", "COVID", "SARS-COV-2", "SARS", "CASE", "PICU", "NICU", "CHEST", "TRIAL", "STUDY"}

PUBMED_STOPWORDS = set("""a about again all almost also although always among an and another any are as at be because been
before being between both but by can could did do does done due during each either enough especially etc for found from
further had has have having here how however i if in into is it its itself just kg km made mainly make may mg might ml mm
most mostly must nearly neither no nor obtained of often on our overall perhaps pmid quite rather really regarding seem
seen several should show showed shown shows significantly since so some such than that the their theirs them themselves
then there therefore these they this those through thus to upon use used using various very was we were what when which
while with within without would""".split())

STOP_NAMES = {
    "In", "Since", "By", "During", "After", "Before", "Until", "From", "Between", "Of", "At", "On", "The", "Table",
    "Figure", "Fig", "See", "Version", "Update", "Edition", "Phase", "Year", "Month", "Day", "Season", "Guidelines",
    "Guideline", "Consensus", "Statement", "Trial", "Study", "Studies", "January", "February", "March", "April",
    "May", "June", "July", "August", "September", "October", "November", "December", "Jan", "Feb", "Mar", "Apr",
    "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec", "Spring", "Summer", "Fall", "Winter", "PMID", "DOI",
    "Accessed", "Published", "Revised", "Updated", "Released", "Copyright", "Vol", "Volume", "Issue", "And", "Or",
    "For", "With", "To", "As", "An", "A", "Is", "Was", "Were", "Reported", "Recommended", "Recommendation",
    "Recommendations", "Society", "Association", "College", "Institute", "Group", "Network", "Campaign",
}

_LET = r"[^\W\d_]"
_TOK = _LET + r"+(?:['’\-]" + _LET + r"+)*"
_PART = r"(?:(?:van|von|de|der|den|del|della|di|da|dos|du|la|le|ter|ten|al|bin|ibn|el)\s+){0,2}"
NAME = _PART + _TOK
_YR = r"((?:19|20)\d{2})[a-z]?(?!\d)"
RE_AU_ETAL = re.compile(r"(?<![\w'’\-])(" + NAME + r"),?\s+et\s+al\.?,?\s*\(?(?:[A-Z][\w.&]*\s+){0,3}?" + _YR)
RE_AU_AND = re.compile(r"(?<![\w'’\-])(" + NAME + r")\s+(?:and|&)\s+" + NAME + r",?\s*\(?" + _YR)
RE_AU_JRN = re.compile(r"\(\s*(" + NAME + r")(?:\s+et\s+al\.?)?,\s*([A-Z][\w.&\s]{1,40}?)\s+" + _YR + r"\s*[;)]")
RE_AU_YEAR = re.compile(r"(?<![\w'’\-])(" + NAME + r"),?\s*\(?" + _YR + r"\)?")
RE_GROUP_WORDS = r"(?:Group|Investigators|Collaborators|Collaborative|Consortium|Network|Committee|Trialists|Taskforce|Task Force|Trial|Study)"
RE_AU_GROUP = re.compile(
    r"(?<![\w'’\-])((?:[A-Z][\w'’\-]*\s+){0,4}?" + RE_GROUP_WORDS + r"(?:\s+" + RE_GROUP_WORDS + r")?"
    r"|[A-Z]{2,}[A-Za-z0-9\-]{0,12}(?:\s+(?:[A-Za-z]+\s+){0,2}?" + RE_GROUP_WORDS + r")?)"
    r"\s*(?:et\s+al\.?)?,?\s*\(?" + _YR)
RE_ENTRY_MARK = re.compile(r"^\s*(?:\[\d{1,3}\]|\(?\d{1,3}[.):]|[-*•])\s+")
RE_ENTRY_AU = re.compile(
    r"^\s*(?:(?:\[\d{1,3}\]|\(?\d{1,3}[.):]|[-*•])\s+)?(" + NAME + r")(?:\s+[A-Z]{1,3}(?:[,.]|\s+et\s+al)|,\s+[A-Z]\.)")
NOT_GROUPS = {"JAMA", "BMJ", "NEJM", "CHEST", "AJRCCM", "ICM", "PCCM", "CCM", "PMID", "DOI", "ICU", "PICU", "NICU",
              "COVID", "RCT", "ARDS", "SOFA", "ELSO", "WHO", "USA", "UK", "ECMO"}
RE_YEAR_ANY = re.compile(r"(?<![\w/.\-])((?:19|20)\d{2})(?![\w\-])")
RE_QUOTED = re.compile(r"[\"“]([^\"”]{25,300})[\"”]")


def _sentence_bounds(line: str, rel: int) -> tuple[int, int]:
    protected = re.sub(r"(?i)\bet al\.", "et al§", line)
    protected = re.sub(r"\b([A-Z])\.", r"\1§", protected)
    protected = re.sub(r"(?i)\b(vs|fig|no|approx|ca|e\.g|i\.e)\.", lambda m: m.group(0)[:-1] + "§", protected)
    start = 0
    for m in re.finditer(r"[.!?]\s+(?=[A-Z\[(\"“])", protected):
        if m.end() <= rel:
            start = m.end()
        elif m.start() >= rel:
            return start, m.start() + 1
    return start, len(line)


def context_for(text: str, start: int, end: int) -> tuple[str, bool, int]:
    """Return (context, is_reference_entry, absolute offset of the context).

    A reference-list line is its own context; anywhere else the context is the single sentence holding the hit."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le == -1 else le
    line = text[ls:le]
    entry = bool(RE_ENTRY_MARK.match(line))
    ref_line = entry or bool(RE_ENTRY_AU.match(line.strip()) and RE_YEAR_ANY.search(line))
    if ref_line and len(line) <= 1500:
        seg, off = line, ls
    else:
        a, b = _sentence_bounds(line, start - ls)
        seg, off = line[a:b], ls + a
        if len(seg) > 700:
            lo = max(0, start - ls - 300)
            seg, off = line[lo: end - ls + 300], ls + lo
    lead = len(seg) - len(seg.lstrip())
    return seg.strip(), entry, off + lead


def _valid_surname(name: str) -> bool:
    toks = name.split()
    if not toks or not toks[-1][0].isupper():
        return False
    if any(t in STOP_NAMES for t in toks):
        return False
    if len(toks[-1]) < 2 or sum(ch.isupper() for ch in toks[-1]) >= 3:
        return False
    return all(t.islower() or t[0].isupper() for t in toks)


def _author_groups(ctx: str) -> list[tuple[int, int, dict]]:
    """All author-year / study-group-year citations in a sentence as (start, end, fields)."""
    cands: list[tuple[int, int, int, dict]] = []  # (start, end, priority, fields)
    for rx, kind in ((RE_AU_JRN, "jrn"), (RE_AU_ETAL, "etal"), (RE_AU_AND, "and"), (RE_AU_YEAR, "year")):
        for m in rx.finditer(ctx):
            name = m.group(1).strip()
            if not _valid_surname(name):
                continue
            if kind == "jrn":
                f = {"first_author": name, "year": int(m.group(3)), "journal": m.group(2).strip()}
            else:
                f = {"first_author": name, "year": int(m.group(2))}
            cands.append((m.start(), m.end(), 0, f))
    for m in RE_AU_GROUP.finditer(ctx):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if name.upper() in NOT_GROUPS or name in STOP_NAMES or len(name) < 3:
            continue
        cands = [c for c in cands if not (abs(c[1] - m.end()) <= 1 and c[3].get("first_author", "") in name
                                          and c[3].get("journal") is None)]
        cands.append((m.start(), m.end(), 1, {"cited_group": name, "year": int(m.group(2))}))
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    kept: list[tuple[int, int, int, dict]] = []
    for c in cands:  # drop spans nested in an earlier, wider one
        if not any(k[0] <= c[0] and c[1] <= k[1] for k in kept):
            kept.append(c)
    return [(c[0], c[1], c[3]) for c in kept]


def _paren_spans(ctx: str) -> list[tuple[int, int]]:
    out, stack = [], []
    for i, ch in enumerate(ctx):
        if ch in "([":
            stack.append(i)
        elif ch in ")]" and stack:
            out.append((stack.pop(), i))
    return out


def attach_cited(ctx: str, spans: list[tuple[int, int]]) -> list[dict]:
    """Parsed cited{} for each identifier span (context coordinates), attached only inside the same sentence.

    Within a sentence an author-year group attaches to an identifier in the same parenthetical, or to the group that
    immediately precedes it; with several candidates and no clear pairing nothing is attached."""
    res: list[dict] = [{} for _ in spans]
    ent = RE_ENTRY_AU.match(ctx)
    base: dict = {}
    if ent and _valid_surname(ent.group(1)):
        ym = RE_YEAR_ANY.search(ctx, ent.end())
        if ym:
            base = {"first_author": ent.group(1), "year": int(ym.group(1))}
    groups = [] if base else _author_groups(ctx)
    paren = _paren_spans(ctx)
    for k, (a, b) in enumerate(spans):
        if base:
            res[k] = dict(base)
            continue
        enc = [p for p in paren if p[0] < a and b <= p[1] + 1]
        scope = min(enc, key=lambda p: p[1] - p[0]) if enc else None
        pick = None
        others = [sp for j, sp in enumerate(spans) if j != k]
        if scope:
            inside = [g for g in groups if g[0] >= scope[0] and g[1] <= scope[1] + 1]
            ids_in = [j for j, sp in enumerate(spans) if scope[0] < sp[0] < scope[1]]
            if len(inside) == 1:
                pick = inside[0]
            elif len(inside) > 1 and len(inside) == len(ids_in):
                pick = inside[ids_in.index(k)]
            elif not inside:
                prev = [g for g in groups if g[1] <= scope[0] and ctx[g[1]:scope[0]].strip() in ("", ",")
                        and not any(g[1] <= sp[0] < scope[0] for sp in others)]
                if len(prev) == 1:
                    pick = prev[0]
        else:
            before = [g for g in groups if g[1] <= a and not any(g[1] <= sp[0] < a for sp in others)]
            after = [g for g in groups if g[0] >= b and not any(b <= sp[0] < g[0] for sp in others)]
            if len(before) == 1:
                pick = before[0]
            elif not before and len(after) == 1:
                pick = after[0]
        if pick:
            res[k] = dict(pick[2])
    quoted = RE_QUOTED.findall(ctx)
    if len(quoted) == 1 and len(quoted[0].split()) >= 5:
        for r in res:
            r["title"] = quoted[0].strip()
    for r in res:
        if r:
            r["source"] = "parsed"
    return res


def parse_cited(context: str, anchor: int | None = None) -> dict:
    """Convenience wrapper: cited{} for a single identifier at `anchor` (context coordinates)."""
    pos = anchor if anchor is not None else len(context)
    return attach_cited(context, [(pos, pos + 1)])[0]


def extract_identifiers(text: str) -> list[dict]:
    """Deterministic extraction. Each hit: key, raw, type, id, context, cited, entry, start."""
    hits: list[dict] = []
    id_spans: list[tuple[int, int]] = []

    def add(typ, ident, raw, s, e, year=None, track=True):
        ctx, entry, off = context_for(text, s, e)
        hit = {"key": f"{typ}:{ident}", "raw": raw, "type": typ, "id": ident, "context": ctx, "cited": {},
               "entry": entry, "start": s, "_ctx_off": off, "_rel": (s - off, e - off)}
        if year:
            hit["year"] = year
        hits.append(hit)
        if track:
            id_spans.append((s, e))

    for m in RE_PMID_LABEL.finditer(text):
        for n in re.finditer(r"\d{5,9}", m.group(1)):
            s = m.start(1) + n.start()
            add("PMID", str(int(n.group(0))), n.group(0), s, s + len(n.group(0)))
        id_spans.append((m.start(), m.end()))
    for rx in (RE_PUBMED_ID, RE_PMID_URL):
        for m in rx.finditer(text):
            add("PMID", str(int(m.group(1))), m.group(0), m.start(), m.end())
    for m in RE_DOI.finditer(text):
        d = clean_trailing(m.group(1))
        if len(d) > 8 and "/" in d:
            add("DOI", d.lower(), d, m.start(1), m.start(1) + len(d))
    for m in RE_PMC.finditer(text):
        add("PMC", m.group(1), m.group(1), m.start(), m.end())
    for rx in (RE_ARXIV_NEW, RE_ARXIV_URL, RE_ARXIV_OLD):
        for m in rx.finditer(text):
            add("ARXIV", m.group(1).lower(), m.group(0), m.start(), m.end())
    for m in RE_NCT.finditer(text):
        add("NCT", m.group(1), m.group(1), m.start(), m.end())
    for m in RE_ISRCTN.finditer(text):
        add("ISRCTN", m.group(1), m.group(1), m.start(), m.end())
    for m in RE_EUDRACT.finditer(text):
        add("EUDRACT", m.group(1), m.group(1), m.start(), m.end())
    # URLs that are not identifier forms
    for m in RE_URL.finditer(text):
        u = clean_trailing(m.group(0))
        s, e = m.start(), m.start() + len(u)
        if any(a < e and s < b for a, b in id_spans):
            continue
        p = urllib.parse.urlparse(u)
        host = (p.hostname or "").lower()
        if not host or p.path in ("", "/"):
            continue
        if host in ("doi.org", "dx.doi.org") or host.endswith("arxiv.org") or host == "pubmed.ncbi.nlm.nih.gov":
            continue
        if host.endswith("clinicaltrials.gov") and "/NCT" in u:
            continue
        norm = f"{p.scheme.lower()}://{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")
        add("URL", norm, u, s, e, track=False)
    # guideline candidates
    gl: list[tuple[int, int, str, int]] = []
    for m in RE_GL_BODY.finditer(text):
        gl.append((m.start(), m.end(), m.group(0), int(m.group(3))))
    for m in RE_GL_WORD.finditer(text):
        span = m.group(0)
        y = re.search(r"((?:19|20)\d{2})\s*$", span)
        gl.append((m.start(), m.end(), span, int(y.group(1)) if y else 0))
    for m in RE_GL_TRIAL.finditer(text):
        if m.group(1).upper() in TRIAL_DENY:
            continue
        y = re.search(r"((?:19|20)\d{2})\s*$", m.group(0))
        gl.append((m.start(), m.end(), m.group(0), int(y.group(1)) if y else 0))
    gl.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    kept: list[tuple[int, int, str, int]] = []
    for g in gl:
        if g[1] - g[0] > 140:
            continue
        if any(k[0] <= g[0] and g[1] <= k[1] for k in kept):
            continue
        kept = [k for k in kept if not (g[0] <= k[0] and k[1] <= g[1])] + [g]
    for s, e, span, y in kept:
        ident = norm_text(span)
        add("GUIDELINE", ident, re.sub(r"\s+", " ", span).strip(), s, e, year=y or None, track=False)
    hits.sort(key=lambda h: h["start"])
    by_ctx: dict[tuple[int, str], list[dict]] = {}
    for h in hits:
        if h["type"] != "GUIDELINE":
            by_ctx.setdefault((h["_ctx_off"], h["context"]), []).append(h)
    for (_, ctx), hs in by_ctx.items():
        for h, c in zip(hs, attach_cited(ctx, [h["_rel"] for h in hs])):
            h["cited"] = c
    for h in hits:
        h.pop("_ctx_off", None)
        h.pop("_rel", None)
    return hits


def normalize_entry_id(typ: str, ident: str) -> str:
    ident = (ident or "").strip()
    if typ == "PMID":
        return str(int(re.sub(r"\D", "", ident)))
    if typ == "DOI":
        return norm_doi(ident)
    if typ == "PMC":
        return "PMC" + re.sub(r"\D", "", ident)
    if typ == "ARXIV":
        return re.sub(r"(?i)^arxiv:\s*|v\d+$", "", ident).lower()
    if typ in ("NCT", "ISRCTN"):
        return ident.upper()
    if typ == "URL":
        p = urllib.parse.urlparse(ident)
        return f"{p.scheme.lower()}://{(p.hostname or '').lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")
    if typ == "GUIDELINE":
        return norm_text(ident)
    return ident


# ----------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------

class Transport(Exception):
    """Network-level failure after retries; never an authoritative answer."""


class Resp:
    def __init__(self, status, body, url, evidence):
        self.status, self.body, self.url, self.evidence = status, body, url, evidence

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError as e:
            raise Transport(f"unparseable JSON: {e}")

    def text(self):
        return self.body.decode("utf-8", "replace")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_is_public(urllib.parse.urlparse(newurl).hostname or ""):
            raise urllib.error.URLError("redirect to a non-public host blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return bool(infos)


class Net:
    GROUP_INTERVAL = {"ncbi": 0.34, "crossref": 0.06, "openalex": 0.12, "arxiv": 3.1, "ctgov": 0.15,
                      "doiorg": 0.1, "isrctn": 0.4, "url": 0.25, "eudract": 0.5}
    SOURCE_GROUP = {"pubmed": "ncbi", "pmc": "ncbi", "crossref": "crossref", "openalex": "openalex",
                    "arxiv": "arxiv", "ctgov": "ctgov", "doiorg": "doiorg", "isrctn": "isrctn", "url": "url",
                    "eudract": "eudract"}

    def __init__(self, raw_dir: str, timeout: float, simulate: set[str]):
        self.raw_dir = raw_dir
        self.timeout = timeout
        self.simulate = simulate
        self.last: dict[str, float] = {}
        self.n_files = 0
        self.mailto = os.environ.get("CITATION_AUDIT_MAILTO", "").strip()
        self.api_key = os.environ.get("NCBI_API_KEY", "").strip()
        if self.api_key:
            self.GROUP_INTERVAL = dict(self.GROUP_INTERVAL, ncbi=0.11)
        ua = f"{TOOL_NAME}/{SCHEMA_VERSION} (citation verifier; verify_citations.py)"
        if self.mailto:
            ua = f"{TOOL_NAME}/{SCHEMA_VERSION} (mailto:{self.mailto})"
        self.ua = ua
        self.requests = 0

    def _wait(self, group: str):
        gap = self.GROUP_INTERVAL.get(group, 0.1)
        wait = self.last.get(group, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last[group] = time.monotonic()

    def ncbi_params(self) -> dict:
        p = {"tool": TOOL_NAME}
        if self.mailto:
            p["email"] = self.mailto
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def polite_params(self) -> dict:
        return {"mailto": self.mailto} if self.mailto else {}

    def get(self, source: str, url: str, headers: dict | None = None, guard_host: bool = False,
            max_bytes: int = 12_000_000, save: bool = True) -> Resp:
        group = self.SOURCE_GROUP.get(source, source)
        hdrs = {"User-Agent": self.ua, "Accept": "*/*"}
        hdrs.update(headers or {})
        last_err = "unknown"
        for attempt in (1, 2):
            self._wait(group)
            self.requests += 1
            try:
                if source in self.simulate:
                    raise urllib.error.URLError("simulated transport failure (timeout)")
                if guard_host and not host_is_public(urllib.parse.urlparse(url).hostname or ""):
                    raise urllib.error.URLError("non-public or unresolvable host blocked")
                req = urllib.request.Request(url, headers=hdrs)
                opener = urllib.request.build_opener(_SafeRedirect) if guard_host else urllib.request.build_opener()
                try:
                    with opener.open(req, timeout=self.timeout) as r:
                        body = r.read(max_bytes)
                        status, final = r.status, r.geturl()
                except urllib.error.HTTPError as he:
                    body = he.read(max_bytes) if he.fp else b""
                    status, final = he.code, url
                    if status == 429 or status >= 500:
                        retry = he.headers.get("Retry-After", "") if he.headers else ""
                        if attempt == 1 and source not in self.simulate:
                            time.sleep(min(5.0, float(retry)) if retry.isdigit() else 1.5)
                        last_err = f"HTTP {status}"
                        continue
                ev = self._save(source, final, status, body, save)
                return Resp(status, body, final, ev)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                last_err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
                if attempt == 1 and source not in self.simulate:
                    time.sleep(1.5)
        raise Transport(f"{source}: {last_err} ({url.split('?')[0]})")

    def _save(self, source, url, status, body, save) -> dict:
        digest = sha256_bytes(body)
        path = None
        if save and self.raw_dir:
            os.makedirs(self.raw_dir, exist_ok=True)
            head = body[:64].lstrip()
            ext = "json" if head[:1] in (b"{", b"[") else ("xml" if head[:1] == b"<" else "txt")
            self.n_files += 1
            path = os.path.abspath(os.path.join(self.raw_dir, f"{source}-{self.n_files:04d}-{digest[:10]}.{ext}"))
            with open(path, "wb") as f:
                f.write(body)
        return {"source": source, "url": scrub_url(url), "http_status": status, "fetched_at": now_iso(),
                "raw_sha256": digest, "raw_path": path}


def scrub_url(url: str) -> str:
    """Drop credential-bearing query parameters from a URL before it is stored."""
    p = urllib.parse.urlparse(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in ("api_key", "email", "mailto")]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))


# ----------------------------------------------------------------------------
# Source parsers
# ----------------------------------------------------------------------------

def _t(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def parse_pubmed_xml(body: bytes) -> dict[str, dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise Transport(f"pubmed: unparseable XML ({e})")
    if root.tag != "PubmedArticleSet":
        raise Transport(f"pubmed: unexpected root <{root.tag}>")
    out: dict[str, dict] = {}
    for art in root.findall("PubmedArticle"):
        mc = art.find("MedlineCitation")
        if mc is None:
            continue
        pmid = _t(mc.find("PMID"))
        a = mc.find("Article")
        if not pmid or a is None:
            continue
        authors, families, lead_coll, seen_person, coll_names = [], [], 0, False, []
        for au in a.findall("AuthorList/Author"):
            if au.find("CollectiveName") is not None:
                nm = _t(au.find("CollectiveName"))
                authors.append(nm)
                families.append(nm)
                coll_names.append(nm)
                if not seen_person:
                    lead_coll += 1
            else:
                ln = _t(au.find("LastName"))
                if not ln:
                    continue
                seen_person = True
                ini = _t(au.find("Initials"))
                authors.append(f"{ln} {ini}".strip())
                families.append(ln)
        years = []
        y = _t(a.find("Journal/JournalIssue/PubDate/Year"))
        if not y:
            y = _t(a.find("Journal/JournalIssue/PubDate/MedlineDate"))
        if to_int_year(y):
            years.append(to_int_year(y))
        for ad in a.findall("ArticleDate/Year"):
            if to_int_year(_t(ad)):
                years.append(to_int_year(_t(ad)))
        doi = pmc = None
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            t = aid.get("IdType")
            if t == "doi" and not doi:
                doi = norm_doi(_t(aid))
            elif t == "pmc" and not pmc:
                pmc = _t(aid)
        if not doi:
            for e in a.findall("ELocationID"):
                if e.get("EIdType") == "doi":
                    doi = norm_doi(_t(e))
        ptypes = [_t(p) for p in a.findall("PublicationTypeList/PublicationType")]
        corr = []
        for cc in mc.findall("CommentsCorrectionsList/CommentsCorrections"):
            corr.append({"ref_type": cc.get("RefType", ""), "pmid": _t(cc.find("PMID")) or None,
                         "source": _t(cc.find("RefSource")) or None})
        abstract = " ".join(_t(x) for x in a.findall("Abstract/AbstractText"))
        jt = _t(a.find("Journal/Title"))
        iso = _t(a.find("Journal/ISOAbbreviation"))
        medabb = _t(mc.find("MedlineJournalInfo/MedlineTA"))
        out[pmid] = {
            "pmid": pmid, "title": _t(a.find("ArticleTitle")), "authors": authors, "families": families,
            "lead_collective": lead_coll, "collective_names": coll_names, "years": years, "journal": jt, "journal_variants": [v for v in (jt, iso, medabb) if v],
            "doi": doi, "pmc": pmc, "publication_types": ptypes, "corrections": corr,
            "has_abstract": bool(abstract.strip()),
        }
    return out


def build_signals(corrections: list[dict], ptypes: list[str], crossref_updates: list[dict],
                  openalex_retracted: bool | None) -> dict:
    sig = {"retraction": [], "concern": [], "erratum": [], "notice": []}
    for c in corrections:
        rt = (c.get("ref_type") or "")
        low = rt.lower()
        desc = f"{rt} PMID {c.get('pmid')}" if c.get("pmid") else rt
        if "retractionin" in low:
            sig["retraction"].append(f"PubMed linkage {desc}")
        elif "expressionofconcernin" in low:
            sig["concern"].append(f"PubMed linkage {desc}")
        elif low in ("erratumin", "correctedandrepublishedin"):
            sig["erratum"].append(desc)
        elif low in ("retractionof", "erratumfor", "expressionofconcernfor", "correctedandrepublishedfrom"):
            sig["notice"].append(f"record is the notice ({desc})")
    if any(p.lower() == "retracted publication" for p in ptypes) and not sig["retraction"]:
        sig["retraction"].append("PubMed publication type 'Retracted Publication'")
    for p in ptypes:
        if p.lower() in ("published erratum", "retraction of publication", "expression of concern"):
            sig["notice"].append(f"publication type '{p}'")
    for u in crossref_updates:
        t = (u.get("type") or "").lower().replace("-", "_")
        d = f"Crossref update {t} ({u.get('DOI', '?')})"
        if t in ("retraction", "withdrawal", "removal"):
            sig["retraction"].append(d)
        elif t == "expression_of_concern":
            sig["concern"].append(d)
        elif t in ("correction", "erratum", "corrigendum", "addendum", "clarification"):
            sig["erratum"].append(d)
    if openalex_retracted:
        sig["retraction"].append("OpenAlex is_retracted true")
    return sig


def new_resolved() -> dict:
    return {"title": None, "authors": [], "first_author": None, "year": None, "journal": None, "pmid": None,
            "doi": None, "pmc": None, "publication_types": [], "corrections": [], "has_abstract": False,
            "match_basis": {"title_variants": [], "author_families": [], "years": [], "journal_variants": [],
                            "signals": {"retraction": [], "concern": [], "erratum": [], "notice": []},
                            "sources": []}}


def resolved_from_pubmed(art: dict) -> dict:
    r = new_resolved()
    r.update({"title": art["title"], "authors": art["authors"][:25], "first_author": art["authors"][0] if art["authors"] else None,
              "year": art["years"][0] if art["years"] else None, "journal": art["journal"], "pmid": art["pmid"],
              "doi": art["doi"], "pmc": art["pmc"], "publication_types": art["publication_types"],
              "corrections": art["corrections"], "has_abstract": art["has_abstract"]})
    mb = r["match_basis"]
    mb.update({"title_variants": [art["title"]], "author_families": art["families"][:200], "years": art["years"],
               "lead_collective": art.get("lead_collective", 0), "collective_names": art.get("collective_names", []),
               "journal_variants": art["journal_variants"], "sources": ["pubmed"]})
    mb["signals"] = build_signals(art["corrections"], art["publication_types"], [], None)
    return r


def parse_crossref(msg: dict) -> dict:
    r = new_resolved()
    title = (msg.get("title") or [""])[0]
    sub = (msg.get("subtitle") or [""])[0]
    variants = [title] + ([f"{title}: {sub}"] if sub else [])
    fams, names = [], []
    for au in msg.get("author") or []:
        fam = au.get("family") or au.get("name") or ""
        if fam:
            fams.append(fam)
            names.append(f"{fam} {''.join(w[0] for w in (au.get('given') or '').split() if w)}".strip())
    years = []
    for k in ("issued", "published-print", "published-online", "published"):
        dp = (msg.get(k) or {}).get("date-parts") or [[None]]
        if dp and dp[0] and dp[0][0]:
            years.append(int(dp[0][0]))
    seen = []
    for y in years:
        if y not in seen:
            seen.append(y)
    jv = [j for j in (msg.get("container-title") or []) + (msg.get("short-container-title") or []) if j]
    ups = msg.get("updated-by") or []
    r.update({"title": title, "authors": names[:25], "first_author": names[0] if names else None,
              "year": seen[0] if seen else None, "journal": (msg.get("container-title") or [None])[0],
              "doi": norm_doi(msg.get("DOI", "")), "has_abstract": bool(msg.get("abstract"))})
    mb = r["match_basis"]
    mb.update({"title_variants": variants, "author_families": fams[:200], "years": seen, "journal_variants": jv,
               "sources": ["crossref"]})
    mb["signals"] = build_signals([], [], ups, None)
    return r


def parse_openalex(w: dict) -> dict:
    r = new_resolved()
    fams, names = [], []
    for a in w.get("authorships") or []:
        dn = ((a.get("author") or {}).get("display_name") or "").strip()
        if dn:
            parts = dn.split()
            fams.append(parts[-1])
            names.append(f"{parts[-1]} {''.join(p[0] for p in parts[:-1])}".strip())
    host = ((w.get("primary_location") or {}).get("source") or {})
    jn = host.get("display_name")
    ids = w.get("ids") or {}
    pm = ids.get("pmid", "") or ""
    pmid = pm.rstrip("/").split("/")[-1] if pm else None
    y = w.get("publication_year")
    r.update({"title": w.get("title") or w.get("display_name"), "authors": names[:25],
              "first_author": names[0] if names else None, "year": y, "journal": jn,
              "pmid": pmid, "doi": norm_doi(w.get("doi") or "") or None})
    mb = r["match_basis"]
    mb.update({"title_variants": [w.get("title") or w.get("display_name") or ""], "author_families": fams[:200],
               "years": [y] if y else [], "journal_variants": [jn] if jn else [], "sources": ["openalex"]})
    mb["signals"] = build_signals([], [], [], bool(w.get("is_retracted")))
    return r


def merge_resolved(primary: dict, other: dict) -> dict:
    """Union of match basis and signals; primary wins on display fields."""
    r = json.loads(json.dumps(primary))
    for k in ("title", "first_author", "year", "journal", "pmid", "doi", "pmc"):
        if not r.get(k) and other.get(k):
            r[k] = other[k]
    if not r["authors"] and other["authors"]:
        r["authors"] = other["authors"]
    r["publication_types"] = sorted(set(r["publication_types"]) | set(other["publication_types"]))
    r["corrections"] = r["corrections"] + [c for c in other["corrections"] if c not in r["corrections"]]
    r["has_abstract"] = r["has_abstract"] or other["has_abstract"]
    a, b = r["match_basis"], other["match_basis"]
    for k in ("title_variants", "years", "journal_variants", "sources"):
        a[k] = a[k] + [x for x in b[k] if x not in a[k]]
    if not a["author_families"]:
        a["author_families"] = b["author_families"]
    for k in a["signals"]:
        a["signals"][k] = a["signals"][k] + [x for x in b["signals"][k] if x not in a["signals"][k]]
    return r


# ----------------------------------------------------------------------------
# Evaluation (pure function: resolved + cited -> status)
# ----------------------------------------------------------------------------

_GROUP_STOP = {"group", "groups", "investigators", "collaborators", "collaborative", "consortium", "network",
               "committee", "trialists", "taskforce", "task", "force", "trial", "study", "the", "and", "of"}


def group_matches(group: str, mb: dict) -> bool:
    gt = set(norm_text(group).split()) - _GROUP_STOP or set(norm_text(group).split())
    for cn in mb.get("collective_names", []):
        if gt and gt <= set(norm_text(cn).split()):
            return True
    return any(gt and gt <= set(norm_text(v).split()) for v in mb["title_variants"] if v)


def context_title_coverage(variants: list[str], context: str) -> float:
    ct = set(norm_text(context).split())
    best = 0.0
    for v in variants:
        vt = set(norm_text(v or "").split())
        if len(vt) >= 3:
            best = max(best, len(vt & ct) / len(vt))
    return best


def context_author_year(mb: dict, context: str) -> bool:
    ct = set(norm_text(context).split())
    lead = mb.get("lead_collective", 0)
    author_in = False
    for f in mb["author_families"][:lead + 1]:
        rt = record_surname_tokens(f)
        if rt and rt <= ct:
            author_in = True
    ys = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", context)]
    year_in = bool(ys and mb["years"] and any(abs(y - r) <= 1 for y in ys for r in mb["years"]))
    return author_in and year_in


def evaluate(resolved: dict, cited: dict | None, context: str | None = None, accept_bare: bool = False) -> dict:
    mb = resolved["match_basis"]
    sig = mb["signals"]
    c = cited or {}
    flags: list[str] = [f"erratum: {x}" for x in sig["erratum"]]
    flags += [f"notice: {x}" for x in sig["notice"]]
    out = {"status": "OK", "reason": None, "note": None, "flags": flags, "sim": None, "basis": None}
    if sig["retraction"]:
        out.update(status="RETRACTED", reason="retraction-signal", note="; ".join(sig["retraction"]))
        return out
    if sig["concern"]:
        out.update(status="CONCERN", reason="expression-of-concern", note="; ".join(sig["concern"]))
        return out
    has_any = any(c.get(k) for k in ("title", "first_author", "cited_group", "year", "journal"))
    t_state = None
    if c.get("title") and mb["title_variants"]:
        sim = title_similarity(c["title"], mb["title_variants"])
        out["sim"] = round(sim, 3)
        t_state = "ok" if sim >= 0.85 else ("grey" if sim >= 0.6 else "bad")
    ctx_title = False
    if context and mb["title_variants"] and (not c.get("title") or t_state in ("grey", "bad") and c.get("source") == "parsed"):
        cov = context_title_coverage(mb["title_variants"], context)
        if cov >= 0.85:
            ctx_title = True  # a quoted fragment parsed from text never outranks the full title in the context
            t_state = None
            out["sim"] = round(cov, 3)
    if t_state == "bad":
        out.update(status="WRONG-ID", reason="title-mismatch",
                   note=f"cited title does not match the resolved record (similarity {out['sim']})")
        return out
    if t_state == "grey":
        out.update(status="NEEDS-REVIEW", reason="title-grey-band",
                   note=f"title similarity {out['sim']} is in the 0.6-0.85 band")
        return out
    problems = []
    a_state = None
    if c.get("first_author"):
        a_state, idx = author_match(str(c["first_author"]), mb["author_families"])
        lead = mb.get("lead_collective", 0)
        if a_state == "later" and lead and idx == lead:
            a_state = "first"
            flags.append("first listed author is a collective name; cited author matches the first individual author")
        if a_state == "later":
            problems.append(("author-not-first", f"cited author is author {idx + 1} of {len(mb['author_families'])}, not first"))
        elif a_state == "none":
            problems.append(("author-mismatch",
                             f"cited first author '{c['first_author']}' is not the record's first author "
                             f"('{mb['author_families'][0]}')"))
    elif c.get("cited_group"):
        # a study group is matched against PubMed collective names, never as a surname; an unmatched group is no evidence
        if group_matches(str(c["cited_group"]), mb):
            a_state = "first"
        else:
            flags.append(f"cited study group '{c['cited_group']}' not found among the record's collective names")
    y_state = None
    cy = to_int_year(c.get("year"))
    if cy and mb["years"]:
        y_state = "ok" if min(abs(cy - y) for y in mb["years"]) <= 1 else "bad"
        if y_state == "bad":
            problems.append(("year-mismatch", f"cited year {cy} differs from record year(s) {sorted(set(mb['years']))} by more than 1"))
    j_state = None
    if c.get("journal"):
        j_state = journal_match(str(c["journal"]), mb["journal_variants"])
        if j_state is False:
            problems.append(("journal-mismatch", f"cited journal '{c['journal']}' differs from '{resolved.get('journal')}'"))
    if problems:
        out.update(status="MISMATCH", reason=problems[0][0], note="; ".join(p[1] for p in problems))
        return out
    weak_note = ("matched on first author and year only; a different paper can share both, so confirm the title "
                 "against the full reference (review --decision confirm)")
    if t_state == "ok":
        out["basis"] = "title"
    elif ctx_title:
        out["basis"] = "context-title"
        flags.append("title found in the context text")
    elif (a_state == "first" and y_state == "ok") or (context and context_author_year(mb, context)):
        # first-author surname + year is not identity evidence: a different paper can share both
        flags.append("author-year-only")
        out.update(status="NEEDS-REVIEW", reason="author-year-only", basis="author-year", note=weak_note)
        return out
    elif accept_bare and not has_any and not context:
        out.update(basis="existence-only", reason="existence-only",
                   note="resolved and screened for retraction/concern; no citing text, so identity was not compared")
        flags.append("existence-only")
    else:
        out.update(status="NEEDS-REVIEW",
                   reason="no-cited-metadata" if not has_any else "insufficient-cited-metadata",
                   note="identifier resolves; no cited metadata to compare" if not has_any else
                   "cited metadata too thin to confirm the identifier resolves to the cited work")
        return out
    if sig["notice"]:
        out.update(status="NEEDS-REVIEW", reason="identifier-is-a-notice", basis=None,
                   note="the identifier is a correction/retraction notice, not the original paper")
        return out
    if sig["erratum"]:
        out.update(status="CORRECTED", reason=out["reason"] or "erratum-linkage",
                   note="; ".join(sig["erratum"]) + (" | " + out["note"] if out["note"] else ""))
    return out


# ----------------------------------------------------------------------------
# Ledger
# ----------------------------------------------------------------------------

def parse_iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


class Ledger:
    def __init__(self, path: str | None):
        self.path = path
        self.newest: dict[str, dict] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    k = row.get("key")
                    if not k:
                        continue
                    prev = self.newest.get(k)
                    if prev is None or (row.get("checked_at") or "") >= (prev.get("checked_at") or ""):
                        self.newest[k] = row

    def fresh(self, key: str, ttl_days: float) -> dict | None:
        row = self.newest.get(key)
        if not row:
            return None
        t = parse_iso(row.get("checked_at"))
        if not t or (dt.datetime.now(dt.timezone.utc) - t) > dt.timedelta(days=ttl_days):
            return None
        return row

    def append(self, rows: list[dict]):
        if not self.path or not rows:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def ledger_row(rec: dict, audit_id: str, source_sha: str) -> dict:
    res = rec.get("resolved") or {}
    return {
        "key": rec["key"], "claim_sha256": claim_hash(rec), "status": rec["status"],
        "resolved": {k: res.get(k) for k in ("title", "first_author", "year", "journal", "pmid", "doi")},
        "checked_at": now_iso(), "audit_id": audit_id, "source_sha256": source_sha,
        "sources": sorted({e["source"] for e in rec.get("evidence", []) if e.get("source") != "ledger"}) or [],
        "resolved_full": res if res.get("match_basis") else None, "reason": rec.get("reason"),
        "review": rec.get("review"), "evidence": [e for e in rec.get("evidence", [])][:12],
    }


def claim_hash(rec: dict) -> str:
    return sha256_text(norm_text(rec.get("claim") or rec.get("context") or ""))


# ----------------------------------------------------------------------------
# Auditor
# ----------------------------------------------------------------------------

class Res:
    """Resolution result for one identifier."""

    def __init__(self):
        self.kind = None            # found | not-found | unverified
        self.resolved = None
        self.reason = None          # for unverified: transport|unindexed|offline
        self.note = None
        self.evidence: list[dict] = []
        self.errors: list[dict] = []
        self.failed: list[str] = []  # sources that failed in transport
        self.from_ledger = False
        self.extra: dict = {}        # url info, candidates, etc.
        self.found_hint = None


class Auditor:
    accept_bare = False

    def __init__(self, net: Net | None, ledger: Ledger | None, ttl_days: float, offline: bool):
        self.net, self.ledger, self.ttl, self.offline = net, ledger, ttl_days, offline
        self.res: dict[str, Res] = {}
        self.pm_cache: dict[str, dict | None] = {}
        self.pm_evidence: dict[str, dict] = {}
        self.pm_failed: dict[str, str] = {}
        self.title_search_cache: dict[str, tuple] = {}

    # --- low-level PubMed ------------------------------------------------
    def efetch(self, pmids: list[str]):
        todo = [p for p in dict.fromkeys(pmids) if p not in self.pm_cache and p not in self.pm_failed]
        for i in range(0, len(todo), 200):
            chunk = todo[i:i + 200]
            params = {"db": "pubmed", "retmode": "xml", "id": ",".join(chunk), **self.net.ncbi_params()}
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
            try:
                for attempt in range(3):
                    if attempt and "pubmed" not in self.net.simulate:
                        time.sleep(2.0 * attempt)
                    try:
                        r = self.net.get("pubmed", url)
                        if r.status != 200:
                            raise Transport(f"pubmed: HTTP {r.status}")
                        arts = parse_pubmed_xml(r.body)
                        break
                    except Transport:
                        if attempt == 2:
                            raise
            except Transport as e:
                for p in chunk:
                    self.pm_failed[p] = str(e)
                continue
            for p in chunk:
                self.pm_cache[p] = arts.get(p)
                self.pm_evidence[p] = r.evidence
            # an efetch may canonicalize ids (superseded PMIDs); keep returned ones too
            for p, a in arts.items():
                self.pm_cache.setdefault(p, a)
                self.pm_evidence.setdefault(p, r.evidence)

    def esearch(self, term: str, retmax: int = 5) -> tuple[list[str], dict]:
        params = {"db": "pubmed", "retmode": "json", "retmax": retmax, "term": term, **self.net.ncbi_params()}
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
        last = "unknown"
        for attempt in range(3):  # NCBI sometimes answers 200 with a backend ERROR body; retry like a timeout
            if attempt and "pubmed" not in self.net.simulate:
                time.sleep(2.0 * attempt)
            r = self.net.get("pubmed", url)
            if r.status != 200:
                last = f"esearch HTTP {r.status}"
                continue
            try:
                j = r.json()
            except Transport as e:
                last = str(e)
                continue
            es = j.get("esearchresult")
            if es is None or "ERROR" in es:
                last = f"esearch error {str(es)[:120] if es else str(j)[:120]}"
                continue
            return [str(x) for x in es.get("idlist", [])], r.evidence
        raise Transport(f"pubmed: {last}")

    # --- per-type resolution ----------------------------------------------
    def from_ledger_row(self, key: str, typ: str) -> Res | None:
        if self.ledger is None or typ not in LEDGER_REUSE_TYPES:
            return None
        row = self.ledger.fresh(key, self.ttl)
        if not row or row.get("status") in ("UNVERIFIED", "NOT-FOUND") or not row.get("resolved_full"):
            return None
        res = Res()
        res.kind = "found"
        res.resolved = json.loads(json.dumps(row["resolved_full"]))
        res.from_ledger = True
        res.evidence = [dict(e, via="ledger") for e in row.get("evidence", [])] or []
        res.evidence.append({"source": "ledger", "url": None, "http_status": None,
                             "fetched_at": row.get("checked_at"), "raw_sha256": None, "raw_path": None})
        res.extra["ledger_row"] = row
        return res

    def offline_res(self) -> Res:
        r = Res()
        r.kind, r.reason, r.note = "unverified", "offline", "audit ran with --offline and no fresh ledger row"
        return r

    def resolve_pmid_from_cache(self, pmid: str, res: Res):
        if pmid in self.pm_failed:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("pubmed")
            res.errors.append({"source": "pubmed", "error": self.pm_failed[pmid]})
            return
        art = self.pm_cache.get(pmid)
        res.evidence.append(self.pm_evidence[pmid])
        if art is None:
            res.kind = "not-found"
            res.note = "PubMed efetch returned no article for this PMID"
            return
        res.kind = "found"
        res.resolved = resolved_from_pubmed(art)

    def resolve_doi(self, doi: str) -> Res:
        res = Res()
        failed: list[tuple[str, str]] = []
        # Crossref
        cr = None
        crossref_status = None
        try:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/:()") + (
                "?" + urllib.parse.urlencode(self.net.polite_params()) if self.net.polite_params() else "")
            r = self.net.get("crossref", url)
            res.evidence.append(r.evidence)
            crossref_status = r.status
            if r.status == 200:
                cr = parse_crossref(r.json().get("message", {}))
            elif r.status != 404:
                raise Transport(f"crossref: HTTP {r.status}")
        except Transport as e:
            failed.append(("crossref", str(e)))
        handle_code = None
        if crossref_status == 404:
            try:
                r = self.net.get("doiorg", "https://doi.org/api/handles/" + urllib.parse.quote(doi, safe="/:()"))
                res.evidence.append(r.evidence)
                try:
                    handle_code = r.json().get("responseCode")
                except Transport as e:
                    failed.append(("doiorg", str(e)))
            except Transport as e:
                failed.append(("doiorg", str(e)))
        # PubMed linkage via DOI
        pm_res = None
        pmids: list[str] = []
        try:
            pmids, ev = self.esearch(f"{doi}[aid]", retmax=3)
            res.evidence.append(ev)
        except Transport as e:
            failed.append(("pubmed", str(e)))
        if pmids:
            self.efetch(pmids[:1])
            p = pmids[0]
            if p in self.pm_failed:
                failed.append(("pubmed", self.pm_failed[p]))
            elif self.pm_cache.get(p):
                res.evidence.append(self.pm_evidence[p])
                pm_res = resolved_from_pubmed(self.pm_cache[p])
                if len(pmids) > 1:
                    pm_res["match_basis"]["signals"]["notice"].append(f"DOI matches {len(pmids)} PubMed records")
        # OpenAlex
        oa = None
        try:
            url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/:()") + (
                "?" + urllib.parse.urlencode(self.net.polite_params()) if self.net.polite_params() else "")
            r = self.net.get("openalex", url)
            res.evidence.append(r.evidence)
            if r.status == 200:
                oa = parse_openalex(r.json())
            elif r.status != 404:
                raise Transport(f"openalex: HTTP {r.status}")
        except Transport as e:
            failed.append(("openalex", str(e)))
        for s, e in failed:
            res.failed.append(s)
            res.errors.append({"source": s, "error": e})
        # decide
        if cr is None and crossref_status == 404 and handle_code == 100 and pm_res is None and oa is None \
                and not [s for s, _ in failed if s in ("doiorg", "pubmed", "openalex")]:
            res.kind = "not-found"
            res.note = "Crossref 404 and doi.org handle responseCode 100 (no such DOI); no PubMed or OpenAlex record"
            return res
        if cr is None and "crossref" in res.failed:
            res.kind, res.reason = "unverified", "transport"
            return res
        base = pm_res or cr or oa
        if base is None:
            if res.failed:
                res.kind, res.reason = "unverified", "transport"
            else:
                res.kind, res.reason = "unverified", "unindexed"
                res.note = ("DOI handle exists but no Crossref, PubMed or OpenAlex record"
                            if handle_code == 1 else "no record in Crossref, PubMed or OpenAlex")
            return res
        merged = base
        for other in (cr, pm_res, oa):
            if other is not None and other is not base:
                merged = merge_resolved(merged, other)
        merged["doi"] = merged.get("doi") or doi
        merged["match_basis"]["sources"] = sorted(set(merged["match_basis"]["sources"]))
        res.kind, res.resolved = "found", merged
        return res

    def resolve_pmc(self, pmc: str) -> Res:
        res = Res()
        params = {"ids": pmc, "format": "json", **self.net.ncbi_params()}
        url = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?" + urllib.parse.urlencode(params)
        try:
            r = self.net.get("pmc", url)
            res.evidence.append(r.evidence)
            if r.status != 200:
                raise Transport(f"pmc: HTTP {r.status}")
            j = r.json()
            recs = j.get("records") or []
            if not recs:
                raise Transport("pmc: empty converter response")
            rec = recs[0]
        except Transport as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("pmc")
            res.errors.append({"source": "pmc", "error": str(e)})
            return res
        if rec.get("status") == "error":
            if "not found" in (rec.get("errmsg") or "").lower() or "invalid" in (rec.get("errmsg") or "").lower():
                res.kind, res.note = "not-found", f"PMC id converter: {rec.get('errmsg')}"
            else:
                res.kind, res.reason = "unverified", "transport"
                res.failed.append("pmc")
                res.errors.append({"source": "pmc", "error": str(rec.get("errmsg"))})
            return res
        sub = None
        if rec.get("pmid"):
            self.efetch([str(rec["pmid"])])
            tmp = Res()
            self.resolve_pmid_from_cache(str(rec["pmid"]), tmp)
            sub = tmp
        elif rec.get("doi"):
            sub = self.resolve_doi(norm_doi(rec["doi"]))
        if sub is None:
            res.kind, res.reason = "unverified", "unindexed"
            res.note = "PMC record has neither a PMID nor a DOI to verify further"
            return res
        res.evidence += sub.evidence
        res.errors += sub.errors
        res.failed += sub.failed
        if sub.kind != "found":
            res.kind, res.reason, res.note = sub.kind, sub.reason, sub.note
            return res
        res.kind, res.resolved = "found", sub.resolved
        res.resolved["pmc"] = pmc
        return res

    def resolve_arxiv_batch(self, ids: list[str]):
        out: dict[str, Res] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
                {"id_list": ",".join(chunk), "max_results": len(chunk)})
            try:
                r = self.net.get("arxiv", url)
                if r.status != 200:
                    raise Transport(f"arxiv: HTTP {r.status}")
                root = ET.fromstring(r.body)
            except (Transport, ET.ParseError) as e:
                for k in chunk:
                    res = Res()
                    res.kind, res.reason = "unverified", "transport"
                    res.failed.append("arxiv")
                    res.errors.append({"source": "arxiv", "error": str(e)})
                    out[k] = res
                continue
            ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
            entries = {}
            for en in root.findall("a:entry", ns):
                eid = _t(en.find("a:id", ns))
                if "/api/errors" in eid:
                    continue
                m = re.search(r"abs/(.+?)(?:v\d+)?$", eid)
                if m:
                    entries[m.group(1).lower()] = en
            for k in chunk:
                res = Res()
                res.evidence.append(r.evidence)
                en = entries.get(k.lower())
                if en is None:
                    res.kind, res.note = "not-found", "arXiv API returned no entry for this identifier"
                else:
                    res.kind = "found"
                    rr = new_resolved()
                    names = [_t(a.find("a:name", ns)) for a in en.findall("a:author", ns)]
                    fams = [n.split()[-1] for n in names if n]
                    short = [f"{n.split()[-1]} {''.join(p[0] for p in n.split()[:-1])}".strip() for n in names if n]
                    y = to_int_year(_t(en.find("a:published", ns)))
                    jref = _t(en.find("x:journal_ref", ns))
                    doi = _t(en.find("x:doi", ns))
                    rr.update({"title": _t(en.find("a:title", ns)), "authors": short[:25],
                               "first_author": short[0] if short else None, "year": y,
                               "journal": jref or None, "doi": norm_doi(doi) if doi else None,
                               "has_abstract": bool(_t(en.find("a:summary", ns)))})
                    mb = rr["match_basis"]
                    mb.update({"title_variants": [rr["title"]], "author_families": fams[:200],
                               "years": [y] if y else [], "journal_variants": [jref] if jref else [],
                               "sources": ["arxiv"]})
                    if re.search(r"(?i)withdrawn", _t(en.find("x:comment", ns)) + " " + _t(en.find("a:summary", ns))[:300]):
                        mb["signals"]["concern"].append("arXiv comment/summary mentions 'withdrawn'")
                    res.resolved = rr
                out[k] = res
        return out

    def resolve_nct(self, nct: str) -> Res:
        res = Res()
        fields = "NCTId,BriefTitle,OfficialTitle,Acronym,OverallStatus,StartDate,LeadSponsorName,WhyStopped"
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct}?" + urllib.parse.urlencode({"fields": fields})
        try:
            r = self.net.get("ctgov", url)
            res.evidence.append(r.evidence)
            if r.status == 404:
                res.kind, res.note = "not-found", "ClinicalTrials.gov returned 404 for this NCT id"
                return res
            if r.status != 200:
                raise Transport(f"ctgov: HTTP {r.status}")
            j = r.json()
        except Transport as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("ctgov")
            res.errors.append({"source": "ctgov", "error": str(e)})
            return res
        ps = j.get("protocolSection", {})
        idm, st = ps.get("identificationModule", {}), ps.get("statusModule", {})
        titles = [idm.get(k) for k in ("briefTitle", "officialTitle", "acronym") if idm.get(k)]
        rr = new_resolved()
        sponsor = (ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name")
        rr.update({"title": idm.get("briefTitle"), "journal": None, "year": to_int_year((st.get("startDateStruct") or {}).get("date")),
                   "first_author": sponsor})
        rr["match_basis"].update({"title_variants": titles, "sources": ["ctgov"]})
        sig = rr["match_basis"]["signals"]
        status = st.get("overallStatus")
        if status in ("WITHDRAWN",):
            sig["notice"].append(f"registry status {status}: {st.get('whyStopped') or 'no reason given'}")
        res.kind, res.resolved = "found", rr
        res.extra["registry_status"] = status
        return res

    def resolve_isrctn(self, isr: str) -> Res:
        res = Res()
        url = "https://www.isrctn.com/api/query/format/default?" + urllib.parse.urlencode({"q": isr, "limit": 1})
        try:
            r = self.net.get("isrctn", url)
            res.evidence.append(r.evidence)
            if r.status != 200:
                raise Transport(f"isrctn: HTTP {r.status}")
            root = ET.fromstring(r.body)
        except (Transport, ET.ParseError) as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("isrctn")
            res.errors.append({"source": "isrctn", "error": str(e)})
            return res
        count = root.get("totalCount")
        if count == "0":
            res.kind, res.note = "not-found", "ISRCTN registry query returned totalCount 0"
            return res
        title = ""
        for el in root.iter():
            if el.tag.endswith("title") and el.text and not title:
                title = el.text.strip()
        if not title:
            res.kind, res.reason = "unverified", "unindexed"
            res.note = "ISRCTN answered but no machine-readable title"
            return res
        rr = new_resolved()
        rr.update({"title": title})
        rr["match_basis"].update({"title_variants": [title], "sources": ["isrctn"]})
        res.kind, res.resolved = "found", rr
        return res

    def resolve_eudract(self, num: str) -> Res:
        res = Res()
        url = "https://www.clinicaltrialsregister.eu/ctr-search/search?" + urllib.parse.urlencode({"query": num})
        try:
            r = self.net.get("eudract", url)
            res.evidence.append(r.evidence)
            if r.status != 200:
                raise Transport(f"eudract: HTTP {r.status}")
        except Transport as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("eudract")
            res.errors.append({"source": "eudract", "error": str(e)})
            return res
        t = r.text()
        m = re.search(r"Full title:\s*</td>\s*<td[^>]*>\s*([^<]+)", t) or re.search(r"Full title:\s*([^<\n]+)", t)
        if m and num in t:
            rr = new_resolved()
            rr.update({"title": m.group(1).strip()})
            rr["match_basis"].update({"title_variants": [m.group(1).strip()], "sources": ["eudract"]})
            res.kind, res.resolved = "found", rr
        else:
            res.kind, res.reason = "unverified", "unindexed"
            res.note = "EudraCT register page gave no machine-readable record"
        return res

    def resolve_url(self, url: str) -> Res:
        res = Res()
        try:
            r = self.net.get("url", url, headers={"Range": "bytes=0-65535", "Accept": "text/html,*/*"},
                             guard_host=True, max_bytes=200_000)
        except Transport as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("url")
            res.errors.append({"source": "url", "error": str(e)})
            return res
        res.evidence.append(r.evidence)
        res.extra["http_status"], res.extra["final_url"] = r.status, scrub_url(r.url)
        if r.status in (404, 410):
            res.kind, res.note = "not-found", f"URL returned HTTP {r.status}"
        elif r.status in (200, 206):
            res.kind = "found"
            rr = new_resolved()
            tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text())
            rr["title"] = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else None
            rr["match_basis"].update({"title_variants": [rr["title"]] if rr["title"] else [], "sources": ["url"]})
            res.resolved = rr
            res.note = f"URL fetched (HTTP {r.status}); final URL {scrub_url(r.url)}; content match is a reviewer job"
        else:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("url")
            res.errors.append({"source": "url", "error": f"HTTP {r.status}"})
        return res

    # --- title search ----------------------------------------------------
    def title_search(self, cited: dict, want_guard: bool = True) -> tuple[dict | None, list[dict], list[str]]:
        """Guarded title search. Returns (found_target, candidates, failed_sources).

        found_target: {"pmid"?, "doi"?, "resolved"?, "source"} or None."""
        title = cited.get("title")
        if not title:
            return None, [], []
        ck = sha256_text(json.dumps([title, cited.get("first_author"), cited.get("year")], sort_keys=True))
        if ck in self.title_search_cache:
            return self.title_search_cache[ck]
        failed: list[str] = []
        cands: list[dict] = []

        def guard(variants, fams, years):
            sim = title_similarity(title, variants)
            if sim < 0.85:
                return False, sim
            if cited.get("first_author"):
                st, _ = author_match(str(cited["first_author"]), fams)
                if st != "first":
                    return False, sim
            cy = to_int_year(cited.get("year"))
            if cy and years and min(abs(cy - y) for y in years) > 1:
                return False, sim
            return True, sim

        result = None
        # 1. PubMed
        try:
            ids = []
            phrase = norm_text(title)
            if phrase:
                ids, ev = self.esearch(f'"{phrase}"[Title:~0]', retmax=10)
            if not ids:
                toks = sorted({t for t in phrase.split() if len(t) > 1 and t not in PUBMED_STOPWORDS},
                              key=len, reverse=True)
                for n_tok in (14, 7):
                    if not toks:
                        break
                    term = " AND ".join(f"{t}[Title]" for t in toks[:n_tok])
                    ids, ev = self.esearch(term, retmax=40)
                    if ids or len(toks) <= n_tok:
                        break
            self.efetch(ids)
            for p in ids:
                art = self.pm_cache.get(p)
                if not art:
                    continue
                ok, sim = guard([art["title"]], art["families"], art["years"])
                cands.append({"pmid": p, "doi": art["doi"], "title": art["title"],
                              "first_author": art["authors"][0] if art["authors"] else None,
                              "year": art["years"][0] if art["years"] else None, "similarity": round(sim, 3)})
                if ok and result is None and (cited.get("first_author") or cited.get("year") or not want_guard):
                    result = {"pmid": p, "doi": art["doi"], "source": "pubmed", "resolved": resolved_from_pubmed(art)}
        except Transport:
            failed.append("pubmed")
        # 2. Crossref
        if result is None:
            try:
                q = title + (" " + str(cited["first_author"]) if cited.get("first_author") else "")
                params = {"query.bibliographic": q, "rows": 5,
                          "select": "DOI,title,subtitle,author,issued", **self.net.polite_params()}
                r = self.net.get("crossref", "https://api.crossref.org/works?" + urllib.parse.urlencode(params))
                if r.status != 200:
                    raise Transport("crossref: search HTTP %s" % r.status)
                for it in (r.json().get("message", {}).get("items") or []):
                    pr = parse_crossref(it)
                    g_ok, best = guard(pr["match_basis"]["title_variants"], pr["match_basis"]["author_families"],
                                       pr["match_basis"]["years"])
                    cands.append({"pmid": None, "doi": pr["doi"], "title": pr["title"], "first_author": pr["first_author"],
                                  "year": pr["year"], "similarity": round(best, 3)})
                    if g_ok and result is None and (cited.get("first_author") or cited.get("year") or not want_guard):
                        result = {"pmid": None, "doi": pr["doi"], "source": "crossref"}
            except Transport:
                failed.append("crossref")
        # 3. OpenAlex
        if result is None:
            try:
                params = {"search": title, "per-page": 5, **self.net.polite_params()}
                r = self.net.get("openalex", "https://api.openalex.org/works?" + urllib.parse.urlencode(params))
                if r.status != 200:
                    raise Transport("openalex: search HTTP %s" % r.status)
                for w in (r.json().get("results") or []):
                    pr = parse_openalex(w)
                    ok, sim = guard([pr["title"] or ""], pr["match_basis"]["author_families"], pr["match_basis"]["years"])
                    cands.append({"pmid": pr["pmid"], "doi": pr["doi"], "title": pr["title"],
                                  "first_author": pr["first_author"], "year": pr["year"], "similarity": round(sim, 3)})
                    if ok and result is None and (pr["doi"] or pr["pmid"]) and (
                            cited.get("first_author") or cited.get("year") or not want_guard):
                        result = {"pmid": pr["pmid"], "doi": pr["doi"], "source": "openalex"}
            except Transport:
                failed.append("openalex")
        cands.sort(key=lambda c: -c["similarity"])
        out = (result, cands[:5], failed)
        self.title_search_cache[ck] = out
        return out

    def resolve_ref_target(self, target: dict) -> Res:
        if target.get("resolved") is not None and target.get("pmid"):
            res = Res()
            res.kind, res.resolved = "found", target["resolved"]
            res.evidence.append(self.pm_evidence.get(target["pmid"], {}))
            res.evidence = [e for e in res.evidence if e]
            return res
        if target.get("doi"):
            return self.resolve_doi(target["doi"])
        res = Res()
        self.efetch([target["pmid"]])
        self.resolve_pmid_from_cache(target["pmid"], res)
        return res

    # --- guideline ---------------------------------------------------------
    def resolve_guideline(self, name: str, year: int | None) -> Res:
        res = Res()
        generic = {"guideline", "guidelines", "consensus", "statement", "statements", "recommendation",
                   "recommendations", "position", "paper", "the", "and", "of", "for", "on", "in"}
        words = [w for w in norm_text(name).split() if w not in generic and not re.fullmatch(r"(19|20)\d\d", w)]
        phrase = " ".join(words) or norm_text(name)
        yr = f" AND {year}[dp]" if year else ""
        queries = [
            f'("{phrase}"[Title/Abstract]){yr} AND (guideline[pt] OR practice guideline[pt] OR '
            f'consensus development conference[pt])',
            f'("{phrase}"[Title/Abstract]){yr} AND (guideline[Title] OR guidelines[Title] OR consensus[Title] OR '
            f'statement[Title] OR recommendations[Title])',
        ]
        ids: list[str] = []
        try:
            for q in queries:
                ids, ev = self.esearch(q, retmax=5)
                res.evidence.append(ev)
                if ids:
                    break
            self.efetch(ids)
        except Transport as e:
            res.kind, res.reason = "unverified", "transport"
            res.failed.append("pubmed")
            res.errors.append({"source": "pubmed", "error": str(e)})
            return res
        cands = []
        for p in ids:
            art = self.pm_cache.get(p)
            if art:
                cands.append({"pmid": p, "doi": art["doi"], "title": art["title"],
                              "first_author": art["authors"][0] if art["authors"] else None,
                              "year": art["years"][0] if art["years"] else None, "journal": art["journal"],
                              "publication_types": art["publication_types"]})
        if not cands:
            res.kind, res.reason = "unverified", "unindexed"
            res.note = "no guideline-type PubMed record found for this name and year"
            return res
        res.kind = "found"
        res.extra["candidates"] = cands
        rr = new_resolved()
        rr["match_basis"]["sources"] = ["pubmed"]
        res.resolved = rr
        return res

    # --- orchestration -----------------------------------------------------
    def resolve_all(self, items: dict[str, dict]):
        """items: key -> {"type","id","occ":[...]}; fills self.res."""
        net_needed: dict[str, dict] = {}
        for key, it in items.items():
            typ = it["type"]
            if typ == "REF":
                net_needed[key] = it
                continue
            led = self.from_ledger_row(key, typ)
            if led:
                self.res[key] = led
            elif self.offline:
                self.res[key] = self.offline_res()
            else:
                net_needed[key] = it
        if not net_needed:
            return
        # batch PubMed
        self.efetch([it["id"] for it in net_needed.values() if it["type"] == "PMID"])
        arxivs = [it["id"] for it in net_needed.values() if it["type"] == "ARXIV"]
        arx = self.resolve_arxiv_batch(arxivs) if arxivs else {}
        for key, it in net_needed.items():
            typ, ident = it["type"], it["id"]
            if typ == "PMID":
                r = Res()
                self.resolve_pmid_from_cache(ident, r)
                self.res[key] = r
            elif typ == "DOI":
                self.res[key] = self.resolve_doi(ident)
            elif typ == "PMC":
                self.res[key] = self.resolve_pmc(ident)
            elif typ == "ARXIV":
                self.res[key] = arx[ident]
            elif typ == "NCT":
                self.res[key] = self.resolve_nct(ident)
            elif typ == "ISRCTN":
                self.res[key] = self.resolve_isrctn(ident)
            elif typ == "EUDRACT":
                self.res[key] = self.resolve_eudract(ident)
            elif typ == "URL":
                self.res[key] = self.resolve_url(it["raw"] if it.get("raw", "").startswith("http") else ident)
            elif typ == "GUIDELINE":
                self.res[key] = self.resolve_guideline(ident, it.get("year"))
            elif typ == "REF":
                pass  # handled in finalize (needs cited)

    def finalize_record(self, key: str, it: dict) -> dict:
        typ = it["type"]
        occs = it["occ"]
        res = self.res.get(key)
        rec = {"key": key, "type": typ, "id": it.get("id"), "raw": it.get("raw"), "context": None, "claim": None,
               "cited": {}, "status": None, "reason": None, "note": None, "flags": [], "resolved": None,
               "found": None, "candidates": [], "same_work_as": None, "review": None, "evidence": [],
               "occurrences": len(occs), "from_ledger": False, "match_basis": None}
        found_info = None
        cands: list[dict] = []
        errors: list[dict] = []

        # REF: guarded title search to get a target
        if typ == "REF":
            cited = occs[0]["cited"]
            if self.offline:
                res = self.offline_res()
            else:
                target, cands, failed = self.title_search(cited)
                if target is not None:
                    res = self.resolve_ref_target(target)
                    found_info = {k: target.get(k) for k in ("pmid", "doi")}
                    rec["same_work_as"] = f"PMID:{target['pmid']}" if target.get("pmid") else f"DOI:{target['doi']}"
                elif failed:
                    res = Res()
                    res.kind, res.reason = "unverified", "transport"
                    res.failed += failed
                    res.errors += [{"source": s, "error": "title search failed in transport"} for s in failed]
                else:
                    res = Res()
                    strong = [c for c in cands if c["similarity"] >= 0.6]
                    if strong:
                        res.kind = "found"
                        res.resolved = None
                        res.extra["ref_candidates_only"] = True
                    else:
                        res.kind, res.note = "not-found", "guarded title search (PubMed, Crossref, OpenAlex) found no matching work"
            self.res[key] = res

        rec["evidence"] = [e for e in (res.evidence if res else []) if e]
        rec["from_ledger"] = bool(res and res.from_ledger)
        errors += (res.errors if res else [])
        rec["_errors"] = errors

        # choose the worst occurrence
        def pick(results):
            return min(results, key=lambda r: SEVERITY.index(r[1]["status"]) if r[1]["status"] in SEVERITY else 99)

        if res.kind == "unverified":
            rec.update(status="UNVERIFIED", reason=res.reason, note=res.note or (
                "; ".join(e["error"] for e in res.errors) if res.errors else None))
            occ = occs[0]
        elif res.kind == "not-found":
            occ = occs[0]
            status, note = "NOT-FOUND", res.note
            # a guarded title search may find the cited work under a different identifier
            cited = next((o["cited"] for o in occs if o["cited"].get("title")), None)
            if cited and typ != "REF" and typ not in ("URL", "GUIDELINE"):
                target, cands2, failed = self.title_search(cited) if not self.offline else (None, [], [])
                cands = cands2
                if failed and target is None:
                    rec.update(status="UNVERIFIED", reason="transport",
                               note="identifier absent from its registry but the guarded title search failed in transport")
                    rec["_errors"] += [{"source": s, "error": "title search failed in transport"} for s in failed]
                    status = None
                elif target is not None:
                    status, note = "WRONG-ID", "identifier does not exist; a guarded title search found the cited work"
                    found_info = {k: target.get(k) for k in ("pmid", "doi")}
            if status:
                rec.update(status=status, reason="identifier-absent" if status == "NOT-FOUND" else "wrong-identifier", note=note)
            rec["cited"], rec["context"], rec["claim"] = occ["cited"], occ["context"], occ.get("claim")
        elif res.kind == "found" and res.resolved is None:
            # REF with only weak candidates, or URL/GUIDELINE handled below
            occ = occs[0]
            rec.update(status="NEEDS-REVIEW", reason="no-guarded-match",
                       note="title search returned similar works but none passed the title+author+year guard")
        else:
            if typ == "URL":
                occ = occs[0]
                rec.update(status="NEEDS-REVIEW", reason="url-fetched", note=res.note)
                rec["resolved"] = res.resolved
                rec["found"] = {"http_status": res.extra.get("http_status"), "final_url": res.extra.get("final_url")}
            elif typ == "GUIDELINE":
                occ = occs[0]
                cands = res.extra.get("candidates", [])
                rec.update(status="NEEDS-REVIEW", reason="guideline-candidates",
                           note=f"{len(cands)} candidate PubMed record(s); confirm the edition with the reviewer path")
                rec["resolved"] = res.resolved
            else:
                results = []
                for o in occs:
                    ev = evaluate(res.resolved, o["cited"], o.get("context"), self.accept_bare)
                    results.append((o, ev))
                occ, ev = pick(results)
                rec.update(status=ev["status"], reason=ev["reason"], note=ev["note"], flags=ev["flags"], match_basis=ev["basis"])
                rec["resolved"] = res.resolved
                if ev["status"] == "WRONG-ID" and not self.offline:
                    target, cands2, failed = self.title_search(occ["cited"])
                    cands = cands2
                    if target is not None:
                        found_info = {k: target.get(k) for k in ("pmid", "doi")}
                        found_info["title"] = None
                    if failed and target is None:
                        rec["flags"].append("title search for the correct work failed in transport: " + ", ".join(failed))
            rec["cited"], rec["context"], rec["claim"] = occ["cited"], occ["context"], occ.get("claim")
            # a failed screening source blocks a clean verdict
            if res.failed and rec["status"] in ("OK", "CORRECTED", "NEEDS-REVIEW") and typ not in ("URL", "GUIDELINE"):
                rec.update(status="UNVERIFIED", reason="transport",
                           note="resolved, but a screening source failed in transport: " + ", ".join(sorted(set(res.failed))))
            elif res.failed and rec["status"] not in ("UNVERIFIED",):
                rec["flags"].append("screening source failed in transport: " + ", ".join(sorted(set(res.failed))))
            # carry an earlier reviewer decision for the same claim
            led_row = res.extra.get("ledger_row") if res.from_ledger else None
            if led_row and rec["status"] == "NEEDS-REVIEW" and led_row.get("status") in ("OK-REVIEWED", "SUPERSEDED") \
                    and led_row.get("claim_sha256") == claim_hash(rec) and led_row.get("review"):
                rec.update(status=led_row["status"], reason="reviewed-earlier", note=(led_row.get("review") or {}).get("note"))
                rec["review"] = led_row["review"]
        if rec["context"] is None:
            rec["cited"], rec["context"], rec["claim"] = occ["cited"], occ["context"], occ.get("claim")
        if found_info is not None:
            rec["found"] = {k: v for k, v in found_info.items() if v}
            if rec["found"] and not rec["found"].get("title"):
                best = next((c for c in cands if (c.get("pmid") and c["pmid"] == found_info.get("pmid")) or
                             (c.get("doi") and c["doi"] == found_info.get("doi"))), None)
                if best:
                    rec["found"]["title"] = best["title"]
        rec["candidates"] = cands
        return rec


# ----------------------------------------------------------------------------
# Audit assembly
# ----------------------------------------------------------------------------

def load_entries(args) -> tuple[list[dict], str, bytes]:
    """Return entries [{type,id,raw,context,claim,cited,entry,year?}], upstream label, source bytes."""
    if args.text:
        data = open(args.text, "rb").read()
        text = data.decode("utf-8", "replace")
        hits = extract_identifiers(text)
        return [{"type": h["type"], "id": h["id"], "raw": h["raw"], "context": h["context"], "claim": h["context"],
                 "cited": h["cited"], "entry": h["entry"], "year": h.get("year")} for h in hits], "text", data
    if args.stdin:
        data = sys.stdin.buffer.read()
        text = data.decode("utf-8", "replace")
        hits = extract_identifiers(text)
        return [{"type": h["type"], "id": h["id"], "raw": h["raw"], "context": h["context"], "claim": h["context"],
                 "cited": h["cited"], "entry": h["entry"], "year": h.get("year")} for h in hits], "stdin", data
    data = open(args.ids, "rb").read()
    arr = json.loads(data.decode("utf-8"))
    if not isinstance(arr, list):
        raise ValueError("--ids file must be a JSON list")
    out = []
    for i, e in enumerate(arr):
        typ = str(e.get("type", "")).upper()
        if typ == "ISRCTN" or typ in ("PMID", "DOI", "PMC", "ARXIV", "NCT", "EUDRACT", "URL", "GUIDELINE", "REF"):
            pass
        else:
            raise ValueError(f"entry {i}: unknown type {e.get('type')!r}")
        cited = dict(e.get("cited") or {})
        if cited:
            cited["source"] = "ids"
        if typ == "REF":
            if not cited.get("title"):
                raise ValueError(f"entry {i}: REF needs cited.title")
            ident = "ref-" + sha256_text(norm_text(cited["title"]) + "|" + norm_text(str(cited.get("first_author", ""))) +
                                         "|" + str(cited.get("year", "")))[:12]
        else:
            if not e.get("id"):
                raise ValueError(f"entry {i}: type {typ} needs an id")
            ident = normalize_entry_id(typ, str(e["id"]))
        out.append({"type": typ, "id": ident, "raw": str(e.get("id", ident)), "context": e.get("context") or e.get("claim"),
                    "claim": e.get("claim") or e.get("context"), "cited": cited, "entry": True,
                    "year": to_int_year(e.get("year"))})
    return out, "ids", data


def group_entries(entries: list[dict], merge: bool) -> list[dict]:
    """merge=True (text mode): one group per key. merge=False (ids mode): one group per entry, input order."""
    groups: list[dict] = []
    by_key: dict[str, dict] = {}
    for i, e in enumerate(entries):
        key = f"{e['type']}:{e['id']}"
        if merge and key in by_key:
            g = by_key[key]
        else:
            g = {"key": key, "type": e["type"], "id": e["id"], "raw": e["raw"], "year": e.get("year"),
                 "occ": [], "entries": 0, "index": i}
            groups.append(g)
            by_key[key] = g
        g["occ"].append({"cited": e["cited"], "context": e["context"], "claim": e["claim"]})
        if e.get("entry"):
            g["entries"] += 1
    return groups


def summarize(records: list[dict], dups: list[dict]) -> dict:
    counts = {s: 0 for s in STATUSES}
    for r in records:
        counts[r["status"]] += 1
    verified = sum(counts[s] for s in VERIFIED)
    blocking = any(r["status"] in BLOCKING for r in records)
    return {"total": len(records), "verified": verified, "counts": counts,
            "audit_pass": (not blocking) and not dups,
            "needs_review": [r["key"] for r in records if r["status"] == "NEEDS-REVIEW"]}


def _sev(status: str) -> int:
    return SEVERITY.index(status) if status in SEVERITY else 99


def run_audit(args) -> int:
    if args.strict and args.offline:
        print("error: --strict forbids --offline", file=sys.stderr)
        return 2
    if sum(bool(x) for x in (args.text, args.ids, args.stdin)) != 1:
        print("error: give exactly one of --text, --ids, --stdin", file=sys.stderr)
        return 2
    simulate = set()
    if args.simulate_transport_fail:
        simulate = {s.strip() for s in args.simulate_transport_fail.split(",") if s.strip()}
        bad = simulate - set(ALL_SOURCES)
        if bad:
            print(f"error: unknown source(s) in --simulate-transport-fail: {sorted(bad)}; known: {list(ALL_SOURCES)}",
                  file=sys.stderr)
            return 2
    try:
        entries, upstream, src_bytes = load_entries(args)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    groups = group_entries(entries, merge=(upstream != "ids"))
    items: dict[str, dict] = {}
    for g in groups:
        items.setdefault(g["key"], g)
    raw_dir = args.raw_dir
    if not raw_dir:
        raw_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), "raw") if args.out else \
            tempfile.mkdtemp(prefix="citation-audit-raw-")
    net = Net(raw_dir, args.timeout, simulate)
    use_ledger = not args.no_ledger and not simulate
    ledger_path = args.ledger or default_ledger()
    ledger = Ledger(ledger_path) if use_ledger else None
    aud = Auditor(net, ledger, args.ttl_days, args.offline)
    aud.accept_bare = bool(args.accept_bare)
    source_sha = sha256_bytes(src_bytes)
    ts = dt.datetime.now(dt.timezone.utc)
    audit_id = ts.strftime("%Y%m%dT%H%M%SZ") + "-" + sha256_text(source_sha + ts.isoformat())[:8]

    aud.resolve_all(items)
    records, errors = [], []
    for g in groups:
        rec = aud.finalize_record(g["key"], g)
        rec["index"] = g["index"]
        for e in rec.pop("_errors", []):
            errors.append({"key": g["key"], "source": e["source"], "error": e["error"]})
        records.append(rec)
    # duplicates and same-work links
    dups = []
    if upstream == "ids":
        counts_by_key: dict[str, int] = {}
        for g in groups:
            counts_by_key[g["key"]] = counts_by_key.get(g["key"], 0) + 1
        for key, n in counts_by_key.items():
            if n >= 2:
                dups.append({"key": key, "count": n, "kind": "same-identifier",
                             "note": f"{key} appears as {n} reference entries"})
    else:
        for g in groups:
            if g["entries"] >= 2:
                dups.append({"key": g["key"], "count": g["entries"], "kind": "same-identifier",
                             "note": f"{g['key']} appears as {g['entries']} reference entries"})
    by_pmid = {r["resolved"]["pmid"]: r["key"] for r in records
               if r["resolved"] and r["resolved"].get("pmid") and r["type"] in ("DOI", "PMC", "PMID")}
    by_doi = {}
    for r in records:
        if r["resolved"] and r["resolved"].get("doi"):
            by_doi.setdefault(r["resolved"]["doi"], []).append(r["key"])
    for r in records:
        if r["same_work_as"]:
            continue
        res = r["resolved"] or {}
        peers = set()
        if res.get("pmid"):
            peers |= {k for k in [by_pmid.get(res["pmid"])] if k}
            peers |= {x["key"] for x in records if x["key"] == f"PMID:{res['pmid']}"}
        if res.get("doi"):
            peers |= set(by_doi.get(res["doi"], []))
            peers |= {x["key"] for x in records if x["key"] == f"DOI:{res['doi']}"}
        peers.discard(r["key"])
        if peers:
            r["same_work_as"] = sorted(peers)[0]
    summ = summarize(records, dups)
    audit = {
        "kind": "CitationAudit", "schema_version": SCHEMA_VERSION, "audit_id": audit_id,
        "upstream_skill": args.upstream_skill or upstream, "source_sha256": source_sha,
        "method": {"patterns": PATTERN_DOC,
                   "screens": ["CommentsCorrections linkage (PubMed efetch XML RefType)", "PubMed publication type",
                               "Crossref updated-by", "OpenAlex is_retracted",
                               "normalized title Jaccard guard (>=0.85 match, 0.6-0.85 review)",
                               "first-author whole-word match", "year +/-1", "tolerant journal match"],
                   "sources": sorted({e["source"] for r in records for e in r["evidence"]}),
                   "offline": bool(args.offline), "accept_bare": bool(args.accept_bare), "simulated_transport_failures": sorted(simulate)},
        "total": summ["total"], "verified": summ["verified"], "counts": summ["counts"],
        "audit_pass": summ["audit_pass"], "records": records, "duplicate_findings": dups, "errors": errors,
        "needs_review": summ["needs_review"],
    }
    if use_ledger:
        audit["ledger_path"] = ledger_path
    text_out = json.dumps(audit, indent=2, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text_out + "\n")
    else:
        print(text_out)
    if use_ledger:
        worst: dict[str, dict] = {}
        for r in records:
            cur = worst.get(r["key"])
            if cur is None or _sev(r["status"]) < _sev(cur["status"]):
                worst[r["key"]] = r
        ledger.append([ledger_row(r, audit_id, source_sha) for r in worst.values()])
    c = summ["counts"]
    print(f"audit {audit_id}: {summ['verified']} of {summ['total']} verified · " +
          ", ".join(f"{k} {v}" for k, v in c.items() if v) + f" · audit_pass {str(summ['audit_pass']).lower()}" +
          (f" · {net.requests} requests" if net.requests else ""), file=sys.stderr if not args.out else sys.stdout)
    return 0 if summ["audit_pass"] else 1


# ----------------------------------------------------------------------------
# block / review / extract
# ----------------------------------------------------------------------------

def _short_found(f: dict | None) -> str:
    if not f:
        return "resolves to a different work"
    parts = []
    if f.get("pmid"):
        parts.append(f"PMID:{f['pmid']}")
    if f.get("doi"):
        parts.append(f"DOI:{f['doi']}")
    return " / ".join(parts) or "resolves to a different work"


def _short_cited(c: dict) -> str:
    bits = [str(c[k]) for k in ("first_author", "year") if c.get(k)]
    if c.get("title"):
        t = c["title"]
        bits.append(f"'{t[:50]}…'" if len(t) > 50 else f"'{t}'")
    return " ".join(bits) or "cited work"


def render_block(audit: dict) -> str:
    recs = audit["records"]

    def lst(items):
        items = list(dict.fromkeys(items))
        return "; ".join(items) if items else "none"

    nf = [r["key"] for r in recs if r["status"] == "NOT-FOUND"]
    wi = [f"{r['key']} (cited {_short_cited(r.get('cited') or {})}) → {_short_found(r.get('found'))}"
          for r in recs if r["status"] == "WRONG-ID"]
    mm = [f"{r['key']} ({r.get('note') or r.get('reason')})" for r in recs if r["status"] == "MISMATCH"]
    rc, co = [], []
    for r in recs:
        sig = ((r.get("resolved") or {}).get("match_basis") or {}).get("signals") or {}
        for x in sig.get("retraction", []):
            rc.append(f"{r['key']} (retracted: {x})")
        for x in sig.get("concern", []):
            rc.append(f"{r['key']} (concern: {x})")
        for x in sig.get("erratum", []):
            co.append(f"{r['key']} ({x})")
        if r["status"] in ("RETRACTED", "CONCERN") and not (sig.get("retraction") or sig.get("concern")):
            rc.append(f"{r['key']} ({'retracted' if r['status'] == 'RETRACTED' else 'concern'}: {r.get('note') or r.get('reason')})")
    wk = [f"{r['key']} ({r['status']})" for r in recs if "author-year-only" in (r.get("flags") or [])]
    eo = [f"{r['key']} ({r['status']})" for r in recs if "existence-only" in (r.get("flags") or [])]
    sp = [f"{r['key']}" + (f" → {r['review'].get('superseded_by')}" if (r.get("review") or {}).get("superseded_by") else "")
          for r in recs if r["status"] == "SUPERSEDED"]
    nr = [r["key"] for r in recs if r["status"] == "NEEDS-REVIEW"]
    uv = [f"{r['key']} ({r.get('reason') or 'unverified'})" for r in recs if r["status"] == "UNVERIFIED"]
    er = sorted({e["key"] for e in audit.get("errors", [])})
    du = [d["note"] if d.get("note") else d["key"] for d in audit.get("duplicate_findings", [])]
    lines = [
        "```",
        "citation-audit",
        f"verified: {audit['verified']} of {audit['total']} citations (identifiers + named guideline references)",
        "method: " + "PMID/DOI/PMC/arXiv/NCT/ISRCTN/EudraCT/URL patterns + guideline name–year pattern · "
                     "errata screened via CommentsCorrections linkage · verifier: verify_citations.py schema "
                     f"{audit['schema_version']}",
        f"not found (replace with [CITATION NEEDED]): {lst(nf)}",
        f"wrong identifier: {lst(wi)}",
        f"metadata mismatch: {lst(mm)}",
        f"retracted / concern: {lst(rc)}",
        f"corrected (erratum): {lst(co)}",
        f"superseded editions: {lst(sp)}",
        f"needs review: {lst(nr)}",
        f"weak match (author-year only): {lst(wk)}",
        f"existence only (no citing text): {lst(eo)}",
        f"unverified: {lst(uv)}",
        f"errors: {lst(er)}",
        f"duplicates: {lst(du)}",
        f"audit_pass: {str(audit['audit_pass']).lower()} · audit_id: {audit['audit_id']}",
        "```",
    ]
    return "\n".join(lines)


def run_block(args) -> int:
    try:
        audit = json.load(open(args.audit, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(render_block(audit))
    return 0


def run_review(args) -> int:
    try:
        audit = json.load(open(args.audit, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    same = [r for r in audit["records"] if r["key"] == args.key]
    if not same:
        print(f"error: key {args.key!r} not in {args.audit}", file=sys.stderr)
        return 2
    targets = [r for r in same if r["status"] == "NEEDS-REVIEW"]
    if not targets:
        print(f"error: {args.key} is {same[0]['status']}; only NEEDS-REVIEW records can be reviewed", file=sys.stderr)
        return 2
    if not (args.note or "").strip():
        print("error: --note is required", file=sys.stderr)
        return 2
    new = {"confirm": "OK-REVIEWED", "reject": "WRONG-ID", "superseded": "SUPERSEDED"}[args.decision]
    if args.decision == "superseded" and not args.superseded_by:
        print("error: --superseded-by is required with --decision superseded", file=sys.stderr)
        return 2
    rev = {"decision": args.decision, "note": args.note.strip(), "at": now_iso()}
    if args.superseded_by:
        rev["superseded_by"] = args.superseded_by
    for rec in targets:
        rec["review"] = dict(rev)
        rec["status"] = new
        rec["reason"] = {"confirm": "reviewer-confirmed", "reject": "reviewer-rejected",
                         "superseded": "reviewer-superseded"}[args.decision]
    rec = targets[0]
    summ = summarize(audit["records"], audit.get("duplicate_findings", []))
    audit.update({"verified": summ["verified"], "counts": summ["counts"], "audit_pass": summ["audit_pass"],
                  "needs_review": summ["needs_review"]})
    tmp = args.audit + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, args.audit)
    lpath = None if args.no_ledger else (args.ledger or audit.get("ledger_path") or default_ledger())
    if lpath:
        Ledger(lpath).append([ledger_row(rec, audit["audit_id"], audit.get("source_sha256", ""))])
    print(f"{args.key} ({len(targets)} record(s)): NEEDS-REVIEW -> {new} · audit_pass {str(audit['audit_pass']).lower()}")
    return 0


def run_extract(args) -> int:
    if bool(args.text) == bool(args.stdin):
        print("error: give exactly one of --text, --stdin", file=sys.stderr)
        return 2
    try:
        data = open(args.text, "rb").read() if args.text else sys.stdin.buffer.read()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    hits = extract_identifiers(data.decode("utf-8", "replace"))
    for h in hits:
        h.pop("start", None)
    print(json.dumps(hits, indent=2, ensure_ascii=False))
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify_citations.py",
        description="Deterministic citation verifier. A script fetches registry records and assigns the status "
                    "(OK, OK-REVIEWED, CORRECTED, NEEDS-REVIEW, WRONG-ID, MISMATCH, NOT-FOUND, RETRACTED, CONCERN, "
                    "SUPERSEDED, UNVERIFIED); a model never assigns OK.",
        epilog="audit exit codes: 0 audit_pass true; 1 audit ran, audit_pass false; 2 usage or internal error. "
               "Env: CITATION_AUDIT_MAILTO (polite-pool contact), NCBI_API_KEY (10 req/s).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="verify every identifier in a text or ids file")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--text", metavar="FILE", help="text file to extract identifiers from")
    g.add_argument("--ids", metavar="FILE.json", help="JSON list of {type, id?, cited?, claim?, context?}")
    g.add_argument("--stdin", action="store_true", help="read text from stdin")
    a.add_argument("--out", metavar="audit.json", help="write audit JSON here (default: stdout)")
    a.add_argument("--raw-dir", metavar="DIR", help="save raw registry answers here (default: <out dir>/raw/ or a temp dir)")
    a.add_argument("--upstream-skill", metavar="NAME", help="name recorded as upstream_skill")
    a.add_argument("--ledger", metavar="PATH", help="ledger JSONL path (default: ~/.claude/data/citation-ledger.jsonl)")
    a.add_argument("--no-ledger", action="store_true", help="neither read nor write the ledger")
    a.add_argument("--ttl-days", type=float, default=30, help="ledger reuse window in days (default 30)")
    a.add_argument("--offline", action="store_true", help="no network: ledger rows only, else UNVERIFIED (offline)")
    a.add_argument("--timeout", type=float, default=15, help="per-request timeout in seconds (default 15)")
    a.add_argument("--strict", action="store_true", help="forbid --offline")
    a.add_argument("--accept-bare", action="store_true",
                   help="for identifiers a pipeline just retrieved from a registry: a record that resolves, has no "
                        "cited metadata and no context text at all, and has no retraction/concern linkage gets OK "
                        "(flag and reason existence-only; CORRECTED if it carries an erratum). Records with any "
                        "context or cited metadata are unaffected.")
    a.add_argument("--simulate-transport-fail", metavar="SOURCE[,SOURCE]",
                   help="test aid: make these sources fail like a timeout (pubmed, crossref, doiorg, openalex, "
                        "arxiv, ctgov, pmc; also url, isrctn, eudract). Disables the ledger for the run.")
    b = sub.add_parser("block", help="print the fenced citation-audit block for an audit.json")
    b.add_argument("--audit", required=True, metavar="audit.json")
    r = sub.add_parser("review", help="record a reviewer decision on a NEEDS-REVIEW record (edits audit.json in place)")
    r.add_argument("--audit", required=True, metavar="audit.json")
    r.add_argument("--key", required=True, help="record key, e.g. PMID:12345")
    r.add_argument("--decision", required=True, choices=["confirm", "reject", "superseded"],
                   help="confirm -> OK-REVIEWED; reject -> WRONG-ID; superseded -> SUPERSEDED")
    r.add_argument("--note", required=True, help="reviewer note (required)")
    r.add_argument("--superseded-by", metavar="TEXT", help="newer edition (required with --decision superseded)")
    r.add_argument("--ledger", metavar="PATH", help="ledger path (default: the path recorded in audit.json)")
    r.add_argument("--no-ledger", action="store_true", help="do not append to the ledger")
    e = sub.add_parser("extract", help="extraction only, JSON to stdout")
    eg = e.add_mutually_exclusive_group()
    eg.add_argument("--text", metavar="FILE")
    eg.add_argument("--stdin", action="store_true")
    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return {"audit": run_audit, "block": run_block, "review": run_review, "extract": run_extract}[args.cmd](args)
    except SystemExit as e:
        return 2 if (e.code not in (0, None)) else 0
    except BrokenPipeError:
        return 0
    except Exception as e:  # internal error: exit 2, never a false pass
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
