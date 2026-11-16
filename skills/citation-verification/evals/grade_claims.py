#!/usr/bin/env python3
"""grade_claims.py - grade the claim-support gates in scripts/claim_packets.py.

Runs every case in claim_cases.jsonl (kinds: check, numbers, assertion). `check` cases run
the real `claim_packets.py check` CLI on a one-packet fixture. No network.

  grade_claims.py [--split train|test|all] [--script PATH] [--cases PATH] [--json]

Exit 0 only when every selected case passes and at least 8 ran.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module(path: str):
    spec = importlib.util.spec_from_file_location("claim_packets_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_check(script: str, case: dict) -> tuple[bool, str]:
    exp = case["expect"]
    if "packets" in case:
        return run_multi(script, case)
    with tempfile.TemporaryDirectory() as td:
        pf, vf, of = (os.path.join(td, n) for n in ("p.json", "v.json", "o.json"))
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"kind": "ClaimPackets", "packets": [case["packet"]], "not_checkable": []}, fh)
        with open(vf, "w", encoding="utf-8") as fh:
            json.dump([case["verdict"]], fh)
        r = subprocess.run([sys.executable, "-I", script, "check", "--packets", pf, "--verdicts", vf,
                            "--out", of], capture_output=True, text=True, timeout=60)
        if r.returncode not in (0, 1):
            return False, f"exit {r.returncode}: {r.stderr.strip()[:120]}"
        with open(of, encoding="utf-8") as fh:
            out = json.load(fh)
    rows = out["blocking"] + out["advisory"]
    want_block = exp.get("blocking", False)
    want_exit = 1 if (want_block or exp.get("incomplete")) else 0
    if bool(out["blocking"]) != want_block or r.returncode != want_exit:
        return False, f"blocking={bool(out['blocking'])} exit={r.returncode}, wanted blocking={want_block} exit={want_exit}"
    if exp.get("not_checked"):
        return (not rows and bool(out["not_checked"])), f"rows={len(rows)} not_checked={len(out['not_checked'])}"
    if exp.get("unmatched"):
        return (not rows and bool(out["unmatched_verdicts"])), f"unmatched={len(out['unmatched_verdicts'])}"
    if len(rows) != 1 or rows[0]["verdict"] != exp["final"]:
        return False, f"got {[x['verdict'] for x in rows]}, wanted {exp['final']}"
    needle = exp.get("gate_contains")
    if needle and not any(needle in g for g in rows[0]["gate"]):
        return False, f"gate {rows[0]['gate']} lacks {needle!r}"
    return True, rows[0]["verdict"]


def run_multi(script: str, case: dict) -> tuple[bool, str]:
    exp = case["expect"]
    with tempfile.TemporaryDirectory() as td:
        pf, vf, of = (os.path.join(td, n) for n in ("p.json", "v.json", "o.json"))
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"kind": "ClaimPackets", "packets": case["packets"], "not_checkable": []}, fh)
        with open(vf, "w", encoding="utf-8") as fh:
            json.dump(case["verdicts"], fh)
        r = subprocess.run([sys.executable, "-I", script, "check", "--packets", pf, "--verdicts", vf,
                            "--out", of], capture_output=True, text=True, timeout=60)
        if r.returncode not in (0, 1):
            return False, f"exit {r.returncode}: {r.stderr.strip()[:120]}"
        with open(of, encoding="utf-8") as fh:
            out = json.load(fh)
    got = (out["claim_pass"], out["claim_complete"], r.returncode, len(out["not_checked"]))
    want = (exp["claim_pass"], exp["claim_complete"], 0 if exp["claim_pass"] else 1, exp["not_checked"])
    return got == want, f"pass,complete,exit,not_checked={got}"


def grade(case: dict, script: str, mod) -> tuple[bool, str]:
    kind = case["kind"]
    if kind == "check":
        return run_check(script, case)
    if kind == "numbers":
        have = mod.claim_numbers(case["claim"])
        got = bool(have) and have <= mod.numbers_in(case["span"])
        return got == case["expect"]["equal"], f"equal={got}"
    if kind == "assertion":
        got = mod.has_checkable_assertion(case["claim"])[0]
        return got == case["expect"]["checkable"], f"checkable={got}"
    return False, f"unknown kind {kind}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "test", "all"], default="all")
    ap.add_argument("--script", default=os.path.join(HERE, "..", "scripts", "claim_packets.py"))
    ap.add_argument("--cases", default=os.path.join(HERE, "claim_cases.jsonl"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    script = os.path.abspath(a.script)
    mod = load_module(script)
    with open(a.cases, encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]
    if a.split != "all":
        cases = [c for c in cases if c["split"] == a.split]
    results = []
    for c in cases:
        try:
            ok, detail = grade(c, script, mod)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"error: {e}"
        results.append({"id": c["id"], "split": c["split"], "kind": c["kind"], "pass": ok, "detail": detail})
    passed = sum(r["pass"] for r in results)
    if a.json:
        print(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=1))
    else:
        for r in results:
            print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['split']:5} {r['kind']:9} {r['id']:30} {r['detail']}")
        print(f"passed {passed}/{len(results)}")
    return 0 if results and passed == len(results) and len(results) >= 8 else 1


if __name__ == "__main__":
    sys.exit(main())
