import gzip
import os
import threading
import time
import unittest
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

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
        def record(*args, **kwargs):
            calls.append(args)
            return ok("python")
        f.get_python = f.windows.get = f.get_winhttp = record
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


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _Trickle(_Quiet):
    """Sends a byte every quarter second, never finishing within any sane time."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            for _ in range(400):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.25)
        except OSError:
            pass


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TransportTest(unittest.TestCase):
    """Real HTTP against local servers (security review findings 5 and 7)."""

    def methods(self, f):
        yield "python", f.get_python
        if f.windows.available:
            yield "windows", f.windows.get
        if f.winhttp.available:
            yield "winhttp", f.get_winhttp

    def test_total_deadline_stops_a_trickling_response(self):
        srv = _serve(_Trickle)
        url = f"http://127.0.0.1:{srv.server_address[1]}/feed"
        try:
            with mock.patch.object(fetcher, "TOTAL_TIMEOUT", 1.5):
                f = Fetcher()
                try:
                    for name, get in self.methods(f):
                        started = time.monotonic()
                        with self.assertRaisesRegex(FetchError, "too long") as caught:
                            get(url)
                        self.assertFalse(caught.exception.network, name)  # no retry the other way
                        self.assertLess(time.monotonic() - started, 8, name)
                finally:
                    f.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_article_redirect_to_blocked_address_is_never_requested(self):
        hits = {"blocked": 0}

        class Blocked(_Quiet):
            def do_GET(self):
                hits["blocked"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"secret")

        blocked = _serve(Blocked)
        blocked_port = blocked.server_address[1]

        class Redirect(_Quiet):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{blocked_port}/admin")
                self.send_header("Content-Length", "0")
                self.end_headers()

        start = _serve(Redirect)
        url = f"http://127.0.0.1:{start.server_address[1]}/article"
        # Treat only the second server as "this computer" so the first one can be reached.
        fake_local = lambda u: urllib.parse.urlsplit(u).port == blocked_port  # noqa: E731
        try:
            with mock.patch.object(fetcher, "is_local_address", fake_local):
                f = Fetcher()
                try:
                    for name, get in self.methods(f):
                        with self.assertRaisesRegex(FetchError, "this computer"):
                            get(url, accept=fetcher.ACCEPT_PAGE, block_local=True)
                        self.assertEqual(hits["blocked"], 0, name)
                    # Feeds the user adds may still redirect anywhere.
                    self.assertEqual(f.get_python(url).body, b"secret")
                finally:
                    f.close()
        finally:
            for srv in (start, blocked):
                srv.shutdown()
                srv.server_close()

    def test_every_request_says_connection_close(self):
        # With keep-alive, .NET sent no Connection header after its first request to a site,
        # and cisa.gov's Akamai refuses exactly those (issue #2). An HTTP/1.1 server is what
        # makes .NET drop the header.
        seen = []

        class Recorder(_Quiet):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                seen.append(self.headers.get("Connection"))
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"<rss/>")

        srv = _serve(Recorder)
        url = f"http://127.0.0.1:{srv.server_address[1]}/feed"
        try:
            f = Fetcher()
            try:
                for name, get in self.methods(f):
                    seen.clear()
                    for _ in range(3):  # the same worker each time, as within one refresh
                        get(url)
                    self.assertEqual([(c or "").lower() for c in seen], ["close"] * 3, name)
            finally:
                f.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_refusal_carries_the_sites_reference(self):
        class Refuse(_Quiet):
            def do_GET(self):
                self.send_response(403)
                self.send_header("X-Reference-Error", "18.d15ec817.1792870186.57d3bca5")
                self.send_header("Content-Length", "13")
                self.end_headers()
                self.wfile.write(b"Access Denied")

        srv = _serve(Refuse)
        url = f"http://127.0.0.1:{srv.server_address[1]}/feed"
        try:
            f = Fetcher()
            try:
                for name, get in self.methods(f):
                    with self.assertRaises(FetchError) as caught:
                        get(url)
                    self.assertEqual(caught.exception.status, 403, name)
                    self.assertEqual(str(caught.exception),
                                     "HTTP 403: the site refused access (reference 18.d15ec817.1792870186.57d3bca5)", name)
            finally:
                f.close()
        finally:
            srv.shutdown()
            srv.server_close()


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.f = Fetcher()
        self.f.winhttp = mock.Mock(available=False)  # exercise .NET/Python when native is unavailable
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

    def test_network_failure_remembers_python_for_that_host(self):
        self.stub(None, FetchError("no route", network=True))
        self.assertEqual(self.f.get("https://x.test/").via, "python")
        self.assertEqual(self.f.get("https://x.test/").via, "python")
        self.assertEqual(self.calls, ["windows", "python", "python"])

    def test_http_errors_do_not_fall_back(self):
        self.stub(None, FetchError("HTTP 404", 404))
        with self.assertRaises(FetchError):
            self.f.get("https://x.test/")
        self.assertEqual(self.calls, ["windows"])

    def test_forbidden_retries_via_python_and_remembers_only_that_site(self):
        def windows(url, name):
            if "cisa" in url:
                raise FetchError("HTTP 403: the site refused access", 403)
            return ok(name)
        self.stub(None, windows)
        self.assertEqual(self.f.get("https://www.cisa.gov/all.xml").via, "python")
        self.assertEqual(self.f.get("https://www.cisa.gov/all.xml").via, "python")
        self.assertEqual(self.f.get("https://other.test/feed").via, "windows")
        self.assertEqual(self.calls, ["windows", "python", "python", "windows"])

    def test_both_fail_reports_both_errors(self):
        self.stub(FetchError("python says no", network=True), FetchError("windows says no", network=True))
        with self.assertRaises(FetchError) as caught:
            self.f.get("https://x.test/")
        self.assertEqual(str(caught.exception), "Windows: windows says no. Python: python says no")
        self.assertTrue(caught.exception.network)
        self.assertIsNone(caught.exception.status)

    def test_same_refusal_from_both_is_one_message_with_the_latest_reference(self):
        self.stub(FetchError("HTTP 403: the site refused access", 403, reference="18.aaa"),
                  FetchError("HTTP 403: the site refused access", 403, reference="18.bbb"))
        with self.assertRaises(FetchError) as caught:
            self.f.get("https://www.cisa.gov/all.xml")
        self.assertEqual(str(caught.exception),
                         "HTTP 403: the site refused access, from both download methods (reference 18.aaa)")
        self.assertEqual(caught.exception.status, 403)  # still counts as a refusal (tools/check_feeds.py)
        self.assertFalse(caught.exception.network)

    def test_reference_must_look_like_an_id(self):
        for junk in ("<b>hi</b>", "a b", "x" * 81, "18.a\r\nInjected: yes"):
            e = FetchError("HTTP 403: the site refused access", 403, reference=junk)
            self.assertIsNone(e.reference, junk)
            self.assertEqual(str(e), "HTTP 403: the site refused access")

    def test_forced_modes(self):
        self.stub(None, None)
        self.f._mode = lambda: "windows"
        self.f.get("https://x.test/")
        self.f._mode = lambda: "python"
        self.f.get("https://x.test/")
        self.assertEqual(self.calls, ["windows", "python"])

    def native(self, outcome=None):
        self.f.winhttp = mock.Mock(available=True)

        def get(*args, **kwargs):
            self.calls.append("winhttp")
            if isinstance(outcome, Exception):
                raise outcome
            return ok("winhttp")
        self.f.get_winhttp = get

    def test_native_is_first_in_automatic_mode(self):
        self.stub(FetchError("refused", 403), FetchError("refused", 403))
        self.native()
        self.assertEqual(self.f.get("https://cisa.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["winhttp"])
        self.calls.clear()
        self.assertEqual(self.f.get("https://cisa.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["winhttp"])
        self.calls.clear()
        self.f.get("https://other.test/feed")
        self.assertEqual(self.calls, ["winhttp"])

    def test_proxy_fallback_is_remembered_per_host(self):
        self.stub(None, None)
        self.native(FetchError("The proxy asked for a login", 407, network=True))
        self.assertEqual(self.f.get("https://cisa.test/feed").via, "windows")
        self.assertEqual(self.f.get("https://cisa.test/feed").via, "windows")
        self.assertEqual(self.f.get("https://other.test/feed").via, "windows")
        self.assertEqual(self.calls, ["winhttp", "windows", "windows", "winhttp", "windows"])

    def test_python_remains_the_third_fallback(self):
        self.stub(None, FetchError("refused", 403))
        self.native(FetchError("refused", 403))
        self.assertEqual(self.f.get("https://x.test/feed").via, "python")
        self.assertEqual(self.calls, ["winhttp", "windows", "python"])
        self.calls.clear()
        self.assertEqual(self.f.get("https://x.test/feed").via, "python")
        self.assertEqual(self.calls, ["python"])

    def test_a_failed_learned_client_can_recover_through_native(self):
        self.stub(None, None)
        self.native(FetchError("refused", 403))
        self.assertEqual(self.f.get("https://x.test/feed").via, "windows")
        self.calls.clear()
        self.stub(None, FetchError("refused", 403))
        self.native()
        self.assertEqual(self.f.get("https://x.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["windows", "winhttp"])
        self.calls.clear()
        self.assertEqual(self.f.get("https://x.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["winhttp"])

    def test_portable_python_fallback_when_windows_clients_are_unavailable(self):
        self.stub(None, None)
        self.f.windows._exe = None
        self.assertEqual(self.f.get("https://x.test/feed").via, "python")
        self.assertEqual(self.calls, ["python"])

    def test_native_is_used_when_powershell_is_unavailable(self):
        self.stub(FetchError("refused", 403), None)
        self.f.windows._exe = None
        self.native()
        self.assertEqual(self.f.get("https://cisa.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["winhttp"])

    def test_all_three_refusals_keep_status_and_latest_reference(self):
        self.stub(FetchError("refused", 403, reference="18.a"), FetchError("refused", 403, reference="18.b"))
        self.native(FetchError("refused", 403, reference="18.c"))
        with self.assertRaises(FetchError) as caught:
            self.f.get("https://cisa.test/feed")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.reference, "18.a")
        self.assertIn("all 3 download methods", str(caught.exception))

    def test_rate_limit_and_size_failure_do_not_try_other_clients(self):
        for error in (FetchError("rate limited", 429), FetchError("The response is too large")):
            self.calls.clear()
            self.stub(None, None)
            self.native(error)
            with self.assertRaises(FetchError):
                self.f.get("https://x.test/feed")
            self.assertEqual(self.calls, ["winhttp"])

    def test_forced_native_mode_does_not_try_original_clients(self):
        self.stub(None, None)
        self.native()
        self.f._mode = lambda: "winhttp"
        self.assertEqual(self.f.get("https://x.test/feed").via, "winhttp")
        self.assertEqual(self.calls, ["winhttp"])

    def test_native_redirects_are_bounded_and_keep_one_total_deadline(self):
        self.f.winhttp = mock.Mock(available=True)
        self.f.winhttp.request.side_effect = [
            (302, b"", {"Location": "/final"}), (200, b"<rss/>", {})]
        r = self.f.get_winhttp("https://public.test/start")
        self.assertEqual(r.url, "https://public.test/final")
        requests = self.f.winhttp.request.call_args_list
        self.assertEqual(requests[0].kwargs["deadline"], requests[1].kwargs["deadline"])
        self.f.winhttp.request.side_effect = None
        self.f.winhttp.request.return_value = (302, b"", {"Location": "/loop"})
        self.f.winhttp.request.reset_mock()
        with self.assertRaisesRegex(FetchError, "too many times"):
            self.f.get_winhttp("https://public.test/loop")
        self.assertEqual(self.f.winhttp.request.call_count, fetcher.MAX_REDIRECTS + 1)

    def test_native_redirect_rejects_local_and_non_http_before_request(self):
        for target in ("http://127.0.0.1/private", "file:///c:/windows/win.ini"):
            self.f.winhttp = mock.Mock(available=True)
            self.f.winhttp.request.return_value = (302, b"", {"Location": target})
            with mock.patch.object(fetcher, "is_local_address", lambda u: "127.0.0.1" in u):
                with self.assertRaises(FetchError):
                    self.f.get_winhttp("https://public.test/article", block_local=True)
            self.assertEqual(self.f.winhttp.request.call_count, 1)


@unittest.skipUnless(os.name == "nt", "WinHTTP requires Windows")
class NativeTransportTest(unittest.TestCase):
    def test_conditional_headers_and_not_modified(self):
        seen = []

        class Conditional(_Quiet):
            def do_GET(self):
                seen.append((self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")))
                if self.headers.get("If-None-Match") == '"test-etag"':
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("ETag", '"test-etag"')
                self.send_header("Last-Modified", "Wed, 21 Oct 2026 12:00:00 GMT")
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"<rss/>")

        srv = _serve(Conditional)
        f = Fetcher()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/feed"
            first = f.get_winhttp(url)
            second = f.get_winhttp(url, first.etag, first.last_modified)
            self.assertEqual(first.body, b"<rss/>")
            self.assertEqual(first.content_type, "application/rss+xml")
            self.assertEqual((second.status, second.body), (304, b""))
            self.assertEqual(seen, [(None, None), ('"test-etag"', "Wed, 21 Oct 2026 12:00:00 GMT")])
        finally:
            f.close()
            srv.shutdown()
            srv.server_close()

    def test_body_caps_with_length_without_length_and_after_decompression(self):
        class Body(_Quiet):
            def do_GET(self):
                payload = b"a" * 1000
                if self.path == "/gzip":
                    payload = gzip.compress(payload)
                self.send_response(200)
                if self.path != "/no-length":
                    self.send_header("Content-Length", str(len(payload)))
                if self.path == "/gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(payload)

        srv = _serve(Body)
        f = Fetcher()
        try:
            with mock.patch.object(fetcher, "MAX_BYTES", 100):
                for path in ("/length", "/no-length", "/gzip"):
                    with self.assertRaisesRegex(FetchError, "too large") as caught:
                        f.get_winhttp(f"http://127.0.0.1:{srv.server_port}{path}")
                    self.assertFalse(caught.exception.network)
        finally:
            f.close()
            srv.shutdown()
            srv.server_close()

    def test_native_does_not_authenticate_an_origin_or_accept_header_injection(self):
        seen = []

        class Auth(_Quiet):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Negotiate")
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = _serve(Auth)
        f = Fetcher()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/feed"
            with self.assertRaises(FetchError) as caught:
                f.get_winhttp(url)
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(seen, [None])
            with self.assertRaisesRegex(FetchError, "Invalid HTTP header"):
                f.get_winhttp(url, etag="bad\r\nInjected: yes")
            self.assertEqual(seen, [None])
        finally:
            f.close()
            srv.shutdown()
            srv.server_close()


@unittest.skipUnless(os.environ.get("RSS_LIVE_TESTS") == "1", "set RSS_LIVE_TESTS=1 to hit the network")
class LiveTest(unittest.TestCase):
    def test_all_methods_fetch_a_real_feed(self):
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
