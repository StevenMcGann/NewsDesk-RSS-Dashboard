"""Release identity, safe extraction and private updater transaction records."""
import ast
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import config
from update_download import MAX_ARCHIVE, UpdateCancelled

MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_NOTES = 200_000
TERMINAL = {"complete", "rolled_back", "failed", "cancelled"}
REQUIRED = ("python/python.exe", "python/pythonw.exe", "app/config.py", "app/launcher.py",
            "app/update_worker.py", "app/installer.py", "app/update_package.py",
            "app/uninstall.bat", "Start NewsDesk.bat", "Install NewsDesk.bat", "NewsDesk.ico", "README.md",
            *("app/static/" + name for name in ("index.html", "app.css", "app.js", "api.js", "articles.js",
              "dialogs.js", "icons.js", "reader.js", "sidebar.js", "update-ui.js", "util.js", "favicon.svg", "icon-256.png",
              "vendor/purify.min.js", "vendor/Readability.js", "vendor/turndown.js")))


def parse_version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", str(text or "").strip())
    return tuple(int(n or 0) for n in match.groups()) if match else None


def asset_metadata(asset, name, tag, limit):
    if not isinstance(asset, dict) or asset.get("name") != name or asset.get("state") != "uploaded":
        raise ValueError("The release is missing its ZIP or checksum")
    size = asset.get("size")
    digest = asset.get("digest")
    identifier = asset.get("id")
    expected = f"https://github.com/{config.GITHUB_REPO}/releases/download/{tag}/{name}"
    if (asset.get("browser_download_url") != expected or not isinstance(size, int) or isinstance(size, bool)
            or not 0 < size <= limit or not isinstance(identifier, int) or isinstance(identifier, bool)
            or identifier <= 0 or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise ValueError("GitHub did not supply a valid, verified release asset")
    return {"id": identifier, "name": name, "url": expected, "size": size, "sha256": digest[7:]}


def release_metadata(data):
    if not isinstance(data, dict):
        raise ValueError("The release reply is not an object")
    version = parse_version(data.get("tag_name"))
    if not version or data.get("draft") or data.get("prerelease"):
        raise ValueError("The release is not a stable version")
    normalized = ".".join(map(str, version))
    tag = str(data["tag_name"])
    # Release URLs are generated from the known repository, never arbitrary HTML.
    latest = {"version": normalized, "name": str(data.get("name") or "")[:200],
              "url": f"https://github.com/{config.GITHUB_REPO}/releases/tag/{tag}",
              "published": str(data.get("published_at") or "")[:40]}
    candidate = None
    reason = None
    try:
        rid = data.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool) or rid <= 0:
            raise ValueError("The release does not include an update identity")
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise ValueError("The release is missing its download assets")
        name = f"NewsDesk-{normalized}.zip"
        def find(filename):
            matches = [a for a in assets if isinstance(a, dict) and a.get("name") == filename]
            if len(matches) != 1:
                raise ValueError("The release is missing an unambiguous ZIP/checksum pair")
            return matches[0]
        archive = asset_metadata(find(name), name, tag, MAX_ARCHIVE)
        checksum = asset_metadata(find(name + ".sha256"), name + ".sha256", tag, 4096)
        identity = hashlib.sha256(json.dumps([rid, tag, archive, checksum], sort_keys=True).encode()).hexdigest()
        candidate = {"identity": identity, "release_id": rid, "version": normalized, "tag": tag,
                     "archive": archive, "checksum": checksum}
    except ValueError as error:
        reason = str(error)
    return latest, candidate, str(data.get("body") or "")[:MAX_NOTES], reason


def verify_file(path, asset):
    if path.stat().st_size != asset["size"]:
        raise ValueError("The update download is incomplete")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else None
        if digest is None:
            stream.seek(0)
            h = hashlib.sha256()
            for chunk in iter(lambda: stream.read(65536), b""):
                h.update(chunk)
            digest = h.hexdigest()
    if not hmac.compare_digest(digest, asset["sha256"]):
        raise ValueError("The release SHA-256 verification failed; nothing was installed")


def verify_checksum(path, archive):
    text = path.read_text(encoding="ascii").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)", text)
    if not match or match[2] != archive["name"] or match[1].lower() != archive["sha256"]:
        raise ValueError("The published checksum does not match the release asset")


