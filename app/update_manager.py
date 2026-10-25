"""User-initiated preparation and handoff to a verified external update worker."""
import json
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
from update_download import ReleaseDownloader, UpdateCancelled
from update_package import (TERMINAL, cache_root, eligibility, extract_package, parse_version,
                            proof, read_job, verify_checksum, verify_file, write_record)

log = logging.getLogger(__name__)
ACTIVE = {"checking", "downloading", "verifying", "staging", "preparing", "restarting"}


class UpdateManager:
    def __init__(self, checker, data_dir, root=None, handoff=None, launch=None):
        self.checker, self.data_dir = checker, Path(data_dir).resolve()
        self.root = Path(root or config.ROOT_DIR).resolve()
        self.launch = launch or {"port": 47821, "stay": False}
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._job = None
        self._worker = None
        self._app = None
        self._thread = None
        self.handoff = Path(handoff).resolve() if handoff else None
        if self.handoff is None:
            try:
                result = json.loads((self.data_dir / "update-result.json").read_text(encoding="utf-8"))
                previous = Path(result["job"])
                record = read_job(previous)
                if Path(record["target"]).resolve() == self.root and record.get("phase") in ("committed", "rolled_back"):
                    self.handoff = previous
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.health_mode = False
        self._inherit_windows = bool(handoff)
        if handoff:
            record = read_job(self.handoff)
            starting = record.get("phase") == "starting" and record["version"] == config.VERSION
            recovering = record.get("phase") == "rolled_back" and record.get("previous_version") == config.VERSION
            if Path(record["target"]).resolve() != self.root or not (starting or recovering):
                raise ValueError("The update startup context does not match this application")
            self.health_mode = starting
        checker.manager = self
        self._eligibility = self._installation_reason()

    def _installation_reason(self):
        reason = eligibility(self.root, sys.executable)
        if self.data_dir.is_relative_to(self.root):
            return "Copies storing data inside the application folder use manual updates"
        try:
            if hasattr(self.checker.settings, "save_folder") and self.checker.settings.save_folder().resolve().is_relative_to(self.root):
                return "Copies saving Markdown inside the application folder use manual updates"
        except (OSError, ValueError, RuntimeError):
            return "The save-folder location could not be checked; use a manual update"
        return reason

    def attach(self, app):
        self._app = app

    def inherit_windows(self, port):
        if self._inherit_windows:
            record = read_job(self.handoff)
            if record.get("port") == port:
                self._app.windows.inherit(record.get("window_ids", []))

    def status(self):
        reason = self._eligibility
        with self._lock:
            job = dict(self._job) if self._job else None
        result = None
        if self.handoff and not self.health_mode:
            try:
                record = read_job(self.handoff)
                if record.get("phase") in ("committed", "rolled_back"):
                    result = {"id": record["id"], "phase": record["phase"], "version": config.VERSION}
            except (OSError, ValueError):
                pass
        return {"can_update": reason is None and self.checker.candidate() is not None,
                "eligibility_reason": reason, "job": job, "last_result": result}

    def reconnect(self, port):
        if self.handoff:
            try:
                record = read_job(self.handoff)
                phase = record.get("phase")
                if phase in ("starting", "committed", "rolled_back"):
                    if phase == "committed" and self.health_mode:
                        phase = "starting"
                    return {"id": record["id"], "version": config.VERSION, "port": port, "phase": phase,
                            "proof": proof(record["nonce"], record["id"], config.VERSION, port, phase)}
            except (OSError, ValueError):
                pass
        return None

    def _set(self, **values):
        with self._lock:
            self._job.update(values)

    def start(self, identity):
        if self.health_mode:
            raise ValueError("Wait for the current update to finish starting")
        if not isinstance(identity, str) or len(identity) != 64:
            raise ValueError("Choose a verified release first")
        reason = self._installation_reason()
        if reason:
            raise ValueError(reason)
        with self._lock:
            if self._job and self._job["phase"] in ACTIVE:
                return dict(self._job)
            self._cancel = threading.Event()
            candidate = self.checker.candidate()
            if not candidate or candidate["identity"] != identity:
                raise ValueError("Check the latest release before updating")
            self._job = {"id": secrets.token_hex(16), "phase": "checking", "bytes": 0,
                         "total": 0, "error": None, "can_cancel": True}
            self._job["version"] = candidate["version"]
            self._job["reconnect"] = {"id": self._job["id"], "nonce": secrets.token_hex(32),
                                      "version": candidate["version"], "previous_version": config.VERSION,
                                      "port": self._app.port}
            self._app.windows.hold_update(True)
            self._thread = threading.Thread(target=self._prepare, args=(identity,), name="update-prepare", daemon=True)
            self._thread.start()
            return dict(self._job)

    def cancel(self):
        with self._lock:
            if self._job and self._job["phase"] in ACTIVE:
                if not self._job["can_cancel"]:
                    raise ValueError("The update is being installed; wait for the restart")
                self._cancel.set()
            return dict(self._job) if self._job else None

    def cancel_for_quit(self):
        self.cancel()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise UpdateCancelled("Update cancelled")

    def _prepare(self, identity):
        directory = None
        handed_off = False
        paused = False
        record = None
        try:
            self.checker.check()  # Cached metadata can never authorize execution.
            candidate = self.checker.candidate()
            if not candidate or candidate["identity"] != identity or parse_version(candidate["version"]) <= parse_version(config.VERSION):
                raise ValueError("The release changed. Check again and review its notes before updating")
            self._check_cancel()
            base = cache_root().resolve()
            base.mkdir(parents=True, exist_ok=True)
            directory = base / self._job["id"]
            directory.mkdir()
            write_record(directory / "owner.json", {"id": directory.name, "target": str(self.root)})
            archive, checksum = directory / candidate["archive"]["name"], directory / candidate["checksum"]["name"]
            downloader = ReleaseDownloader(self.checker.fetcher)
            self._set(phase="downloading", version=candidate["version"], total=candidate["archive"]["size"])
            downloader.download(candidate["archive"]["url"], archive, candidate["archive"]["size"], self._cancel,
                                lambda n: self._set(bytes=n))
            downloader.download(candidate["checksum"]["url"], checksum, candidate["checksum"]["size"], self._cancel, limit=4096)
            self._set(phase="verifying")
            verify_file(archive, candidate["archive"])
            verify_file(checksum, candidate["checksum"])
            verify_checksum(checksum, candidate["archive"])
            self._check_cancel()
            self._set(phase="staging")
            payload = extract_package(archive, directory / "extracted", candidate["version"], self._cancel)
            # Reserve room on the destination volume as well as the cache volume.
            expanded = sum(p.stat().st_size for p in payload.rglob("*") if p.is_file())
            if shutil.disk_usage(self.root.parent).free < expanded * 2 + 64 * 1024 * 1024:
                raise ValueError("There is not enough space beside the application to install this update")
            nonce = self._job["reconnect"]["nonce"]
            record = {"format": 1, "id": directory.name, "nonce": nonce, "target": str(self.root),
                      "payload": str(payload), "data_dir": str(self.data_dir), "version": candidate["version"],
                      "previous_version": config.VERSION, "parent_pid": os.getpid(), "phase": "prepared",
                      "port": self._app.port, "stay": bool(self.launch.get("stay"))}
            job_path = directory / "job.json"
            write_record(job_path, record)
            with (directory / "worker.log").open("ab") as worker_log:
                self._worker = subprocess.Popen(
                    [str(payload / "python" / "pythonw.exe"), "-B", str(payload / "app" / "update_worker.py"), str(job_path)],
                    cwd=directory, stdin=subprocess.DEVNULL, stdout=worker_log, stderr=worker_log,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            self._set(phase="preparing")
            deadline = time.monotonic() + 60
            while True:
                self._check_cancel()
                try:
                    ready = json.loads((directory / "worker.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    ready = {}
                if ready.get("phase") == "ready":
                    break
                if ready.get("phase") == "failed" or self._worker.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError("The update helper could not prepare this release")
                time.sleep(.1)
            # Stop accepting normal API calls before making a consistent snapshot.
            self._app.maintenance = True
            paused = True
            if not self._app.refresher.pause(120) or not self._app.wait_idle(120):
                raise ValueError("The app is still finishing work; try the update again later")
            self._check_cancel()
            reason = self._installation_reason()  # Settings can change during download.
            if reason:
                raise ValueError(reason)
            backup = directory / "data-backup"
            backup.mkdir()
            with self._app.store.lock, self._app.settings._lock:
                snapshot = sqlite3.connect(str(backup / "dashboard.db"))
                try:
                    self._app.store.db.backup(snapshot)
                finally:
                    snapshot.close()
                settings = self.data_dir / "settings.json"
                if settings.exists():
                    shutil.copy2(settings, backup / "settings.json")
                write_record(backup / "manifest.json", {"settings_present": settings.exists()})
            with self._lock:
                self._check_cancel()
                # Every authenticated window can obtain this read-only reconnect
                # nonce; it is never the API launch token and is never sent to ping.
                self._set(phase="restarting", can_cancel=False,
                          reconnect={"id": record["id"], "nonce": nonce, "version": record["version"],
                                     "previous_version": config.VERSION, "port": self._app.port})
                record["phase"] = "handoff"
                record["window_ids"] = self._app.windows.open_ids()
                write_record(job_path, record)
                handed_off = True
            # Give foreground windows one status poll to save their view/context.
            time.sleep(2)
            self._app.windows.request_quit()
        except Exception as error:
            cancelled = isinstance(error, UpdateCancelled)
            self._set(phase="cancelled" if cancelled else "failed", can_cancel=False,
                      error=None if cancelled else (str(error) if isinstance(error, ValueError) else
                                                  "The update could not be prepared; see app.log. Your current copy is unchanged."))
            if not cancelled:
                log.exception("Update preparation failed")
            if record and not handed_off:
                record["phase"] = "cancelled"
                write_record(directory / "job.json", record)
        finally:
            if not handed_off:
                if paused:
                    self._app.maintenance = False
                    self._app.refresher.resume()
                self._app.windows.hold_update(False)
                if directory and self._cancel.is_set() and self._worker is None:
                    if directory.parent == cache_root().resolve() and directory.name == self._job["id"]:
                        shutil.rmtree(directory)
                # The staged runtime may still be held by its worker. Preserve
                # these owned files for the next safe cleanup/recovery attempt.

    def activate_after_commit(self, start_services):
        deadline = time.monotonic() + 120
        while self.health_mode:
            try:
                record = read_job(self.handoff)
                if record.get("phase") == "committed":
                    self._app.maintenance = False
                    self._app.windows.hold_update(False)
                    self._app.windows.reset_startup()
                    start_services()
                    self.health_mode = False
                    return
            except (OSError, ValueError):
                log.exception("Could not read update startup status")
            if time.monotonic() >= deadline:
                log.error("Update startup was not committed; stopping the read-only replacement. Recovery files were preserved.")
                self._app.windows.request_quit()
                return
            time.sleep(.2)
