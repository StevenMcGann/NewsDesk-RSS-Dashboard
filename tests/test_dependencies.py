"""tools/dependencies.json must match what is actually bundled and documented."""
import json
import re
import unittest

from _path import APP, ROOT

COMPONENTS = {c["name"]: c for c in json.loads((ROOT / "tools" / "dependencies.json").read_text(encoding="utf-8"))["components"]}


class ManifestTest(unittest.TestCase):
    def test_matches_third_party_md(self):
        table = (ROOT / "THIRD-PARTY.md").read_text(encoding="utf-8")
        documented = dict(re.findall(r"^\| ([^|]+?) \| ([\d.]+) \|", table, re.M))
        aliases = {"Lucide icons": "Lucide icons", "@mozilla/readability": "@mozilla/readability"}
        for name, component in COMPONENTS.items():
            if component["ecosystem"] == "python.org":
                continue
            self.assertEqual(documented.get(aliases.get(name, name)), component["version"], name)

    def test_matches_the_build_script(self):
        script = (ROOT / "build_portable.ps1").read_text(encoding="utf-8")
        pinned = re.search(r"\$PythonVersion = '([\d.]+)'", script).group(1)
        self.assertEqual(COMPONENTS["Python (embeddable, bundled in the zip)"]["version"], pinned)

    def test_matches_the_vendored_files(self):
        purify = (APP / "static" / "vendor" / "purify.min.js").read_text(encoding="utf-8")
        self.assertIn(f"DOMPurify {COMPONENTS['DOMPurify']['version']}", purify[:300])
        feedparser_init = (APP / "vendor" / "feedparser" / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(feedparser_init, rf"__version__ = ['\"]{re.escape(COMPONENTS['feedparser']['version'])}['\"]")
        icons = (APP / "static" / "icons.js").read_text(encoding="utf-8")
        self.assertIn(f"lucide-static v{COMPONENTS['Lucide icons']['version']}", icons)


if __name__ == "__main__":
    unittest.main()
