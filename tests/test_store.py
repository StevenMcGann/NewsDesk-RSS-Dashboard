import tempfile
import time
import unittest
from pathlib import Path

from _path import sample

import feeds
import opml
from store import NotFound, Store, UNSEEN_GRACE


def entry(guid, title="T", published=None, **extra):
    e = {"guid": guid, "url": f"https://x.test/{guid}", "title": title, "author": None,
         "published": published or time.time() - 3600, "summary": "", "content": "",
         "snippet": title, "image_url": None}
    e.update(extra)
    return e


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.meta = {"title": "Feed", "site_url": "https://x.test/", "icon_url": None}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_feed(self, url="https://x.test/rss", folder=None):
        return self.store.add_feed(url, "Feed " + url, folder)

    def test_default_tags_seeded(self):
        names = [t["name"] for t in self.store.state()["tags"]]
        self.assertEqual(names, ["Favorite", "Important", "Read later"])

    def test_refresh_dedupes_and_keeps_state(self):
        fid = self.add_feed()
        self.assertEqual(self.store.record_fetch(fid, self.meta, [entry("a"), entry("b")]), 2)
        item = self.store.list_items({})["items"][0]
        self.store.update_item(item["id"], read=True, starred=True)
        self.assertEqual(self.store.record_fetch(fid, self.meta, [entry("a", "New title"), entry("b", "New title")]), 0)
        again = self.store.item(item["id"])
        self.assertTrue(again["read"] and again["starred"])
        self.assertEqual(again["title"], "New title")

    def test_real_feed_round_trip(self):
        fid = self.add_feed()
        meta, entries = feeds.parse(sample("rss2.xml"), "https://news.example.com/rss")
        self.assertEqual(self.store.record_fetch(fid, meta, entries), 4)
        state = self.store.state()
        self.assertEqual(state["feeds"][0]["unread"], 4)
        self.assertEqual(state["feeds"][0]["site_url"], "https://news.example.com/")
        self.assertEqual(state["totals"]["unread"], 4)

    def test_filters(self):
        parent = self.store.add_folder("Parent")
        child = self.store.add_folder("Child", parent)
        f1, f2, f3 = self.add_feed("https://a.test", parent), self.add_feed("https://b.test", child), self.add_feed("https://c.test")
        now = time.time()
        self.store.record_fetch(f1, self.meta, [entry("1", "Alpha 100%", now - 10)])
        self.store.record_fetch(f2, self.meta, [entry("2", "Beta", now - 20)])
        self.store.record_fetch(f3, self.meta, [entry("3", "Gamma", now - 40 * 86400)])
        ids = {i["title"]: i["id"] for i in self.store.list_items({})["items"]}

        def titles(**f):
            return [i["title"] for i in self.store.list_items(f)["items"]]

        self.assertEqual(titles(view=f"folder:{parent}"), ["Alpha 100%", "Beta"])
        self.assertEqual(titles(view=f"folder:{child}"), ["Beta"])
        self.assertEqual(titles(view=f"feed:{f3}"), ["Gamma"])
        self.assertEqual(titles(q="100%"), ["Alpha 100%"])
        self.assertEqual(titles(q="0%"), ["Alpha 100%"])
        self.assertEqual(titles(q="_"), [])
        self.assertEqual(titles(since=now - 86400), ["Alpha 100%", "Beta"])
        self.store.update_item(ids["Beta"], read=True, starred=True, tags=[1])
        self.assertEqual(titles(status="unread"), ["Alpha 100%", "Gamma"])
        self.assertEqual(titles(status="read"), ["Beta"])
        self.assertEqual(titles(view="starred"), ["Beta"])
        self.assertEqual(titles(tags=["1"]), ["Beta"])
        self.assertEqual([i["title"] for i in self.store.list_items({}, sort="old")["items"]],
                         ["Gamma", "Beta", "Alpha 100%"])
        page = self.store.list_items({}, page=2, size=2)
        self.assertEqual((page["total"], len(page["items"])), (3, 1))

    def test_mark_read_by_filter_and_ids(self):
        f1, f2 = self.add_feed("https://a.test"), self.add_feed("https://b.test")
        self.store.record_fetch(f1, self.meta, [entry("1"), entry("2")])
        self.store.record_fetch(f2, self.meta, [entry("3")])
        self.assertEqual(self.store.mark_read(filters={"view": f"feed:{f1}"}), 2)
        self.assertEqual(self.store.state()["totals"]["unread"], 1)
        third = self.store.list_items({"view": f"feed:{f2}"})["items"][0]["id"]
        self.assertEqual(self.store.mark_read(ids=[third]), 1)
        self.assertEqual(self.store.mark_read(ids=[third]), 0)
        self.assertEqual(self.store.mark_read(filters={}, read=False), 3)

    def test_prune_keeps_what_the_user_kept(self):
        fid = self.add_feed()
        old = time.time() - 100 * 86400
        self.store.record_fetch(fid, self.meta, [entry(g, g, old) for g in ("plain", "star", "tag", "unread", "seen")],
                                now=time.time() - UNSEEN_GRACE - 60)
        items = {i["title"]: i["id"] for i in self.store.list_items({})["items"]}
        for title in ("plain", "star", "tag", "seen"):
            self.store.update_item(items[title], read=True)
        self.store.update_item(items["star"], starred=True)
        self.store.update_item(items["tag"], tags=[2])
        self.store.record_fetch(fid, self.meta, [entry("seen", "seen", old)])  # still in the feed
        self.assertEqual(self.store.prune(60), 1)
        left = sorted(i["title"] for i in self.store.list_items({})["items"])
        self.assertEqual(left, ["seen", "star", "tag", "unread"])

    def test_not_modified_keeps_current_items_alive(self):
        fid = self.add_feed()
        past = time.time() - UNSEEN_GRACE - 60
        self.store.record_fetch(fid, self.meta, [entry("gone")], now=past - 60)
        self.store.record_fetch(fid, self.meta, [entry("current")], now=past)
        self.store.record_not_modified(fid)
        seen = {r["guid"]: r["last_seen"] for r in self.store._rows("SELECT guid, last_seen FROM items")}
        self.assertGreater(seen["current"], past)
        self.assertEqual(seen["gone"], past - 60)

    def test_errors_bump_version_once(self):
        fid = self.add_feed()
        v = self.store.version
        self.store.record_error(fid, "boom")
        self.store.record_error(fid, "boom")
        self.assertEqual(self.store.version, v + 1)
        self.assertEqual(self.store.state()["feeds"][0]["last_error"], "boom")
        self.store.record_fetch(fid, self.meta, [])
        self.assertIsNone(self.store.state()["feeds"][0]["last_error"])

    def test_move_and_reorder(self):
        a, b = self.store.add_folder("A"), self.store.add_folder("B")
        child = self.store.add_folder("Child", a)
        with self.assertRaises(ValueError):
            self.store.move("folder", a, child, 0)
        self.store.move("folder", b, None, 0)
        self.assertEqual([f["name"] for f in self.store.state()["folders"] if f["parent_id"] is None], ["B", "A"])
        f1, f2 = self.add_feed("https://1.test"), self.add_feed("https://2.test")
        self.store.move("feed", f2, a, 0)
        self.store.move("feed", f1, a, 0)
        feeds_in_a = [f["id"] for f in self.store.state()["feeds"] if f["folder_id"] == a]
        self.assertEqual(feeds_in_a, [f1, f2])
        with self.assertRaises(NotFound):
            self.store.move("feed", 999, None, 0)

    def test_delete_folder_moves_contents_up(self):
        top = self.store.add_folder("Top")
        mid = self.store.add_folder("Mid", top)
        low = self.store.add_folder("Low", mid)
        fid = self.add_feed("https://m.test", mid)
        self.store.delete_folder(mid)
        state = self.store.state()
        self.assertEqual({f["id"]: f["parent_id"] for f in state["folders"]}, {top: None, low: top})
        self.assertEqual(state["feeds"][0]["folder_id"], top)
        self.assertEqual(state["feeds"][0]["id"], fid)

    def test_update_feed(self):
        fid = self.add_feed("https://a.test")
        other = self.add_feed("https://b.test")
        folder = self.store.add_folder("F")
        self.store.update_feed(fid, title="  Renamed  ", folder_id=folder, full_text=True)
        feed = self.store.feed(fid)
        self.assertEqual((feed["title"], feed["folder_id"], feed["full_text"]), ("Renamed", folder, 1))
        with self.assertRaises(ValueError):
            self.store.update_feed(other, url="https://a.test")
        with self.assertRaises(ValueError):
            self.add_feed("https://a.test")

    def test_only_web_addresses_are_stored(self):
        fid = self.store.add_feed("https://a.test/rss", "A", site_url="javascript:alert(1)", icon_url="data:image/png,x")
        feed = self.store.feed(fid)
        self.assertEqual((feed["site_url"], feed["icon_url"]), (None, None))
        for bad in ("file:///c:/windows/win.ini", "javascript:alert(1)", "  "):
            with self.assertRaises(ValueError):
                self.store.add_feed(bad, "Bad")
            with self.assertRaises(ValueError):
                self.store.update_feed(fid, url=bad)
        self.store.record_fetch(fid, {"site_url": "vbscript:x", "icon_url": "https://a.test/i.png"}, [])
        feed = self.store.feed(fid)
        self.assertEqual((feed["site_url"], feed["icon_url"]), (None, "https://a.test/i.png"))

    def test_tags(self):
        tid = self.store.add_tag("Work")
        with self.assertRaises(ValueError):
            self.store.add_tag("work")
        self.store.update_tag(tid, color="#123456")
        with self.assertRaises(ValueError):
            self.store.update_tag(tid, color="red")
        fid = self.add_feed()
        self.store.record_fetch(fid, self.meta, [entry("x")])
        item = self.store.list_items({})["items"][0]["id"]
        self.store.update_item(item, tags=[tid, 1, 999])
        self.assertEqual(sorted(self.store.item(item)["tags"]), sorted([tid, 1]))
        self.store.delete_tag(tid)
        self.assertEqual(self.store.item(item)["tags"], [1])

    def test_opml_import_reuses_folders_and_skips_known_feeds(self):
        tree = opml.parse(b"""<opml version="2.0"><body>
            <outline text="News"><outline text="A" xmlUrl="https://a.test/rss"/>
              <outline text="Deep"><outline text="B" xmlUrl="https://b.test/rss"/></outline></outline>
            <outline text="C" xmlUrl="https://c.test/rss"/></body></opml>""")
        first = self.store.import_tree(tree)
        self.assertEqual((first["folders"], first["feeds"], first["skipped"]), (2, 3, 0))
        second = self.store.import_tree(tree)
        self.assertEqual((second["folders"], second["feeds"], second["skipped"]), (0, 0, 3))
        exported = self.store.export_tree()
        self.assertEqual(opml.parse(opml.build(exported)), exported)


if __name__ == "__main__":
    unittest.main()
