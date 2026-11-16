#!/usr/bin/env python3
"""Grade verify_citations.py against the seeded-defect case bank (cases.jsonl).

Usage:
    grade.py [--split train|test|all] [--script PATH] [--cases PATH] [--json]
             [--batch] [--timeout SEC] [--verbose]

Each case is run against the script under test with a throwaway ledger and a
throwaway raw directory, so a real ledger (~/.claude/data/citation-ledger.jsonl)
is never read or written. By default one `audit` invocation runs per case
(simplest mapping, immune to ordering or duplicate-key behaviour). `--batch`
runs every case that needs no simulated failure in one invocation per split.

Case inputs: an `--ids` entry (`input` is a dict with type/id/cited) or free text
(`input` is {"text": "..."} run with `--text`; the record whose key equals
`expect.key` is graded, a missing record fails the case).

Metrics:
    accuracy = share of cases whose returned status is in expect.status_in
    false_ok = cases whose expected set excludes OK / OK-REVIEWED / CORRECTED
               but which came back as one of those three (an unsafe pass)
    false_notfound = simulated-transport cases that came back NOT-FOUND (the
               spec forbids it; bar == 0, fails the run)
    Cases in category "known_limit" are probes: printed with status and flags,
    never counted in accuracy, false_ok or false_notfound.

Exit codes:
    0  false_ok == 0, false_notfound == 0 and accuracy >= 0.90
    1  graded, but false_ok > 0, false_notfound > 0 or accuracy < 0.90
    2  usage problem: script absent, cases unreadable, nothing selected
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCRIPT = os.path.normpath(os.path.join(HERE, "..", "scripts", "verify_citations.py"))
DEFAULT_CASES = os.path.join(HERE, "cases.jsonl")
OK_LIKE = {"OK", "OK-REVIEWED", "CORRECTED"}
ACC_BAR = 0.90


def load_cases(path, split):
    cases = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: bad JSON ({e})")
            for k in ("id", "split", "category", "input", "expect"):
                if k not in c:
                    raise ValueError(f"{path}:{n}: case missing '{k}'")
            if split == "all" or c["split"] == split:
                cases.append(c)
    return cases


def run_audit(script, inputs, simulate, timeout, workdir, text=None):
    """Run one audit over a list of --ids entries, or over `text` with --text.
    Returns (exit_code, audit_dict|None, note)."""
    out_path = os.path.join(workdir, "audit.json")
    if text is not None:
        in_path = os.path.join(workdir, "draft.txt")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        src = ["--text", in_path]
    else:
        in_path = os.path.join(workdir, "ids.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(inputs, f, ensure_ascii=False)
        src = ["--ids", in_path]
    cmd = [sys.executable, script, "audit"] + src + ["--out", out_path,
           "--raw-dir", os.path.join(workdir, "raw"),
           "--ledger", os.path.join(workdir, "ledger.jsonl")]
    if simulate:
        cmd += ["--simulate-transport-fail", simulate]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
    except subprocess.TimeoutExpired:
        return None, None, f"timeout after {timeout}s"
    audit = None
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                audit = json.load(f)
        except json.JSONDecodeError:
            audit = None
    if audit is None:
        try:
            audit = json.loads(p.stdout)
        except (json.JSONDecodeError, ValueError):
            audit = None
    note = ""
    if p.returncode == 2 or audit is None:
        note = (p.stderr.strip().splitlines() or p.stdout.strip().splitlines() or ["no output"])[-1][:160]
    return p.returncode, audit, note


def key_of(entry):
    t = str(entry.get("type", "")).upper()
    i = str(entry.get("id", "")).strip()
    if t == "DOI":
        i = i.lower()
    return f"{t}:{i}"


def grade_case(case, status, note, rec=None):
    exp = case["expect"]["status_in"]
    extra = {}
    if rec:
        extra = {"flags": rec.get("flags") or [], "basis": (rec.get("resolved") or {}).get("match_basis")}
    if status is None:
        r = {"verdict": "ERROR", "status": None, "note": note}
    elif status in exp:
        r = {"verdict": "PASS", "status": status, "note": note}
    elif status in OK_LIKE and not (set(exp) & OK_LIKE):
        r = {"verdict": "FALSE-OK", "status": status, "note": note}
    else:
        r = {"verdict": "FAIL", "status": status, "note": note}
    if status == "NOT-FOUND" and (case.get("simulate_transport_fail") or case["category"] == "transport_fail"):
        r["false_notfound"] = True
    r.update(extra)
    return r


def main():
    ap = argparse.ArgumentParser(description="Grade verify_citations.py against the case bank.")
    ap.add_argument("--split", choices=["train", "test", "all"], default="all")
    ap.add_argument("--script", default=DEFAULT_SCRIPT, help="path to verify_citations.py (default: ../scripts/verify_citations.py)")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--json", action="store_true", help="print one JSON summary instead of the table")
    ap.add_argument("--batch", action="store_true", help="one invocation for all non-simulated cases (default: one per case)")
    ap.add_argument("--timeout", type=int, default=240, help="seconds per audit invocation")
    ap.add_argument("--verbose", action="store_true", help="print the failure note for each non-passing case")
    a = ap.parse_args()

    if not os.path.isfile(a.script):
        print(f"grade.py: script not found: {a.script}", file=sys.stderr)
        return 2
    try:
        cases = load_cases(a.cases, a.split)
    except (OSError, ValueError) as e:
        print(f"grade.py: cannot load cases: {e}", file=sys.stderr)
        return 2
    if not cases:
        print(f"grade.py: no cases selected (split={a.split})", file=sys.stderr)
        return 2

    results = OrderedDict()
    with tempfile.TemporaryDirectory(prefix="citeval-") as root:
        todo_batch = [c for c in cases if a.batch and not c.get("simulate_transport_fail") and "text" not in c["input"]]
        batched = {c["id"] for c in todo_batch}
        if todo_batch:
            wd = os.path.join(root, "batch")
            os.makedirs(wd)
            code, audit, note = run_audit(a.script, [c["input"] for c in todo_batch], None, a.timeout, wd)
            recs = (audit or {}).get("records") or []
            by_key = {r.get("key"): r for r in recs if r.get("key")}
            for i, c in enumerate(todo_batch):
                rec = None
                if len(recs) == len(todo_batch):
                    rec = recs[i]
                elif key_of(c["input"]) in by_key and c["input"].get("type") != "REF":
                    rec = by_key[key_of(c["input"])]
                status = rec.get("status") if rec else None
                results[c["id"]] = grade_case(c, status, note or ("" if rec else "no record returned"), rec)
        for c in cases:
            if c["id"] in batched:
                continue
            wd = os.path.join(root, c["id"])
            os.makedirs(wd)
            is_text = "text" in c["input"]
            code, audit, note = run_audit(a.script, [c["input"]], c.get("simulate_transport_fail"), a.timeout, wd,
                                          text=c["input"]["text"] if is_text else None)
            recs = (audit or {}).get("records") or []
            if is_text:
                want = c["expect"].get("key")
                rec = next((r for r in recs if r.get("key") == want), None)
                if rec is None and audit is not None and not note:
                    note = f"no record with key {want} (got {[r.get('key') for r in recs]})"
            else:
                rec = recs[0] if recs else None
            status = rec.get("status") if rec else None
            if status is None and not note:
                note = f"exit {code}, no record returned"
            results[c["id"]] = grade_case(c, status, note, rec)

    probes = [c for c in cases if c["category"] == "known_limit"]
    scored = [c for c in cases if c["category"] != "known_limit"]
    if not scored:
        print("grade.py: only known_limit probes selected; nothing to score", file=sys.stderr)
        return 2
    total = len(scored)
    passed = sum(1 for c in scored if results[c["id"]]["verdict"] == "PASS")
    false_ok = [c["id"] for c in scored if results[c["id"]]["verdict"] == "FALSE-OK"]
    false_nf = [c["id"] for c in scored if results[c["id"]].get("false_notfound")]
    errors = [c["id"] for c in scored if results[c["id"]]["verdict"] == "ERROR"]
    accuracy = passed / total
    cats = OrderedDict()
    for c in scored:
        d = cats.setdefault(c["category"], [0, 0])
        d[1] += 1
        d[0] += results[c["id"]]["verdict"] == "PASS"
    ok = (not false_ok) and (not false_nf) and accuracy >= ACC_BAR

    summary = {
        "split": a.split, "script": a.script, "cases": total, "passed": passed,
        "accuracy": round(accuracy, 4), "false_ok": len(false_ok), "false_ok_ids": false_ok,
        "false_notfound": len(false_nf), "false_notfound_ids": false_nf,
        "errors": len(errors), "error_ids": errors, "bar": {"accuracy": ACC_BAR, "false_ok": 0, "false_notfound": 0},
        "pass": ok,
        "by_category": {k: {"passed": v[0], "n": v[1]} for k, v in cats.items()},
        "cases_detail": [{"id": c["id"], "split": c["split"], "category": c["category"],
                          "expected": c["expect"]["status_in"], "got": results[c["id"]]["status"],
                          "verdict": results[c["id"]]["verdict"], "flags": results[c["id"]].get("flags"),
                          "match_basis": results[c["id"]].get("basis")} for c in cases],
        "probes": [{"id": c["id"], "got": results[c["id"]]["status"], "flags": results[c["id"]].get("flags"),
                    "match_basis": results[c["id"]].get("basis")} for c in probes],
    }
    if a.json:
        print(json.dumps(summary, indent=2))
        return 0 if ok else 1

    w = max(len(c["id"]) for c in cases)
    print(f"{'case':<{w}}  {'category':<20} {'verdict':<9} got  <-  expected")
    for c in cases:
        r = results[c["id"]]
        tag = "PROBE" if c["category"] == "known_limit" else r["verdict"]
        fl = f"  flags={r.get('flags')} basis={r.get('basis')}" if "text" in c["input"] else ""
        print(f"{c['id']:<{w}}  {c['category']:<20} {tag:<9} {r['status']}  <-  {'|'.join(c['expect']['status_in'])}{fl}")
        if a.verbose and r["verdict"] != "PASS" and r["note"]:
            print(f"{'':<{w}}    note: {r['note']}")
    print()
    for k, v in cats.items():
        print(f"  {k:<20} {v[0]}/{v[1]}")
    print()
    print(f"split={a.split} cases={total} passed={passed} errors={len(errors)}")
    print(f"accuracy={accuracy:.4f} (bar >= {ACC_BAR:.2f})")
    print(f"false_ok={len(false_ok)} (bar == 0){' ' + ','.join(false_ok) if false_ok else ''}")
    print(f"false_notfound={len(false_nf)} (bar == 0){' ' + ','.join(false_nf) if false_nf else ''}")
    for c in probes:
        r = results[c["id"]]
        print(f"known_limit probe {c['id']}: status={r['status']} flags={r.get('flags')} basis={r.get('basis')} (excluded from metrics)")
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
