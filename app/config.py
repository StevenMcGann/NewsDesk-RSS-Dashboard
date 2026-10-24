"""App locations and user settings."""
import json
import os
import threading
from pathlib import Path

APP_NAME = "NewsDesk RSS Dashboard"  # shown to people
SHORT_NAME = "NewsDesk"  # used for folders and files
VERSION = "1.0.2"
GITHUB_REPO = "StevenMcGann/NewsDesk-RSS-Dashboard"  # where releases are published
APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
STARTER_OPML = ROOT_DIR / "starter-feeds.opml"

DEFAULT_TEMPLATE = """# {{title}}

Source: [{{source}}]({{url}}) · {{published}}

{{content}}
"""

# key -> (type, allowed values or (min, max), default)
SCHEMA = {
    "refresh_minutes": (int, (5, 1440), 30),
    "page_size": (int, (5, 200), 20),
    "retention_days": (int, (1, 3650), 60),
    "mark_read_on_open": (bool, None, True),
    "block_remote_images": (bool, None, False),
    "theme": (str, ("dark", "light", "system"), "dark"),
    "view_mode": (str, ("cards", "list", "feed"), "cards"),
    "group_by": (str, ("none", "date", "feed", "date_feed"), "none"),
    "sort": (str, ("new", "old"), "new"),
    "status_filter": (str, ("all", "unread", "read"), "unread"),
    "fetch_mode": (str, ("auto", "python", "windows"), "auto"),
    "save_folder": (str, None, ""),
    "md_template": (str, None, DEFAULT_TEMPLATE),
    "sidebar_width": (int, (180, 600), 280),
    "reader_width": (int, (0, 3000), 0),
    "sidebar_collapsed": (bool, None, False),
    "first_run_done": (bool, None, False),
    "check_updates": (bool, None, True),
    "update_dismissed": (str, None, ""),  # version whose "update available" note was closed
}


def data_dir() -> Path:
    """Per-user data folder: %APPDATA%\\NewsDesk (override with NEWSDESK_DATA)."""
    override = os.environ.get("NEWSDESK_DATA")
    base = Path(override) if override else Path(os.environ.get("APPDATA") or Path.home()) / SHORT_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def documents_dir() -> Path:
    """The real Documents folder, which OneDrive folder redirection may have moved."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                             (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            path = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                          ctypes.byref(path)) == 0:
                try:
                    return Path(path.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except Exception:
            pass
    return Path.home() / "Documents"


def default_save_folder() -> Path:
    return documents_dir() / SHORT_NAME


class Settings:
    """settings.json in the data folder, merged over defaults and validated on write."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._values = {key: spec[2] for key, spec in SCHEMA.items()}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            self._values.update(self._validate(stored, strict=False))
        except (OSError, ValueError):
            pass

    def all(self) -> dict:
        with self._lock:
            return dict(self._values)

    def get(self, key):
        with self._lock:
            return self._values[key]

    def update(self, changes: dict) -> dict:
        clean = self._validate(changes, strict=True)
        with self._lock:
            self._values.update(clean)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return dict(self._values)

    def save_folder(self) -> Path:
        value = self.get("save_folder").strip()
        return Path(os.path.expandvars(value)).expanduser() if value else default_save_folder()

    @staticmethod
    def _validate(changes: dict, strict: bool) -> dict:
        clean = {}
        for key, value in changes.items():
            spec = SCHEMA.get(key)
            if spec is None:
                if strict:
                    raise ValueError(f"Unknown setting: {key}")
                continue
            kind, allowed, _ = spec
            try:
                if kind is bool:
                    if not isinstance(value, bool):
                        raise ValueError
                elif kind is int:
                    value = int(value)
                    if allowed and not allowed[0] <= value <= allowed[1]:
                        raise ValueError
                else:
                    if not isinstance(value, str) or (allowed and value not in allowed):
                        raise ValueError
                    if len(value) > 20000:
                        raise ValueError
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"Invalid value for {key}") from None
                continue
            clean[key] = value
        return clean
