"""Install NewsDesk for the current user, with as little Windows integration as possible.

Copies the app to %LOCALAPPDATA%\\Programs\\NewsDesk, adds desktop and Start menu
shortcuts, and puts "Uninstall NewsDesk.bat" (app/uninstall.bat) plus an install.json
listing the shortcuts in the install folder. Nothing goes in the registry. Run by
"Install NewsDesk.bat" with the bundled Python.

Shortcuts are made through the Windows shell's own COM object (IShellLink) via ctypes,
so this works even where PowerShell scripts or VBScript are blocked.
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import config  # noqa: E402

COPY = ("python", "app", "Start NewsDesk.bat", "NewsDesk.ico", "README.md", "LICENSE", "THIRD-PARTY.md",
        "starter-feeds.opml")
SHORTCUT = f"{config.SHORT_NAME}.lnk"
MANIFEST = "install.json"
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"


class InstallError(Exception):
    pass


# ---- Windows shell helpers -------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    value = GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(value))
    return value


def known_folder(folder_id: str) -> Path:
    """Desktop and Start menu paths, following OneDrive or other folder redirection."""
    path = ctypes.c_wchar_p()
    ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(_guid(folder_id)), 0, None, ctypes.byref(path))
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def _method(obj, index, *argtypes):
    """Call slot `index` of a COM object's vtable."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    func = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])
    return lambda *args: func(obj, *args)


def create_shortcut(path: Path, target: Path, arguments: str, workdir: Path, icon: Path, description: str):
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    try:
        link = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,
                               ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")), ctypes.byref(link))
        try:
            # IShellLinkW slots: 7 SetDescription, 9 SetWorkingDirectory, 11 SetArguments,
            # 17 SetIconLocation, 20 SetPath
            _method(link, 20, wintypes.LPCWSTR)(str(target))
            _method(link, 11, wintypes.LPCWSTR)(arguments)
            _method(link, 9, wintypes.LPCWSTR)(str(workdir))
            _method(link, 17, wintypes.LPCWSTR, ctypes.c_int)(str(icon), 0)
            _method(link, 7, wintypes.LPCWSTR)(description)
            persist = ctypes.c_void_p()
            _method(link, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid("{0000010B-0000-0000-C000-000000000046}")), ctypes.byref(persist))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(str(path), True)  # IPersistFile.Save
            finally:
                _method(persist, 2)()  # Release
        finally:
            _method(link, 2)()  # Release
    finally:
        ole32.CoUninitialize()


def running_from(folder: Path) -> list:
    """IDs of processes (other than this one) whose program file is inside folder.

    A plain rename test isn't enough: Windows lets you rename the folder of a running
    program unless something holds that exact folder open.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD))
    pids = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    prefix = str(folder.resolve()).lower().rstrip("\\") + "\\"
    found = []
    for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if pid in (0, os.getpid()):
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) \
                    and name.value.lower().startswith(prefix):
                found.append(pid)
        finally:
            kernel32.CloseHandle(handle)
    return found


RUNNING = "NewsDesk is still running. Close its window, wait about 30 seconds, then try again."


# ---- install ---------------------------------------------------------------------------

def copy_app(source: Path, target: Path):
    """Copy into a fresh folder and swap it in, so an update leaves no stale files.

    Renaming the old folder fails while NewsDesk runs from it, which is how a running
    copy is detected before anything is overwritten.
    """
    if target.exists() and running_from(target):
        raise InstallError(RUNNING)
    staging = target.with_name(target.name + ".new")
    old = target.with_name(target.name + ".old")
    for leftover in (staging, old):
        shutil.rmtree(leftover, ignore_errors=True)
    staging.mkdir(parents=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in COPY:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, staging / name, ignore=skip)
        elif item.is_file():
            shutil.copy2(item, staging / name)
    if target.exists():
        try:
            target.rename(old)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(RUNNING) from None
    staging.rename(target)
    shutil.rmtree(old, ignore_errors=True)


def install(source: Path, target: Path, shortcut_dirs: list) -> list:
    if source.resolve() != target.resolve():
        copy_app(source, target)
    pythonw = target / "python" / "pythonw.exe"
    shortcuts = []
    for folder in shortcut_dirs:
        link = folder / SHORTCUT
        create_shortcut(link, pythonw, f'"{target / "app" / "launcher.py"}"', target,
                        target / "NewsDesk.ico", config.APP_NAME)
        shortcuts.append(link)
    # The uninstaller is a fixed script; this manifest tells it what to remove. Its
    # presence also marks the folder as installed, so the zip copy can't be "uninstalled".
    (target / MANIFEST).write_text(json.dumps({"version": config.VERSION,
                                               "shortcuts": [str(s) for s in shortcuts]}, indent=2),
                                   encoding="utf-8")
    shutil.copyfile(target / "app" / "uninstall.bat", target / f"Uninstall {config.SHORT_NAME}.bat")
    return shortcuts


def remove_shortcuts(target: Path) -> list:
    """Delete the shortcuts listed in the install manifest (run by the uninstaller)."""
    manifest = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
    removed = []
    for link in map(Path, manifest.get("shortcuts", [])):
        if link.suffix.lower() == ".lnk" and link.name == SHORTCUT:
            link.unlink(missing_ok=True)
            removed.append(link)
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Install {config.APP_NAME} for this user")
    parser.add_argument("--dir", type=Path, help="install folder (default: %%LOCALAPPDATA%%\\Programs\\NewsDesk)")
    parser.add_argument("--desktop", type=Path, help="folder for the desktop shortcut")
    parser.add_argument("--start-menu", type=Path, help="folder for the Start menu shortcut")
    parser.add_argument("--no-start", action="store_true", help="don't offer to start NewsDesk afterwards")
    parser.add_argument("--remove-shortcuts", action="store_true", help=argparse.SUPPRESS)  # used by the uninstaller
    args = parser.parse_args(argv)

    source = config.ROOT_DIR
    if args.remove_shortcuts:  # first step of uninstalling; exit code 2 means "still running"
        if running_from(source):
            print(RUNNING)
            return 2
        for link in remove_shortcuts(source):
            print(f"Removed {link}")
        return 0
    target = args.dir or Path(os.environ["LOCALAPPDATA"]) / "Programs" / config.SHORT_NAME
    if not (source / "python" / "pythonw.exe").is_file():
        print("This copy of NewsDesk has no bundled Python, so it can't be installed.")
        return 1
    print(f"Installing {config.APP_NAME} {config.VERSION}")
    print(f"  to {target}")
    try:
        shortcuts = install(source, target, [args.desktop or known_folder(FOLDERID_DESKTOP),
                                             args.start_menu or known_folder(FOLDERID_PROGRAMS)])
    except (InstallError, OSError) as e:
        print(f"\nInstall failed: {e}")
        return 1
    print("\nDone. Shortcuts:")
    for link in shortcuts:
        print(f"  {link}")
    print(f"\nTo remove NewsDesk later, run \"Uninstall {config.SHORT_NAME}.bat\" in {target}.")
    if source.resolve() != target.resolve():
        print("You can delete the folder you installed from.")
    if not args.no_start and input("\nStart NewsDesk now? (y/n) ").strip().lower().startswith("y"):
        subprocess.Popen([str(target / "python" / "pythonw.exe"), str(target / "app" / "launcher.py")],
                         cwd=target, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
