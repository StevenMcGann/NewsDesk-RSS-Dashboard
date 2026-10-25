r"""Disposable, offline packaged updater acceptance test (never your installation).

Run with a packaged runtime:
  NewsDesk\python\python.exe -B test_update_acceptance.py --candidate .\NewsDesk
Add --interactive to open the printed localhost URL and use the Update dialog.
The only GitHub/download substitutions live in generated fixture processes here;
no test URL, bypass switch or fixture hook is shipped in the application.
"""
import argparse
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile


FEED_URL = "https://newsdesk-acceptance.invalid/feed"
FEED = b'<rss version="2.0"><channel><title>Fixture</title><item><guid>keep-me</guid><title>Keep me</title><link>https://example.invalid/item</link></item></channel></rss>'
OFFLINE_PATCH = '''
if __name__ == "__main__":
    from fetcher import Response, FetchError
    def _acceptance_feed(self, url, *args, **kwargs):
        if url == %r:
            return Response(200, url, %r, "application/rss+xml", None, None, "fixture")
        raise FetchError("Offline acceptance fixture: external networking blocked")
    Fetcher.get = _acceptance_feed
'''
FAILURE_PATCH = '''
if __name__ == "__main__":
    import sqlite3, json
    _data = Path(sys.argv[sys.argv.index("--data") + 1])
    _db = sqlite3.connect(str(_data / "dashboard.db"))
    _db.execute("ALTER TABLE items ADD COLUMN acceptance_migration TEXT")
    _db.commit(); _db.close()
    (_data / "settings.json").write_text('{"theme":"light"}', encoding="utf-8")
    raise RuntimeError("Intentional acceptance-test startup failure")
'''

RUNNER = r'''
import json, pathlib, sys, threading, time
root = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / "app"), str(root / "app" / "vendor")]
import launcher, updates, update_manager
from fetcher import Fetcher, FetchError, Response
from update_download import UpdateCancelled
fixture = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
class OfflineFetcher(Fetcher):
    def get(self, url, *args, **kwargs):
        if url == updates.API_URL:
            return Response(200, url, json.dumps(fixture["release"]).encode(), "application/json", None, None, "fixture")
        if url == fixture["feed_url"]:
            return Response(200, url, fixture["feed"].encode(), "application/rss+xml", None, None, "fixture")
        raise FetchError("Offline acceptance fixture: external networking blocked")
class Checker(updates.UpdateChecker):
    def __init__(self, *args):
        super().__init__(*args)
        self.check()
class FileDownload:
    def __init__(self, fetcher): pass
    def download(self, url, destination, size, cancel, progress=lambda n: None, limit=134217728):
        source = pathlib.Path(fixture["downloads"][url])
        if source.stat().st_size != size or size > limit: raise ValueError("Invalid fixture size")
        with source.open("rb") as inp, destination.open("wb") as out:
            total = 0
            for chunk in iter(lambda: inp.read(65536), b""):
                if cancel.is_set(): raise UpdateCancelled("Update cancelled")
                out.write(chunk); total += len(chunk); progress(total)
                time.sleep(fixture["delay"])
launcher.Fetcher = OfflineFetcher
launcher.UpdateChecker = Checker
update_manager.ReleaseDownloader = FileDownload
raise SystemExit(launcher.main(["--no-window", "--port", "0", "--data", fixture["data"]]))
'''


