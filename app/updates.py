"""Check GitHub for a newer NewsDesk release, at most once a day.

It only informs: nothing is downloaded or run. The UI shows a note linking to the
release page, where the user gets the new zip and runs Install NewsDesk again.
"""
import json
import logging
import re
import threading
import time
from pathlib import Path

import config
import feeds
from fetcher import FetchError

log = logging.getLogger(__name__)

API_URL = f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{config.GITHUB_REPO}/releases"
CHECK_EVERY = 24 * 3600
FIRST_CHECK_DELAY = 30  # let the first feed refresh go first


def parse_version(text):
    """"v1.2.3" or "1.2" -> (1, 2, 3) / (1, 2, 0); None if it isn't a plain version."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", str(text or "").strip())
    return tuple(int(part or 0) for part in m.groups()) if m else None


class UpdateChecker:
    def __init__(self, fetcher, settings, data_dir: Path):
        self.fetcher = fetcher
        self.settings = settings
        self.path = data_dir / "update.json"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def status(self) -> dict:
        with self._lock:
            state = dict(self.state)
        latest = state.get("latest")
        newer = parse_version(latest["version"]) if latest else None
        return {
            "current": config.VERSION,
            "latest": latest,
            "available": bool(newer and newer > parse_version(config.VERSION)),
            "checked_at": state.get("checked_at"),
            "error": state.get("error"),
            "releases_url": RELEASES_URL,
        }

    def check(self) -> dict:
        state = {"checked_at": time.time(), "latest": None, "error": None}
        try:
            r = self.fetcher.get(API_URL, accept="application/vnd.github+json")
            data = json.loads(r.body)
            version = parse_version(data.get("tag_name"))
            if not version:
                raise ValueError(f"unrecognized version {data.get('tag_name')!r}")
            url = str(data.get("html_url") or "")
            if not url.startswith(RELEASES_URL + "/"):
                url = RELEASES_URL
            state["latest"] = {
                "version": ".".join(map(str, version)),
                "name": feeds.text(data.get("name"))[:200],
                "url": url,
                "published": str(data.get("published_at") or "")[:40],
            }
        except FetchError as e:
            if e.status != 404:  # 404 just means no release has been published yet
                state["error"] = str(e)
        except (ValueError, AttributeError) as e:
            state["error"] = f"GitHub sent an unexpected reply ({e})"
        with self._lock:
            self.state = state
        try:
            self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError:
            log.exception("Could not save update status")
        if state["error"]:
            log.info("Update check failed: %s", state["error"])
        return self.status()

    def due(self) -> bool:
        if not self.settings.get("check_updates"):
            return False
        with self._lock:
            last = self.state.get("checked_at") or 0
        return time.time() - last >= CHECK_EVERY

    def start(self):
        threading.Thread(target=self._loop, name="updates", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self._stop.wait(FIRST_CHECK_DELAY):
            return
        while True:
            if self.due():
                try:
                    self.check()
                except Exception:
                    log.exception("Update check crashed")
            if self._stop.wait(3600):
                return
