"""External, verified updater. This process never runs from the replaced folder."""
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(APP_DIR), str(APP_DIR / "vendor")]

import config  # noqa: E402
from installer import AppReplacement, InstallError, running_from  # noqa: E402
from update_package import cache_root, eligibility, package_version, proof, read_job, write_record  # noqa: E402

log = logging.getLogger("update_worker")


class ParentProcess:
    def __init__(self, pid):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("Invalid parent process")
        self.pid, self.handle = pid, None
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.kernel.OpenProcess.restype = ctypes.c_void_p
            self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)
            if not self.handle:
                raise OSError("Could not track the original app process")

    def wait(self, seconds):
        if self.handle:
            return self.kernel.WaitForSingleObject(self.handle, int(seconds * 1000)) == 0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(.1)
        return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


def restore_data(record, directory):
    data = Path(record["data_dir"]).resolve()
    backup = directory / "data-backup"
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    # Only these known application files are restored. No exported Markdown,
    # subscription URL or arbitrary journal path can become a delete target.
    for suffix in ("-wal", "-shm"):
        (data / ("dashboard.db" + suffix)).unlink(missing_ok=True)
    shutil.copy2(backup / "dashboard.db", data / "dashboard.db")
    if manifest["settings_present"]:
        shutil.copy2(backup / "settings.json", data / "settings.json")
    else:
        (data / "settings.json").unlink(missing_ok=True)