def request(port, method, path, body=None, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


def wait_instance(data, version, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            instance = json.loads((data / "instance.json").read_text(encoding="utf-8"))
            status, raw = request(instance["port"], "GET", "/api/ping")
            ping = json.loads(raw)
            if status == 200 and ping["version"] == version:
                _, page = request(instance["port"], "GET", "/")
                token = re.search(rb'<meta name="api-token" content="([^"]+)"', page).group(1).decode()
                status, _ = request(instance["port"], "GET", "/api/state", token=token)
                if status == 200:
                    return instance, token
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        time.sleep(.1)
    raise AssertionError("The expected application did not become ready")


def stop_fixture(data):
    # Graceful shutdown only, using the token of this disposable fixture.
    for _ in range(3):
        try:
            instance = json.loads((data / "instance.json").read_text(encoding="utf-8"))
            _, page = request(instance["port"], "GET", "/")
            token = re.search(rb'<meta name="api-token" content="([^"]+)"', page).group(1).decode()
            request(instance["port"], "POST", "/api/quit", {}, token)
        except (OSError, ValueError, KeyError, AttributeError):
            return
        for _ in range(75):
            if not (data / "instance.json").exists():
                return
            time.sleep(.1)


def run_case(candidate, base, mode, interactive=False, hold_seconds=0):
    from config import Settings
    from store import Store
    from update_package import package_version
    current = package_version(candidate)
    pieces = list(map(int, current.split(".")))
    pieces[2] += 1
    future = ".".join(map(str, pieces))
    root = base / mode
    root.mkdir()
    target, data = root / "Application with spaces" / "NewsDesk", root / "User data"
    shutil.copytree(candidate, target)
    data.mkdir()
    settings = Settings(data / "settings.json")
    settings.update({"check_updates": False, "first_run_done": True, "theme": "dark", "save_folder": str(data / "Saved")})
    saved_settings = (data / "settings.json").read_bytes()
    saved_note = data / "Saved" / "keep.md"
    saved_note.parent.mkdir()
    saved_note.write_text("Exported note must stay intact", encoding="utf-8")
    store = Store(data / "dashboard.db")
    feed_id = store.add_feed(FEED_URL, "Fixture")
    entry = {"guid": "keep-me", "url": "https://example.invalid/item", "title": "Keep me", "author": None,
             "published": time.time(), "summary": "", "content": "", "snippet": "Keep me", "image_url": None}
    store.record_fetch(feed_id, {"title": "Fixture", "site_url": "", "icon_url": None}, [entry])
    item = store.list_items({})["items"][0]
    store.update_item(item["id"], read=True, starred=True)
    store.close()
    shortcuts = [str(root / "Custom Desktop" / "NewsDesk.lnk")]
    shortcut = pathlib.Path(shortcuts[0])
    shortcut.parent.mkdir()
    shortcut.write_bytes(b"unchanged shortcut fixture")
    if mode == "installed":
        (target / "install.json").write_text(json.dumps({"version": current, "shortcuts": shortcuts}))
        shutil.copyfile(target / "app" / "uninstall.bat", target / "Uninstall NewsDesk.bat")
    original_launcher = (target / "app" / "launcher.py").read_text(encoding="utf-8")
    offline = OFFLINE_PATCH % (FEED_URL, FEED)
    old_launcher = original_launcher.replace('if __name__ == "__main__":', offline + '\nif __name__ == "__main__":')
    (target / "app" / "launcher.py").write_text(old_launcher, encoding="utf-8")
    archive = root / f"NewsDesk-{future}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in candidate.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(candidate).as_posix()
            content = path.read_bytes()
            if relative == "app/config.py":
                content = content.replace(f'VERSION = "{current}"'.encode(), f'VERSION = "{future}"'.encode())
            if relative == "app/launcher.py":
                patch = offline + (FAILURE_PATCH if mode == "rollback" else "")
                content = original_launcher.replace('if __name__ == "__main__":', patch + '\nif __name__ == "__main__":').encode()
            zipped.writestr("NewsDesk/" + relative, content)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(digest + " *" + archive.name + "\n", encoding="ascii")
    assets, downloads = [], {}
    for identifier, path in enumerate((archive, checksum), 10):
        url = f"https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/releases/download/v{future}/{path.name}"
        assets.append({"id": identifier, "name": path.name, "state": "uploaded", "size": path.stat().st_size,
                       "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), "browser_download_url": url})
        downloads[url] = str(path)
    fixture = {"data": str(data), "feed_url": FEED_URL, "feed": FEED.decode(), "downloads": downloads,
               "delay": .02 if interactive else .001,
               "release": {"id": 4321, "tag_name": "v" + future, "name": "Offline acceptance fixture", "assets": assets,
                           "html_url": f"https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/releases/tag/v{future}",
                           "body": "Offline acceptance test: this is not a published release.\n<img src='https://example.invalid/never-load' onerror='alert(1)'>\n- Verify progress, Cancel and restart."}}
    manifest = root / "fixture.json"
    manifest.write_text(json.dumps(fixture), encoding="utf-8")
    runner = root / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    env = dict(os.environ, LOCALAPPDATA=str(root / "Private profile"))
    proc = None
    try:
        with (root / "runner.log").open("wb") as log:
            proc = subprocess.Popen([str(target / "python" / "python.exe"), "-u", "-B", str(runner), str(target), str(manifest)],
                                    env=env, stdout=log, stderr=log)
        instance, token = wait_instance(data, current)
        if interactive:
            print(json.dumps({"mode": mode, "url": f"http://127.0.0.1:{instance['port']}/", "fixture_only": True}), flush=True)
        else:
            _, raw = request(instance["port"], "GET", "/api/update/status", token=token)
            status = json.loads(raw)
            assert status["can_update"], status
            code, _ = request(instance["port"], "POST", "/api/update/start", {"candidate": status["candidate"]}, token)
            assert code == 200
        jobs = root / "Private profile" / "NewsDesk" / "Updates"
        deadline = time.monotonic() + (600 if interactive else 180)
        result = None
        while time.monotonic() < deadline:
            for path in jobs.glob("*/job.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("phase") in ("committed", "rolled_back", "failed"):
                    result = record
                    break
            if result:
                break
            # Preparation errors do not necessarily create a worker record.
            if not jobs.exists() or not list(jobs.glob("*/job.json")):
                try:
                    _, raw = request(instance["port"], "GET", "/api/update/status", token=token)
                    active = json.loads(raw).get("job")
                    if active and active["phase"] == "failed":
                        raise AssertionError(active["error"])
                except (OSError, ValueError):
                    pass
            time.sleep(.2)
        assert result, "No terminal update transaction; fixture files were preserved"
        expected_phase = "rolled_back" if mode == "rollback" else "committed"
        assert result["phase"] == expected_phase, result
        expected_version = current if mode == "rollback" else future
        _, _ = wait_instance(data, expected_version)
        assert package_version(target) == expected_version
        owned_prefix = target.parent / ("." + target.name + ".update-" + result["id"])
        retained = pathlib.Path(str(owned_prefix) + (".failed" if mode == "rollback" else ".old"))
        assert retained.is_dir(), "Previous/failed application recovery files must be retained"
        db = sqlite3.connect(str(data / "dashboard.db"))
        row = db.execute("SELECT read,starred,guid FROM items WHERE guid='keep-me'").fetchone()
        assert row == (1, 1, "keep-me"), row
        assert "acceptance_migration" not in [r[1] for r in db.execute("PRAGMA table_info(items)")]
        db.close()
        expected_settings = saved_settings
        if interactive:
            # Reading/filter choices made during attended testing are user writes.
            expected_settings = (jobs / result["id"] / "data-backup" / "settings.json").read_bytes()
        assert (data / "settings.json").read_bytes() == expected_settings
        assert saved_note.read_text(encoding="utf-8") == "Exported note must stay intact"
        assert shortcut.read_bytes() == b"unchanged shortcut fixture"
        if mode == "installed":
            installed = json.loads((target / "install.json").read_text())
            assert installed["version"] == future and installed["shortcuts"] == shortcuts
        else:
            assert not (target / "install.json").exists()
        outcome = {"mode": mode, "phase": result["phase"], "version": expected_version, "data_preserved": True,
                   "shortcuts_preserved": True, "external_network": False}
        (root / "acceptance-result.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
        if interactive:
            print(json.dumps(outcome), flush=True)
            if hold_seconds:
                print(f"Keeping the disposable app open for {hold_seconds} seconds for browser checks", flush=True)
                time.sleep(hold_seconds)
            else:
                input("Verify reconnected windows, then press Enter here to close this disposable app: ")
        return outcome
    finally:
        stop_fixture(data)
        if proc:
            proc.wait(timeout=20)
        # Wait for the owned worker to release the staged Python runtime.
        for worker_path in (root / "Private profile" / "NewsDesk" / "Updates").glob("*/worker.json"):
            try:
                from update_worker import ParentProcess
                worker = ParentProcess(json.loads(worker_path.read_text())["pid"])
                worker.wait(15)
                worker.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent / "dist" / "NewsDesk")
    parser.add_argument("--mode", choices=("all", "portable", "installed", "rollback"), default="all")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--hold-seconds", type=int, default=0, help="hold the updated interactive fixture open without a console prompt")
    parser.add_argument("--keep", action="store_true", help="retain the disposable fixtures for inspection")
    args = parser.parse_args()
    candidate = args.candidate.resolve()
    if not (candidate / "python" / "python.exe").is_file():
        parser.error("--candidate must contain the packaged app and python folders")
    sys.path[:0] = [str(candidate / "app"), str(candidate / "app" / "vendor")]
    parent = pathlib.Path(tempfile.gettempdir()).resolve()
    base = pathlib.Path(tempfile.mkdtemp(prefix="newsdesk-updater-acceptance-", dir=parent)).resolve()
    assert base.parent == parent and base.name.startswith("newsdesk-updater-acceptance-")
    modes = ("portable", "installed", "rollback") if args.mode == "all" else (args.mode,)
    success = False
    try:
        for mode in modes:
            print(json.dumps(run_case(candidate, base, mode, args.interactive, args.hold_seconds)), flush=True)
        success = True
    finally:
        if success and not args.keep:
            shutil.rmtree(base)  # Absolute, freshly-created, verified disposable root.
        else:
            print(json.dumps({"retained_fixture": str(base)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
