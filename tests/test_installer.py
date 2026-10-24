import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

import installer


@unittest.skipUnless(os.name == "nt", "Windows only")
class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "zip" / "NewsDesk"
        for rel in ("python/pythonw.exe", "app/launcher.py", "app/__pycache__/x.pyc", "NewsDesk.ico", "README.md"):
            (self.source / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.source / rel).write_text(rel, encoding="utf-8")
        self.target = self.root / "Programs" / "NewsDesk"

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_skips_caches_and_replaces_old_files(self):
        installer.copy_app(self.source, self.target)
        self.assertTrue((self.target / "app" / "launcher.py").is_file())
        self.assertFalse((self.target / "app" / "__pycache__").exists())
        (self.target / "app" / "stale.py").write_text("old", encoding="utf-8")
        installer.copy_app(self.source, self.target)
        self.assertFalse((self.target / "app" / "stale.py").exists())
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["NewsDesk"])

    def test_running_copy_is_detected_before_overwriting(self):
        installer.copy_app(self.source, self.target)
        with open(self.target / "python" / "pythonw.exe", "rb"):  # held open, as a running app would
            with self.assertRaisesRegex(installer.InstallError, "still running"):
                installer.copy_app(self.source, self.target)
        self.assertTrue((self.target / "python" / "pythonw.exe").is_file())
        self.assertFalse(self.target.with_name("NewsDesk.new").exists())

    def test_shortcut_points_at_the_app(self):
        link = self.root / "Desktop" / "NewsDesk.lnk"
        installer.create_shortcut(link, self.target / "python" / "pythonw.exe",
                                  f'"{self.target / "app" / "launcher.py"}"', self.target,
                                  self.target / "NewsDesk.ico", "NewsDesk RSS Dashboard")
        read = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
             "$s.TargetPath; $s.Arguments; $s.WorkingDirectory; $s.IconLocation; $s.Description"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(read, [str(self.target / "python" / "pythonw.exe"),
                                f'"{self.target / "app" / "launcher.py"}"', str(self.target),
                                f'{self.target / "NewsDesk.ico"},0', "NewsDesk RSS Dashboard"])

    def test_install_records_shortcuts_and_uninstall_removes_only_those(self):
        (self.source / "app" / "uninstall.bat").write_bytes((_path.APP / "uninstall.bat").read_bytes())
        dirs = [self.root / "Desktop", self.root / "Start menü 100%"]
        links = installer.install(self.source, self.target, dirs)
        self.assertTrue(all(link.is_file() for link in links))
        self.assertTrue((self.target / "Uninstall NewsDesk.bat").is_file())
        manifest = json.loads((self.target / "install.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["shortcuts"], [str(link) for link in links])
        bystander = dirs[0] / "Other.lnk"
        bystander.write_text("x", encoding="utf-8")
        manifest["shortcuts"].append(str(bystander))  # a tampered manifest still can't delete other files
        (self.target / "install.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(installer.remove_shortcuts(self.target), links)
        self.assertFalse(any(link.exists() for link in links))
        self.assertTrue(bystander.exists())

    def test_detects_a_program_running_from_the_folder(self):
        self.target.mkdir(parents=True)
        program = self.target / "python" / "pinger.exe"
        program.parent.mkdir()
        program.write_bytes(Path(os.environ["SystemRoot"], "System32", "PING.EXE").read_bytes())
        self.assertEqual(installer.running_from(self.target), [])
        proc = subprocess.Popen([str(program), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
        try:
            self.assertEqual(installer.running_from(self.target), [proc.pid])
            self.assertEqual(installer.running_from(self.root / "Programs" / "News"), [])  # no prefix mix-ups
            with self.assertRaisesRegex(installer.InstallError, "still running"):
                installer.copy_app(self.source, self.target)
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(installer.running_from(self.target), [])

    def test_batch_files_are_ascii_with_windows_line_endings(self):
        for path in (_path.APP / "uninstall.bat", _path.ROOT / "Install NewsDesk.bat", _path.ROOT / "Start NewsDesk.bat"):
            raw = path.read_bytes()
            self.assertTrue(raw.isascii(), path.name)
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), path.name)


if __name__ == "__main__":
    unittest.main()
