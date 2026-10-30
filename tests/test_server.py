import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _path import sample

import config
import server
from fetcher import FetchError, Response
from lifecycle import WindowTracker
from refresher import Refresher
from server import App, create_server
from store import Store
from updates import UpdateChecker

PAGES = {
    "https://site.test/": (200, sample("page.html"), "text/html; charset=windows-1252"),
    "https://site.test/feed.xml": (200, sample("rss2.xml"), "application/rss+xml"),
    "https://news.example.com/a/1": (200, b"<html><body><article>Full</article></body></html>", "text/html"),
}


class FakeFetcher:
    calls = []

    def get(self, url, etag=None, last_modified=None, accept=None, block_local=False):
        FakeFetcher.calls.append({"url": url, "block_local": block_local})
        if url not in PAGES:
            raise FetchError("HTTP 404: the site was not found", 404)
        status, body, kind = PAGES[url]
        return Response(status, url, body, kind, None, None, "python")

    def diagnose(self):
        return {"results": []}

    def close(self):
        pass


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = config.Settings(data / "settings.json")
        cls.settings.update({"save_folder": str(data / "Saved")})
        cls.store = Store(data / "t.db")
        refresher = Refresher(cls.store, FakeFetcher(), cls.settings)
        cls.app = App(cls.store, cls.settings, FakeFetcher(), refresher, data)
        cls.srv = create_server(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Token"] = self.app.token
        h.update(headers or {})
        payload = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, payload, h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if raw:
            return resp, data
        return resp.status, (json.loads(data) if data else None)

    def test_token_host_and_origin_checks(self):
        self.assertEqual(self.call("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Token": "wrong"})[0], 403)
        self.assertEqual(self.call("GET", "/api/state", headers={"Host": f"evil.test:{self.port}"})[0], 403)
        self.assertEqual(self.call("POST", "/api/refresh", {}, headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.call("GET", "/api/ping", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)

    def test_index_carries_token_and_security_headers(self):
        self.app.token = "fixture__launch_token"  # URL-safe random tokens can contain __.
        resp, body = self.call("GET", "/", token=False, raw=True)
        self.assertIn(self.app.token.encode(), body)
        self.assertNotIn(b"__API_TOKEN__", body)
        self.assertNotIn(b"__ICON_VERSION__", body)
        self.assertIn(f'favicon.svg?v={self.srv.icon_version}"'.encode(), body)
        self.assertEqual(len(self.srv.icon_version), 10)
        self.assertEqual(self.call("GET", f"/favicon.svg?v={self.srv.icon_version}", token=False, raw=True)[0].status, 200)
        self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_static_traversal_blocked(self):
        for path in ("/../config.py", "/..%2fconfig.py", "/%2e%2e/launcher.py", "/vendor/../../store.py",
                     "/%5C..%5Cconfig.py", "/C:%5CWindows%5Cwin.ini", "/app.css%00", "/app.css::$DATA"):
            self.assertEqual(self.call("GET", path, token=False)[0], 404, path)
        resp, _ = self.call("GET", "/app.css", token=False, raw=True)
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/css"))
        self.assertEqual(self.call("GET", "/vendor/purify.min.js", token=False, raw=True)[0].status, 200)

    def test_static_never_touches_network_paths(self):
        # "\\host\share" paths make Windows connect out with the user's credentials (NTLM).
        # Record any UNC path the server hands to the file system instead of letting it through.
        touched = []

        def guard(real):
            def wrapper(path, *args, **kwargs):
                if str(path).startswith(("\\\\", "//")):
                    touched.append(str(path))
                    return str(path) if real is os.path.realpath else (_ for _ in ()).throw(FileNotFoundError(path))
                return real(path, *args, **kwargs)
            return wrapper

        with mock.patch("os.path.realpath", guard(os.path.realpath)), \
                mock.patch("os.stat", guard(os.stat)), mock.patch("os.lstat", guard(os.lstat)):
            for path in ("/%5C%5Cattacker.invalid%5Cshare%5Cx.png", "/%5C%5C%3F%5CUNC%5Cattacker.invalid%5Cs%5Cx",
                         "/%2F%2Fattacker.invalid%2Fshare%2Fx.png"):
                self.assertEqual(self.call("GET", path, token=False)[0], 404, path)
        self.assertEqual(touched, [])

    def test_non_ascii_token_is_refused_cleanly(self):
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Token": "caf\u00e9"})[0], 403)
        with mock.patch.object(self.app.windows, "bye") as bye:
            self.assertEqual(self.call("POST", "/api/bye?t=caf%C3%A9", {}, token=False)[0], 200)
            bye.assert_not_called()

    def test_window_ids_keep_other_windows_alive_after_close(self):
        previous = self.app.windows
        now = [1000]
        self.app.windows = WindowTracker(clock=lambda: now[0])
        try:
            self.assertEqual(self.call("POST", "/api/heartbeat", {})[0], 400)
            self.assertEqual(self.call("POST", "/api/heartbeat", {"window_id": "one"}, token=False)[0], 403)
            for identifier in ("one", "two"):
                self.assertEqual(self.call("POST", "/api/heartbeat", {"window_id": identifier})[0], 200)
            self.assertEqual(self.call("POST", f"/api/bye?t={self.app.token}&w=one", token=False)[0], 200)
            now[0] += 3600
            self.assertIsNone(self.app.windows.shutdown_reason())
            self.assertEqual(self.call("POST", "/api/bye?t=wrong&w=two", token=False)[0], 200)
            self.assertIsNone(self.app.windows.shutdown_reason())
            self.assertEqual(self.call("POST", f"/api/bye?t={self.app.token}&w=two", token=False)[0], 200)
            now[0] += 25
            self.assertEqual(self.app.windows.shutdown_reason(), "All registered windows closed")
        finally:
            self.app.windows = previous

    def test_quit_requires_the_launch_token_and_same_origin(self):
        with mock.patch.object(self.app.windows, "request_quit") as quit_app:
            self.assertEqual(self.call("POST", "/api/quit", {}, token=False)[0], 403)
            self.assertEqual(self.call("POST", "/api/quit", {}, headers={"Origin": "https://evil.test"})[0], 403)
            quit_app.assert_not_called()
            self.assertEqual(self.call("POST", "/api/quit", {})[0], 200)
            quit_app.assert_called_once_with()

    def test_update_routes_require_token_and_never_accept_paths(self):
        checker = UpdateChecker(FakeFetcher(), self.settings, Path(self.tmp.name))
        manager = mock.Mock()
        checker.manager = manager
        manager.status.return_value = {"can_update": True, "job": None}
        manager.start.return_value = {"id": "test", "phase": "checking"}
        manager.cancel.return_value = {"id": "test", "phase": "cancelled"}
        self.app.updates = checker
        try:
            for method, path in (("GET", "/api/update/status"), ("GET", "/api/update/notes"),
                                  ("POST", "/api/update/start"), ("POST", "/api/update/cancel")):
                self.assertEqual(self.call(method, path, {}, token=False)[0], 403)
                self.assertEqual(self.call(method, path, {}, headers={"Origin": "https://evil.test"})[0], 403)
            self.assertEqual(self.call("POST", "/api/update/start", {"candidate": "selected", "target": "C:/evil", "url": "https://evil.test"})[0], 200)
            manager.start.assert_called_once_with("selected")
            self.assertEqual(self.call("POST", "/api/update/cancel", {})[0], 200)
            self.assertEqual(self.call("GET", "/api/update/notes")[0], 200)
        finally:
            self.app.updates = None

    def test_update_maintenance_blocks_mutations_and_drains_calls(self):
        self.app.maintenance = True
        try:
            self.assertEqual(self.call("PATCH", "/api/settings", {"theme": "light"})[0], 503)
            self.assertEqual(self.call("GET", "/api/items")[0], 503)
            self.assertEqual(self.call("GET", "/api/ping", token=False)[0], 200)
            self.assertEqual(self.call("POST", "/api/heartbeat", {"window_id": "update-wait"})[0], 200)
            self.assertTrue(self.app.wait_idle(.1))
        finally:
            self.app.maintenance = False

    def test_bad_content_length_is_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/refresh")
        conn.putheader("Content-Length", "lots")
        conn.putheader("X-Token", self.app.token)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_idle_connections_time_out(self):
        with mock.patch.object(server.Handler, "timeout", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            sock.sendall(b"GET /api/ping HTTP/1.1\r\n")  # never finish the request
            started = time.monotonic()
            self.assertEqual(sock.recv(1024), b"")  # server hangs up
            self.assertLess(time.monotonic() - started, 4)
            sock.close()

    def test_article_download_blocks_this_computer(self):
        store = self.app.store
        feed_id = store.add_feed("https://article-test.example/feed", "Article test")
        store.record_fetch(feed_id, {}, [{
            "guid": "a1", "url": "https://news.example.com/a/1", "title": "Article test item", "author": None,
            "published": None, "summary": "", "content": "", "snippet": "", "image_url": None}])
        item_id = self.call("GET", f"/api/items?view=feed:{feed_id}")[1]["items"][0]["id"]
        FakeFetcher.calls.clear()
        self.assertEqual(self.call("GET", f"/api/article/{item_id}")[0], 200)
        self.assertEqual(FakeFetcher.calls, [{"url": "https://news.example.com/a/1", "block_local": True}])

    def test_subscribe_read_and_save_flow(self):
        status, feed = self.call("POST", "/api/feeds", {"url": "site.test/"})
        self.assertEqual(status, 200, feed)
        self.assertEqual((feed["url"], feed["articles"]), ("https://site.test/feed.xml", 4))
        self.assertEqual(self.call("POST", "/api/feeds", {"url": "https://site.test/feed.xml"})[0], 400)

        status, listing = self.call("GET", f"/api/items?view=feed:{feed['id']}&status=unread&size=2")
        self.assertEqual((listing["total"], len(listing["items"])), (4, 2))
        first = listing["items"][0]["id"]
        self.assertEqual(self.call("PATCH", f"/api/items/{first}", {"read": True, "tags": [1]})[0], 200)
        status, item = self.call("GET", f"/api/items/{first}")
        self.assertTrue(item["read"])
        self.assertEqual(item["tags"], [1])

        thumb = next(i for i in self.call("GET", "/api/items?q=Thumbnail")[1]["items"])
        status, article = self.call("GET", f"/api/article/{thumb['id']}")
        self.assertIn("<article>Full</article>", article["html"])
        self.assertEqual(self.call("PUT", f"/api/article/{thumb['id']}", {"html": "<p>Cached</p>"})[0], 200)
        self.assertEqual(self.call("GET", f"/api/article/{thumb['id']}")[1]["content_full"], "<p>Cached</p>")

        status, saved = self.call("POST", "/api/save-markdown", {"item_id": thumb["id"], "markdown": "Hello **md**"})
        self.assertEqual(status, 200, saved)
        text = Path(saved["path"]).read_text(encoding="utf-8")
        self.assertIn("Hello **md**", text)
        self.assertIn('source: "Example News & Views"', text)
        self.assertEqual(self.call("GET", "/api/items?view=saved")[1]["total"], 1)

        status, result = self.call("POST", "/api/items/mark-read", {"filter": {"view": f"feed:{feed['id']}"}})
        self.assertEqual(result["changed"], 3)

    def test_unknown_feed_address(self):
        status, body = self.call("POST", "/api/feeds", {"url": "https://missing.test/"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])

    def test_opml_export_then_import(self):
        self.call("POST", "/api/folders", {"name": "Imported"})
        resp, body = self.call("GET", "/api/opml/export", raw=True)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        status, result = self.call("POST", "/api/opml/import", body, headers={"Content-Type": "text/x-opml"})
        self.assertEqual(status, 400 if b"xmlUrl" not in body else 200)
        status, err = self.call("POST", "/api/opml/import", b"<html/>", headers={"Content-Type": "text/xml"})
        self.assertEqual(status, 400)

    def test_layout_sizes_persist_independently_and_reject_out_of_range(self):
        keys = ("cards_size", "list_size", "feed_size")
        before = {key: self.settings.get(key) for key in keys}
        try:
            for key in keys:
                self.assertEqual(self.call("PATCH", "/api/settings", {key: 2})[0], 200)
            for key, value in zip(keys, (0, 3, 4)):
                self.assertEqual(self.call("PATCH", "/api/settings", {key: value})[0], 200)
                for invalid in (-1, 5, "large"):
                    self.assertEqual(self.call("PATCH", "/api/settings", {key: invalid})[0], 400)
            reloaded = config.Settings(self.settings.path)
            self.assertEqual([reloaded.get(key) for key in keys], [0, 3, 4])
            self.assertEqual(self.call("GET", "/api/state")[1]["settings"]["cards_size"], 0)
        finally:
            self.settings.update(before)

    def test_settings_validation(self):
        self.assertEqual(self.call("PATCH", "/api/settings", {"page_size": 50})[1]["page_size"], 50)
        self.assertEqual(self.call("PATCH", "/api/settings", {"fetch_mode": "winhttp"})[1]["fetch_mode"], "winhttp")
        self.assertEqual(self.call("PATCH", "/api/settings", {"fetch_mode": "invalid"})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/settings", {"page_size": 0})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/settings", {"nope": 1})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/settings", {"theme": "neon"})[0], 400)

    def test_update_check_route(self):
        self.assertEqual(self.call("POST", "/api/update/check", {}, token=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/update/check", {})[0], 400)  # no checker attached
        tag = {"tag_name": "v99.1.0", "name": "x", "html_url": f"https://github.com/{config.GITHUB_REPO}/releases/tag/v99.1.0"}

        class ReleaseFetcher:
            def get(self, url, **kw):
                return Response(200, url, json.dumps(tag).encode(), "application/json", None, None, "python")

        self.app.updates = UpdateChecker(ReleaseFetcher(), self.settings, Path(self.tmp.name))
        try:
            status, body = self.call("POST", "/api/update/check", {})
            self.assertEqual((status, body["available"], body["latest"]["version"]), (200, True, "99.1.0"))
            self.assertTrue(self.call("POST", "/api/heartbeat", {"window_id": "test-updates"})[1]["update"]["available"])
            self.assertTrue(self.call("GET", "/api/state")[1]["update"]["available"])
        finally:
            self.app.updates = None

    def test_errors_are_json(self):
        self.assertEqual(self.call("GET", "/api/items/99999")[0], 404)
        self.assertEqual(self.call("POST", "/api/folders", b"{not json")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/state")[0], 404)


if __name__ == "__main__":
    unittest.main()
