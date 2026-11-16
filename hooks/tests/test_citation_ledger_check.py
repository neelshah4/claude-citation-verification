#!/usr/bin/env python3
"""Tests for citation-ledger-check.py. No network; temp dirs and env overrides only.
Run: python3 -m unittest test_citation_ledger_check   (from this directory)
  or: python3 -m unittest discover -s <this dir> -p 'test_citation_ledger_check.py'"""
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "citation-ledger-check.py")

CLIN = "In this randomized trial of PICU patients (PMID: 12345678) mortality fell."
NOW = datetime.datetime.now(datetime.timezone.utc)


def iso(days_ago=0):
    return (NOW - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(key, status="OK", days_ago=1):
    return {"key": key, "claim_sha256": "x", "status": status,
            "resolved": {}, "checked_at": iso(days_ago), "audit_id": "a", "sources": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.log = os.path.join(self.tmp.name, "logs", "hook.log")
        # a verifier path that does not exist forces the regex fallback
        self.missing_verifier = os.path.join(self.tmp.name, "nope.py")

    def write_ledger(self, rows, raw_lines=()):
        with open(self.ledger, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            for line in raw_lines:
                f.write(line + "\n")

    def run_hook(self, msg, mode=None, hook_input=None, env=None, stdin=None, verifier=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CITATION_")}
        e.update({"CITATION_LEDGER_PATH": self.ledger, "CITATION_HOOK_LOG_PATH": self.log,
                  "CITATION_VERIFIER_PATH": verifier or self.missing_verifier,
                  "HOME": self.tmp.name})
        if mode:
            e["CITATION_HOOK_MODE"] = mode
        e.update(env or {})
        inp = {"last_assistant_message": msg, "permission_mode": "default", "session_id": "s1"}
        inp.update(hook_input or {})
        payload = stdin if stdin is not None else json.dumps(inp)
        r = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                           text=True, env=e, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr

    def log_rows(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]


class TestSpecCases(Base):
    def test_covered_identifier_silent(self):
        self.write_ledger([row("PMID:12345678")])
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.log_rows(), [])

    def test_uncovered_warn_logs_and_system_message(self):
        self.write_ledger([row("PMID:99999999")])
        rc, out, _ = self.run_hook(CLIN, mode="warn")
        self.assertEqual(rc, 0)
        msg = json.loads(out)
        self.assertIn("PMID:12345678", msg["systemMessage"])
        self.assertNotIn("decision", msg)
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["key"], rows[0]["reason"], rows[0]["mode"]),
                         ("PMID:12345678", "no_row", "warn"))

    def test_default_mode_is_warn(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", json.loads(out))

    def test_uncovered_block_emits_block_json(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, mode="block")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["decision"], "block")
        self.assertIn("verify_citations.py audit", d["reason"])
        self.assertIn("PMID:12345678", d["reason"])

    def test_block_mode_stop_hook_active_never_blocks(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, mode="block", hook_input={"stop_hook_active": True})
        self.assertEqual((rc, out), (0, ""))

    def test_stale_row_is_uncovered(self):
        self.write_ledger([row("PMID:12345678", days_ago=45)])
        rc, out, _ = self.run_hook(CLIN)
        self.assertIn("systemMessage", json.loads(out))
        self.assertEqual(self.log_rows()[0]["reason"], "stale")

    def test_ttl_env_widens_window(self):
        self.write_ledger([row("PMID:12345678", days_ago=45)])
        rc, out, _ = self.run_hook(CLIN, env={"CITATION_LEDGER_TTL_DAYS": "60"})
        self.assertEqual(out, "")

    def test_non_clinical_bare_doi_silent(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook("The build script is described at doi 10.5281/zenodo.123456 for reference.")
        self.assertEqual((rc, out), (0, ""))

    def test_doi_with_clinical_anchor_flags(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook("The cohort study appeared in the journal (doi:10.1001/jama.2020.1234).")
        self.assertIn("DOI:10.1001/jama.2020.1234", json.loads(out)["systemMessage"])

    def test_malformed_stdin_exit_zero(self):
        for bad in ("{not json", "", "[]", "null"):
            rc, out, _ = self.run_hook("x", stdin=bad)
            self.assertEqual((rc, out), (0, ""), bad)

    def test_subagent_exit_zero(self):
        self.write_ledger([])
        for k in ("agent_id", "agent_type"):
            rc, out, _ = self.run_hook(CLIN, hook_input={k: "abc"})
            self.assertEqual((rc, out), (0, ""), k)

    def test_plan_and_bypass_mode_exit_zero(self):
        self.write_ledger([])
        for pm in ("plan", "bypassPermissions"):
            rc, out, _ = self.run_hook(CLIN, hook_input={"permission_mode": pm})
            self.assertEqual((rc, out), (0, ""), pm)

    def test_identifiers_inside_audit_block_exempt(self):
        self.write_ledger([])
        msg = ("```citation-audit\nverified: 0 of 1\nnot found: PMID:12345678\n"
               "needs review: DOI:10.1001/jama.2020.1234\n```\nThe trial results follow.")
        rc, out, _ = self.run_hook(msg)
        self.assertEqual((rc, out), (0, ""))

    def test_bracketed_markers_exempt(self):
        self.write_ledger([])
        msg = ("Trial data [CITATION NEEDED PMID: 12345678] and [RETRACTED PMID 23456789] "
               "and [WRONG ID: PMID 34567890 is a different paper] in patients.")
        rc, out, _ = self.run_hook(msg)
        self.assertEqual((rc, out), (0, ""))


class TestExtras(Base):
    def test_off_mode_exits_immediately(self):
        self.write_ledger([])
        # off must not even read stdin: pass garbage and a message that would otherwise warn
        rc, out, _ = self.run_hook(CLIN, mode="off")
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.log_rows(), [])
        rc, out, _ = self.run_hook(CLIN, mode="off", stdin="{garbage")
        self.assertEqual((rc, out), (0, ""))

    def test_unknown_mode_behaves_as_warn(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, mode="loud")
        self.assertIn("systemMessage", json.loads(out))

    def test_message_reporting_failure_not_flagged(self):
        self.write_ledger([])
        msg = ("In the randomized trial of patients, PMID: 12345678 was not found in PubMed. "
               "The DOI 10.1001/jama.2020.1234 does not exist either.")
        rc, out, _ = self.run_hook(msg)
        self.assertEqual((rc, out), (0, ""))

    def test_failure_report_exempts_only_its_own_sentence(self):
        self.write_ledger([])
        msg = ("The cohort study (PMID: 11111111) reported lower mortality. "
               "PMID: 22222222 was not found.")
        rc, out, _ = self.run_hook(msg)
        text = json.loads(out)["systemMessage"]
        self.assertIn("PMID:11111111", text)
        self.assertNotIn("PMID:22222222", text)

    def test_malformed_ledger_line_skipped(self):
        self.write_ledger([row("PMID:12345678")], raw_lines=['{"key": "PMID:12345678", broken', "not json", '"str"', "[1]"])
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual((rc, out), (0, ""))
        # malformed lines before the good row also do not hide it
        with open(self.ledger, "w") as f:
            f.write("{oops PMID:12345678\n")
            f.write(json.dumps(row("PMID:12345678")) + "\n")
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual((rc, out), (0, ""))

    def test_newest_row_wins(self):
        # older OK, newer RETRACTED -> uncovered
        self.write_ledger([row("PMID:12345678", "OK", 10), row("PMID:12345678", "RETRACTED", 2)])
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual(self.log_rows()[0]["reason"], "status:RETRACTED")
        # older RETRACTED, newer OK -> covered (order in file reversed)
        os.remove(self.log)
        self.write_ledger([row("PMID:12345678", "OK", 2), row("PMID:12345678", "RETRACTED", 10)])
        rc, out, _ = self.run_hook(CLIN)
        self.assertEqual(out, "")

    def test_blocking_statuses_uncovered_passing_covered(self):
        for st, covered in (("OK", True), ("OK-REVIEWED", True), ("CORRECTED", True),
                            ("SUPERSEDED", True), ("NEEDS-REVIEW", False), ("NOT-FOUND", False),
                            ("UNVERIFIED", False), ("WRONG-ID", False), ("MISMATCH", False),
                            ("CONCERN", False)):
            self.write_ledger([row("PMID:12345678", st)])
            rc, out, _ = self.run_hook(CLIN)
            self.assertEqual(out == "", covered, st)

    def test_missing_ledger_file_warns_not_crashes(self):
        rc, out, _ = self.run_hook(CLIN)  # ledger file never created
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", json.loads(out))

    def test_unwritable_log_does_not_break(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, env={"CITATION_HOOK_LOG_PATH": "/dev/null/x/log"})
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", json.loads(out))

    def test_nct_alone_is_anchor(self):
        self.write_ledger([])
        rc, out, _ = self.run_hook("See NCT01234567 for details.")
        self.assertIn("NCT:NCT01234567", json.loads(out)["systemMessage"])

    def test_doi_ledger_key_case_insensitive_match(self):
        self.write_ledger([row("DOI:10.1001/jama.2020.1234")])
        rc, out, _ = self.run_hook("The cohort study (https://doi.org/10.1001/JAMA.2020.1234).")
        self.assertEqual(out, "")


def load_hook():
    spec = importlib.util.spec_from_file_location("clc_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STUB = '''
import re
def extract_identifiers(text):
    out = []
    for m in re.finditer(r"PMID\\D{0,3}(\\d{5,9})", text, re.I):
        out.append({"key": "PMID:" + m.group(1), "type": "PMID"})
    for m in re.finditer(r"pubmed\\.ncbi\\.nlm\\.nih\\.gov/(\\d{5,9})", text):
        out.append({"key": "PMID:" + m.group(1), "type": "PMID"})
    for m in re.finditer(r"doi(?:\\.org/|:)\\s?(10\\.\\d{4,9}/[^\\s,;)]+)", text, re.I):
        out.append({"key": "DOI:" + m.group(1).rstrip(".").lower(), "type": "DOI"})
    for m in re.finditer(r"NCT\\d{8}", text):
        out.append({"key": "NCT:" + m.group(0), "type": "NCT"})
    if "STUBMARKER" in text:
        out.append({"key": "PMID:55555555", "type": "PMID"})
    out.append({"key": "URL:https://example.org/x", "type": "URL"})  # non-clinical type, must be dropped
    return out
'''

SAMPLE = ("Trial A (PMID: 12345678; see https://pubmed.ncbi.nlm.nih.gov/23456789) and doi:10.1001/jama.2020.1234. "
          "Registered as NCT01234567. Also PMID 345678.")


class TestExtraction(Base):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp.name, "stub_verifier.py")
        with open(self.stub, "w") as f:
            f.write(STUB)
        self.mod = load_hook()

    def test_stub_and_fallback_keys_identical(self):
        os.environ["CITATION_VERIFIER_PATH"] = self.missing_verifier
        fb = self.mod.extract_keys(SAMPLE)
        os.environ["CITATION_VERIFIER_PATH"] = self.stub
        try:
            st = self.mod.extract_keys(SAMPLE)
        finally:
            os.environ.pop("CITATION_VERIFIER_PATH", None)
        self.assertEqual(set(fb), set(st))
        self.assertEqual(set(fb), {"PMID:12345678", "PMID:23456789", "PMID:345678",
                                   "DOI:10.1001/jama.2020.1234", "NCT:NCT01234567"})

    def test_stub_branch_is_really_used(self):
        self.write_ledger([row("PMID:12345678")])
        text = CLIN + " STUBMARKER"
        rc, out, _ = self.run_hook(text, verifier=self.stub)
        self.assertIn("PMID:55555555", json.loads(out)["systemMessage"])
        rc, out, _ = self.run_hook(text)  # fallback: marker unknown, only covered PMID
        self.assertEqual(out, "")

    def test_broken_verifier_falls_back(self):
        bad = os.path.join(self.tmp.name, "bad.py")
        with open(bad, "w") as f:
            f.write("raise RuntimeError('boom')\n")
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, verifier=bad)
        self.assertEqual(rc, 0)
        self.assertIn("PMID:12345678", json.loads(out)["systemMessage"])

    def test_verifier_import_cannot_pollute_stdout(self):
        noisy = os.path.join(self.tmp.name, "noisy.py")
        with open(noisy, "w") as f:
            f.write("print('NOISE')\n" + STUB)
        self.write_ledger([])
        rc, out, _ = self.run_hook(CLIN, verifier=noisy)
        self.assertTrue(out.startswith("{"))
        json.loads(out)

    def test_fallback_normalisation(self):
        f = self.mod.fallback_keys
        self.assertEqual(f("(doi: 10.1056/NEJMoa2034577)."), ["DOI:10.1056/nejmoa2034577"])
        self.assertEqual(f("see 10.1001/jama.2020.1234."), ["DOI:10.1001/jama.2020.1234"])
        self.assertEqual(f("nct01234567"), ["NCT:NCT01234567"])
        self.assertEqual(f("PMID 1234"), [])          # too short
        self.assertEqual(f("12345678"), [])            # bare number is never a PMID
        self.assertEqual(f("PMID:12345678 PMID:12345678"), ["PMID:12345678"])


class TestTiming(Base):
    def test_20k_row_ledger_fast(self):
        with open(self.ledger, "w") as f:
            for i in range(20000):
                f.write(json.dumps(row("PMID:%d" % (10000000 + i))) + "\n")
            f.write(json.dumps(row("PMID:12345678")) + "\n")
        t0 = time.perf_counter()
        rc, out, _ = self.run_hook(CLIN)
        dt = time.perf_counter() - t0
        print("\n[timing] 20,001-row ledger, full subprocess: %.3f s" % dt, file=sys.stderr)
        self.assertEqual((rc, out), (0, ""))
        self.assertLess(dt, 1.0)


if __name__ == "__main__":
    unittest.main()
