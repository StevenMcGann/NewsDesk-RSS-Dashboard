"""Local web server: the JSON API plus the static UI, on 127.0.0.1 only.

Every API call must carry the per-launch token that is written into index.html, and
requests with a foreign Host or Origin header are refused. Together these stop other
web pages open in the browser from driving the API (CSRF and DNS rebinding).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import config
import export_md
import feeds
import opml
from fetcher import ACCEPT_PAGE, FetchError
from store import NotFound

log = logging.getLogger(__name__)

MAX_BODY = 20 * 1024 * 1024
MAX_ARTICLE = 3 * 1024 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src * data: blob:; "
       "media-src * blob:; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".json": "application/json"}

ROUTES = [(method, re.compile(f"^{pattern}$"), name) for method, pattern, name in [
    ("GET", r"/api/ping", "ping"),
    ("GET", r"/api/state", "state"),
    ("GET", r"/api/items", "items"),
    ("GET", r"/api/items/(\d+)", "item"),
    ("PATCH", r"/api/items/(\d+)", "item_update"),
    ("POST", r"/api/items/mark-read", "mark_read"),
    ("POST", r"/api/feeds", "feed_add"),
    ("PATCH", r"/api/feeds/(\d+)", "feed_update"),
    ("DELETE", r"/api/feeds/(\d+)", "feed_delete"),
    ("POST", r"/api/folders", "folder_add"),
    ("PATCH", r"/api/folders/(\d+)", "folder_update"),
    ("DELETE", r"/api/folders/(\d+)", "folder_delete"),
    ("POST", r"/api/tree/move", "tree_move"),
    ("POST", r"/api/tags", "tag_add"),
    ("PATCH", r"/api/tags/(\d+)", "tag_update"),
    ("DELETE", r"/api/tags/(\d+)", "tag_delete"),
    ("POST", r"/api/refresh", "refresh"),
    ("GET", r"/api/article/(\d+)", "article"),
    ("PUT", r"/api/article/(\d+)", "article_store"),
    ("POST", r"/api/save-markdown", "save_markdown"),
    ("POST", r"/api/open-folder", "open_folder"),
    ("POST", r"/api/opml/import", "opml_import"),
    ("GET", r"/api/opml/export", "opml_export"),
    ("GET", r"/api/settings", "settings_get"),
    ("PATCH", r"/api/settings", "settings_update"),
    ("POST", r"/api/diagnose", "diagnose"),
    ("POST", r"/api/update/check", "update_check"),
    ("POST", r"/api/heartbeat", "heartbeat"),
    ("POST", r"/api/bye", "bye"),
]]
PUBLIC = {"ping", "bye"}  # bye comes from navigator.sendBeacon, which carries the token in the query


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Raw:
    """A non-JSON API response (file download)."""

    def __init__(self, body: bytes, content_type: str, filename: str):
        self.body, self.content_type, self.filename = body, content_type, filename


class App:
    """The services request handlers use, plus window-liveness tracking."""

    def __init__(self, store, settings, fetcher, refresher, data_dir: Path, updates=None):
        self.store = store
        self.settings = settings
        self.fetcher = fetcher
        self.refresher = refresher
        self.data_dir = data_dir
        self.updates = updates
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.started = time.monotonic()
        self.last_heartbeat = None
        self.bye_at = None

    def heartbeat(self):
        self.last_heartbeat = time.monotonic()
        self.bye_at = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows, SO_REUSEADDR would let two servers share a port

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's slow reverse-DNS lookup
        self.server_name, self.server_port = self.server_address[:2]


def static_files() -> dict:
    """Every servable UI file, keyed by its URL path ("vendor/purify.min.js")."""
    root = config.STATIC_DIR.resolve()
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}


def create_server(app: App, port: int) -> Server:
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError:
        srv = Server(("127.0.0.1", 0), Handler)
    srv.app = app
    # Requests are looked up in this fixed list and never turned into file-system paths:
    # on Windows a decoded "\\host\share" path would make Windows connect to that host
    # with the user's credentials (NTLM).
    srv.static_files = static_files()
    # Browsers cache icons by URL, so tag the icon links with a hash of the icon files.
    icons = b"".join(srv.static_files[name].read_bytes() for name in ("favicon.svg", "icon-256.png")
                     if name in srv.static_files)
    srv.icon_version = hashlib.sha256(icons).hexdigest()[:10]
    app.port = srv.server_address[1]
    return srv


def same_secret(given: str, expected: str) -> bool:
    # compare_digest refuses non-ASCII str, so compare bytes.
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"), expected.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NewsDesk"
    sys_version = ""
    timeout = 60  # drop connections that stall mid-request

    def log_message(self, fmt, *args):  # the default writes to stderr, which is None under pythonw
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch()

    do_POST = do_PATCH = do_PUT = do_DELETE = do_GET

    @property
    def app(self) -> App:
        return self.server.app

    # ---- plumbing ---------------------------------------------------------------

    def _dispatch(self):
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"error": "Bad request"})
            if length > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "That upload is too large"})
            self.body = self.rfile.read(length) if length > 0 else b""
            if not self._origin_ok():
                return self._json(403, {"error": "Forbidden"})
            url = urlsplit(self.path)
            self.query = parse_qs(url.query)
            if url.path.startswith("/api/"):
                return self._api(url.path)
            if self.command != "GET":
                return self._json(405, {"error": "Method not allowed"})
            return self._static(url.path)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _origin_ok(self) -> bool:
        port = self.app.port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _api(self, path):
        for method, pattern, name in ROUTES:
            m = pattern.match(path)
            if m and method == self.command:
                break
        else:
            return self._json(404, {"error": "Not found"})
        if name not in PUBLIC and not same_secret(self.headers.get("X-Token", ""), self.app.token):
            return self._json(403, {"error": "Forbidden"})
        try:
            result = getattr(self, "api_" + name)(*(int(g) for g in m.groups()))
        except ApiError as e:
            return self._json(e.status, {"error": str(e)})
        except NotFound as e:
            return self._json(404, {"error": str(e)})
        except (ValueError, FetchError, feeds.FeedError) as e:
            return self._json(400, {"error": str(e)})
        except Exception:
            log.exception("%s %s failed", method, name)  # route constants, never request text, in the log
            return self._json(500, {"error": "Something went wrong. Details are in app.log."})
        if isinstance(result, Raw):
            disposition = f'attachment; filename="{result.filename}"'
            return self._send(200, result.body, result.content_type, {"Content-Disposition": disposition})
        return self._json(200, result if result is not None else {"ok": True})

    def _static(self, path):
        name = unquote(path)[1:]
        if name in ("", "index.html"):
            page = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
            page = page.replace("__API_TOKEN__", self.app.token).replace("__ICON_VERSION__", self.server.icon_version)
            return self._send(200, page.encode("utf-8"),
                              CONTENT_TYPES[".html"])
        target = self.server.static_files.get(name)
        if target is None:
            return self._json(404, {"error": "Not found"})
        kind = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return self._send(200, target.read_bytes(), kind, {"Cache-Control": "no-cache"})

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status, body: bytes, content_type: str, extra=None):
        headers = {"Content-Type": content_type, "Content-Length": str(len(body)),
                   "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                   "Content-Security-Policy": CSP, "Cache-Control": "no-store"}
        headers.update(extra or {})
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def data(self) -> dict:
        if not self.body:
            return {}
        try:
            value = json.loads(self.body)
        except ValueError:
            raise ApiError(400, "Bad request") from None
        if not isinstance(value, dict):
            raise ApiError(400, "Bad request")
        return value

    def arg(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    # ---- endpoints ----------------------------------------------------------------

    def api_ping(self):
        return {"app": "newsdesk", "version": config.VERSION}

    def api_state(self):
        a = self.app
        state = a.store.state()
        state["settings"] = a.settings.all()
        state["refresh"] = a.refresher.snapshot()
        state["app"] = {
            "version": config.VERSION,
            "data_dir": str(a.data_dir),
            "save_folder": str(a.settings.save_folder()),
            "default_save_folder": str(config.default_save_folder()),
            "starter_available": config.STARTER_OPML.is_file(),
            "default_template": config.DEFAULT_TEMPLATE,
        }
        state["update"] = a.updates.status() if a.updates else None
        return state

    def api_items(self):
        filters = {
            "view": self.arg("view", "all"),
            "status": self.arg("status", "all"),
            "tags": [t for t in (self.arg("tags") or "").split(",") if t.isdigit()],
            "q": self.arg("q", ""),
            "since": self.arg("since"),
            "starred": self.arg("starred") == "1",
        }
        return self.app.store.list_items(filters, self.arg("sort", "new"), self.arg("page", 1), self.arg("size", 20))

    def api_item(self, item_id):
        return self.app.store.item(item_id)

    def api_item_update(self, item_id):
        d = self.data()
        self.app.store.update_item(item_id, read=d.get("read"), starred=d.get("starred"), tags=d.get("tags"))

    def api_mark_read(self):
        d = self.data()
        changed = self.app.store.mark_read(ids=d.get("ids"), filters=d.get("filter"), read=d.get("read", True))
        return {"changed": changed}

    def api_feed_add(self):
        d = self.data()
        if not str(d.get("url") or "").strip():
            raise ApiError(400, "Enter a website or feed address")
        return self.app.refresher.subscribe(d["url"], d.get("folder_id"), (d.get("title") or "").strip() or None)

    def api_feed_update(self, feed_id):
        d = self.data()
        changes = {k: d[k] for k in ("title", "url", "folder_id", "full_text") if k in d}
        self.app.store.update_feed(feed_id, **changes)
        if "url" in changes:
            self.app.refresher.request([feed_id])

    def api_feed_delete(self, feed_id):
        self.app.store.delete_feed(feed_id)

    def api_folder_add(self):
        d = self.data()
        return {"id": self.app.store.add_folder(d.get("name"), d.get("parent_id"))}

    def api_folder_update(self, folder_id):
        d = self.data()
        self.app.store.update_folder(folder_id, name=d.get("name"), collapsed=d.get("collapsed"))

    def api_folder_delete(self, folder_id):
        self.app.store.delete_folder(folder_id)

    def api_tree_move(self):
        d = self.data()
        if d.get("kind") not in ("feed", "folder"):
            raise ApiError(400, "Bad request")
        self.app.store.move(d["kind"], int(d["id"]), d.get("parent_id"), int(d.get("index", 0)))

    def api_tag_add(self):
        d = self.data()
        return {"id": self.app.store.add_tag(d.get("name"), d.get("color"))}

    def api_tag_update(self, tag_id):
        d = self.data()
        self.app.store.update_tag(tag_id, name=d.get("name"), color=d.get("color"))

    def api_tag_delete(self, tag_id):
        self.app.store.delete_tag(tag_id)

    def api_refresh(self):
        self.app.refresher.request(self.data().get("feed_ids"))
        return self.app.refresher.snapshot()

    def api_article(self, item_id):
        item = self.app.store.item(item_id)
        if item["content_full"]:
            return {"content_full": item["content_full"], "url": item["url"]}
        if not item["url"]:
            raise ApiError(400, "This article has no web link")
        r = self.app.fetcher.get(item["url"], accept=ACCEPT_PAGE, block_local=True)
        if "html" not in r.content_type.lower() and b"<html" not in r.body[:4096].lower():
            raise ApiError(400, f"That link isn't a web page ({r.content_type or 'unknown type'})")
        return {"html": feeds.decode_html(r.body, r.content_type), "url": r.url}

    def api_article_store(self, item_id):
        html = self.data().get("html") or ""
        if len(html) > MAX_ARTICLE:
            raise ApiError(413, "That article is too large to keep")
        self.app.store.set_full_content(item_id, html)

    def api_save_markdown(self):
        d = self.data()
        item = self.app.store.item(int(d.get("item_id") or 0))
        markdown = str(d.get("markdown") or "")
        tags = self.app.store.tag_names(item["tags"])
        folder = self.app.settings.save_folder()
        try:
            path = export_md.save(folder, item, markdown, tags, self.app.settings.get("md_template"))
        except OSError as e:
            raise ApiError(400, f"Couldn't save to {folder}: {e.strerror or e}") from None
        self.app.store.set_saved(item["id"], str(path))
        return {"path": str(path), "folder": str(folder)}

    def api_open_folder(self):
        d = self.data()
        if d.get("which") == "data":
            folder = self.app.data_dir
        else:
            folder = self.app.settings.save_folder()
            if d.get("item_id"):
                saved = self.app.store.item(int(d["item_id"]))["saved_path"]
                if saved and Path(saved).is_file():
                    subprocess.Popen(f'explorer /select,"{saved}"')
                    return
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def api_opml_import(self):
        if self.headers.get("Content-Type", "").startswith("application/json"):
            if not self.data().get("starter"):
                raise ApiError(400, "Bad request")
            raw = config.STARTER_OPML.read_bytes()
        else:
            raw = self.body
        tree = opml.parse(raw)
        if not opml.count_feeds(tree):
            raise ApiError(400, "That file doesn't contain any feeds")
        result = self.app.store.import_tree(tree)
        if result["feed_ids"]:
            self.app.refresher.request(result["feed_ids"])
        self.app.settings.update({"first_run_done": True})
        return result

    def api_opml_export(self):
        body = opml.build(self.app.store.export_tree())
        return Raw(body, "text/x-opml; charset=utf-8", f"newsdesk-{time.strftime('%Y-%m-%d')}.opml")

    def api_settings_get(self):
        return self.app.settings.all()

    def api_settings_update(self):
        d = self.data()
        values = self.app.settings.update(d)
        if "refresh_minutes" in d:
            self.app.refresher.poke()
        return dict(values, save_folder_resolved=str(self.app.settings.save_folder()))

    def api_diagnose(self):
        return self.app.fetcher.diagnose()

    def api_update_check(self):
        if not self.app.updates:
            raise ApiError(400, "Update checks aren't available")
        return self.app.updates.check()

    def api_heartbeat(self):
        self.app.heartbeat()
        return {"version": self.app.store.version, "refresh": self.app.refresher.snapshot(),
                "update": self.app.updates.status() if self.app.updates else None}

    def api_bye(self):
        if same_secret(self.arg("t", ""), self.app.token):
            self.app.bye_at = time.monotonic()