def launch_app(record, job_path=None):
    target = Path(record["target"])
    command = [str(target / "python" / "pythonw.exe"), "-B", str(target / "app" / "launcher.py"),
               "--data", record["data_dir"], "--port", str(record["port"]), "--no-window"]
    if record.get("stay"):
        command.append("--stay")
    if job_path:
        command.extend(["--update-session", str(job_path)])
    return subprocess.Popen(command, cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def wait_healthy(child, record, timeout=60):
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise InstallError("The replacement app did not start")
        try:
            instance = json.loads((Path(record["data_dir"]) / "instance.json").read_text(encoding="utf-8"))
            port = instance["port"]
            if instance["pid"] != child.pid or not isinstance(port, int) or not 0 < port < 65536:
                raise ValueError("Waiting for the replacement instance")
            with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
                ping = json.loads(response.read(16384))
            handshake = ping.get("reconnect") or {}
            if (ping.get("app") == "newsdesk" and ping.get("version") == record["version"]
                    and handshake.get("id") == record["id"] and handshake.get("phase") == "starting"
                    and handshake.get("proof") == proof(record["nonce"], record["id"], record["version"], port, "starting")):
                return port
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(.2)
    raise InstallError("The replacement app did not pass its startup check")


def save_result(record, job_path):
    write_record(Path(record["data_dir"]) / "update-result.json", {"job": str(job_path)})


def cleanup_previous(target, current):
    """Best effort; only complete updater records for this exact installation."""
    base = cache_root().resolve()
    for directory in base.iterdir():
        if not directory.is_dir() or directory.is_symlink() or directory == current:
            continue
        try:
            record = read_job(directory / "job.json")
            if record.get("phase") not in ("committed", "cancelled") or Path(record["target"]).resolve() != target:
                continue
            status = json.loads((directory / "worker.json").read_text(encoding="utf-8"))
            try:
                process = ParentProcess(status["pid"])
            except OSError:
                process = None
            if process:
                try:
                    if not process.wait(0):
                        continue
                finally:
                    process.close()
            transaction = AppReplacement(record["payload"], target, record["version"], record["id"])
            for owned in (transaction.backup, transaction.staging, transaction.failed):
                if owned.parent != target.parent or owned.is_symlink():
                    raise ValueError("Unsafe old updater directory")
                if owned.exists():
                    shutil.rmtree(owned)
            if directory.parent == base:
                shutil.rmtree(directory)
        except (OSError, ValueError, KeyError):
            log.info("Preserved old update recovery files")


def run(job_path, recover=False):
    job_path = Path(job_path).resolve()
    record = read_job(job_path)
    directory = job_path.parent
    target, data = Path(record["target"]).resolve(), Path(record["data_dir"]).resolve()
    if data.is_relative_to(target) or target == target.parent:
        raise ValueError("User data must be separate from the application folder")
    transaction = AppReplacement(record["payload"], target, record["version"], record["id"])
    status_path = directory / "worker.json"
    def status(phase, error=None):
        write_record(status_path, {"pid": os.getpid(), "phase": phase, "error": error})
    # The sibling lock also excludes an update from another data-directory instance.
    import launcher
    lock = launcher.acquire_lock(target.with_name(f".{target.name}.update.lock"))
    if lock is None:
        status("failed", "Another update is already running")
        return 1
    parent = None
    child = None
    switched = False
    committed = False
    parent_exited = False
    try:
        if recover:
            if record.get("phase") == "committed":
                return 0
            if os.name == "nt" and target.exists() and running_from(target):
                raise InstallError("Close NewsDesk before recovering this update")
            transaction.rollback()
            restore_data(record, directory)
            record["phase"] = "rolled_back"
            write_record(job_path, record)
            save_result(record, job_path)
            launch_app(record)
            status("rolled_back")
            return 0
        if record.get("phase") != "prepared" or package_version(Path(record["payload"])) != record["version"]:
            raise InstallError("The update is not ready for installation")
        reason = eligibility(target)
        if reason:
            raise InstallError(reason)
        if package_version(target) != record["previous_version"]:
            raise InstallError("The installed version changed during preparation")
        parent = ParentProcess(record["parent_pid"])
        transaction.prepare()
        status("ready")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            changed = read_job(job_path)
            if any(changed.get(k) != record.get(k) for k in ("target", "data_dir", "payload", "nonce", "version", "parent_pid")):
                raise InstallError("The update transaction changed unexpectedly")
            if changed.get("phase") == "handoff":
                record = changed
                break
            if changed.get("phase") in ("cancelled", "failed"):
                if transaction.staging.exists() and transaction.staging.parent == target.parent:
                    shutil.rmtree(transaction.staging)
                record["phase"] = "cancelled"
                write_record(job_path, record)
                status("cancelled")
                return 0
            time.sleep(.1)
        else:
            raise InstallError("Update handoff timed out")
        status("waiting")
        if not parent.wait(120):
            raise InstallError("The original process is still running; nothing was replaced")
        parent_exited = True
        if not (directory / "data-backup" / "manifest.json").is_file():
            raise InstallError("The user-data backup is missing")
        if package_version(target) != record["previous_version"]:
            raise InstallError("The installed version changed before replacement")
        record["phase"] = "installing"
        write_record(job_path, record)
        transaction.apply()
        switched = True
        record["phase"] = "starting"
        write_record(job_path, record)
        status("starting")
        child = launch_app(record, job_path)
        port = wait_healthy(child, record)
        record.update(phase="committed", installed_port=port)
        write_record(job_path, record)
        committed = True
        save_result(record, job_path)
        status("complete")
        if port != record["port"]:
            launcher.open_window(f"http://127.0.0.1:{port}/")
        cleanup_previous(target, directory)
        return 0
    except Exception:
        log.exception("Update transaction failed; retaining recovery files")
        if committed:
            # The new app may already be accepting user writes. A later log,
            # result-file or cache-cleanup failure must never undo that data.
            return 0
        if child is not None and child.poll() is None:
            child.terminate()  # Only our own, uncommitted health-check child.
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        if switched or transaction.backup.exists() or parent_exited:
            try:
                if os.name == "nt" and target.exists() and running_from(target):
                    raise InstallError("Close NewsDesk before restoring its data; recovery files have been preserved")
                transaction.rollback()
                restore_data(record, directory)
                record["phase"] = "rolled_back"
                write_record(job_path, record)
                save_result(record, job_path)
                launch_app(record)
                status("rolled_back", "The update did not start; your previous version and data were restored")
                return 1
            except Exception:
                log.exception("Automatic recovery could not finish")
        record["phase"] = "failed"
        write_record(job_path, record)
        status("failed", "The update could not finish. Recovery files have been preserved")
        return 1
    finally:
        if parent:
            parent.close()
        lock.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--recover"):
        raise SystemExit("Usage: update_worker.py <private job.json> [--recover]")
    try:
        raise SystemExit(run(sys.argv[1], len(sys.argv) == 3))
    except Exception:
        log.exception("Update worker could not initialize")
        raise SystemExit(1)