def package_version(root):
    tree = ast.parse((root / "app" / "config.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("The package has no version")


def extract_package(archive, destination, version, cancel=None):
    if destination.exists():
        raise ValueError("The extraction destination already exists")
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError("The update archive exceeds its extraction limits")
        names, files = set(), set()
        for info in entries:
            name = info.orig_filename
            parts = name.rstrip("/").split("/")
            mode = stat.S_IFMT(info.external_attr >> 16)
            if (not name or "\\" in name or "\0" in name or parts[0] != "NewsDesk" or
                    any(p in ("", ".", "..") or p != p.rstrip(" .") or ":" in p or len(p) > 255 or
                        any(ord(c) < 32 or c in '<>"|?*' for c in p) or
                        re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(?:\..*)?", p) for p in parts) or
                    mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1):
                raise ValueError("The update contains an unsafe archive path or file type")
            key = "/".join(parts).casefold()
            if key in names:
                raise ValueError("The update contains duplicate archive paths")
            names.add(key)
            if not info.is_dir():
                files.add(key)
        if any(any("/".join(key.split("/")[:n]) in files for n in range(1, len(key.split("/")))) for key in names):
            raise ValueError("The update contains conflicting archive paths")
        expanded = sum(i.file_size for i in entries)
        if shutil.disk_usage(destination.parent).free < expanded * 3 + archive.stat().st_size + 64 * 1024 * 1024:
            raise ValueError("There is not enough free space to stage and back up the update")
        destination.mkdir()
        written = 0
        for info in entries:
            if cancel and cancel.is_set():
                raise UpdateCancelled("Update cancelled")
            target = destination.joinpath(*info.filename.rstrip("/").split("/"))
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Archive path escaped its staging directory")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as source, target.open("xb") as output:
                for chunk in iter(lambda: source.read(65536), b""):
                    if cancel and cancel.is_set():
                        raise UpdateCancelled("Update cancelled")
                    written += len(chunk)
                    if written > MAX_EXPANDED:
                        raise ValueError("The update archive exceeds its extraction limits")
                    output.write(chunk)
        root = destination / "NewsDesk"
        if any(not (root / name).is_file() for name in REQUIRED) or package_version(root) != version:
            raise ValueError("The package structure or version does not match the selected release")
        return root


def eligibility(root, executable=None):
    root = Path(root).resolve()
    if os.name != "nt":
        return "In-app installation is available on Windows only"
    if (root / ".git").exists() or not all((root / name).is_file() for name in ("python/python.exe", "python/pythonw.exe", "app/launcher.py")):
        return "Source checkouts use manual updates"
    if executable is not None and Path(executable).resolve().parent != root / "python":
        return "This copy is not running its bundled Python; use a manual update"
    if str(root).startswith("\\\\") or ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) not in (2, 3):
        return "Shared or network folders use manual updates"
    try:
        # A real create test handles Windows ACLs better than os.access().
        with tempfile.TemporaryFile(dir=root.parent, prefix=".newsdesk-update-probe-"):
            pass
        with tempfile.TemporaryFile(dir=root, prefix=".newsdesk-update-probe-"):
            pass
        if (root / "install.json").exists():
            manifest = json.loads((root / "install.json").read_text(encoding="utf-8"))
            if not isinstance(manifest.get("shortcuts"), list) or not all(isinstance(s, str) for s in manifest["shortcuts"]):
                return "The installation record is invalid; use a manual update"
    except (OSError, ValueError, AttributeError):
        return "This application folder is not writable; use a manual update"
    return None


def cache_root():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "NewsDesk" / "Updates"


def write_record(path, data):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_job(path):
    path = Path(path).resolve()
    if path.name != "job.json" or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name) or path.parent.parent != cache_root().resolve():
        raise ValueError("Invalid update transaction location")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("id") != path.parent.name or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("nonce", ""))):
        raise ValueError("Invalid update transaction record")
    for field in ("target", "data_dir", "payload"):
        if not isinstance(data.get(field), str) or not Path(data[field]).is_absolute():
            raise ValueError("Invalid update transaction path")
    if Path(data["payload"]).resolve() != path.parent / "extracted" / "NewsDesk":
        raise ValueError("Invalid staged update location")
    if not parse_version(data.get("version")):
        raise ValueError("Invalid transaction version")
    windows = data.get("window_ids", [])
    if not isinstance(windows, list) or len(windows) > 4096 or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in windows):
        raise ValueError("Invalid update window registrations")
    return data


def proof(nonce, identifier, version, port, phase):
    message = f"{identifier}\n{version}\n{port}\n{phase}".encode("utf-8")
    return hmac.new(bytes.fromhex(nonce), message, hashlib.sha256).hexdigest()
