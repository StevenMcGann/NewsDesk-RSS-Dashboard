"""Updater trust boundaries and replacement/recovery behavior, without live GitHub."""
import base64
from contextlib import ExitStack, nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
import config
import installer
import update_download
import update_manager
import update_package as package
import update_worker
import updates
from fetcher import Fetcher
from test_updates import FakeFetcher, FakeSettings


def release(version="99.0.0", body="## Changes\n- Works offline\n<img src='https://example.test/pixel'>"):
    assets = []
    for identifier, name, data in ((10, f"NewsDesk-{version}.zip", b"zip"),
                                   (11, f"NewsDesk-{version}.zip.sha256", b"checksum")):
        assets.append({"id": identifier, "name": name, "state": "uploaded", "size": len(data),
                       "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                       "browser_download_url": f"https://github.com/{config.GITHUB_REPO}/releases/download/v{version}/{name}"})
    return {"id": 123, "tag_name": "v" + version, "name": "NewsDesk " + version, "body": body,
            "html_url": f"https://github.com/{config.GITHUB_REPO}/releases/tag/v{version}", "assets": assets}


def make_package(path, version="99.0.0", extra=None, omit=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in package.REQUIRED:
            if name in omit:
                continue
            data = f'VERSION = "{version}"\n' if name == "app/config.py" else "fixture"
            archive.writestr("NewsDesk/" + name, data)
        if extra:
            extra(archive)


class ReleaseMetadataTest(unittest.TestCase):
    def test_identity_binds_release_and_both_assets(self):
        data = release()
        _, first, _, reason = package.release_metadata(data)
        self.assertIsNone(reason)
        self.assertEqual(first["archive"]["size"], 3)
        for mutate in (lambda d: d.update(id=124), lambda d: d["assets"][0].update(id=12),
                       lambda d: d["assets"][0].update(digest="sha256:" + "f" * 64)):
            changed = json.loads(json.dumps(data))
            mutate(changed)
            self.assertNotEqual(package.release_metadata(changed)[1]["identity"], first["identity"])

    def test_invalid_or_incomplete_assets_disable_install_not_notes(self):
        for change in (lambda d: d.update(assets=[]), lambda d: d["assets"].append(d["assets"][0]),
                       lambda d: d["assets"][0].update(browser_download_url="https://evil.test/file.zip"),
                       lambda d: d["assets"][0].update(digest=None),
                       lambda d: d["assets"][0].update(size=package.MAX_ARCHIVE + 1),
                       lambda d: d["assets"][0].update(size=True)):
            data = release()
            change(data)
            latest, candidate, notes, reason = package.release_metadata(data)
            self.assertEqual(latest["version"], "99.0.0")
            self.assertIsNone(candidate)
            self.assertTrue(notes)
            self.assertTrue(reason)

    def test_notes_are_bounded_and_absent_from_recurring_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = updates.UpdateChecker(FakeFetcher(release(body="x" * 300_000)), FakeSettings(), Path(tmp))
            result = checker.check()
            self.assertNotIn("notes", result)
            self.assertLess(len(json.dumps(result)), 2000)
            self.assertEqual(len(checker.notes()["notes"]), package.MAX_NOTES)

    def test_prerelease_and_draft_are_not_installed(self):
        for key in ("draft", "prerelease"):
            data = release()
            data[key] = True
            with self.assertRaises(ValueError):
                package.release_metadata(data)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "release.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_valid_structure_and_matching_version(self):
        make_package(self.archive)
        extracted = package.extract_package(self.archive, self.root / "unpacked", "99.0.0")
        self.assertEqual(package.package_version(extracted), "99.0.0")
        with self.assertRaises(ValueError):
            package.extract_package(self.archive, self.root / "wrong", "99.0.1")
        make_package(self.archive, omit=("app/static/app.js",))
        with self.assertRaisesRegex(ValueError, "structure"):
            package.extract_package(self.archive, self.root / "missing-ui", "99.0.0")

    @unittest.skipUnless(os.name == "nt", "Windows destination checks")
    def test_source_read_only_and_network_copies_keep_manual_updates(self):
        target = self.root / "NewsDesk"
        for name in ("python/python.exe", "python/pythonw.exe", "app/launcher.py"):
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.assertIsNone(package.eligibility(target))
        (target / ".git").touch()
        self.assertIn("Source", package.eligibility(target))
        (target / ".git").unlink()
        with mock.patch.object(package.tempfile, "TemporaryFile", side_effect=PermissionError("read only")):
            self.assertIn("not writable", package.eligibility(target))
        with mock.patch.object(package.ctypes.windll.kernel32, "GetDriveTypeW", return_value=4):
            self.assertIn("network", package.eligibility(target))

    def test_rejects_windows_escape_and_ambiguous_paths(self):
        for i, name in enumerate(("../escape", "/NewsDesk/absolute", "NewsDesk/../escape", "NewsDesk\\bad",
                                  "NewsDesk/C:/escape", "NewsDesk/app/file:stream", "NewsDesk/CON.txt",
                                  "NewsDesk/app/name.", "NewsDesk/app/name ", "NewsDesk//empty", "NewsDesk/APP/CONFIG.py",
                                  "NewsDesk/app/file?.txt", "NewsDesk/CONIN$", "NewsDesk/COM¹.txt")):
            def extra(z):
                info = zipfile.ZipInfo("placeholder")
                info.filename = name  # Preserve raw Windows backslashes in the ZIP header.
                z.writestr(info, "bad")
            make_package(self.archive, extra=extra)
            with self.assertRaises(ValueError, msg=name):
                package.extract_package(self.archive, self.root / f"bad{i}", "99.0.0")
        self.assertFalse((self.root / "escape").exists())

    def test_rejects_links_conflicting_files_and_limits(self):
        def link(z):
            info = zipfile.ZipInfo("NewsDesk/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "../outside")
        for i, extra in enumerate((link, lambda z: z.writestr("NewsDesk/app", "conflict"))):
            make_package(self.archive, extra=extra)
            with self.assertRaises(ValueError):
                package.extract_package(self.archive, self.root / f"invalid{i}", "99.0.0")
        make_package(self.archive)
        for key, value in (("MAX_ENTRIES", 1), ("MAX_EXPANDED", 10)):
            with mock.patch.object(package, key, value), self.assertRaises(ValueError):
                package.extract_package(self.archive, self.root / key, "99.0.0")
        with mock.patch.object(package.shutil, "disk_usage", return_value=mock.Mock(free=0)), self.assertRaises(ValueError):
            package.extract_package(self.archive, self.root / "full", "99.0.0")
        self.assertFalse((self.root / "full").exists())

    def test_hashes_and_published_checksum_must_all_agree(self):
        self.archive.write_bytes(b"verified bytes")
        expected = {"name": "release.zip", "size": 14, "sha256": hashlib.sha256(b"verified bytes").hexdigest()}
        package.verify_file(self.archive, expected)
        sidecar = self.root / "checksum"
        sidecar.write_text(expected["sha256"] + " *release.zip\n", encoding="ascii")
        package.verify_checksum(sidecar, expected)
        for text in ("0" * 64 + " *release.zip", expected["sha256"] + " *other.zip", "not a checksum"):
            sidecar.write_text(text, encoding="ascii")
            with self.assertRaises(ValueError):
                package.verify_checksum(sidecar, expected)
        self.archive.write_bytes(b"tampered bytes")
        with self.assertRaises(ValueError):
            package.verify_file(self.archive, expected)

    def test_extraction_cancellation_does_not_touch_a_bystander(self):
        make_package(self.archive)
        bystander = self.root / "keep.txt"
        bystander.write_text("keep")
        event = threading.Event()
        event.set()
        with self.assertRaises(update_download.UpdateCancelled):
            package.extract_package(self.archive, self.root / "cancelled", "99.0.0", event)
        self.assertEqual(bystander.read_text(), "keep")


class StreamingDownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/data")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/unsafe":
                    self.send_response(302)
                    self.send_header("Location", "http://untrusted.test/update.zip")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"a" * (200000 if self.path != "/short" else 1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_streaming_clients_follow_checked_redirects_and_enforce_size(self):
        for mode in ("python", "winhttp", "windows"):
            f = Fetcher(lambda: mode)
            if (mode == "winhttp" and not f.winhttp.available) or (mode == "windows" and not f.windows.available):
                f.close()
                continue
            def validate(url):
                if not url.startswith(self.url + "/"):
                    raise ValueError("Untrusted redirect")
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "release.zip"
                downloader = update_download.ReleaseDownloader(f, validate=validate)
                counts = []
                downloader.download(self.url + "/redirect", target, 200000, threading.Event(), counts.append)
                self.assertEqual(target.read_bytes(), b"a" * 200000)
                self.assertEqual(counts[-1], 200000)
                with self.assertRaises(ValueError):
                    downloader.download(self.url + "/short", target, 200000, threading.Event())
                with self.assertRaises((ValueError, update_download.ResponseTooLarge)):
                    downloader.download(self.url + "/data", target, 10, threading.Event())
                with self.assertRaisesRegex(ValueError, "Untrusted"):
                    downloader.download(self.url + "/unsafe", target, 200000, threading.Event())
                cancelled = threading.Event()
                with self.assertRaises(update_download.UpdateCancelled):
                    downloader.download(self.url + "/data", target, 200000, cancelled, lambda n: cancelled.set())
            f.close()

    def test_production_url_policy_and_initial_cancel(self):
        for url in ("http://github.com/a", "https://github.com.evil.test/a", "https://user@github.com/a",
                    "https://github.com:444/a", "https://127.0.0.1/a", "file:///c:/a"):
            with self.assertRaises(ValueError):
                update_download.trusted_download_url(url)
        f = Fetcher(lambda: "python")
        with tempfile.TemporaryDirectory() as tmp:
            event = threading.Event()
            event.set()
            with self.assertRaises(update_download.UpdateCancelled):
                update_download.ReleaseDownloader(f).download("https://github.com/file", Path(tmp) / "x", 10, event)
            self.assertFalse((Path(tmp) / "x").exists())
        f.close()


class FakeClock:
    """Stands in for installer.time so rename retries run instantly."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    @classmethod
    def patch(cls):
        return mock.patch.object(installer, "time", cls())


class RecordTest(unittest.TestCase):
    """The app, the update worker and the restarted app share small JSON records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "job.json"
        package.write_record(self.path, {"phase": "prepared"})

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self):
        return sorted(p.name for p in self.path.parent.glob("*.tmp"))

    def test_replace_retries_while_another_process_has_the_record_open(self):
        replace = os.replace
        busy = {"count": 3}
        def denied(source, target):
            if busy["count"]:
                busy["count"] -= 1
                raise PermissionError("[WinError 5] Access is denied")
            return replace(source, target)
        with mock.patch.object(package.os, "replace", denied), mock.patch.object(package, "time", FakeClock()):
            package.write_record(self.path, {"phase": "handoff"})
        self.assertEqual(json.loads(self.path.read_text()), {"phase": "handoff"})
        self.assertEqual(self.leftovers(), [])

    def test_replace_gives_up_after_its_window_and_keeps_the_old_record(self):
        clock = FakeClock()
        with mock.patch.object(package.os, "replace", side_effect=PermissionError("denied")), \
                mock.patch.object(package, "time", clock), self.assertRaises(PermissionError):
            package.write_record(self.path, {"phase": "handoff"})
        self.assertGreaterEqual(clock.now, package.REPLACE_RETRY_SECONDS)
        self.assertEqual(json.loads(self.path.read_text()), {"phase": "prepared"})
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing rules")
    def test_open_record_blocks_a_single_replace_but_not_a_retried_one(self):
        # Issue #10's "Access is denied": the worker had job.json open at the moment
        # the app replaced it.
        reader = open(self.path, "rb")
        try:
            with self.assertRaises(PermissionError):
                package.write_record(self.path, {"phase": "handoff"}, retry_seconds=0)
            threading.Timer(0.3, reader.close).start()
            package.write_record(self.path, {"phase": "handoff"})
        finally:
            reader.close()
        self.assertEqual(json.loads(self.path.read_text()), {"phase": "handoff"})
        self.assertEqual(self.leftovers(), [])

    def test_listing_shows_a_replaced_record_without_opening_it(self):
        before = update_worker.listed_stamp(self.path)
        time.sleep(0.01)
        package.write_record(self.path, {"phase": "handoff", "window_ids": []})
        self.assertNotEqual(update_worker.listed_stamp(self.path), before)
        self.assertIsNone(update_worker.listed_stamp(self.path.with_name("missing.json")))


class ReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "payload"
        self.target = self.root / "portable copy with spaces"
        for root, version in ((self.source, "99.0.0"), (self.target, "1.1.0")):
            for name in ("app/config.py", "app/uninstall.bat", "python/pythonw.exe", "README.md"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'VERSION = "{version}"' if name.endswith("config.py") else version)
        self.txn = installer.AppReplacement(self.source, self.target, "99.0.0", "a" * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def test_portable_swap_and_rollback_preserve_original(self):
        self.txn.prepare()
        self.txn.apply()
        self.assertEqual(package.package_version(self.target), "99.0.0")
        self.assertTrue(self.txn.backup.exists())
        self.assertFalse((self.target / "install.json").exists())
        self.txn.rollback()
        self.assertEqual(package.package_version(self.target), "1.1.0")

    def test_installed_update_preserves_custom_shortcuts(self):
        shortcuts = [str(self.root / "custom desktop" / "NewsDesk.lnk")]
        (self.target / "install.json").write_text(json.dumps({"version": "1.1.0", "shortcuts": shortcuts}))
        self.txn.prepare()
        self.txn.apply()
        manifest = json.loads((self.target / "install.json").read_text())
        self.assertEqual(manifest, {"version": "99.0.0", "shortcuts": shortcuts})
        self.assertTrue((self.target / "Uninstall NewsDesk.bat").is_file())

    def locked(self, path, times):
        """Make renames of `path` fail as if a scanner held a file open, `times` times."""
        rename = Path.rename
        remaining = {"count": times}
        def fake(source, target):
            if source == path and remaining["count"]:
                remaining["count"] -= 1
                raise PermissionError("[WinError 32] The process cannot access the file")
            return rename(source, target)
        return mock.patch.object(Path, "rename", fake), remaining

    def test_swap_failure_and_running_process_do_not_destroy_old_app(self):
        self.txn.prepare()
        lock, _ = self.locked(self.txn.staging, 10_000)
        with lock, FakeClock.patch() as clock, self.assertRaises(PermissionError):
            self.txn.apply()
        self.assertGreaterEqual(clock.now, installer.RENAME_RETRY_SECONDS - 2)
        self.assertEqual(package.package_version(self.target), "1.1.0")
        if os.name == "nt":
            with mock.patch.object(installer, "running_from", return_value=[123]), self.assertRaises(installer.InstallError):
                self.txn.apply()

    def test_briefly_locked_folders_are_retried_not_rolled_back(self):
        # Endpoint security and indexers hold just-closed files for a moment; a work PC
        # rolled back 1.1.0 -> 1.1.4 when the single rename attempt hit that window.
        self.txn.prepare()
        for path in (self.txn.target, self.txn.staging):
            with self.subTest(locked=path.name):
                lock, remaining = self.locked(path, 3)
                with lock, FakeClock.patch():
                    self.txn.apply()
                self.assertEqual(remaining["count"], 0)
                self.assertEqual(package.package_version(self.target), "99.0.0")
                self.txn.rollback()  # Restore the original layout for the next case.
                self.assertEqual(package.package_version(self.target), "1.1.0")
                self.txn.failed.rename(self.txn.staging)

    def test_rollback_retries_a_briefly_locked_replacement(self):
        self.txn.prepare()
        self.txn.apply()
        lock, remaining = self.locked(self.txn.target, 2)
        with lock, FakeClock.patch():
            self.txn.rollback()
        self.assertEqual(remaining["count"], 0)
        self.assertEqual(package.package_version(self.target), "1.1.0")
        self.assertEqual(package.package_version(self.txn.failed), "99.0.0")

    def test_only_in_use_errors_are_retried(self):
        self.txn.prepare()
        calls = []
        def missing(source, target):
            calls.append(source)
            raise FileNotFoundError("gone")
        with mock.patch.object(Path, "rename", missing), FakeClock.patch() as clock, \
                self.assertRaises(FileNotFoundError):
            self.txn.apply()
        self.assertEqual((len(calls), clock.now), (1, 0))

    def test_data_snapshot_restores_database_and_settings_only(self):
        data = self.root / "user data"
        data.mkdir()
        backup = self.root / "data-backup"
        backup.mkdir()
        db = sqlite3.connect(str(backup / "dashboard.db"))
        db.execute("CREATE TABLE original (value TEXT)")
        db.execute("INSERT INTO original VALUES ('reading history')")
        db.commit()
        db.close()
        (backup / "settings.json").write_text('{"theme":"dark"}')
        (backup / "manifest.json").write_text('{"settings_present":true}')
        (data / "settings.json").write_text('{"theme":"wrong"}')
        (data / "saved.md").write_text("my exported note")
        update_worker.restore_data({"data_dir": str(data)}, self.root)
        db = sqlite3.connect(str(data / "dashboard.db"))
        self.assertEqual(db.execute("SELECT value FROM original").fetchone()[0], "reading history")
        db.close()
        self.assertEqual((data / "settings.json").read_text(), '{"theme":"dark"}')
        self.assertEqual((data / "saved.md").read_text(), "my exported note")


class WorkerTransactionTest(unittest.TestCase):
    def run_worker(self, *, fail_apply=False, fail_after_commit=False, stamps=None, reads=()):
        """Exercise worker control flow, including the durable commit boundary.

        `stamps` stands in for job.json's folder listing during the handoff wait.
        `reads` are job.json reads before the handoff: field overrides of the record,
        or an exception to raise. self.reads counts read_job calls.
        """
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
            root = Path(tmp)
            (root / "data-backup").mkdir()
            (root / "data-backup" / "manifest.json").write_text('{}')
            job = root / "job.json"
            record = {"format": 1, "id": "a" * 32, "nonce": "b" * 64,
                      "target": str(root / "app"), "data_dir": str(root / "data"),
                      "payload": str(root / "payload"), "version": "99.0.0", "previous_version": "1.1.0",
                      "parent_pid": 123, "phase": "prepared", "port": 1234}
            changed = dict(record, phase="handoff")
            txn = mock.Mock()
            txn.backup.exists.return_value = False
            if fail_apply:
                txn.apply.side_effect = PermissionError("locked application file")
            self.reads = patches.enter_context(
                mock.patch.object(update_worker, "read_job", side_effect=[
                    record, *(r if isinstance(r, BaseException) else dict(record, **r) for r in reads), changed]))
            if stamps is not None:
                patches.enter_context(mock.patch.object(update_worker, "listed_stamp", side_effect=stamps))
            patches.enter_context(mock.patch.object(update_worker, "AppReplacement", return_value=txn))
            patches.enter_context(mock.patch.object(update_worker, "eligibility", return_value=None))
            patches.enter_context(mock.patch.object(update_worker, "package_version", side_effect=["99.0.0", "1.1.0", "1.1.0"]))
            parent = patches.enter_context(mock.patch.object(update_worker, "ParentProcess"))
            parent.return_value.wait.return_value = True
            import launcher
            patches.enter_context(mock.patch.object(launcher, "acquire_lock", return_value=mock.Mock()))
            launch = patches.enter_context(mock.patch.object(update_worker, "launch_app"))
            child = launch.return_value
            child.poll.return_value = None
            patches.enter_context(mock.patch.object(update_worker, "wait_healthy", return_value=1234))
            restore = patches.enter_context(mock.patch.object(update_worker, "restore_data"))
            patches.enter_context(mock.patch.object(update_worker, "cleanup_previous"))
            saved = patches.enter_context(mock.patch.object(update_worker, "save_result"))
            if fail_after_commit:
                saved.side_effect = OSError("result pointer is locked")
            failing = fail_apply or fail_after_commit
            with self.assertLogs("update_worker", level="ERROR") if failing else nullcontext():
                code = update_worker.run(job)
            return code, txn, child, restore, launch, record

    def test_failed_swap_relaunches_previous_application(self):
        code, txn, child, restore, launch, record = self.run_worker(fail_apply=True)
        self.assertEqual(code, 1)
        txn.rollback.assert_called_once()
        restore.assert_called_once()
        launch.assert_called_once()
        child.terminate.assert_not_called()

    def test_handoff_wait_reads_job_only_after_the_app_replaces_it(self):
        # Issue #10: the app's "handoff" write failed with "Access is denied" because
        # the worker had job.json open; 1.1.0-1.1.4 don't retry that write.
        code, txn, *_ = self.run_worker(stamps=[(1, 10), (1, 10), (1, 10), (2, 11)], reads=[{}])
        self.assertEqual(code, 0)
        txn.apply.assert_called_once()
        self.assertEqual(self.reads.call_count, 3)  # Start, first look, after the change.

    def test_handoff_wait_survives_a_briefly_unreadable_job(self):
        code, txn, *_ = self.run_worker(reads=[PermissionError("[WinError 5] Access is denied")])
        self.assertEqual(code, 0)
        txn.apply.assert_called_once()

    def test_result_write_failure_after_commit_never_rolls_back_or_kills_child(self):
        code, txn, child, restore, launch, _ = self.run_worker(fail_after_commit=True)
        self.assertEqual(code, 0)
        txn.rollback.assert_not_called()
        restore.assert_not_called()
        child.terminate.assert_not_called()


class ManagerTest(unittest.TestCase):
    def test_duplicate_starts_share_one_job_and_can_cancel(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update_manager, "eligibility", return_value=None):
            checker = updates.UpdateChecker(FakeFetcher(release()), FakeSettings(), Path(tmp))
            checker.check()
            manager = update_manager.UpdateManager(checker, Path(tmp), root=Path(tmp) / "app")
            manager.attach(mock.Mock(port=1234))
            identity = checker.candidate()["identity"]
            hold = threading.Event()
            with mock.patch.object(manager, "_prepare", side_effect=lambda _: hold.wait(3)):
                barrier = threading.Barrier(4)
                def start_window(_):
                    barrier.wait()
                    return manager.start(identity)
                with ThreadPoolExecutor(4) as windows:
                    jobs = list(windows.map(start_window, range(4)))
                self.assertEqual(len({job["id"] for job in jobs}), 1)
                manager.cancel()
                self.assertTrue(manager._cancel.is_set())
                hold.set()
                manager._thread.join(3)

    def test_changed_release_fails_before_download_or_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update_manager, "eligibility", return_value=None):
            checker = updates.UpdateChecker(FakeFetcher(release()), FakeSettings(), Path(tmp))
            checker.check()
            identity = checker.candidate()["identity"]
            manager = update_manager.UpdateManager(checker, Path(tmp), root=Path(tmp) / "app")
            app = mock.Mock(port=1234)
            manager.attach(app)
            checker.fetcher.outcome = release("99.0.1")
            with mock.patch.object(update_manager, "ReleaseDownloader") as downloader, self.assertLogs("update_manager", level="ERROR"):
                manager.start(identity)
                manager._thread.join(3)
                downloader.assert_not_called()
            self.assertEqual(manager.status()["job"]["phase"], "failed")
            app.windows.request_quit.assert_not_called()

    def finished_job(self, cache, root, phase, version="1.1.4", previous="1.1.0"):
        directory = cache / ("d" * 32)
        directory.mkdir(parents=True)
        package.write_record(directory / "job.json", {
            "format": 1, "id": directory.name, "nonce": "e" * 64, "target": str(root),
            "payload": str(directory / "extracted" / "NewsDesk"), "data_dir": str(cache.parent / "data"),
            "version": version, "previous_version": previous, "phase": phase, "port": 1234})
        return directory / "job.json"

    def last_result(self, phase, running):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                mock.patch.object(update_manager, "eligibility", return_value=None), \
                mock.patch.object(config, "VERSION", running):
            data, root = Path(tmp) / "data", Path(tmp) / "app"
            data.mkdir()
            job = self.finished_job(package.cache_root(), root, phase)
            (data / "update-result.json").write_text(json.dumps({"job": str(job)}))
            checker = updates.UpdateChecker(FakeFetcher(release()), FakeSettings(), data)
            manager = update_manager.UpdateManager(checker, data, root=root)
            return manager.status()["last_result"]

    def test_update_result_is_reported_by_the_version_it_left_running(self):
        self.assertEqual(self.last_result("committed", "1.1.4")["phase"], "committed")
        self.assertEqual(self.last_result("rolled_back", "1.1.0")["phase"], "rolled_back")

    def test_manual_install_after_a_rollback_does_not_report_a_failure(self):
        # Issue #10: 1.1.0 -> 1.1.4 rolled back; after a manual 1.1.4 install the app
        # still said the update had failed.
        self.assertIsNone(self.last_result("rolled_back", "1.1.4"))
        self.assertIsNone(self.last_result("committed", "1.1.5"))

    def test_reconnect_proof_binds_job_version_port_and_phase(self):
        nonce = "a" * 64
        first = package.proof(nonce, "b" * 32, "1.1.0", 1234, "committed")
        for args in (("c" * 32, "1.1.0", 1234, "committed"), ("b" * 32, "1.1.1", 1234, "committed"),
                     ("b" * 32, "1.1.0", 4321, "committed"), ("b" * 32, "1.1.0", 1234, "starting")):
            self.assertNotEqual(first, package.proof(nonce, *args))
