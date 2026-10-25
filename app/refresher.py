"""Background feed refreshing and adding new subscriptions."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import feeds
from fetcher import ACCEPT_FEED, FetchError, check_url

log = logging.getLogger(__name__)

ALL = "all"


class Refresher:
    def __init__(self, store, fetcher, settings, workers=8):
        self.store = store
        self.fetcher = fetcher
        self.settings = settings
        self.workers = workers
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = None  # None, ALL, or a set of feed ids
        self._last_full = None
        self.status = {"running": False, "done": 0, "total": 0, "errors": 0, "last_run": None}

    def start(self):
        threading.Thread(target=self._loop, name="refresher", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def pause(self, timeout):
        with self._lock:
            self._paused.set()
        self._wake.set()
        return self._idle.wait(timeout)

    def resume(self):
        self._paused.clear()
        self._wake.set()

    def request(self, feed_ids=None):
        """Queue a refresh of some feeds, or all of them (resets the timer)."""
        with self._lock:
            if not feed_ids or self._pending == ALL:
                self._pending = ALL
            else:
                self._pending = (self._pending or set()) | {int(i) for i in feed_ids}
            self.status["running"] = True  # so a poll right now doesn't report "finished"
        self._wake.set()

    def poke(self):
        """Re-read the interval setting."""
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.status)

    def _loop(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self._wake.wait(1)
                self._wake.clear()
                continue
            due = (self._last_full or 0) + self.settings.get("refresh_minutes") * 60
            self._wake.wait(max(0.0, due - time.time()))
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                pending, self._pending = self._pending, None
            if pending is None and time.time() < due:
                continue
            try:
                self.run(None if pending in (None, ALL) else sorted(pending))
            except Exception:
                log.exception("Refresh failed")

    def run(self, feed_ids=None):
        with self._lock:
            if self._paused.is_set() or self._stop.is_set():
                return
            self._idle.clear()
        try:
            targets = self.store.feeds_for_fetch(feed_ids)
            with self._lock:
                self.status.update(running=True, done=0, total=len(targets), errors=0)
            with ThreadPoolExecutor(self.workers, thread_name_prefix="fetch") as pool:
                for ok in pool.map(self._refresh_one, targets):
                    with self._lock:
                        self.status["done"] += 1
                        self.status["errors"] += 0 if ok else 1
            if feed_ids is None and not self._paused.is_set() and not self._stop.is_set():
                removed = self.store.prune(self.settings.get("retention_days"))
                if removed:
                    log.info("Pruned %d old articles", removed)
                self._last_full = time.time()
        finally:
            with self._lock:
                self.status.update(running=False, last_run=time.time())
                self._idle.set()

    def _refresh_one(self, feed) -> bool:
        if self._paused.is_set() or self._stop.is_set():
            return True
        try:
            r = self.fetcher.get(feed["url"], feed["etag"], feed["last_modified"])
            if self._paused.is_set() or self._stop.is_set():
                return True
            if r.status == 304:
                self.store.record_not_modified(feed["id"])
                return True
            meta, entries = feeds.parse(r.body, r.url, r.content_type)
            self.store.record_fetch(feed["id"], meta, entries, r.etag, r.last_modified)
            return True
        except (FetchError, feeds.FeedError) as e:
            self.store.record_error(feed["id"], str(e))
        except Exception as e:
            log.exception("Refreshing %s failed", feed["url"])
            self.store.record_error(feed["id"], f"Unexpected error: {e}")
        return False

    def subscribe(self, address: str, folder_id=None, title=None) -> dict:
        """Find the feed behind a site or feed address, add it and store its articles."""
        address = address.strip()
        if "://" not in address:
            address = "https://" + address
        url = check_url(address)
        if self.store.feed_by_url(url):
            raise ValueError("You already follow this feed")
        r = self.fetcher.get(url, accept=ACCEPT_FEED)
        try:
            meta, entries = feeds.parse(r.body, r.url, r.content_type)
        except feeds.FeedError:
            url, r, meta, entries = self._discover(r)
        if self.store.feed_by_url(url):
            raise ValueError("You already follow this feed")
        feed_id = self.store.add_feed(url, title or meta["title"], folder_id, meta["site_url"], meta["icon_url"])
        new = self.store.record_fetch(feed_id, meta, entries, r.etag, r.last_modified)
        return {"id": feed_id, "title": title or meta["title"], "url": url, "articles": new}

    def _discover(self, page):
        if "html" not in page.content_type.lower() and b"<html" not in page.body[:4096].lower():
            raise feeds.FeedError("That address isn't a feed or a web page")
        for candidate in feeds.discover(page.body, page.url)[:8]:
            try:
                r = self.fetcher.get(candidate, accept=ACCEPT_FEED)
                meta, entries = feeds.parse(r.body, r.url, r.content_type)
                return candidate, r, meta, entries
            except (FetchError, feeds.FeedError):
                continue
        raise feeds.FeedError("Couldn't find a feed on that page. Try the site's RSS link instead.")
