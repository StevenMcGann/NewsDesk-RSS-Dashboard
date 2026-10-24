import time
import unittest

from _path import sample

import feeds


class RssTwoTest(unittest.TestCase):
    def setUp(self):
        self.meta, self.entries = feeds.parse(sample("rss2.xml"), "https://news.example.com/rss", "application/rss+xml")
        self.by_title = {e["title"]: e for e in self.entries}

    def test_meta(self):
        self.assertEqual(self.meta["title"], "Example News & Views")
        self.assertEqual(self.meta["site_url"], "https://news.example.com/")
        self.assertEqual(self.meta["icon_url"], "https://news.example.com/favicon.ico")

    def test_all_entries(self):
        self.assertEqual(len(self.entries), 4)

    def test_media_thumbnail_and_author(self):
        e = self.by_title["Thumbnail story"]
        self.assertEqual(e["image_url"], "https://cdn.example.com/thumb1.jpg")
        self.assertEqual(e["author"], "Ada Writer")
        self.assertEqual(e["guid"], "story-1")

    def test_inline_image_skips_tracking_pixels_and_resolves_relative(self):
        e = self.by_title["Inline image story"]
        self.assertEqual(e["image_url"], "https://news.example.com/images/photo2.png")

    def test_content_is_sanitized_by_feedparser(self):
        self.assertNotIn("<script", self.by_title["Inline image story"]["content"])

    def test_enclosure_image(self):
        self.assertEqual(self.by_title["No guid story"]["image_url"], "https://cdn.example.com/three.jpg")

    def test_guid_falls_back_to_link_then_hash(self):
        self.assertEqual(self.by_title["No guid story"]["guid"], "https://news.example.com/a/3")
        orphan = self.by_title["Neither guid nor link"]
        self.assertTrue(orphan["guid"].startswith("sha1:"))
        self.assertIsNone(orphan["url"])

    def test_future_dates_are_dropped(self):
        self.assertIsNone(self.by_title["Neither guid nor link"]["published"])
        self.assertLess(self.by_title["Thumbnail story"]["published"], time.time())

    def test_snippet_is_short_plain_text(self):
        s = self.by_title["Neither guid nor link"]["snippet"]
        self.assertLessEqual(len(s), feeds.SNIPPET_CHARS + 1)
        self.assertTrue(s.endswith("…"))
        self.assertNotIn("<", self.by_title["Inline image story"]["snippet"])


class OtherFormatsTest(unittest.TestCase):
    def test_atom(self):
        meta, entries = feeds.parse(sample("atom.xml"), "https://blog.example.org/atom.xml")
        self.assertEqual(meta["title"], "Example Blog")
        self.assertEqual(meta["icon_url"], "https://blog.example.org/favicon.png")
        e = entries[0]
        self.assertEqual(e["url"], "https://blog.example.org/posts/relative")
        self.assertIn("full", e["content"])
        self.assertEqual(e["summary"], "The summary.")
        self.assertEqual(e["author"], "Grace")

    def test_rss1(self):
        meta, entries = feeds.parse(sample("rss1.rdf"), "https://rdf.example.net/index.rdf")
        self.assertEqual(meta["title"], "RDF Site")
        self.assertEqual(entries[0]["title"], "RDF item")
        self.assertIsNotNone(entries[0]["published"])

    def test_json_feed(self):
        meta, entries = feeds.parse(sample("feed.json"), "https://json.example.com/feed.json", "application/feed+json")
        self.assertEqual(meta["title"], "JSON Example")
        first, second = entries
        self.assertEqual(first["author"], "Linus")
        self.assertEqual(first["image_url"], "https://json.example.com/hero.jpg")
        self.assertEqual(second["url"], "https://json.example.com/j2")
        self.assertEqual(second["title"], "(untitled)")
        self.assertIn("<p>Para &lt;two&gt;.</p>", second["content"])
        self.assertEqual(second["image_url"], "https://json.example.com/j2.png")
        self.assertAlmostEqual(second["published"] - first["published"], 86400 + 5 * 3600)

    def test_malformed_feed_still_parses(self):
        meta, entries = feeds.parse(sample("malformed.xml"), "https://broken.example.com/rss")
        self.assertEqual(len(entries), 2)
        self.assertIn("Unescaped", entries[0]["title"])

    def test_html_page_is_not_a_feed(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse(sample("page.html"), "https://news.example.com/", "text/html")


class PagesTest(unittest.TestCase):
    def test_discover_orders_comment_feeds_last_and_adds_guesses(self):
        found = feeds.discover(sample("page.html"), "https://news.example.com/section/")
        self.assertEqual(found[:3], ["https://news.example.com/feed.xml", "https://news.example.com/atom",
                                     "https://news.example.com/comments/feed"])
        self.assertIn("https://news.example.com/rss", found)
        self.assertEqual(len(found), len(set(found)))

    def test_decode_html_uses_meta_charset(self):
        body = '<meta charset="windows-1252"><p>caf\xe9</p>'.encode("cp1252")
        self.assertIn("café", feeds.decode_html(body, "text/html"))
        self.assertIn("café", feeds.decode_html("<p>café</p>".encode(), "text/html; charset=utf-8"))

    def test_text_strips_tags_and_entities(self):
        self.assertEqual(feeds.text("<b>A</b> &amp; <i>B</i><script>x()</script>"), "A & B")


if __name__ == "__main__":
    unittest.main()
