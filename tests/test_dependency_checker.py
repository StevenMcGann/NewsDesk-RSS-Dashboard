"""Regression coverage for runtime vulnerability queries and incomplete checks."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from _path import ROOT

spec = importlib.util.spec_from_file_location("check_dependencies", ROOT / "tools/check_dependencies.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

PYTHON = {"name": "Python", "ecosystem": "python.org", "package": "python", "version": "3.14.8"}
SHA = "a" * 40


class Output(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


class RuntimeQueryTest(unittest.TestCase):
    def test_python_version_lookup_does_not_hide_network_failures(self):
        listing = (b'<a href="3.14.10/">3.14.10/</a>', {})
        for error in (OSError("timed out"), HTTPError("url", 503, "unavailable", {}, None)):
            with self.subTest(error=error), mock.patch.object(checker, "get", side_effect=[listing, error]):
                with self.assertRaises(OSError):
                    checker.latest(PYTHON)
        for responses in ([(b"changed listing format", {})],
                          [listing, HTTPError("url", 404, "missing", {}, None)]):
            with mock.patch.object(checker, "get", side_effect=responses):
                with self.assertRaises(ValueError):
                    checker.latest(PYTHON)
        with mock.patch.object(checker, "get", side_effect=[listing, (b"", {"Last-Modified": "Sun, 25 Oct 2026 12:00:00 GMT"})]):
            self.assertEqual(checker.latest(PYTHON)[0], "3.14.10")

    def test_annotated_release_tag_is_resolved_in_official_repository(self):
        with mock.patch.object(checker, "get_json", side_effect=[
                {"object": {"type": "tag", "sha": SHA, "url": "https://untrusted.test/"}},
                {"object": {"type": "commit", "sha": "b" * 40}}]) as get:
            self.assertEqual(checker.python_commit("3.14.10"), "b" * 40)
        self.assertEqual(get.call_args_list, [
            mock.call("https://api.github.com/repos/python/cpython/git/ref/tags/v3.14.10"),
            mock.call(f"https://api.github.com/repos/python/cpython/git/tags/{SHA}")])

    def test_lightweight_tag_and_invalid_targets(self):
        with mock.patch.object(checker, "get_json", return_value={"object": {"type": "commit", "sha": SHA}}):
            self.assertEqual(checker.python_commit("3.14.10"), SHA)
        for obj in ({"type": "tree", "sha": SHA}, {"type": "commit", "sha": "bad"}):
            with self.subTest(obj=obj), mock.patch.object(checker, "get_json", return_value={"object": obj}):
                with self.assertRaises(ValueError):
                    checker.python_commit("3.14.10")
        with mock.patch.object(checker, "get_json", return_value={"object": {"type": "tag", "sha": SHA}}):
            with self.assertRaises(ValueError):
                checker.python_commit("3.14.10")
        with self.assertRaises(ValueError):
            checker.python_commit("3.14.10/../../main")

    def test_runtime_uses_commit_query_and_preserves_psf_cve_evidence(self):
        detail = {"id": "PSF-2026-46", "aliases": ["CVE-2026-115557"], "details": "Compromised CA keys"}
        responses = [{"results": [{"vulns": [{"id": "PSF-2026-46"}]}]}, detail]
        with mock.patch.object(checker, "python_commit", return_value=SHA), \
                mock.patch.object(checker, "get_json", side_effect=responses) as get:
            found = checker.vulnerabilities([PYTHON])
        self.assertEqual(get.call_args_list[0], mock.call(
            "https://api.osv.dev/v1/querybatch", {"queries": [{"commit": SHA}]}))
        self.assertIn("CVE-2026-115557", found["Python"][0][1])

    def test_package_queries_still_work_and_empty_results_are_valid(self):
        component = {"name": "DOMPurify", "ecosystem": "npm", "package": "dompurify", "version": "3.4.17"}
        with mock.patch.object(checker, "get_json", return_value={"results": [{}]}) as get:
            self.assertEqual(checker.vulnerabilities([component]), {})
        self.assertEqual(get.call_args.args[1]["queries"][0], {
            "package": {"name": "dompurify", "ecosystem": "npm"}, "version": "3.4.17"})

    def test_pagination_does_not_drop_later_runtime_findings(self):
        with mock.patch.object(checker, "get_json", side_effect=[
                {"results": [{}, {"next_page_token": "next"}]},
                {"results": [{"vulns": [{"id": "PSF-2026-46"}]}]}]) as get:
            results = checker.osv_results([{"version": "1"}, {"commit": SHA}])
        self.assertEqual(results, [[], [{"id": "PSF-2026-46"}]])
        self.assertEqual(get.call_args.args[1], {"queries": [{"commit": SHA, "page_token": "next"}]})

    def test_incomplete_malformed_and_looping_results_are_rejected(self):
        for batch in ({}, {"results": []}, {"results": [None]}, {"results": [{"error": "failed"}]},
                      {"results": [{"vulns": None}]}, {"results": [{"vulns": [{"id": "../bad"}]}]}):
            with self.subTest(batch=batch), mock.patch.object(checker, "get_json", return_value=batch):
                with self.assertRaises((ValueError, KeyError)):
                    checker.osv_results([{"commit": SHA}])
        with mock.patch.object(checker, "get_json", return_value={"results": [{"next_page_token": "repeat"}]}):
            with self.assertRaises(ValueError):
                checker.osv_results([{"commit": SHA}])

    def test_duplicate_aliases_and_withdrawn_records(self):
        records = [{"id": "CVE-2026-115557"}, {"id": "PSF-2026-46"}, {"id": "PSF-2026-45"}]
        def detail(url):
            vid = url.rsplit("/", 1)[-1]
            return {"id": vid, "aliases": ["CVE-2026-115557"], "summary": "Security fix",
                    "withdrawn": "2026-10-01" if vid.endswith("45") else None}
        with mock.patch.object(checker, "python_commit", return_value=SHA), \
                mock.patch.object(checker, "osv_results", return_value=[records]), \
                mock.patch.object(checker, "get_json", side_effect=detail):
            self.assertEqual(len(checker.vulnerabilities([PYTHON])["Python"]), 1)


class ReportTest(unittest.TestCase):
    def run_report(self, findings=None, vuln_error=None, latest_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"components": [PYTHON]}), encoding="utf-8")
            output = root / "output"
            stdout = Output()
            with mock.patch.object(checker, "MANIFEST", manifest), \
                    mock.patch.object(sys, "argv", ["check_dependencies"]), \
                    mock.patch.object(sys, "stdout", stdout), \
                    mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": ""}), \
                    mock.patch.object(checker, "vulnerabilities", return_value=findings or {}, side_effect=vuln_error), \
                    mock.patch.object(checker, "latest", side_effect=latest_error,
                                      return_value=("3.14.10", datetime.now(timezone.utc))):
                code = checker.main()
            return code, stdout.getvalue(), output.read_text()

    def test_security_finding_bypasses_update_wait(self):
        code, report, output = self.run_report({"Python": [("PSF-2026-46", "CVE-2026-115557") ]})
        self.assertEqual(code, 1)
        self.assertIn("do not wait 14 days", report)
        self.assertIn("attention=true", output)

    def test_coverage_failure_never_claims_clean_or_closes_issue(self):
        for failure in (OSError("offline"), ValueError("malformed"), KeyError("results"), TypeError("invalid")):
            with self.subTest(failure=failure):
                code, report, output = self.run_report(vuln_error=failure)
                self.assertEqual(code, 2)
                self.assertIn("coverage incomplete", report)
                self.assertNotIn("No unreviewed vulnerabilities", report)
                self.assertIn("attention=true", output)

    def test_version_check_failure_also_keeps_attention(self):
        code, report, output = self.run_report(latest_error=OSError("offline"))
        self.assertEqual(code, 2)
        self.assertIn("Version check incomplete", report)
        self.assertNotIn("No unreviewed vulnerabilities", report)
        self.assertIn("attention=true", output)

    def test_nonsecurity_updates_keep_waiting_period(self):
        code, report, output = self.run_report()
        self.assertEqual(code, 0)
        self.assertIn("wait until", report)
        self.assertIn("attention=false", output)


if __name__ == "__main__":
    unittest.main()
