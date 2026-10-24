import gzip
import os
import unittest
import urllib.request
import zlib

import _path  # noqa: F401

import fetcher
from fetcher import FetchError, Fetcher, Response


def ok(via):
    return Response(200, "https://x.test/", b"body", "text/xml", None, None, via)


class DecompressTest(unittest.TestCase):
    def test_gzip_and_deflate(self):
        data = b"hello " * 1000
        self.assertEqual(fetcher._decompress(gzip.compress(data), "gzip"), data)
        self.assertEqual(fetcher._decompress(zlib.compress(data), "deflate"), data)
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        self.assertEqual(fetcher._decompress(raw.compress(data) + raw.flush(), "deflate"), data)
        self.assertEqual(fetcher._decompress(data, ""), data)

    def test_decompression_bomb_is_capped(self):
        bomb = gzip.compress(b"\0" * (fetcher.MAX_BYTES + 10))
        with self.assertRaises(FetchError):
            fetcher._decompress(bomb, "gzip")

    def test_only_http_urls(self):
        for url in ("file:///c:/windows/win.ini", "ftp://x.test/", "javascript:alert(1)", "nohost"):
            with self.assertRaises(FetchError):
                fetcher.check_url(url)


class LocalAddressTest(unittest.TestCase):
    """Article links come from feeds, so they must not reach this computer's own services."""

    def test_detects_this_computer(self):
        for url in ("http://localhost/", "http://LOCALHOST.:47821/x", "http://app.localhost/",
                    "http://127.0.0.1:47821/", "http://127.9.9.9/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://0.0.0.0/", "http://169.254.169.254/latest/meta-data", "http:///nohost"):
            self.assertTrue(fetcher.is_local_address(url), url)

    def test_allows_lan_and_internet_addresses(self):
        for url in ("http://10.1.2.3/", "http://192.168.3.158:8123/", "http://172.16.0.5/", "https://93.184.215.14/"):
            self.assertFalse(fetcher.is_local_address(url), url)

    def test_block_local_refuses_before_any_request(self):
        f = Fetcher()
        calls = []
        f.get_python = f.windows.get = lambda *a, **k: calls.append(a)
        try:
            with self.assertRaisesRegex(FetchError, "this computer"):
                f.get("http://127.0.0.1:47821/", block_local=True)
            self.assertEqual(calls, [])
            f.get("http://127.0.0.1:47821/feed.xml")  # feeds the user adds may be local
            self.assertEqual(len(calls), 1)
        finally:
            f.close()

    def test_redirects_only_to_web_addresses_and_never_home_for_articles(self):
        req = urllib.request.Request("https://example.test/a")
        blocking = fetcher._Redirects(block_local=True)
        for target, message in (("ftp://example.test/x", "isn't http"), ("file:///c:/windows/win.ini", "isn't http"),
                                ("http://127.0.0.1:47821/", "this computer")):
            with self.assertRaisesRegex(FetchError, message):
                blocking.redirect_request(req, None, 302, "Found", {}, target)
        self.assertEqual(blocking.redirect_request(req, None, 302, "Found", {}, "https://other.test/b").full_url,
                         "https://other.test/b")
        lenient = fetcher._Redirects(block_local=False)
        self.assertIsNotNone(lenient.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/"))

    def test_python_path_has_no_ftp_or_file_handler(self):
        f = Fetcher()
        try:
            for url in ("ftp://example.test/x", "file:///c:/windows/win.ini"):
                with self.assertRaisesRegex(FetchError, "unknown url type"):
                    f.get_python(url)
        finally:
            f.close()


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.f = Fetcher()
        self.calls = []

    def tearDown(self):
        self.f.close()

    def stub(self, python, windows):
        def make(name, outcome):
            def method(url, etag=None, last_modified=None, accept=None, block_local=False):
                self.calls.append(name)
                if callable(outcome):
                    return outcome(url, name)
                if isinstance(outcome, Exception):
                    raise outcome
                return ok(name)
            return method
        self.f.get_python = make("python", python)
        self.f.windows.get = make("windows", windows)
        self.f.windows._exe = "powershell.exe"

    def test_network_failure_falls_back_and_sticks(self):
        self.stub(FetchError("no route", network=True), None)
        self.assertEqual(self.f.get("https://x.test/").via, "windows")
        self.assertEqual(self.f.get("https://x.test/").via, "windows")
        self.assertEqual(self.calls, ["python", "windows", "windows"])

    def test_http_errors_do_not_fall_back(self):
        self.stub(FetchError("HTTP 404", 404), None)
        with self.assertRaises(FetchError):
            self.f.get("https://x.test/")
        self.assertEqual(self.calls, ["python"])

    def test_forbidden_retries_via_windows_and_remembers_only_that_site(self):
        def python(url, name):
            if "cisa" in url:
                raise FetchError("HTTP 403: the site refused access", 403)
            return ok(name)
        self.stub(python, None)
        self.assertEqual(self.f.get("https://www.cisa.gov/all.xml").via, "windows")
        self.assertEqual(self.f.get("https://www.cisa.gov/all.xml").via, "windows")
        self.assertEqual(self.f.get("https://other.test/feed").via, "python")
        self.assertEqual(self.calls, ["python", "windows", "windows", "python"])

    def test_both_fail_reports_first_error(self):
        self.stub(FetchError("python says no", network=True), FetchError("windows says no", network=True))
        with self.assertRaisesRegex(FetchError, "python says no"):
            self.f.get("https://x.test/")

    def test_forced_modes(self):
        self.stub(None, None)
        self.f._mode = lambda: "windows"
        self.f.get("https://x.test/")
        self.f._mode = lambda: "python"
        self.f.get("https://x.test/")
        self.assertEqual(self.calls, ["windows", "python"])


@unittest.skipUnless(os.environ.get("RSS_LIVE_TESTS") == "1", "set RSS_LIVE_TESTS=1 to hit the network")
class LiveTest(unittest.TestCase):
    def test_both_methods_fetch_a_real_feed(self):
        f = Fetcher()
        try:
            report = f.diagnose()
        finally:
            f.close()
        self.assertTrue(all(r["ok"] for r in report["results"]), report)

    def test_conditional_get_via_windows(self):
        f = Fetcher(lambda: "windows")
        try:
            first = f.get(fetcher.TEST_URL)
            second = f.get(fetcher.TEST_URL, first.etag, first.last_modified)
        finally:
            f.close()
        self.assertEqual(first.status, 200)
        self.assertIn(second.status, (200, 304))


if __name__ == "__main__":
    unittest.main()
