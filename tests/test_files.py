"""OPML and Markdown export."""
import tempfile
import time
import unittest
from pathlib import Path

import _path  # noqa: F401

import export_md
import opml


class OpmlTest(unittest.TestCase):
    def test_round_trip_nested(self):
        tree = [
            {"type": "folder", "title": "Tech", "children": [
                {"type": "feed", "title": "Ars & Co", "url": "https://ars.test/rss", "site_url": "https://ars.test/"},
                {"type": "folder", "title": "Deep", "children": [
                    {"type": "feed", "title": "Deep feed", "url": "https://deep.test/rss", "site_url": None}]},
            ]},
            {"type": "feed", "title": "Loose", "url": "https://loose.test/rss", "site_url": None},
        ]
        self.assertEqual(opml.parse(opml.build(tree)), tree)
        self.assertEqual(opml.count_feeds(tree), 3)

    def test_skips_empty_folders_and_non_http(self):
        tree = opml.parse(b"""<?xml version="1.0"?><opml version="1.0"><head/><body>
            <outline text="Empty"/>
            <outline text="Bad" xmlUrl="file:///c:/secret"/>
            <outline title="Titled" xmlurl="http://ok.test/feed"/></body></opml>""")
        self.assertEqual(tree, [{"type": "feed", "title": "Titled", "url": "http://ok.test/feed", "site_url": None}])

    def test_drops_non_web_site_links(self):
        tree = opml.parse(b"""<opml version="2.0"><body>
            <outline text="A" xmlUrl="https://a.test/rss" htmlUrl="javascript:alert(document.domain)"/>
            <outline text="B" xmlUrl="https://b.test/rss" htmlUrl="  https://b.test/  "/></body></opml>""")
        self.assertEqual([n["site_url"] for n in tree], [None, "https://b.test/"])

    def test_rejects_non_opml(self):
        for data in (b"not xml", b"<rss><channel/></rss>"):
            with self.assertRaises(ValueError):
                opml.parse(data)


class MarkdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Saved"
        self.item = {"id": 1, "title": 'What: "AI" <now>/later?', "url": "https://x.test/a", "feed_title": "X News",
                     "author": "Ann", "published": time.mktime((2026, 10, 5, 9, 30, 0, 0, 0, -1)), "saved_path": None}

    def tearDown(self):
        self.tmp.cleanup()

    def test_safe_filename(self):
        self.assertEqual(export_md.safe_filename(self.item["title"], self.item["published"]),
                         "2026-10-05 What AI now later.md")
        self.assertTrue(export_md.safe_filename("x" * 500).endswith("x.md"))
        self.assertLess(len(export_md.safe_filename("x" * 500)), 120)
        self.assertTrue(export_md.safe_filename("...").endswith(" Untitled.md"))

    def test_render(self):
        text = export_md.render(self.item, "Body with {{title}} braces", ["Read later"], "# {{title}}\n{{tags}}\n\n{{content}}")
        self.assertTrue(text.startswith('---\ntitle: "What: \\"AI\\" <now>/later?"\n'))
        self.assertIn('tags: ["read-later"]', text)
        self.assertIn("#read-later", text)
        self.assertIn("Body with {{title}} braces", text)
        self.assertIn('source: "X News"', text)

    def test_save_new_duplicate_and_resave(self):
        first = export_md.save(self.folder, self.item, "one", [], "{{content}}")
        self.assertEqual(first.parent, self.folder)
        second = export_md.save(self.folder, self.item, "two", [], "{{content}}")
        self.assertTrue(second.name.endswith("(2).md"))
        self.item["saved_path"] = str(first)
        again = export_md.save(self.folder, self.item, "three", [], "{{content}}")
        self.assertEqual(again, first)
        self.assertIn("three", first.read_text(encoding="utf-8"))
        self.assertEqual(len(list(self.folder.glob("*.md"))), 2)

    def test_saved_path_outside_folder_is_ignored(self):
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("keep", encoding="utf-8")
        self.item["saved_path"] = str(outside)
        path = export_md.save(self.folder, self.item, "new", [], "{{content}}")
        self.assertEqual(path.parent, self.folder)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
