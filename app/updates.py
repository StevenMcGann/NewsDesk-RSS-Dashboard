"""Daily release notifications; installation requires an explicit user request."""
import json
import logging
import threading
import time
from pathlib import Path

import config
import feeds
from fetcher import FetchError
from update_package import parse_version, release_metadata

log = logging.getLogger(__name__)

API_URL = f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{config.GITHUB_REPO}/releases"
CHECK_EVERY = 24 * 3600
FIRST_CHECK_DELAY = 30  # let the first feed refresh go first


class UpdateChecker:
    def __init__(self, fetcher, settings, data_dir: Path):
        self.fetcher = fetcher
        self.settings = settings
        self.path = data_dir / "update.json"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.manager = None
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}

    def status(self) -> dict:
        with self._lock:
            state = dict(self.state)
        latest = state.get("latest")
        if not isinstance(latest, dict):
            latest = None
        newer = parse_version(latest.get("version")) if latest else None
        candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
        result = {
            "current": config.VERSION,
            "latest": latest,
            "available": bool(newer and newer > parse_version(config.VERSION)),
            "checked_at": state.get("checked_at"),
            "error": state.get("error"),
            "releases_url": RELEASES_URL,
            "candidate": candidate.get("identity"),
            "download_size": candidate["archive"].get("size") if isinstance(candidate.get("archive"), dict) else None,
            "manual_reason": state.get("manual_reason"),
        }
        if self.manager:
            result.update(self.manager.status())
        else:
            result.update(can_update=False, job=None)
        return result

    def notes(self):
        with self._lock:
            candidate = self.state.get("candidate")
            return {"latest": self.state.get("latest"), "notes": self.state.get("notes") or "No release notes were supplied.",
                    "candidate": candidate.get("identity") if isinstance(candidate, dict) else None}

    def candidate(self):
        with self._lock:
            value = self.state.get("candidate")
            return json.loads(json.dumps(value)) if isinstance(value, dict) else None

    def check(self) -> dict:
        state = {"checked_at": time.time(), "latest": None, "error": None}
        try:
            r = self.fetcher.get(API_URL, accept="application/vnd.github+json")
            data = json.loads(r.body)
            latest, candidate, notes, reason = release_metadata(data)
            url = str(data.get("html_url") or "")
            if not url.startswith(RELEASES_URL + "/"):
                url = RELEASES_URL
            latest["name"] = feeds.text(latest["name"])[:200]
            latest["url"] = url
            state.update(latest=latest, candidate=candidate, notes=notes, manual_reason=reason)
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
