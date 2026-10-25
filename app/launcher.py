"""Start NewsDesk: one local server per user, shown in an Edge app window.

Double-clicking again while it runs just opens another window. The server exits on
its own once every window has been closed.
"""
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
for _path in (APP_DIR / "vendor", APP_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import logging.handlers  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402

import config  # noqa: E402
from fetcher import Fetcher  # noqa: E402
from refresher import Refresher  # noqa: E402
from server import App, create_server  # noqa: E402
from store import Store  # noqa: E402
from updates import UpdateChecker  # noqa: E402

DEFAULT_PORT = 47821

log = logging.getLogger("launcher")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data", help="data folder (default: %%APPDATA%%\\NewsDesk)")
    parser.add_argument("--no-window", action="store_true", help="don't open a window")
    parser.add_argument("--stay", action="store_true", help="keep running when windows close")
    parser.add_argument("--diagnose", action="store_true", help="test feed downloads and print a report")
    args = parser.parse_args(argv)

    if args.data:
        os.environ["NEWSDESK_DATA"] = args.data
    data = config.data_dir()
    setup_logging(data, console=args.diagnose)

    if args.diagnose:
        fetcher = Fetcher()
        try:
            print(json.dumps(fetcher.diagnose(), indent=2))
        finally:
            fetcher.close()
        return 0

    lock = acquire_lock(data / "instance.lock")
    if lock is None:
        url = running_url(data, wait=10)
        if not url:
            raise RuntimeError("NewsDesk seems to be starting already. Try again in a moment.")
        if not args.no_window:
            open_window(url)
        return 0
    try:
        serve(data, args)
    finally:
        (data / "instance.json").unlink(missing_ok=True)
        lock.close()
    return 0


def serve(data: Path, args):
    settings = config.Settings(data / "settings.json")
    store = Store(data / "dashboard.db")
    fetcher = Fetcher(lambda: settings.get("fetch_mode"))
    refresher = Refresher(store, fetcher, settings)
    updates = UpdateChecker(fetcher, settings, data)
    app = App(store, settings, fetcher, refresher, data, updates)
    srv = create_server(app, args.port)
    url = f"http://127.0.0.1:{app.port}/"
    (data / "instance.json").write_text(json.dumps({"port": app.port, "pid": os.getpid()}), encoding="utf-8")
    log.info("NewsDesk %s serving %s (data in %s)", config.VERSION, url, data)
    refresher.start()
    updates.start()
    threading.Thread(target=watchdog, args=(app, srv, args.stay), name="watchdog", daemon=True).start()
    if not args.no_window:
        open_window(url)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        refresher.stop()
        updates.stop()
        fetcher.close()
        srv.server_close()
        store.close()
        log.info("Stopped")


def watchdog(app: App, srv, stay=False):
    while True:
        time.sleep(5)
        reason = app.windows.shutdown_reason(stay=stay)
        if reason:
            break
    log.info("%s; shutting down", reason)
    srv.shutdown()


def acquire_lock(path: Path):
    """Hold an exclusive lock for the life of the process; None if another copy has it."""
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def running_url(data: Path, wait: float):
    deadline = time.monotonic() + wait
    while True:
        try:
            port = json.loads((data / "instance.json").read_text(encoding="utf-8"))["port"]
            url = f"http://127.0.0.1:{port}/"
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "api/ping", timeout=2) as r:
                if json.loads(r.read()).get("app") == "newsdesk":
                    return url
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() > deadline:
            return None
        time.sleep(0.5)


def find_edge():
    if os.name != "nt":
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path = winreg.QueryValue(key, None)
                if path and os.path.isfile(path):
                    return path
        except OSError:
            pass
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe") if base else ""
        if path and os.path.isfile(path):
            return path
    return None


def open_window(url: str):
    edge = find_edge()
    if edge:
        size = []
        try:
            import ctypes
            width, height = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
            size = [f"--window-size={min(1600, int(width * 0.9))},{min(1000, int(height * 0.9))}"]
        except Exception:
            pass
        try:
            subprocess.Popen([edge, f"--app={url}", *size])
            return
        except OSError:
            log.exception("Could not start Edge")
    webbrowser.open(url)


def setup_logging(data: Path, console: bool):
    handlers = [logging.handlers.RotatingFileHandler(data / "app.log", maxBytes=1_000_000, backupCount=2,
                                                     encoding="utf-8")]
    if console and sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def show_error(message: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, config.APP_NAME, 0x10)
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("launcher").exception("Startup failed")
        show_error(f"NewsDesk couldn't start:\n\n{exc}\n\nDetails are in app.log in %APPDATA%\\NewsDesk.")
        sys.exit(1)
